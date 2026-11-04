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

**Linux / macOS / WSL** (Bash, curl, and tar)

```bash
curl -fsSL https://raw.githubusercontent.com/ignarsoft/ahab/main/install.sh | bash
```

For WSL, run the command inside your Linux distribution. Keep the installation,
Python environment, and OCI credentials in its Linux filesystem.

Installation is unattended and requires no administrator access, Git, or preinstalled
Python. It downloads Ahab, uses Python 3.11+ if available, or installs a private Python
3.13 runtime through uv. It then creates `.venv`, installs dependencies, and adds an
`ahab` launcher. Existing configuration, credentials, and state are preserved.

| Platform | Installation | Command |
| --- | --- | --- |
| Linux / macOS / WSL | `~/.local/share/ahab` | `~/.local/bin/ahab` |

New terminals can use `ahab`; the installer adds `~/.local/bin` to
`.profile`, `.bashrc`, and `.zshrc`.
The launcher uses the installation directory so configuration and state stay consistent.

Set `AHAB_INSTALL_DIR`, `AHAB_PYTHON`, `AHAB_REPOSITORY`, or `AHAB_REF` before installing
to override the destination, Python executable, GitHub repository, or revision.
Rerun the command to update. Stop any running hunter before updating.

These commands execute downloaded code. Review the installer first if needed.
Downloads use GitHub, PyPI, and, when Python is missing, Astral's uv installer/runtime sources.

Installation does not start the hunter or provision cloud resources. Before use, supply
an OCI API profile in `~/.oci/config`, an SSH public key, and an existing subnet.
Run `ahab discover` to inspect available resources.

Set `subnet_id` and `ssh_key_file` in the installation's `ahab.toml`. Optionally choose an `image_id`;
otherwise Ahab selects a compatible Ubuntu image and keeps it across restarts.
Relative paths use the working directory. OCI API keys and SSH keys are separate.

```bash
ahab doctor          # Read-only checks.
ahab run --dry-run   # Read-only checks.
ahab run             # Can create a VM.
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

## Linux service

Requires systemd; on WSL, enable systemd in your distribution first.

Install Ahab in `/opt/ahab/.venv` and create a dedicated `ahab` system user.
Use `deploy/ahab.service` with:

- Configuration: `/etc/ahab/ahab.toml`
- Notification variables: `/etc/ahab/notifications.env` (mode `600`)
- OCI profile and API key: `/var/lib/ahab/.oci/`
- SSH public key: `/var/lib/ahab/.ssh/id_ed25519.pub`
- `state_dir`: `/var/lib/ahab/state`, writable by `ahab`

Set absolute paths in the configuration and OCI profile. The service cannot read
`/home` directories. Keep application files root-owned and credentials private.

```bash
sudo -u ahab /opt/ahab/.venv/bin/ahab doctor --config /etc/ahab/ahab.toml
sudo cp deploy/ahab.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ahab
journalctl -u ahab -f
```

The service stops after a result and retries only transient failures. Exit codes:
`0` result, `1` error, `2` search limit, `75` transient failure, `130` interruption.

## Development

From a local checkout, run `bash install.sh --dev`.
Local installs use the checkout's `.venv` and do not change PATH.

```bash
bash install.sh --dev
.venv/bin/python -m pytest -q
.venv/bin/ruff check ahab tests
.venv/bin/ruff format --check ahab tests
```

Tests use mocks without Oracle credentials, including remote-installer download mocks.
CI tests installation on Linux and macOS. WSL uses the Linux installer; dedicated WSL,
live cloud provisioning, and systemd tests are not included.

## License

MIT. See [LICENSE](LICENSE).
