#!/usr/bin/env python3
"""Request one Always Free eligible OCI Ampere A1 instance, safely."""

from __future__ import annotations

import argparse
import email.utils
import fcntl
import hashlib
import json
import logging
import math
import os
import random
import re
import signal
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

try:
    import oci
except ImportError:  # --version and --status remain usable without the SDK.
    oci = None


VERSION = "1.0.0"
SHAPE = "VM.Standard.A1.Flex"
MICRO_SHAPE = "VM.Standard.E2.1.Micro"
FREE_LIMITS = {
    # The Always Free / free-account allocation is 1,500 OCPU-hours and
    # 9,000 GB-hours, equivalent to 2 OCPUs and 12 GB for a full month.
    "FREE": (Decimal("2"), Decimal("12")),
    # Oracle's Ampere page gives paid accounts 3,000 OCPU-hours and
    # 18,000 GB-hours, equivalent to 4 OCPUs and 24 GB. See README for the
    # conflicting wording on the Always Free page and the conservative default.
    "PAYG": (Decimal("4"), Decimal("24")),
}
FREE_STORAGE_GB = Decimal("200")
MIN_BOOT_VOLUME_GB = 50
MAX_BOOT_VOLUME_GB = 32768
RETRY_TOKEN_MAX_AGE_SECONDS = 23 * 60 * 60
LOGGER = logging.getLogger("oracle-snatch")
STOP_EVENT = threading.Event()


class ConfigError(ValueError):
    """A setting is missing or invalid."""


class SafetyError(RuntimeError):
    """The program cannot prove that continuing is safe."""


class RetryableError(RuntimeError):
    """An API read failed temporarily; retry after backoff."""


@dataclass(frozen=True)
class Settings:
    subnet_id: str
    ssh_public_key_file: Path
    compartment_id: str | None
    image_id: str | None
    display_name: str
    account_type: str
    ocpus: Decimal
    memory_gb: Decimal
    boot_volume_gb: int
    interval_seconds: int
    state_file: Path
    result_file: Path
    oci_config_file: Path
    oci_profile: str
    telegram_token: str | None
    telegram_chat_id: str | None
    discord_webhook_url: str | None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_env_file(path: str | Path | None) -> dict[str, str]:
    """Read the small KEY=VALUE format used by the existing project."""
    values: dict[str, str] = {}
    if not path or not Path(path).is_file():
        return values
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def setting(file_values: dict[str, str], name: str, default: str = "") -> str:
    return os.environ.get(name) or file_values.get(name) or default


def _decimal(values: dict[str, str], name: str, default: str) -> Decimal:
    try:
        return Decimal(setting(values, name, default))
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a number") from exc


def _integer(values: dict[str, str], name: str, default: str) -> int:
    value = setting(values, name, default)
    if not re.fullmatch(r"[0-9]+", value):
        raise ConfigError(f"{name} must be a positive integer")
    return int(value)


def parse_settings(file_values: dict[str, str]) -> Settings:
    subnet_id = setting(file_values, "SUBNET_ID")
    if not subnet_id.startswith("ocid1.subnet."):
        raise ConfigError("SUBNET_ID must be a subnet OCID")

    public_key_path = Path(os.path.expanduser(setting(
        file_values, "SSH_PUBLIC_KEY_FILE", "~/.ssh/id_ed25519.pub")))
    if not public_key_path.is_file():
        raise ConfigError("SSH_PUBLIC_KEY_FILE does not point to a readable public key")
    try:
        key_text = public_key_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError("SSH_PUBLIC_KEY_FILE cannot be read") from exc
    if not key_text or "PRIVATE KEY" in key_text or not key_text.startswith(("ssh-", "ecdsa-", "sk-")):
        raise ConfigError("SSH_PUBLIC_KEY_FILE must contain an SSH public key, never a private key")

    compartment_id = setting(file_values, "COMPARTMENT_ID") or None
    if compartment_id and not compartment_id.startswith(("ocid1.compartment.", "ocid1.tenancy.")):
        raise ConfigError("COMPARTMENT_ID must be a compartment or tenancy OCID")
    image_id = setting(file_values, "IMAGE_ID") or None
    if image_id and not image_id.startswith("ocid1.image."):
        raise ConfigError("IMAGE_ID must be an image OCID")

    account_type = setting(file_values, "ACCOUNT_TYPE", "FREE").upper()
    if account_type not in FREE_LIMITS:
        raise ConfigError("ACCOUNT_TYPE must be FREE or PAYG")
    if account_type == "PAYG" and setting(file_values, "PAYG_FREE_LIMITS_CONFIRMED").upper() != "YES":
        raise ConfigError(
            "PAYG requires PAYG_FREE_LIMITS_CONFIRMED=YES after checking the current OCI free allowance"
        )
    ocpus = _decimal(file_values, "OCPUS", "2")
    memory_gb = _decimal(file_values, "MEMORY_GB", "12")
    if not ocpus.is_finite() or ocpus < 1 or ocpus > 80 or ocpus != ocpus.to_integral_value():
        raise ConfigError("OCPUS must be a whole number from 1 to 80")
    if not memory_gb.is_finite() or memory_gb < 1 or memory_gb > ocpus * 64:
        raise ConfigError("MEMORY_GB must be from 1 to 64 GB per OCPU")
    boot_volume_gb = _integer(file_values, "BOOT_VOLUME_GB", str(MIN_BOOT_VOLUME_GB))
    if not MIN_BOOT_VOLUME_GB <= boot_volume_gb <= MAX_BOOT_VOLUME_GB:
        raise ConfigError(f"BOOT_VOLUME_GB must be from {MIN_BOOT_VOLUME_GB} to {MAX_BOOT_VOLUME_GB}")
    interval_seconds = _integer(file_values, "INTERVAL_SECONDS", "120")
    if interval_seconds < 120:
        raise ConfigError("INTERVAL_SECONDS must be at least 120 to respect OCI API limits")

    display_name = setting(file_values, "DISPLAY_NAME", "snatch-arm").strip()
    if not display_name or len(display_name) > 255 or any(ord(ch) < 32 for ch in display_name):
        raise ConfigError("DISPLAY_NAME must contain 1 to 255 printable characters")
    telegram_token = setting(file_values, "TG_BOT_TOKEN") or None
    telegram_chat_id = setting(file_values, "TG_CHAT_ID") or None
    if bool(telegram_token) != bool(telegram_chat_id):
        raise ConfigError("TG_BOT_TOKEN and TG_CHAT_ID must both be set or both be empty")
    webhook = setting(file_values, "DISCORD_WEBHOOK_URL") or None
    if webhook:
        parsed = urllib.parse.urlsplit(webhook)
        if parsed.scheme != "https" or parsed.hostname not in {"discord.com", "discordapp.com"} or not parsed.path.startswith("/api/webhooks/"):
            raise ConfigError("DISCORD_WEBHOOK_URL must be an HTTPS Discord webhook URL")

    state_file = Path(os.path.expanduser(setting(file_values, "STATE_FILE", "/data/state.json")))
    result_file = Path(os.path.expanduser(setting(file_values, "RESULT_FILE", "/data/snatched.json")))
    if state_file.resolve() == result_file.resolve():
        raise ConfigError("STATE_FILE and RESULT_FILE must be different paths")

    return Settings(
        subnet_id=subnet_id,
        ssh_public_key_file=public_key_path,
        compartment_id=compartment_id,
        image_id=image_id,
        display_name=display_name,
        account_type=account_type,
        ocpus=ocpus,
        memory_gb=memory_gb,
        boot_volume_gb=boot_volume_gb,
        interval_seconds=interval_seconds,
        state_file=state_file,
        result_file=result_file,
        oci_config_file=Path(os.path.expanduser(setting(file_values, "OCI_CONFIG_FILE", "~/.oci/config"))),
        oci_profile=setting(file_values, "OCI_PROFILE", "DEFAULT"),
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
        discord_webhook_url=webhook,
    )


