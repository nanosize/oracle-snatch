# oracle-snatch

日本語版は[README.ja.md](README.ja.md)です。

This tool uses Oracle's official OCI Python SDK to request one VM.Standard.A1.Flex instance until capacity is available. It checks Always Free eligibility before each new request and stops without creating an instance whenever it cannot confirm that the configured free allowances will be respected. It never deletes, changes, or stops existing OCI resources.

## Free allowances and billing limits

Always Free compute resources must be created in the tenancy home region. Oracle's Always Free resources page describes the A1 allowance as 1,500 OCPU-hours and 9,000 GB-hours per month, equivalent to 2 OCPUs and 12 GB for an always-on free account. Oracle's Ampere compute page describes 3,000 OCPU-hours and 18,000 GB-hours for paid accounts, equivalent to 4 OCPUs and 24 GB. The Always Free page also uses wording that says 1,500/9,000 hours apply to all tenancies, so the two pages are not fully consistent.

The safe default is `ACCOUNT_TYPE=FREE`, capped at 2 OCPUs and 12 GB. To use the 4/24 PAYG limit, set `ACCOUNT_TYPE=PAYG` and `PAYG_FREE_LIMITS_CONFIRMED=YES` after confirming the current allowance in the OCI Console. OCI does not expose account type in a way this tool can reliably infer.

Up to two AMD `VM.Standard.E2.1.Micro` instances have a separate compute allowance and do not consume A1 CPU or memory allowance. Their boot volumes still count toward shared storage. Boot and block volumes share 200 GB of Always Free storage in the home region; the default boot volume is 50 GB. The tool inventories instances, boot volumes, and block volumes in every tenancy compartment. It conservatively counts all non-terminated A1 instances, including stopped instances.

OCI API inventory and shape data cannot guarantee the final invoice. Oracle's billing aggregation, resources the API caller cannot see, other regions, and other services can affect charges. This tool never creates resources outside the home region; review existing resources and costs for other regions separately in Billing & Cost Management, Cost Analysis, and budgets.

The tool conservatively checks current instance allocations against always-on limits. Resource inventory cannot recover cumulative OCPU-hours or memory-hours from instances terminated or deleted earlier in the current month, so verify month-to-date usage and remaining free allowance in the OCI Console as well.

## OCI API key and IAM

Create an API key from your OCI user profile, upload its public key, and configure the standard OCI SDK `config` file with user OCID, tenancy OCID, fingerprint, region, and private-key path. Do not commit the config or private key to Git. Docker mounts the OCI directory read-only. In the mounted config, point `key_file` to its container path, for example `/run/oci/oci_api_key.pem`.

Use a dedicated user and group. Replace `<group>`, `<target-compartment>`, and `<network-compartment>` in this starting policy:

```text
Allow group <group> to inspect compartments in tenancy
Allow group <group> to inspect tenancies in tenancy
Allow group <group> to read instances in tenancy
Allow group <group> to inspect vnic-attachments in tenancy
Allow group <group> to inspect vnics in tenancy
Allow group <group> to inspect volume-family in tenancy
Allow group <group> to read instance-images in tenancy
Allow group <group> to manage instance-family in compartment <target-compartment>
Allow group <group> to use virtual-network-family in compartment <network-compartment>
```

`manage instance-family` is required to launch an instance and also grants permissions beyond creation. The application itself never modifies or deletes an existing resource. If OCI cannot list every compartment or inspect every relevant instance and volume, it stops. Review the OCI Core Services IAM reference for tighter individual permissions or marketplace image requirements.

## SUBNET_ID and image

- `SUBNET_ID`: In the home region, use Networking → Virtual Cloud Networks → your VCN → Subnets. Select a public subnet if you need the assigned public IP.
- `COMPARTMENT_ID`: Optional target compartment OCID. Defaults to the tenancy root. Inventory always covers the full tenancy.
- `IMAGE_ID`: Optional image OCID. If omitted, the tool selects the newest compatible Canonical Ubuntu image.
- OCI profile region must equal the tenancy home region. The tool refuses to create instances in another region.

## Docker Compose

```bash
git clone https://github.com/nanosize/oracle-snatch.git
cd oracle-snatch
cp .env.example .env
mkdir -p secrets/oci data
```

Set `SUBNET_ID` in `.env`. Put the OCI config and private API key in `secrets/oci/`, and put only the SSH public key in `secrets/ssh_authorized_keys.pub`. Configure `key_file` in the OCI config as `/run/oci/<key-file-name>`. Set `PUID` and `PGID` to the owner IDs of the mounted files and data directory (`id -u` and `id -g` on Debian).

```bash
docker compose build
docker compose run --rm oracle-snatch --dry-run
docker compose up -d
docker compose logs -f oracle-snatch
```

The image uses a Python slim base that supports amd64 and arm64. Compose publishes no ports, runs as a non-root user, limits the service to 0.5 CPU and 256 MB RAM, mounts credentials read-only, and rotates JSON logs at 10 MB with three files. The restart policy is `no`, so successful completion and permanent configuration errors do not restart forever. To resume after a host reboot, start the Stack again; its persistent state is checked before any retry.

## Portainer Stacks

