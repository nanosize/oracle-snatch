from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import snatch


class FakeServiceError(Exception):
    def __init__(self, status: int, code: str, message: str, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


class FakeModel:
    def __init__(self, **values):
        self.__dict__.update(values)


def fake_sdk():
    models = SimpleNamespace(
        LaunchInstanceDetails=FakeModel,
        LaunchInstanceShapeConfigDetails=FakeModel,
        CreateVnicDetails=FakeModel,
        InstanceSourceViaImageDetails=FakeModel,
    )
    return SimpleNamespace(
        core=SimpleNamespace(models=models),
        retry=SimpleNamespace(NoneRetryStrategy=lambda: object()),
        exceptions=SimpleNamespace(ServiceError=FakeServiceError, RequestException=OSError),
    )


def response(data, headers=None):
    return SimpleNamespace(data=data, headers=headers or {})


def make_settings(tmp_path: Path, **overrides):
    key = tmp_path / "ssh.pub"
    key.write_text("ssh-ed25519 AAAATEST test@example", encoding="utf-8")
    values = {
        "subnet_id": "ocid1.subnet.oc1..subnet",
        "ssh_public_key_file": key,
        "compartment_id": None,
        "image_id": None,
        "display_name": "snatch-arm",
        "account_type": "FREE",
        "ocpus": Decimal("2"),
        "memory_gb": Decimal("12"),
        "boot_volume_gb": 50,
        "interval_seconds": 120,
        "state_file": tmp_path / "state.json",
        "result_file": tmp_path / "snatched.json",
        "oci_config_file": tmp_path / "oci-config",
        "oci_profile": "DEFAULT",
        "telegram_token": None,
        "telegram_chat_id": None,
        "discord_webhook_url": None,
    }
    values.update(overrides)
    return snatch.Settings(**values)


class FakeIdentity:
    def get_tenancy(self, tenancy_id):
        return response(SimpleNamespace(home_region_key="NRT"))

    def list_region_subscriptions(self, tenancy_id, **kwargs):
        return response([SimpleNamespace(region_key="NRT", region_name="ap-tokyo-1")])

    def list_compartments(self, tenancy_id, **kwargs):
        return response([])

    def list_availability_domains(self, tenancy_id, **kwargs):
        return response([SimpleNamespace(name="Tokyo-AD-1")])


class FakeCompute:
    def __init__(self, instances=None, launch_result=None, launch_error=None):
        self.instances = instances or []
        self.launch_result = launch_result
        self.launch_error = launch_error
        self.launch_calls = []

    def list_instances(self, compartment_id, **kwargs):
        return response(self.instances)

    def list_images(self, compartment_id, **kwargs):
        return response([SimpleNamespace(id="ocid1.image.oc1..ubuntu", display_name="Ubuntu ARM64")])

    def get_instance(self, instance_id):
        return response(SimpleNamespace(
            id=instance_id,
            display_name="snatch-arm",
            lifecycle_state="RUNNING",
        ))

    def list_vnic_attachments(self, compartment_id, **kwargs):
        return response([])

    def launch_instance(self, details, **kwargs):
        self.launch_calls.append((details, kwargs))
        if self.launch_error:
            raise self.launch_error
        return response(self.launch_result)


class FakeNetwork:
    def get_subnet(self, subnet_id):
        return response(SimpleNamespace(lifecycle_state="AVAILABLE", compartment_id="ocid1.tenancy.oc1..tenant"))

    def get_vnic(self, vnic_id):
        return response(SimpleNamespace(public_ip="203.0.113.10"))


class FakeBlockStorage:
    def list_volumes(self, compartment_id, **kwargs):
        return response([])

    def list_boot_volumes(self, compartment_id, **kwargs):
        return response([])


def clients(compute=None):
    return (FakeIdentity(), compute or FakeCompute(), FakeNetwork(), FakeBlockStorage())


def config_hash(settings, tenancy_id="ocid1.tenancy.oc1..tenant"):
    key_text = settings.ssh_public_key_file.read_text(encoding="utf-8").strip()
    target = settings.compartment_id or tenancy_id
    return snatch.hashlib.sha256(json.dumps({
        "display_name": settings.display_name,
        "subnet_id": settings.subnet_id,
        "image_selector": settings.image_id or "AUTO_COMPATIBLE_IMAGE",
        "account_type": settings.account_type,
        "ocpus": str(settings.ocpus),
        "memory_gb": str(settings.memory_gb),
        "boot_volume_gb": settings.boot_volume_gb,
        "ssh_public_key": key_text,
        "target_compartment": target,
    }, sort_keys=True).encode()).hexdigest()


def test_parse_settings_reads_and_validates_values(tmp_path, monkeypatch):
    for name in (
        "SUBNET_ID", "SSH_PUBLIC_KEY_FILE", "ACCOUNT_TYPE", "OCPUS", "MEMORY_GB",
        "PAYG_FREE_LIMITS_CONFIRMED",
        "BOOT_VOLUME_GB", "INTERVAL_SECONDS", "DISPLAY_NAME", "COMPARTMENT_ID",
        "IMAGE_ID", "TG_BOT_TOKEN", "TG_CHAT_ID", "DISCORD_WEBHOOK_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    key = tmp_path / "ssh.pub"
    key.write_text("ssh-ed25519 AAAATEST test@example", encoding="utf-8")
    settings = snatch.parse_settings({
        "SUBNET_ID": "ocid1.subnet.oc1..subnet",
        "SSH_PUBLIC_KEY_FILE": str(key),
        "ACCOUNT_TYPE": "FREE",
        "OCPUS": "2",
        "MEMORY_GB": "12",
        "BOOT_VOLUME_GB": "50",
        "INTERVAL_SECONDS": "120",
    })
    assert settings.account_type == "FREE"
    assert settings.ocpus == Decimal("2")
    assert settings.memory_gb == Decimal("12")
    assert settings.boot_volume_gb == 50
    with pytest.raises(snatch.ConfigError):
        snatch.parse_settings({"SUBNET_ID": "bad", "SSH_PUBLIC_KEY_FILE": str(key)})
    with pytest.raises(snatch.ConfigError, match="PAYG_FREE_LIMITS_CONFIRMED"):
        snatch.parse_settings({
            "SUBNET_ID": "ocid1.subnet.oc1..subnet",
            "SSH_PUBLIC_KEY_FILE": str(key),
            "ACCOUNT_TYPE": "PAYG",
        })


def test_compute_usage_separates_amd_micro_from_a1():
    usage = snatch.compute_usage([
        SimpleNamespace(shape="VM.Standard.E2.1.Micro", lifecycle_state="RUNNING"),
        SimpleNamespace(shape="VM.Standard.E2.1.Micro", lifecycle_state="STOPPED"),
        SimpleNamespace(
            id="a1", shape=snatch.SHAPE, lifecycle_state="STOPPED",
            shape_config=SimpleNamespace(ocpus=1, memory_in_gbs=8),
        ),
        SimpleNamespace(shape=snatch.SHAPE, lifecycle_state="TERMINATED", shape_config=None),
    ])
    assert usage == {
        "a1_count": 1,
        "a1_ocpus": Decimal("1"),
        "a1_memory_gb": Decimal("8"),
        "micro_count": 2,
    }


def test_storage_usage_sums_boot_and_block_volumes():
    boot = [SimpleNamespace(size_in_gbs=50, lifecycle_state="AVAILABLE")]
    block = [
        SimpleNamespace(size_in_gbs=100, lifecycle_state="AVAILABLE"),
        SimpleNamespace(size_in_gbs=40, lifecycle_state="TERMINATED"),
    ]
    assert snatch.storage_usage_gb(boot, block) == Decimal("150")


def test_free_quota_rejects_cpu_memory_or_storage_overage(tmp_path):
    settings = make_settings(tmp_path)
    usage = {"a1_count": 0, "a1_ocpus": Decimal("0"), "a1_memory_gb": Decimal("0"), "micro_count": 2}
    snatch.check_free_capacity(usage, Decimal("100"), settings)
    with pytest.raises(snatch.SafetyError, match="CPU"):
        snatch.check_free_capacity({**usage, "a1_ocpus": Decimal("1")}, Decimal("100"), settings)
    with pytest.raises(snatch.SafetyError, match="storage"):
        snatch.check_free_capacity(usage, Decimal("151"), settings)
    payg = make_settings(tmp_path, account_type="PAYG", ocpus=Decimal("4"), memory_gb=Decimal("24"))
    snatch.check_free_capacity(usage, Decimal("100"), payg)


def test_all_pages_fetches_every_page():
    calls = []

    def page(page=None):
        calls.append(page)
        if page is None:
            return response([1], {"opc-next-page": "next"})
        return response([2])

    assert snatch.all_pages(page) == [1, 2]
    assert calls == [None, "next"]


def test_backoff_honors_retry_after_and_increases():
    def no_jitter(low, high):
        return low

    assert snatch.backoff_seconds(1, jitter=no_jitter) == 120
    assert snatch.backoff_seconds(2, jitter=no_jitter) == 240
    assert snatch.backoff_seconds(1, retry_after=600, jitter=no_jitter) == 600
    assert snatch.backoff_seconds(1, retry_after=3600, jitter=no_jitter) == 3600
    assert snatch.classify_service_error(FakeServiceError(429, "TooManyRequests", "slow down")) == "rate_limited"
    assert snatch.classify_service_error(FakeServiceError(409, "LimitExceeded", "quota exceeded")) == "quota"
    assert snatch.classify_service_error(FakeServiceError(401, "NotAuthenticated", "bad key")) == "authentication"


def test_state_file_is_atomically_persisted_and_private(tmp_path):
    path = tmp_path / "state.json"
    store = snatch.StateStore(path)
    store.update(status="requesting", opc_retry_token="token")
    reloaded = snatch.StateStore(path)
    assert reloaded.load()["opc_retry_token"] == "token"
    assert path.stat().st_mode & 0o777 == 0o600


def test_process_lock_prevents_duplicate_runner(tmp_path):
    state_path = tmp_path / "state.json"
    with snatch.ProcessLock(state_path):
        with pytest.raises(snatch.SafetyError, match="Another oracle-snatch"):
            with snatch.ProcessLock(state_path):
                pass


def test_notification_failure_is_best_effort_and_does_not_log_webhook(tmp_path, monkeypatch, caplog):
    settings = make_settings(tmp_path, discord_webhook_url="https://discord.com/api/webhooks/secret/token")

    def fail(*args, **kwargs):
        raise OSError("failed URL contains token")

    monkeypatch.setattr(snatch.urllib.request, "urlopen", fail)
    snatch.notify(settings, "safe message")
    assert "notification failed" in caplog.text
    assert "secret/token" not in caplog.text


def test_http_429_retry_keeps_same_token_and_unknown_result_is_not_new_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(snatch, "oci", fake_sdk())
    settings = make_settings(tmp_path)
    store = snatch.StateStore(settings.state_file)
    store.load()
    runner = snatch.Runner(settings, clients(), store, threading.Event())
    runner._backoff = lambda *args: None
    op = runner._new_operation("ocid1.tenancy.oc1..tenant", "ap-tokyo-1", "ocid1.tenancy.oc1..tenant", "Tokyo-AD-1", "ocid1.image.oc1..ubuntu", "ssh-ed25519 AAAATEST", "hash")
    token = op["opc_retry_token"]

    throttled = FakeCompute(launch_error=FakeServiceError(429, "TooManyRequests", "rate limit", {"Retry-After": "120"}))
    runner.compute = throttled
    assert runner._request_pending(op)[0] == "retry"
    assert store.state["opc_retry_token"] == token

    offline = FakeCompute(launch_error=TimeoutError("connection dropped"))
    runner.compute = offline
    assert runner._request_pending(op)[0] == "retry"
    assert len(offline.launch_calls) == 1
    assert store.state["opc_retry_token"] == token
    assert store.state["status"] == "requesting"


def test_capacity_shortage_clears_only_rejected_request_and_allows_later_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(snatch, "oci", fake_sdk())
    settings = make_settings(tmp_path)
    store = snatch.StateStore(settings.state_file)
    store.load()
    runner = snatch.Runner(settings, clients(), store, threading.Event())
    op = runner._new_operation("ocid1.tenancy.oc1..tenant", "ap-tokyo-1", "ocid1.tenancy.oc1..tenant", "Tokyo-AD-1", "image", "ssh-ed25519 AAAATEST", "hash")
    rejected_token = op["opc_retry_token"]
    runner.compute = FakeCompute(launch_error=FakeServiceError(500, "InternalError", "Out of host capacity."))

    assert runner._request_pending(op)[0] == "capacity"
    assert store.state["status"] == "idle"
    assert "opc_retry_token" not in store.state

    next_op = runner._new_operation("ocid1.tenancy.oc1..tenant", "ap-tokyo-1", "ocid1.tenancy.oc1..tenant", "Tokyo-AD-1", "image", "ssh-ed25519 AAAATEST", "hash")
    assert next_op["opc_retry_token"] != rejected_token


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (FakeServiceError(409, "LimitExceeded", "resource quota exceeded"), "quota"),
        (FakeServiceError(401, "NotAuthenticated", "not authorized"), "authentication"),
    ],
)
def test_quota_and_authentication_errors_stop(tmp_path, monkeypatch, error, category):
    monkeypatch.setattr(snatch, "oci", fake_sdk())
    settings = make_settings(tmp_path)
    store = snatch.StateStore(settings.state_file)
    store.load()
    runner = snatch.Runner(settings, clients(FakeCompute(launch_error=error)), store, threading.Event())
    op = runner._new_operation("ocid1.tenancy.oc1..tenant", "ap-tokyo-1", "ocid1.tenancy.oc1..tenant", "Tokyo-AD-1", "image", "ssh-ed25519 AAAATEST", "hash")
    assert runner._request_pending(op)[0] == "stopped"
    assert store.state["status"] == "blocked"
    assert store.state["stop_category"] == category