class StateStore:
    """Small JSON state file, written atomically alongside its destination."""

    def __init__(self, path: Path):
        self.path = path
        self.state: dict[str, Any] = {"schema_version": 1, "status": "idle", "attempts": 0}

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.state
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SafetyError(f"State file is unreadable or invalid: {self.path}") from exc
        if not isinstance(loaded, dict) or loaded.get("schema_version") != 1:
            raise SafetyError("State file has an unsupported format; refusing to create an instance")
        self.state = loaded
        return self.state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(self.state, indent=2, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def update(self, **values: Any) -> None:
        self.state.update(values)
        self.save()


class ProcessLock:
    """Prevent two containers/processes from using the same state directory."""

    def __init__(self, state_path: Path):
        self.path = state_path.with_suffix(state_path.suffix + ".lock")
        self.stream: Any = None

    def __enter__(self) -> ProcessLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.stream.close()
            raise SafetyError("Another oracle-snatch process is using this state directory") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self.stream:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


def safe_decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise SafetyError(f"OCI returned an unreadable {label}; refusing to assume it is zero") from exc
    if not number.is_finite() or number < 0:
        raise SafetyError(f"OCI returned an invalid {label}; refusing to assume it is zero")
    return number


def compute_usage(instances: list[Any]) -> dict[str, Any]:
    """Count provisioned A1 resources separately from AMD Micro instances."""
    a1_ocpus = Decimal("0")
    a1_memory = Decimal("0")
    a1_count = 0
    micro_count = 0
    for instance in instances:
        if getattr(instance, "lifecycle_state", None) in {"TERMINATED", "DELETED"}:
            continue
        shape = getattr(instance, "shape", "")
        if shape == MICRO_SHAPE:
            micro_count += 1
        if shape != SHAPE:
            continue
        shape_config = getattr(instance, "shape_config", None)
        if shape_config is None:
            raise SafetyError(f"A1 instance {getattr(instance, 'id', '(unknown)')} has no readable shape configuration")
        a1_ocpus += safe_decimal(getattr(shape_config, "ocpus", None), "A1 OCPU allocation")
        a1_memory += safe_decimal(getattr(shape_config, "memory_in_gbs", None), "A1 memory allocation")
        a1_count += 1
    return {
        "a1_count": a1_count,
        "a1_ocpus": a1_ocpus,
        "a1_memory_gb": a1_memory,
        "micro_count": micro_count,
    }


def storage_usage_gb(boot_volumes: list[Any], block_volumes: list[Any]) -> Decimal:
    """Boot and block volumes share one home-region free allowance."""
    total = Decimal("0")
    for volume in [*boot_volumes, *block_volumes]:
        if getattr(volume, "lifecycle_state", None) in {"TERMINATED", "DELETED"}:
            continue
        total += safe_decimal(getattr(volume, "size_in_gbs", None), "volume size")
    return total


def check_free_capacity(
    usage: dict[str, Any],
    storage_gb: Decimal,
    settings: Settings,
) -> None:
    cpu_limit, memory_limit = FREE_LIMITS[settings.account_type]
    if usage["a1_ocpus"] + settings.ocpus > cpu_limit:
        raise SafetyError(
            f"A1 CPU free allowance would be exceeded: {usage['a1_ocpus']} + {settings.ocpus} > {cpu_limit} OCPU"
        )
    if usage["a1_memory_gb"] + settings.memory_gb > memory_limit:
        raise SafetyError(
            f"A1 memory free allowance would be exceeded: {usage['a1_memory_gb']} + {settings.memory_gb} > {memory_limit} GB"
        )
    if storage_gb + settings.boot_volume_gb > FREE_STORAGE_GB:
        raise SafetyError(
            f"Shared boot/block storage free allowance would be exceeded: {storage_gb} + {settings.boot_volume_gb} > {FREE_STORAGE_GB} GB"
        )


def inventory_report(usage: dict[str, Any], storage_gb: Decimal, settings: Settings) -> dict[str, Any]:
    cpu_limit, memory_limit = FREE_LIMITS[settings.account_type]
    return {
        "a1_instances": usage["a1_count"],
        "a1_ocpus_in_use": str(usage["a1_ocpus"]),
        "a1_memory_gb_in_use": str(usage["a1_memory_gb"]),
        "a1_ocpu_allowance": str(cpu_limit),
        "a1_memory_allowance_gb": str(memory_limit),
        "e2_micro_instances": usage["micro_count"],
        "boot_and_block_storage_gb_in_use": str(storage_gb),
        "boot_and_block_storage_allowance_gb": str(FREE_STORAGE_GB),
    }


def all_pages(method: Callable[..., Any], *args: Any, **kwargs: Any) -> list[Any]:
    """Read every OCI API page; an incomplete inventory must not permit launch."""
    page_kwargs = dict(kwargs)
    items: list[Any] = []
    while True:
        response = method(*args, **page_kwargs)
        items.extend(response.data or [])
        headers = getattr(response, "headers", {}) or {}
        next_page = None
        for key, value in headers.items():
            if str(key).lower() == "opc-next-page":
                next_page = value
                break
        if not next_page:
            return items
        page_kwargs["page"] = next_page


def launch_payload(
    settings: Settings,
    compartment_id: str,
    ad_name: str,
    image_id: str,
    ssh_key_text: str,
    operation_id: str,
) -> dict[str, Any]:
    return {
        "display_name": settings.display_name,
        "compartment_id": compartment_id,
        "availability_domain": ad_name,
        "shape": SHAPE,
        "shape_config": {"ocpus": float(settings.ocpus), "memory_in_gbs": float(settings.memory_gb)},
        "create_vnic_details": {
            "subnet_id": settings.subnet_id,
            "assign_public_ip": True,
            "assign_ipv6_ip": False,
        },
        "source_details": {
            "image_id": image_id,
            "boot_volume_size_in_gbs": settings.boot_volume_gb,
        },
        "metadata": {"ssh_authorized_keys": ssh_key_text},
        "freeform_tags": {"oracle-snatch-operation": operation_id},
    }


def launch_model(payload: dict[str, Any]) -> Any:
    if oci is None:
        raise ConfigError("The OCI SDK is not installed. Install requirements.txt")
    model_values = dict(payload)
    model_values["shape_config"] = oci.core.models.LaunchInstanceShapeConfigDetails(
        **payload["shape_config"]
    )
    model_values["create_vnic_details"] = oci.core.models.CreateVnicDetails(
        **payload["create_vnic_details"]
    )
    source_values = dict(payload["source_details"])
    source_values.pop("source_type", None)
    model_values["source_details"] = oci.core.models.InstanceSourceViaImageDetails(
        **source_values
    )
    return oci.core.models.LaunchInstanceDetails(**model_values)


def classify_service_error(error: Any) -> str:
    status = getattr(error, "status", None)
    code = str(getattr(error, "code", "") or "")
    message = str(getattr(error, "message", "") or "")
    lower = message.lower()
    if status == 429:
        return "rate_limited"
    if code in {"LimitExceeded", "QuotaExceeded", "ServiceLimitExceeded"} or (
        "limit" in lower and "exceed" in lower
    ) or ("quota" in lower and "exceed" in lower):
        return "quota"
    if status in {401, 403}:
        return "authentication"
    if status == 404:
        return "configuration"
    if "out of host capacity" in lower or "host capacity is not available" in lower:
        return "capacity"
    if status in {408, 500, 502, 503, 504}:
        return "transient"
    if isinstance(status, int) and 400 <= status < 500:
        return "configuration"
    return "unknown"


def retry_after_seconds(error: Any) -> float | None:
    headers = getattr(error, "headers", {}) or {}
    raw: Any = None
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            raw = value
            break
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(str(raw))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - utc_now()).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def backoff_seconds(
    failures: int,
    retry_after: float | None = None,
    base: int = 120,
    maximum: int = 1800,
    jitter: Callable[[float, float], float] = random.uniform,
) -> int:
    exponent = min(max(failures - 1, 0), 20)
    exponential = min(maximum, base * (2**exponent))
    floor = max(float(exponential), retry_after or 0.0)
    if retry_after is not None and retry_after >= maximum:
        return math.ceil(retry_after)
    if floor >= maximum:
        return maximum
    return min(maximum, math.ceil(jitter(floor, min(maximum, floor * 1.2))))