Create a Stack from the Git repository `nanosize/oracle-snatch` and select `compose.yaml`. Enter the values from `.env.example` in the Stack environment. Set `OCI_CONFIG_DIR`, `SSH_PUBLIC_KEY_HOST_PATH`, and `DATA_DIR` to absolute host paths. Set `PUID` and `PGID` to the owners of those files. Protect Telegram tokens and Discord webhook URLs as secrets.

## Configuration

| Setting | Default | Description |
|---|---:|---|
| `SUBNET_ID` | required | Subnet OCID in the home region |
| `SSH_PUBLIC_KEY_FILE` | Compose: `/run/secrets/ssh_authorized_keys.pub` | SSH public key only |
| `ACCOUNT_TYPE` | `FREE` | `FREE` = 2 OCPU/12 GB; `PAYG` = 4/24 after verifying eligibility |
| `PAYG_FREE_LIMITS_CONFIRMED` | unset | Required as `YES` when `ACCOUNT_TYPE=PAYG` |
| `OCPUS` | `2` | A1 OCPUs, whole number from 1 to 80 |
| `MEMORY_GB` | `12` | Memory in GB, at most 64 GB per OCPU and within account allowance |
| `BOOT_VOLUME_GB` | `50` | Boot volume size, 50–32,768 GB |
| `COMPARTMENT_ID` | tenancy root | Target only; inventory is tenancy-wide |
| `IMAGE_ID` | automatic | Optional image OCID |
| `DISPLAY_NAME` | `snatch-arm` | Instance display name |
| `INTERVAL_SECONDS` | `120` | Pause after a full pass through availability domains; minimum 120 |
| `OCI_CONFIG_FILE` | `~/.oci/config` | SDK config file; Compose uses `/run/oci/config` |
| `OCI_PROFILE` | `DEFAULT` | OCI config profile |
| `STATE_FILE` | `/data/state.json` | Durable request status, retry token, and instance OCID |
| `RESULT_FILE` | `/data/snatched.json` | Durable result |
| `TG_BOT_TOKEN`, `TG_CHAT_ID` | disabled | Telegram notifications; set both |
| `DISCORD_WEBHOOK_URL` | disabled | Discord webhook notifications |

## CLI, dry run, and status

```bash
python3 snatch.py --dry-run
python3 snatch.py --status
python3 snatch.py --version
```

Dry run validates OCI authentication, home region, subnet, image, all-compartment inventory, and the requested shape. It uses read-only OCI APIs and never launches or changes an OCI resource. `--status` reads only the local state file and does not connect to OCI.

## Idempotency and retries

Before a launch API call, the tool atomically persists the exact request payload, availability domain, an operation tag, and a unique `opc_retry_token`. It disables SDK-side retries so one visible retry policy controls the request. Immediately after OCI returns success, it persists the instance OCID before any other work.

After a lost response or process exit, it searches every compartment for the operation tag. If no instance is found, it reuses the exact stored request and token; it never creates a new token for an unknown request. OCI retry tokens expire after 24 hours. If the result remains unknown at 23 hours, the tool stops and asks for manual OCI Console reconciliation rather than risking a duplicate.

On success, the tool waits for `RUNNING`, saves the name, OCID, region, CPU, memory, and public IP, sends acquisition and running notifications, and exits. A persisted success prevents a second instance after a restart. A file lock prevents concurrent processes from sharing one state directory. SIGTERM and SIGINT leave request state durable.

Capacity shortages are retried silently. HTTP 429 respects `Retry-After` and uses exponential backoff with jitter, starting at 120 seconds. Quota excess, authentication/permission errors, permanent configuration errors, and unresolved outcomes stop the process and trigger notifications. Notification failure never restarts a launch. Requests are serial; the tool does not parallelize or evade OCI rate limits.

## Logs and troubleshooting

```bash
docker compose logs -f oracle-snatch
docker compose run --rm oracle-snatch --status
```

- Invalid subnet or compartment: check the OCID and OCI profile region.
- Home-region error: change the profile region to the tenancy home region.
- Free allowance stop: inspect A1 usage and all boot/block volumes across compartments in the OCI Console.
- Authentication/permission error: check API-key fingerprint, private-key path, group membership, and the IAM policy.
- Permission denied in Docker: align `PUID`/`PGID` with the mounted files and data directory.
- A `requesting` state after restart: keep the state file, run `--status`, and check the `oracle-snatch-operation` tag in the OCI Console. Deleting the state file while a request is unresolved can allow a duplicate launch.

Credentials and webhook URLs are not logged. The JSON state contains instance details and the SSH public key; do not commit it and restrict access to the data directory.

## Tests and license

```bash
python3 -m pip install -r requirements-dev.txt
ruff check .
pytest -q
```

OCI APIs are mocked in tests; no real OCI resource is created. GitHub Actions runs Ruff and pytest. The original MIT license and copyright notice are retained.

## Official references

- [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Oracle Cloud Infrastructure Free Tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm)
- [Arm-Based Compute](https://docs.oracle.com/en-us/iaas/Content/Compute/References/arm.htm)
- [Creating an Instance](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/launchinginstance.htm)
- [Python SDK ComputeClient](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/client/oci.core.ComputeClient.html)
- [OCI Core Services IAM Policy Reference](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/corepolicyreference.htm)
- [OCI IAM Policy Syntax](https://docs.oracle.com/en-us/iaas/Content/Identity/Concepts/policysyntax.htm)
