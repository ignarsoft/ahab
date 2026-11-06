<div align="center">

# Ahab

**A patient hunter for elusive Oracle Cloud A1 capacity.**

[![CI](https://github.com/ignarsoft/ahab/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ignarsoft/ahab/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/ignarsoft/ahab?style=flat&color=1f6f8b)](https://github.com/ignarsoft/ahab/releases/latest)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-1f6f8b)](LICENSE)

<img src="docs/assets/ahab-final-chase.jpg" alt="Ahab raising his harpoon during the final chase of Moby-Dick, illustrated by I. W. Taber" width="250">

<sub><em>Ahab in his final chase with Moby Dick</em> — I. W. Taber, 1902.<br>
<a href="https://commons.wikimedia.org/wiki/File:Moby_Dick_final_chase.jpg">Wikimedia Commons</a> · Public domain in the United States.</sub>

<br><br>

**Linux · macOS · WSL**

[Setup](#setup) · [Capacity checks](#capacity-checks) · [Recovery](#recovery) · [Notifications](#notifications) · [Systemd](#systemd-service-linux) · [Development](#development)

</div>

---

A restart-safe capacity hunter for Oracle Cloud's `VM.Standard.A1.Flex`.
Defaults: **2 OCPU · 12 GB RAM · 50 GB boot volume**.

| Built for the long chase | What it does |
| --- | --- |
| **Keep searching** | Rotates through compatible Availability Domains and checks capacity before launching. |
| **Back off gracefully** | Uses exponential backoff, jitter, and `Retry-After`. |
| **Resume safely** | Preserves launch requests and retry tokens across restarts. |
| **Know when it lands** | Saves results and sends Telegram or HTTPS webhook notifications. |

> [!IMPORTANT]
> Capacity and free-tier eligibility are **not guaranteed**. Check your account limits,
> existing resources, and pricing before running. Ahab requires the tenancy home region
> and never creates networks or deletes instances.

## Setup

**Linux / macOS / WSL** (Bash, curl, tar, and sudo)

```bash
curl -fsSL https://raw.githubusercontent.com/ignarsoft/ahab/main/install.sh | sudo bash
```

For WSL, run this inside your Linux distribution. Enable systemd only if you want
the optional background service; manual operation does not require it.

The installer runs as root and sets up:
- Application in `/opt/ahab` (owned by root)
- Configuration in `/etc/ahab/ahab.toml` and `/etc/ahab/notifications.env`
- Private state and credentials in `/var/lib/ahab` (root-owned until service setup)
- A system-wide `ahab` command in `/usr/local/bin`
- Optional systemd service, running as the dedicated `ahab` user

No manual PATH changes are needed. The installer uses Python 3.11+ if available,
or installs a private Python 3.13 runtime via uv.

These commands execute downloaded code. Review the installer first if needed.
Downloads use GitHub, PyPI, and, when Python is missing, Astral's uv sources.

Installation alone does not start hunting. On interactive Linux terminals with
systemd, the installer asks whether to install the service, then separately whether
to validate and start it. **Confirming startup can create a VM.** For unattended
installation without prompts or startup, append `-s -- --no-systemd` to `sudo bash`.

After installing, supply:
- OCI API profile at `/var/lib/ahab/.oci/config`
- OCI private API key at `/var/lib/ahab/.oci/key.pem`; set the profile's `key_file`
  to that absolute path
- SSH **public** key at `/var/lib/ahab/.ssh/id_ed25519.pub` (not your SSH private key)
- Subnet ID in `/etc/ahab/ahab.toml`

Use `sudo` to create/edit these protected files. Set credential files to mode `600`.
When the Linux service account exists, make files under `/var/lib/ahab` accessible
to it with `sudo chown -R ahab:ahab /var/lib/ahab`. Without the service account,
keep them root-owned. Do not make credentials world-readable.

Then run `sudo ahab doctor` (read-only checks) and `sudo ahab run` (starts hunting).
The launcher uses the `ahab` service account when present on Linux; otherwise it
runs as root. Configuration stays private, and manual and service runs share the
same state owner. Do not bypass the launcher for operational commands.

Set `AHAB_PYTHON`, `AHAB_REPOSITORY`, or `AHAB_REF` before installing to override
the Python executable, GitHub repository, or revision. The install location is fixed
at `/opt/ahab`. Rerun the command to update. Stop any running hunter before updating.

Set `subnet_id` and `ssh_key_file` in `/etc/ahab/ahab.toml`. Optionally choose an `image_id`;
otherwise Ahab selects a compatible Ubuntu image and keeps it across restarts.
New system installs generate absolute credential and state paths. Updates preserve
existing configuration: for older installs, change `oci_config`, `ssh_key_file`, and
`state_dir` to the `/var/lib/ahab` paths above if they still use `~` or relative paths.
The repository example retains portable development defaults.
OCI API keys and SSH keys are separate.

```bash
sudo ahab doctor        # Read-only checks
sudo ahab run --dry-run # Read-only checks
sudo ahab run           # Can create a VM
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
retried with `sudo ahab run`; delivery may repeat after an interruption.

## Notifications

Edit `/etc/ahab/notifications.env` with `sudo`. The installed launcher and systemd
service both load this file. Supported keys:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
AHAB_WEBHOOK_URL
```

Use one `KEY=value` per line. Blank lines and lines beginning with `#` are ignored;
matching outer single or double quotes are optional. Values are literal: no shell
commands, substitutions, `export`, escapes, or inline comments. Unknown and duplicate
keys are rejected. Existing process environment variables take precedence; `sudo`
may discard caller environment variables, so prefer the shared file.

Webhooks require HTTPS and receive `text` and `content` fields. Tokens are not stored
in TOML. The file is root-owned and private (service setup grants read access only
to the `ahab` group). Direct development CLI runs load it only with `--env-file PATH`.

## systemd service (Linux)

Install the service without unattended startup:

```bash
curl -fsSL https://raw.githubusercontent.com/ignarsoft/ahab/main/install.sh | sudo bash -s -- --systemd
```

This creates the `ahab` system user, transfers state ownership to it, and installs
`deploy/ahab.service`. It does not enable or start the service unless you confirm the
separate interactive startup question or explicitly pass `--systemd --start`.
A running systemd instance is required; unsupported hosts fail clearly.

After configuring credentials and subnet, start explicitly:

```bash
sudo ahab doctor && sudo systemctl enable --now ahab
sudo journalctl -u ahab -f
```

Alternatively, rerun the installer with `--systemd --start`. It runs the same
read-only `doctor` check as the service user before enabling/starting. A failed
check aborts startup. A successful check does not guarantee capacity or free pricing.
Stop the service before updating: `sudo systemctl stop ahab`. Updates preserve
configuration and state. The installer refuses to update an active service.
Do not run a manual hunter and the service concurrently.

The service runs as the `ahab` user with `/var/lib/ahab` as its home directory.
Exit codes: `0` result, `1` error, `2` search limit, `75` transient failure, `130` interruption.
Only transient failures are restarted; configuration and quota errors stay stopped.

## Development

From a local checkout:

```bash
sudo bash install.sh --dev
```

This installs from the checkout beside `install.sh` (no download), with development
dependencies in the same system-wide layout. It skips service prompts unless you
explicitly add `--systemd`. It is not an unprivileged local installation.

Installer tests use `DESTDIR` to stage all filesystem writes in temporary directories
without root. Staging refuses systemd setup and is not a relocatable deployment.

```bash
/opt/ahab/.venv/bin/python -m pytest -q
/opt/ahab/.venv/bin/ruff check ahab tests
/opt/ahab/.venv/bin/ruff format --check ahab tests
```

Tests use mocks without Oracle credentials, including remote-installer download mocks.
CI covers Python 3.13 on Linux/macOS and bootstrap without preinstalled Python.
A separate Ubuntu job installs the actual systemd unit and checks startup rejection,
service identity, secret access, notification loading, state ownership, and sandboxed
execution. Its workload is a local probe with networking denied, not an OCI hunter.
WSL uses the Linux installer; dedicated WSL and live cloud provisioning tests are not
included.

## Credits

The illustration above is **I. W. Taber's** depiction of Ahab's final chase of
Moby-Dick, from the **1902 edition of *Moby Dick*, Charles Scribner's Sons, New York**.
Image source and rights information: [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Moby_Dick_final_chase.jpg).
The illustration is public domain in the United States; its rights status is separate
from the project's software license.

## License

Code: **MIT**. See [LICENSE](LICENSE).