def _instance_matches_operation(instance: Any, operation_id: str) -> bool:
    tags = getattr(instance, "freeform_tags", {}) or {}
    return tags.get("oracle-snatch-operation") == operation_id


def setup_clients(config: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    if oci is None:
        raise ConfigError("The OCI SDK is not installed. Install requirements.txt")
    oci.config.validate_config(config)
    # Disable SDK-side retries so the application can persist one request token
    # and apply one bounded, visible retry policy consistently.
    no_retry = oci.retry.NoneRetryStrategy()
    return (
        oci.identity.IdentityClient(config, retry_strategy=no_retry),
        oci.core.ComputeClient(config, retry_strategy=no_retry),
        oci.core.VirtualNetworkClient(config, retry_strategy=no_retry),
        oci.core.BlockstorageClient(config, retry_strategy=no_retry),
    )


def notify(settings: Settings, message: str) -> None:
    """Best effort only; a failed notification never re-enters launch logic."""
    if settings.telegram_token and settings.telegram_chat_id:
        try:
            body = json.dumps({"chat_id": settings.telegram_chat_id, "text": message}).encode()
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=15):
                pass
        except Exception as exc:  # Do not log exception text; it may contain the bot token.
            LOGGER.warning("Telegram notification failed (%s)", type(exc).__name__)
    if settings.discord_webhook_url:
        try:
            body = json.dumps({"content": message}).encode()
            request = urllib.request.Request(
                settings.discord_webhook_url,
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=15):
                pass
        except Exception as exc:  # Never write a webhook URL or token to logs.
            LOGGER.warning("Discord notification failed (%s)", type(exc).__name__)