def test_instance_creation_persists_ocid_then_stops_after_running(tmp_path, monkeypatch):
    monkeypatch.setattr(snatch, "oci", fake_sdk())
    settings = make_settings(tmp_path)
    instance = SimpleNamespace(id="ocid1.instance.oc1..new", lifecycle_state="PROVISIONING")
    compute = FakeCompute(launch_result=instance)
    store = snatch.StateStore(settings.state_file)
    store.load()
    runner = snatch.Runner(settings, clients(compute), store, threading.Event())

    assert runner.run("ocid1.tenancy.oc1..tenant", "ap-tokyo-1") == 0
    assert store.state["status"] == "succeeded"
    assert store.state["instance_id"] == instance.id
    assert len(compute.launch_calls) == 1
    assert compute.launch_calls[0][1]["opc_retry_token"] == store.state["opc_retry_token"]
    assert json.loads(settings.result_file.read_text())["instance_id"] == instance.id


def test_recovery_finds_tagged_instance_without_launching_again(tmp_path, monkeypatch):
    monkeypatch.setattr(snatch, "oci", fake_sdk())
    settings = make_settings(tmp_path)
    operation_id = "operation-123"
    found = SimpleNamespace(
        id="ocid1.instance.oc1..recovered",
        display_name="snatch-arm",
        lifecycle_state="RUNNING",
        shape=snatch.SHAPE,
        shape_config=SimpleNamespace(ocpus=2, memory_in_gbs=12),
        freeform_tags={"oracle-snatch-operation": operation_id},
    )
    compute = FakeCompute(instances=[found])
    store = snatch.StateStore(settings.state_file)
    store.state = {
        "schema_version": 1,
        "status": "requesting",
        "attempts": 1,
        "transient_failures": 0,
        "operation_id": operation_id,
        "opc_retry_token": "stable-token",
        "request_created_at": snatch.utc_now().isoformat(),
        "tenancy_id": "ocid1.tenancy.oc1..tenant",
        "region": "ap-tokyo-1",
        "availability_domain": "Tokyo-AD-1",
        "target_compartment_id": "ocid1.tenancy.oc1..tenant",
        "request_config_sha256": config_hash(settings),
        "request_payload": {
            "source_details": {"image_id": "ocid1.image.oc1..saved"},
        },
    }
    store.save()
    store.load()
    runner = snatch.Runner(settings, clients(compute), store, threading.Event())

    assert runner.run("ocid1.tenancy.oc1..tenant", "ap-tokyo-1") == 0
    assert compute.launch_calls == []
    assert store.state["status"] == "succeeded"
    assert store.state["instance_id"] == found.id


def test_dry_run_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(snatch, "oci", fake_sdk())
    settings = make_settings(tmp_path)
    store = snatch.StateStore(settings.state_file)
    store.load()
    compute = FakeCompute()
    runner = snatch.Runner(settings, clients(compute), store, threading.Event())
    result = runner.dry_run("ocid1.tenancy.oc1..tenant", "ap-tokyo-1")
    assert result == 0
    assert compute.launch_calls == []
    assert not settings.state_file.exists()
    assert not settings.result_file.exists()
