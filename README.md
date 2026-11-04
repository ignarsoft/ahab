# Ahab

A restart-safe Oracle Cloud capacity hunter for `VM.Standard.A1.Flex`.
Defaults: **2 OCPU, 12 GB RAM, 50 GB boot volume**.

- Rotates through compatible Availability Domains.
- Checks capacity before launching, with optional fallback.
- Uses exponential backoff, jitter, and `Retry-After`.
- Preserves launch requests and retry tokens across restarts.
- Saves results and supports Telegram and HTTPS webhooks.

Capacity and free-tier eligibility are **not guaranteed**. Check your account limits,
existing resources, and pricing before running. Ahab requires the tenancy home region
and never creates networks or deletes instances.

## Setup

**Linux / macOS / WSL** (Bash, curl, tar, and sudo)

```bash
curl -fsSL https://raw.githubusercontent.com/ignarsoft/ahab/main/install.sh | sudo bash
```

For WSL, run the command inside your Linux distribution with systemd enabled.

The installer runs as root and sets up:
- Application in `/opt/ahab` (owned by root)
- Configuration in `/etc/ahab/ahab.toml` and `/etc/ahab/notifications.env`
- State and credentials in `/var/lib/ahab` (owned by the `ahab` user)
- A system-wide `ahab` command in `/usr/local/bin`
- Optional systemd service (enabled with `--systemd`)

No manual PATH changes are needed. The installer uses Python 3.11+ if available,
or installs a private Python 3.13 runtime via uv.

These commands execute downloaded code. Review the installer first if needed.
Downloads use GitHub, PyPI, and, when Python is missing, Astral's uv sources.

Installation does not start the hunter or provision cloud resources. After installing,
supply:
- OCI API profile at `/var/lib/ahab/.oci/config`
- SSH public key at `/var/lib/ahab/.ssh/id_ed25519.pub`
- Subnet ID and SSH key path in `/etc/ahab/ahab.toml`

Then run `ahab doctor` (read-only checks) and `ahab run` (starts hunting).

Set `AHAB_PYTHON`, `AHAB_REPOSITORY`, or `AHAB_REF` before installing to override
the Python executable, GitHub repository, or revision. The install location is fixed
at `/opt/ahab`. Rerun the command to update. Stop any running hunter before updating.

Set `subnet_id` and `ssh_key_file` in `/etc/ahab/ahab.toml`. Optionally choose an `image_id`;
otherwise Ahab selects a compatible Ubuntu image and keeps it across restarts.
All paths in the configuration are absolute. OCI API keys and SSH keys are separate.

```bash
ahab doctor          # Read-only checks
ahab run --dry-run   # Read-only checks
ahab run             # Can create a VM
```

Use `--config PATH` to select another configuration file. Public SSH access also
requires suitable routes, security rules, and guest firewall settings.

## Capacity checks

| `capacity_mode` | Behavior |
| --- | --- |
| `auto` | Checks capacity; falls back to launch if reports are unsupported, inaccessible, or inconclusive. |
| `required` | Launches only after a positive report. |
| `off` | Attempts launches without capacity reports. |

A positive report does not reserve capacity. Rate limits and transient failures
trigger backoff, not fallback. Authentication, quota, and configuration errors stop
the search. The default interval is 120 seconds plus up to 30 seconds of jitter.

## Recovery

`state_dir` stores the journal, local process lock, and `result.json` containing the
instance ID, observed state, IPs, and console URL.

Run **one hunter per target**, using the same state directory after restarts. The
lock does not coordinate separate hosts. Matching names or `ahab-hunter-id` tags
prevent another launch, including for stopped instances.

Do not delete the journal or change instance settings during an uncertain launch.
Requests older than 23 hours require manual reconciliation rather than a new token.
Once a launch is accepted, polling or notification failures never trigger another
creation. Results may still show `PROVISIONING` when the wait timeout expires.

Attempt limits and deadlines persist across restarts. Failed notifications can be
retried with `ahab run`; delivery may repeat after an interruption.

## Notifications

Set these environment variables as needed:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
AHAB_WEBHOOK_URL
```

Webhooks require HTTPS and receive `text` and `content` fields. Tokens are not stored
in TOML. Manual runs do not load `.env` files.

## systemd service (Linux)

The installer can enable the service automatically:

```bash
curl -fsSL https://raw.githubusercontent.com/ignarsoft/ahab/main/install.sh | sudo bash -s -- --systemd
```

Or run `sudo bash install.sh --systemd` after installation.

This creates the `ahab` system user, installs the unit file, enables and starts the service.
Logs: `journalctl -u ahab -f`

The service runs as the `ahab` user with `/var/lib/ahab` as its home directory.
Exit codes: `0` result, `1` error, `2` search limit, `75` transient failure, `130` interruption.
Only transient failures are restarted; configuration and quota errors stay stopped.

## Development

From a local checkout:

```bash
sudo bash install.sh --dev
```

This installs from the current directory (no download) and skips systemd setup.
Local installs do not create the systemd service.

```bash
/opt/ahab/.venv/bin/python -m pytest -q
/opt/ahab/.venv/bin/ruff check ahab tests
/opt/ahab/.venv/bin/ruff format --check ahab tests
```

Tests use mocks without Oracle credentials, including remote-installer download mocks.
CI tests installation on Linux and macOS. WSL uses the Linux installer; dedicated WSL,
live cloud provisioning, and systemd tests are not included.

## License

MIT. See [LICENSE](LICENSE).
