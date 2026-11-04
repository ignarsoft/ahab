#!/usr/bin/env bash

# Keep piped downloads from executing until the full installer is parsed.
ahab_install() (
    set -euo pipefail
    umask 077
    local python="${AHAB_PYTHON:-}" dev=0 source_dir="" root temp uv="" remote=0
    local repo="${AHAB_REPOSITORY:-ignarsoft/ahab}" ref="${AHAB_REF:-main}"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --python) [[ $# -ge 2 ]] || { echo 'Missing --python value.' >&2; exit 1; }; python="$2"; shift 2 ;;
            --dev) dev=1; shift ;;
            -h|--help) echo 'Usage: bash install.sh [--python PATH] [--dev]'; exit 0 ;;
            *) echo "Unknown option: $1" >&2; exit 1 ;;
        esac
    done
    if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
        source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    fi
    if [[ -n "$source_dir" && -f "$source_dir/pyproject.toml" ]]; then
        root="$source_dir"
    else
        remote=1
        root="${AHAB_INSTALL_DIR:-$HOME/.local/share/ahab}"
    fi
    mkdir -p "$root"
    root="$(cd -- "$root" && pwd)"
    temp="$(mktemp -d)"
    trap 'rm -rf -- "$temp"' EXIT

    download() { curl --fail --show-error --silent --location --retry 3 --proto '=https' --tlsv1.2 "$1" -o "$2"; }
    supported_python() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; }

    if [[ -n "$python" ]]; then
        supported_python "$python" || { echo 'Python 3.11+ is required.' >&2; exit 1; }
    else
        for candidate in python3 python python3.14 python3.13 python3.12 python3.11; do
            if command -v "$candidate" >/dev/null 2>&1 && supported_python "$candidate"; then
                python="$(command -v "$candidate")"
                break
            fi
        done
    fi
    if [[ -e "$root/.venv" ]]; then
        if [[ ! -x "$root/.venv/bin/python" ]] || ! supported_python "$root/.venv/bin/python"; then
            echo 'Existing .venv is incompatible. Rename it and run the installer again.' >&2
            exit 1
        fi
    elif [[ -z "$python" ]] || ! "$python" -c 'import venv, ensurepip' >/dev/null 2>&1; then
        echo 'Installing a private Python runtime...'
        download https://astral.sh/uv/install.sh "$temp/uv-install.sh"
        UV_UNMANAGED_INSTALL="$temp/uv" sh "$temp/uv-install.sh"
        uv="$temp/uv/uv"
        export UV_PYTHON_INSTALL_DIR="$root/python"
        export UV_PYTHON_BIN_DIR="$root/python-bin"
        "$uv" python install 3.13 --no-progress
        python="$("$uv" python find --managed-python 3.13)"
    fi

    if [[ "$remote" -eq 1 ]]; then
        echo "Downloading Ahab ($ref)..."
        download "https://api.github.com/repos/$repo/tarball/$ref" "$temp/ahab.tar.gz"
        mkdir "$temp/source"
        tar -xzf "$temp/ahab.tar.gz" -C "$temp/source" --strip-components=1
        [[ -f "$temp/source/pyproject.toml" && -d "$temp/source/ahab" ]] || { echo 'Invalid source archive.' >&2; exit 1; }
        # Only application files are updated; credentials, configuration, and state stay untouched.
        mkdir -p "$root/ahab"
        cp -R "$temp/source/ahab/." "$root/ahab/"
        cp "$temp/source/pyproject.toml" "$temp/source/ahab.example.toml" \
            "$temp/source/README.md" "$temp/source/LICENSE" "$root/"
    fi
    cd -- "$root"
    if [[ ! -e .venv ]]; then
        "$python" -m venv .venv
    fi
    local package="$root"
    [[ "$dev" -eq 0 ]] || package="$root[dev]"
    echo 'Installing Ahab...'
    "$root/.venv/bin/python" -m pip install --no-input --disable-pip-version-check --editable "$package"
    [[ -e ahab.toml ]] || cp ahab.example.toml ahab.toml

    if [[ "$remote" -eq 1 ]]; then
        local bin="$HOME/.local/bin" profile
        mkdir -p "$bin"
        {
            echo '#!/usr/bin/env bash'
            printf 'cd -- %q || exit 1\n' "$root"
            printf 'exec %q "$@"\n' "$root/.venv/bin/ahab"
        } > "$bin/ahab"
        chmod 700 "$bin/ahab"
        local profiles=("$HOME/.profile" "$HOME/.bashrc" "${ZDOTDIR:-$HOME}/.zshrc")
        [[ ! -f "$HOME/.bash_profile" ]] || profiles+=("$HOME/.bash_profile")
        for profile in "${profiles[@]}"; do
            if ! grep -Fq '# Ahab PATH' "$profile" 2>/dev/null; then
                printf '\nexport PATH="$HOME/.local/bin:$PATH" # Ahab PATH\n' >> "$profile"
            fi
        done
        printf '\nInstalled. Run now: %s/ahab discover\n' "$bin"
        echo 'New terminals can use: ahab discover'
    else
        echo 'Installed. Run: .venv/bin/ahab discover'
    fi
    printf 'Configuration: %s/ahab.toml\n' "$root"
)
ahab_install "$@"