class Runner:
    def __init__(
        self,
        settings: Settings,
        clients: tuple[Any, Any, Any, Any],
        store: StateStore,
        stop_event: threading.Event = STOP_EVENT,
    ):
        self.settings = settings
        self.identity, self.compute, self.network, self.blockstorage = clients
        self.store = store
        self.stop_event = stop_event
        self.attempt = int(store.state.get("attempts", 0))
        self.failure_count = int(store.state.get("transient_failures", 0))

    def _read_call(self, method: Callable[..., Any], *args: Any, label: str, **kwargs: Any) -> Any:
        failures = 0
        while not self.stop_event.is_set():
            try:
                return method(*args, **kwargs)
            except Exception as error:
                if oci is not None and isinstance(error, oci.exceptions.ServiceError):
                    kind = classify_service_error(error)
                    if kind in {"rate_limited", "transient"}:
                        failures += 1
                        delay = backoff_seconds(failures, retry_after_seconds(error))
                        LOGGER.warning(
                            "Attempt %s, AD %s: %s read received %s; retrying in %s seconds",
                            self.attempt,
                            self.store.state.get("availability_domain", "inventory"),
                            label,
                            getattr(error, "status", "OCI error"),
                            delay,
                        )
                        self.stop_event.wait(delay)
                        continue
                    raise SafetyError(f"OCI {label} failed ({getattr(error, 'status', 'error')} {getattr(error, 'code', '')}): {getattr(error, 'message', '')}") from error
                request_exception = getattr(getattr(oci, "exceptions", None), "RequestException", ()) if oci is not None else ()
                if isinstance(error, (TimeoutError, ConnectionError, OSError)) or (
                    request_exception and isinstance(error, request_exception)
                ):
                    failures += 1
                    delay = backoff_seconds(failures)
                    LOGGER.warning(
                        "Attempt %s, AD %s: %s read had a communication failure; retrying in %s seconds",
                        self.attempt,
                        self.store.state.get("availability_domain", "inventory"),
                        label,
                        delay,
                    )
                    self.stop_event.wait(delay)
                    continue
                raise SafetyError(f"OCI {label} failed with {type(error).__name__}") from error
        raise SafetyError(f"Shutdown requested during OCI {label}")

    def _pages(self, method: Callable[..., Any], *args: Any, label: str, **kwargs: Any) -> list[Any]:
        def read_page(*page_args: Any, **page_kwargs: Any) -> Any:
            return self._read_call(method, *page_args, label=label, **page_kwargs)

        return all_pages(read_page, *args, **kwargs)

    def _tenancy_context(self, tenancy_id: str) -> tuple[str, list[str], list[str]]:
        tenancy = self._read_call(self.identity.get_tenancy, tenancy_id, label="tenancy lookup").data
        subscriptions = self._pages(self.identity.list_region_subscriptions, tenancy_id, label="region subscription listing")
        home_region = next(
            (item.region_name for item in subscriptions if item.region_key == tenancy.home_region_key),
            None,
        )
        if not home_region:
            raise SafetyError("Could not identify the tenancy home region from region subscriptions")
        compartments = self._pages(
            self.identity.list_compartments,
            tenancy_id,
            label="compartment listing",
            compartment_id_in_subtree=True,
            access_level="ANY",
        )
        compartment_ids = [tenancy_id]
        compartment_ids.extend(
            item.id for item in compartments if getattr(item, "lifecycle_state", "ACTIVE") != "DELETED"
        )
        availability_domains = [
            item.name for item in self._pages(self.identity.list_availability_domains, tenancy_id, label="availability domain listing")
        ]
        if not availability_domains:
            raise SafetyError("OCI returned no availability domains; refusing to launch")
        return home_region, compartment_ids, availability_domains

    def _image(self, compartment_id: str) -> Any:
        if self.settings.image_id:
            image = self._read_call(self.compute.get_image, self.settings.image_id, label="image lookup").data
            if getattr(image, "lifecycle_state", "AVAILABLE") not in {"AVAILABLE", "ACTIVE"}:
                raise ConfigError("IMAGE_ID does not refer to an available image")
            return image
        images = self._pages(
            self.compute.list_images,
            compartment_id,
            label="image listing",
            operating_system="Canonical Ubuntu",
            shape=SHAPE,
            sort_by="TIMECREATED",
            sort_order="DESC",
        )
        if not images:
            raise ConfigError(f"No compatible Canonical Ubuntu image found for {SHAPE}")
        return images[0]

    def inventory(
        self,
        tenancy_id: str,
        compartment_ids: list[str],
        availability_domains: list[str],
    ) -> tuple[dict[str, Any], Decimal, list[Any]]:
        instances = self._pages(
            self.compute.list_instances,
            tenancy_id,
            label="instance inventory",
            compartment_id_in_subtree=True,
        )
        boot_volumes: list[Any] = []
        block_volumes: list[Any] = []
        for compartment_id in compartment_ids:
            block_volumes.extend(self._pages(
                self.blockstorage.list_volumes,
                label="block volume inventory",
                compartment_id=compartment_id,
            ))
            for ad_name in availability_domains:
                boot_volumes.extend(self._pages(
                    self.blockstorage.list_boot_volumes,
                    label="boot volume inventory",
                    compartment_id=compartment_id,
                    availability_domain=ad_name,
                ))
        usage = compute_usage(instances)
        storage = storage_usage_gb(boot_volumes, block_volumes)
        return usage, storage, instances

    def _check_target_compartment(self, target_id: str, visible_ids: list[str]) -> None:
        if target_id not in visible_ids:
            raise SafetyError("Target COMPARTMENT_ID is not visible in the tenancy-wide compartment listing")

    def _find_operation(self, instances: list[Any], operation_id: str) -> Any | None:
        matches = [item for item in instances if _instance_matches_operation(item, operation_id)]
        if len(matches) > 1:
            raise SafetyError("More than one instance has the pending operation tag; refusing further launches")
        return matches[0] if matches else None

    def _reconcile_retry(
        self,
        operation: dict[str, Any],
        tenancy_id: str,
        compartment_ids: list[str],
        availability_domains: list[str],
    ) -> str:
        """Recheck an ambiguous request before resubmitting the same token."""
        usage, storage, instances = self.inventory(tenancy_id, compartment_ids, availability_domains)
        found = self._find_operation(instances, operation["operation_id"])
        if found is not None:
            self.store.update(
                status="created",
                instance_id=found.id,
                lifecycle_state=found.lifecycle_state,
                recovered_by_operation_tag=True,
            )
            return "created"
        try:
            check_free_capacity(usage, storage, self.settings)
        except SafetyError as exc:
            self._fatal(
                "unknown request outcome",
                f"Free allowance cannot be confirmed for retry; no further request will be sent. {exc}",
            )
            return "stopped"
        LOGGER.info(
            "Attempt %s, AD %s: no tagged instance found; free allowance still permits retry with the same token",
            operation.get("attempts"),
            operation["availability_domain"],
        )
        return "retry"

    def _fatal(self, category: str, message: str) -> int:
        self.store.update(status="blocked", stop_category=category, stop_reason=message)
        LOGGER.error("Stopping safely (%s): %s", category, message)
        notify(self.settings, f"oracle-snatch stopped: {category}\n{message}")
        return 1

    def _backoff(self, error: Any, kind: str, ad_name: str) -> None:
        self.failure_count += 1
        self.store.update(transient_failures=self.failure_count)
        delay = backoff_seconds(self.failure_count, retry_after_seconds(error))
        LOGGER.warning(
            "Attempt %s, AD %s: %s; retrying in %s seconds",
            self.attempt,
            ad_name,
            kind,
            delay,
        )
        self.stop_event.wait(delay)

    def _new_operation(
        self,
        tenancy_id: str,
        region: str,
        target_compartment: str,
        ad_name: str,
        image_id: str,
        ssh_key_text: str,
        request_config_hash: str,
    ) -> dict[str, Any]:
        operation_id = str(uuid.uuid4())
        payload = launch_payload(
            self.settings,
            target_compartment,
            ad_name,
            image_id,
            ssh_key_text,
            operation_id,
        )
        self.attempt += 1
        operation = {
            "schema_version": 1,
            "status": "requesting",
            "attempts": self.attempt,
            "transient_failures": self.failure_count,
            "operation_id": operation_id,
            "opc_retry_token": str(uuid.uuid4()),
            "request_created_at": utc_now().isoformat(),
            "tenancy_id": tenancy_id,
            "region": region,
            "availability_domain": ad_name,
            "target_compartment_id": target_compartment,
            "request_config_sha256": request_config_hash,
            "request_payload": payload,
        }
        self.store.state = operation
        self.store.save()
        return operation

    def _request_pending(self, operation: dict[str, Any]) -> tuple[str, Any | None]:
        """Use exactly the durable payload and token until OCI resolves it."""
        ad_name = operation["availability_domain"]
        created_at = datetime.fromisoformat(operation["request_created_at"])
        if (utc_now() - created_at).total_seconds() >= RETRY_TOKEN_MAX_AGE_SECONDS:
            return "stopped", self._fatal(
                "unknown request outcome",
                "Pending opc_retry_token is near its 24-hour expiry; manual OCI reconciliation is required",
            )
        try:
            response = self.compute.launch_instance(
                launch_model(operation["request_payload"]),
                opc_retry_token=operation["opc_retry_token"],
                retry_strategy=oci.retry.NoneRetryStrategy() if oci is not None else None,
            )
        except Exception as error:
            if oci is not None and isinstance(error, oci.exceptions.ServiceError):
                kind = classify_service_error(error)
                message = str(getattr(error, "message", "") or "OCI service error")
                if kind == "capacity":
                    LOGGER.info("Attempt %s, AD %s: no host capacity", self.attempt, ad_name)
                    self.store.state = {
                        "schema_version": 1,
                        "status": "idle",
                        "attempts": self.attempt,
                        "transient_failures": self.failure_count,
                        "tenancy_id": operation["tenancy_id"],
                        "region": operation["region"],
                    }
                    self.store.save()
                    return "capacity", None
                if kind == "quota":
                    self._fatal("quota", message)
                    return "stopped", None
                if kind in {"authentication", "configuration"}:
                    return "stopped", self._fatal(kind, message)
                if kind == "rate_limited":
                    self._backoff(error, "HTTP 429", ad_name)
                    return "retry", None
                if kind == "transient":
                    self._backoff(error, f"HTTP {error.status}", ad_name)
                    return "retry", None
                return "stopped", self._fatal("unknown OCI error", message)

            request_exception = getattr(getattr(oci, "exceptions", None), "RequestException", ()) if oci is not None else ()
            if isinstance(error, (TimeoutError, ConnectionError, OSError)) or (
                request_exception and isinstance(error, request_exception)
            ):
                self._backoff(error, type(error).__name__, ad_name)
                return "retry", None
            # A local/unknown failure can happen after a request was sent. Keep
            # its token and stop rather than risk a duplicate create.
            return "stopped", self._fatal("unknown request outcome", type(error).__name__)

        instance = response.data
        # This is deliberately the first operation after a successful API call.
        self.store.update(
            status="created",
            instance_id=instance.id,
            lifecycle_state=getattr(instance, "lifecycle_state", "PROVISIONING"),
            created_at=utc_now().isoformat(),
            success_notification_attempted=False,
            running_notification_attempted=False,
        )
        return "created", instance

    def _wait_for_running(self, operation: dict[str, Any]) -> int:
        instance_id = operation["instance_id"]
        target_compartment = operation["target_compartment_id"]
        while not self.stop_event.is_set():
            try:
                instance = self._read_call(self.compute.get_instance, instance_id, label="instance status lookup").data
                if instance.lifecycle_state == "RUNNING":
                    attachments = self._pages(
                        self.compute.list_vnic_attachments,
                        target_compartment,
                        label="VNIC attachment lookup",
                        instance_id=instance_id,
                    )
                    public_ip = None
                    for attachment in attachments:
                        vnic = self._read_call(self.network.get_vnic, attachment.vnic_id, label="VNIC lookup").data
                        if getattr(vnic, "public_ip", None):
                            public_ip = vnic.public_ip
                            break
                    result = {
                        "instance_id": instance_id,
                        "display_name": instance.display_name,
                        "region": operation["region"],
                        "availability_domain": operation["availability_domain"],
                        "shape": SHAPE,
                        "ocpus": float(self.settings.ocpus),
                        "memory_gb": float(self.settings.memory_gb),
                        "public_ip": public_ip,
                        "created_at": operation.get("created_at"),
                    }
                    self._write_result(result)
                    if not operation.get("success_notification_attempted"):
                        notify(self.settings, self._notification_text("Instance acquired", result))
                        self.store.update(success_notification_attempted=True, public_ip=public_ip)
                        operation.update(success_notification_attempted=True, public_ip=public_ip)
                    if not operation.get("running_notification_attempted"):
                        notify(self.settings, self._notification_text("Instance running", result))
                        self.store.update(running_notification_attempted=True)
                        operation["running_notification_attempted"] = True
                    self.store.update(status="succeeded", lifecycle_state="RUNNING", public_ip=public_ip)
                    LOGGER.info("Instance is RUNNING; result saved to %s", self.settings.result_file)
                    return 0
                if instance.lifecycle_state in {"TERMINATING", "TERMINATED", "DELETED"}:
                    return self._fatal(
                        "instance lifecycle",
                        f"Acquired instance entered {instance.lifecycle_state}; no replacement will be created",
                    )
                self.store.update(lifecycle_state=instance.lifecycle_state)
                LOGGER.info("Instance %s state is %s; checking again in 15 seconds", instance_id, instance.lifecycle_state)
                self.stop_event.wait(15)
            except Exception as error:
                if isinstance(error, SafetyError):
                    return self._fatal("instance status or authentication", str(error))
                if isinstance(error, OSError):
                    return self._fatal("local persistence", type(error).__name__)
                LOGGER.warning("Attempt %s, AD %s: instance status lookup failed (%s)", self.attempt, operation["availability_domain"], type(error).__name__)
                self.stop_event.wait(120)
        return 0

    def _notification_text(self, heading: str, result: dict[str, Any]) -> str:
        return (
            f"oracle-snatch: {heading}\n"
            f"Name: {result['display_name']}\n"
            f"OCID: {result['instance_id']}\n"
            f"Region: {result['region']}\n"
            f"CPU: {result['ocpus']:g} OCPU\n"
            f"Memory: {result['memory_gb']:g} GB\n"
            f"Public IP: {result['public_ip'] or 'none'}"
        )

    def _write_result(self, result: dict[str, Any]) -> None:
        path = self.settings.result_file
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def dry_run(self, tenancy_id: str, oci_region: str) -> int:
        region, compartment_ids, ads = self._tenancy_context(tenancy_id)
        if region != oci_region:
            raise SafetyError(f"Configured region {oci_region} is not home region {region}")
        target_compartment = self.settings.compartment_id or tenancy_id
        self._check_target_compartment(target_compartment, compartment_ids)
        subnet = self._read_call(self.network.get_subnet, self.settings.subnet_id, label="subnet lookup").data
        if getattr(subnet, "lifecycle_state", "AVAILABLE") not in {"AVAILABLE", "ACTIVE"}:
            raise ConfigError("SUBNET_ID is not an available subnet")
        image = self._image(target_compartment)
        usage, storage, _ = self.inventory(tenancy_id, compartment_ids, ads)
        check_free_capacity(usage, storage, self.settings)
        LOGGER.info("Dry run: configuration, OCI auth, home region, image, subnet and free allowance are valid")
        LOGGER.info("Home region: %s; availability domains: %s", region, ", ".join(ads))
        LOGGER.info("Image: %s (%s)", getattr(image, "display_name", "image"), image.id)
        LOGGER.info(
            "Target: %s %s OCPU / %s GB, boot %s GB; inventory: %s",
            SHAPE,
            self.settings.ocpus,
            self.settings.memory_gb,
            self.settings.boot_volume_gb,
            inventory_report(usage, storage, self.settings),
        )
        if self.store.state.get("status") == "requesting":
            LOGGER.info("A prior operation is pending; dry run did not retry or alter it")
        LOGGER.info("Dry run complete. No OCI resources were changed.")
        return 0

    def run(self, tenancy_id: str, oci_region: str) -> int:
        region, compartment_ids, ads = self._tenancy_context(tenancy_id)
        if region != oci_region:
            raise SafetyError(f"Configured region {oci_region} is not home region {region}")
        state = self.store.state
        if state.get("tenancy_id") and state["tenancy_id"] != tenancy_id:
            raise SafetyError("State file belongs to another tenancy; refusing to create")
        if state.get("region") and state["region"] != region:
            raise SafetyError("State file belongs to another region; refusing to create")
        if state.get("status") == "succeeded":
            LOGGER.info("A previous instance was acquired successfully; stopping without another launch")
            return 0
        if state.get("status") == "blocked":
            LOGGER.error("State is blocked (%s): %s", state.get("stop_category"), state.get("stop_reason"))
            return 1
        if state.get("status") == "created":
            return self._wait_for_running(state)

        target_compartment = self.settings.compartment_id or tenancy_id
        self._check_target_compartment(target_compartment, compartment_ids)
        ssh_key_text = self.settings.ssh_public_key_file.read_text(encoding="utf-8").strip()
        if state.get("status") == "requesting":
            image_id = state["request_payload"]["source_details"]["image_id"]
            image = type("SavedImage", (), {"id": image_id})()
        else:
            image = self._image(target_compartment)
        request_hash = hashlib.sha256(json.dumps({
            "display_name": self.settings.display_name,
            "subnet_id": self.settings.subnet_id,
            "image_selector": self.settings.image_id or "AUTO_COMPATIBLE_IMAGE",
            "account_type": self.settings.account_type,
            "ocpus": str(self.settings.ocpus),
            "memory_gb": str(self.settings.memory_gb),
            "boot_volume_gb": self.settings.boot_volume_gb,
            "ssh_public_key": ssh_key_text,
            "target_compartment": target_compartment,
        }, sort_keys=True).encode()).hexdigest()

        if state.get("status") == "requesting" and state.get("request_config_sha256") != request_hash:
            return self._fatal(
                "configuration changed during pending request",
                "The pending OCI request must be reconciled before changing the launch settings",
            )

        if state.get("status") == "requesting":
            operation = state
            usage, storage, instances = self.inventory(tenancy_id, compartment_ids, ads)
            found = self._find_operation(instances, operation["operation_id"])
            if found is not None:
                # Recover a success if the process died between the API response
                # and the immediate local write of the instance OCID.
                self.store.update(
                    status="created",
                    instance_id=found.id,
                    lifecycle_state=found.lifecycle_state,
                    recovered_by_operation_tag=True,
                )
                return self._wait_for_running(self.store.state)
            created_at = datetime.fromisoformat(operation["request_created_at"])
            if (utc_now() - created_at).total_seconds() >= RETRY_TOKEN_MAX_AGE_SECONDS:
                return self._fatal(
                    "unknown request outcome",
                    "Pending opc_retry_token is near its 24-hour expiry and no safe result is known; manual OCI reconciliation is required",
                )
            check_free_capacity(usage, storage, self.settings)
            LOGGER.warning(
                "Recovering pending request attempt %s in %s with the same opc_retry_token",
                operation.get("attempts"),
                operation["availability_domain"],
            )
            while not self.stop_event.is_set():
                outcome, _ = self._request_pending(operation)
                if outcome == "created":
                    return self._wait_for_running(self.store.state)
                if outcome == "retry":
                    if self.stop_event.is_set():
                        break
                    reconciled = self._reconcile_retry(operation, tenancy_id, compartment_ids, ads)
                    if reconciled == "created":
                        return self._wait_for_running(self.store.state)
                    if reconciled == "stopped":
                        return 1
                    continue
                if outcome == "capacity":
                    break
                if outcome == "stopped":
                    return 1
            # A definite capacity response clears the pending request; continue
            # below with the next AD and a new token.

        LOGGER.info("Home region %s; availability domains: %s", region, ", ".join(ads))
        LOGGER.info("Target %s %s OCPU / %s GB; default interval %s seconds", SHAPE, self.settings.ocpus, self.settings.memory_gb, self.settings.interval_seconds)
        while not self.stop_event.is_set():
            usage, storage, _ = self.inventory(tenancy_id, compartment_ids, ads)
            check_free_capacity(usage, storage, self.settings)
            LOGGER.info("Free quota check passed: %s", inventory_report(usage, storage, self.settings))
            for ad_name in ads:
                if self.stop_event.is_set():
                    break
                usage, storage, _ = self.inventory(tenancy_id, compartment_ids, ads)
                check_free_capacity(usage, storage, self.settings)
                operation = self._new_operation(
                    tenancy_id,
                    region,
                    target_compartment,
                    ad_name,
                    image.id,
                    ssh_key_text,
                    request_hash,
                )
                LOGGER.info("Attempt %s, AD %s: requesting %s", self.attempt, ad_name, SHAPE)
                while not self.stop_event.is_set():
                    outcome, _ = self._request_pending(operation)
                    if outcome == "created":
                        return self._wait_for_running(self.store.state)
                    if outcome == "retry":
                        if self.stop_event.is_set():
                            break
                        reconciled = self._reconcile_retry(operation, tenancy_id, compartment_ids, ads)
                        if reconciled == "created":
                            return self._wait_for_running(self.store.state)
                        if reconciled == "stopped":
                            return 1
                        continue
                    if outcome == "capacity":
                        break
                    if outcome == "stopped":
                        return 1
            LOGGER.info("No capacity in any AD; attempt %s. Next round in %s seconds", self.attempt, self.settings.interval_seconds)
            self.stop_event.wait(self.settings.interval_seconds)
        LOGGER.info("Shutdown requested; state is safely persisted")
        return 0


