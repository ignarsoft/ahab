#!/usr/bin/env bash

# Keep piped downloads from executing until the full installer is parsed.
ahab_install() (
    set -euo pipefail
    umask 022

    # --- defaults ---
    # DESTDIR stages every write without modifying the host system.
    local dest="${DESTDIR:-}"
    if [[ -n "$dest" && ( "$dest" != /* || "$dest" == / ) ]]; then
        echo 'DESTDIR must be an absolute staging directory other than /.' >&2
        exit 1
    fi
    local root="$dest/opt/ahab"
    local config_dir="$dest/etc/ahab"
    local state_dir="$dest/var/lib/ahab"
    local bin_dir="$dest/usr/local/bin"
    local repo="${AHAB_REPOSITORY:-ignarsoft/ahab}"
    local ref="${AHAB_REF:-main}"
    local python="${AHAB_PYTHON:-}"
    local dev=0
    local with_systemd=0
    local remote=1

    # --- arg parsing ---
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --python) [[ $# -ge 2 ]] || { echo 'Missing --python value.' >&2; exit 1; }; python="$2"; shift 2 ;;
            --dev) dev=1; remote=0; shift ;;
            --systemd) with_systemd=1; shift ;;
            --no-systemd) with_systemd=0; shift ;;
            -h|--help)
                cat <<'EOF'
Usage: sudo bash install.sh [options]

Installs Ahab to /opt/ahab with configuration in /etc/ahab and state in /var/lib/ahab.

Options:
  --python PATH       Use this Python interpreter (must be 3.11+ on Linux/macOS)
  --dev               Install from current directory (no download), skip systemd setup
  --systemd           Enable and start systemd service after install (Linux only)
  --no-systemd        Do not configure systemd (default)
  -h, --help          Show this help

Environment overrides:
  AHAB_REPOSITORY     GitHub repo (default: ignarsoft/ahab)
  AHAB_REF            Git ref (default: main)
  AHAB_PYTHON         Python executable path
  DESTDIR            Stage all files under this absolute path; no root required

Run without --dev to download from GitHub. Requires root for /opt, /etc, /var.
EOF
                exit 0
                ;;
            *) echo "Unknown option: $1" >&2; exit 1 ;;
        esac
    done

    if [[ -n "$dest" && "$with_systemd" -eq 1 ]]; then
        echo 'Systemd setup is unavailable with DESTDIR.' >&2
        exit 1
    fi
    if [[ -z "$dest" && $EUID -ne 0 ]]; then
        echo "This installer must run as root. Use: sudo bash install.sh" >&2
        exit 1
    fi

    # --- detect source dir for --dev ---
    local source_dir=""
    if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
        source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    fi
    if [[ -n "$source_dir" && -f "$source_dir/pyproject.toml" && "$dev" -eq 1 ]]; then
        remote=0
    elif [[ "$dev" -eq 1 ]]; then
        echo "--dev requires running from the repository root." >&2
        exit 1
    fi

    # --- check platform ---
    local platform
    platform="$(uname -s)"
    if [[ "$platform" != "Linux" && "$platform" != "Darwin" ]]; then
        echo "Supported platforms: Linux, macOS (and WSL on Windows)." >&2
        exit 1
    fi

    # --- Python detection ---
    supported_python() {
        "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11) or sys.platform not in ("linux", "darwin"))' >/dev/null 2>&1
    }

    if [[ -n "$python" ]]; then
        supported_python "$python" || { echo 'A Linux or macOS Python 3.11+ interpreter is required.' >&2; exit 1; }
    else
        for candidate in python3 python python3.14 python3.13 python3.12 python3.11; do
            if command -v "$candidate" >/dev/null 2>&1 && supported_python "$candidate"; then
                python="$(command -v "$candidate")"
                break
            fi
        done
    fi

    # --- download if remote ---
    local temp
    temp="$(mktemp -d)"
    trap 'rm -rf -- "$temp"' EXIT

    download() { curl --fail --show-error --silent --location --retry 3 --proto '=https' --tlsv1.2 "$1" -o "$2"; }

    if [[ "$remote" -eq 1 ]]; then
        echo "Downloading Ahab ($ref) from $repo..."
        download "https://api.github.com/repos/$repo/tarball/$ref" "$temp/ahab.tar.gz"
        mkdir -p "$temp/source"
        tar -xzf "$temp/ahab.tar.gz" -C "$temp/source" --strip-components=1
        [[ -f "$temp/source/pyproject.toml" && -d "$temp/source/ahab" ]] || { echo 'Invalid source archive.' >&2; exit 1; }
        source_dir="$temp/source"
    fi

    # --- install application files ---
    echo "Installing application to $root..."
    mkdir -p "$root/ahab" "$root/deploy"
    cp -R "$source_dir/ahab/." "$root/ahab/"
    cp -R "$source_dir/deploy/." "$root/deploy/"
    cp "$source_dir/pyproject.toml" "$source_dir/ahab.example.toml" \
       "$source_dir/README.md" "$source_dir/LICENSE" "$root/"

    # --- private Python if needed ---
    if [[ -z "$python" ]] || ! "$python" -c 'import venv, ensurepip' >/dev/null 2>&1; then
        echo 'Installing a private Python runtime...'
        download https://astral.sh/uv/install.sh "$temp/uv-install.sh"
        UV_UNMANAGED_INSTALL="$temp/uv" sh "$temp/uv-install.sh"
        uv="$temp/uv/uv"
        export UV_PYTHON_INSTALL_DIR="$root/python"
        export UV_PYTHON_BIN_DIR="$root/python-bin"
        "$uv" python install 3.13 --no-progress
        python="$("$uv" python find --managed-python 3.13)"
    fi

    # --- virtual environment ---
    if [[ ! -e "$root/.venv" ]]; then
        echo "Creating virtual environment..."
        "$python" -m venv "$root/.venv"
    elif [[ ! -x "$root/.venv/bin/python" ]] || ! supported_python "$root/.venv/bin/python"; then
        echo 'Existing .venv is incompatible. Remove it and run the installer again.' >&2
        exit 1
    fi

    # --- install package ---
    local package="$root"
    [[ "$dev" -eq 0 ]] || package="$root[dev]"
    echo "Installing Ahab package..."
    "$root/.venv/bin/python" -m pip install --no-input --disable-pip-version-check --editable "$package"

    # Application code and native libraries must be readable by runtime users.
    chmod -R a+rX "$root"
    umask 077

    # --- configuration directory ---
    echo "Setting up configuration in $config_dir..."
    mkdir -p "$config_dir"
    mkdir -p "$state_dir/.oci" "$state_dir/.ssh"
    chmod 700 "$state_dir" "$state_dir/.oci" "$state_dir/.ssh"

    if [[ ! -e "$config_dir/ahab.toml" ]]; then
        cp "$root/ahab.example.toml" "$config_dir/ahab.toml"
        # Installed configuration must not depend on the invoking user's HOME.
        sed -e 's|^state_dir = .*|state_dir = "/var/lib/ahab/state"|' \
            -e 's|^oci_config = .*|oci_config = "/var/lib/ahab/.oci/config"|' \
            -e 's|^ssh_key_file = .*|ssh_key_file = "/var/lib/ahab/.ssh/id_ed25519.pub"|' \
            "$config_dir/ahab.toml" > "$config_dir/ahab.toml.tmp"
        mv "$config_dir/ahab.toml.tmp" "$config_dir/ahab.toml"
    fi

    # --- notifications template ---
    if [[ ! -e "$config_dir/notifications.env" ]]; then
        cat > "$config_dir/notifications.env" <<'EOF'
# Telegram bot token (get from @BotFather)
# TELEGRAM_BOT_TOKEN=

# Telegram chat ID (your user/group ID)
# TELEGRAM_CHAT_ID=

# HTTPS webhook URL (Discord/Slack-compatible, receives JSON with "text" and "content")
# AHAB_WEBHOOK_URL=
EOF
        chmod 600 "$config_dir/notifications.env"
    fi

    # --- wrapper script in /usr/local/bin ---
    mkdir -p "$bin_dir"
    cat > "$bin_dir/ahab" <<EOF
#!/usr/bin/env bash
set -euo pipefail
# Use the service identity when installed, so manual runs share its state safely.
if [[ "\$(uname -s)" == Linux ]] && id -u ahab >/dev/null 2>&1; then
    if [[ \$EUID -eq 0 ]]; then
        exec runuser -u ahab -- env HOME="$state_dir" "\$0" "\$@"
    elif [[ \$EUID -ne \$(id -u ahab) ]]; then
        echo 'Run this command with sudo: sudo ahab <command>' >&2
        exit 1
    fi
elif [[ \$EUID -ne 0 ]]; then
    echo 'Run this command with sudo: sudo ahab <command>' >&2
    exit 1
fi
export HOME="$state_dir"
cd -- "$root" || exit 1
exec "$root/.venv/bin/ahab" --config "$config_dir/ahab.toml" "\$@"
EOF
    chmod 755 "$bin_dir/ahab"

    # --- systemd service (Linux only) ---
    if [[ "$platform" == "Linux" && "$with_systemd" -eq 1 ]]; then
        echo "Configuring systemd service..."

        # create ahab user if not exists
        if ! id -u ahab >/dev/null 2>&1; then
            useradd --system --home-dir "$state_dir" --create-home --shell /usr/sbin/nologin ahab
        fi

        # ensure state dir ownership
        chown -R ahab:ahab "$state_dir"
        chmod 700 "$state_dir"

        chown root:ahab "$config_dir" "$config_dir/ahab.toml"
        chmod 750 "$config_dir"
        chmod 640 "$config_dir/ahab.toml"

        # install unit file from deploy/
        cp "$root/deploy/ahab.service" /etc/systemd/system/ahab.service

        systemctl daemon-reload
        systemctl enable --now ahab
        echo "systemd service enabled and started. View logs: journalctl -u ahab -f"
    fi

    # --- summary ---
    echo
    echo "Installation complete."
    echo "  Command:        ahab (in /usr/local/bin)"
    echo "  App dir:        $root"
    echo "  Config:         $config_dir/ahab.toml"
    echo "  Notifications:  $config_dir/notifications.env"
    echo "  State dir:      $state_dir"
    echo
    echo "Next steps:"
    echo "  1. Edit $config_dir/ahab.toml — set subnet_id and ssh_key_file"
    echo "  2. Place OCI API config at /var/lib/ahab/.oci/config (key_file must point to a local key)"
    echo "  3. Copy SSH public key to /var/lib/ahab/.ssh/id_ed25519.pub"
    echo "  4. Run: sudo ahab doctor (read-only checks)"
    echo "  5. Run: sudo ahab run    (starts hunting; can create a VM)"
    if [[ "$platform" == "Linux" && "$with_systemd" -eq 0 ]]; then
        echo "  To run as a service: sudo bash install.sh --systemd"
    fi
)
ahab_install "$@"