def _safe_status(state_path: Path) -> int:
    store = StateStore(state_path)
    state = store.load()
    visible = {
        "status": state.get("status", "idle"),
        "attempts": state.get("attempts", 0),
        "region": state.get("region"),
        "availability_domain": state.get("availability_domain"),
        "instance_id": state.get("instance_id"),
        "instance_name": (state.get("request_payload") or {}).get("display_name"),
        "lifecycle_state": state.get("lifecycle_state"),
        "public_ip": state.get("public_ip"),
        "operation_id": state.get("operation_id"),
        "stop_category": state.get("stop_category"),
        "stop_reason": state.get("stop_reason"),
    }
    print(json.dumps(visible, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely request one OCI Always Free A1.Flex instance")
    parser.add_argument("--config", default=os.environ.get("ORACLE_SNATCH_CONFIG", "config.env"))
    parser.add_argument("--dry-run", action="store_true", help="validate settings and free quota without changing OCI resources")
    parser.add_argument("--status", action="store_true", help="show the persisted local acquisition state")
    parser.add_argument("--version", action="store_true", help="show version")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )
    file_values = load_env_file(args.config)
    if args.version:
        print(VERSION)
        return 0
    if args.status:
        state_path = Path(os.path.expanduser(setting(file_values, "STATE_FILE", "/data/state.json")))
        try:
            return _safe_status(state_path)
        except SafetyError as exc:
            LOGGER.error("Cannot read status safely: %s", exc)
            return 1

    try:
        settings = parse_settings(file_values)
        if oci is None:
            raise ConfigError("The OCI SDK is not installed. Install requirements.txt")
        oci_config = oci.config.from_file(str(settings.oci_config_file), settings.oci_profile)
        oci.config.validate_config(oci_config)
        tenancy_id = oci_config["tenancy"]
        oci_region = oci_config["region"]
        store = StateStore(settings.state_file)
        store.load()
        if args.dry_run:
            clients = setup_clients(oci_config)
            return Runner(settings, clients, store).dry_run(tenancy_id, oci_region)
        with ProcessLock(settings.state_file):
            store.load()
            clients = setup_clients(oci_config)
            signal.signal(signal.SIGTERM, lambda *_: STOP_EVENT.set())
            signal.signal(signal.SIGINT, lambda *_: STOP_EVENT.set())
            return Runner(settings, clients, store).run(tenancy_id, oci_region)
    except (ConfigError, SafetyError) as exc:
        LOGGER.error("Stopping safely: %s", exc)
        if not args.dry_run:
            try:
                if "store" in locals() and store.state.get("status") not in {"blocked", "succeeded"}:
                    store.update(status="blocked", stop_category="safety or configuration", stop_reason=str(exc))
                if "settings" in locals():
                    notify(settings, f"oracle-snatch stopped: configuration or safety error\n{exc}")
            except Exception:
                LOGGER.error("Could not persist or send the safety stop state")
        return 1
    except Exception as exc:
        # Do not print arbitrary SDK exceptions that could contain request data.
        LOGGER.error("Stopping safely after %s", type(exc).__name__)
        if not args.dry_run:
            try:
                if "store" in locals() and store.state.get("status") not in {"blocked", "succeeded"}:
                    store.update(status="blocked", stop_category="unexpected error", stop_reason=type(exc).__name__)
                if "settings" in locals():
                    notify(settings, f"oracle-snatch stopped: {type(exc).__name__}")
            except Exception:
                LOGGER.error("Could not persist or send the error stop state")
        return 1


if __name__ == "__main__":
    sys.exit(main())
