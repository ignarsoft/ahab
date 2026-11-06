import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(
    not BASH or sys.platform not in ("linux", "darwin"),
    reason="Installer requires Linux/macOS or WSL Python",
)


@pytest.fixture
def sandbox(tmp_path):
    checkout = tmp_path / "checkout with spaces"
    checkout.mkdir()
    for name in ("install.sh", "ahab.example.toml", "pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, checkout / name)
    for name in ("ahab", "deploy"):
        shutil.copytree(ROOT / name, checkout / name)
    python = checkout / "fake python"
    python.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == -c ]]; then\n'
        '  if [[ "$2" == "import venv, ensurepip" ]]; then exit 0; fi\n'
        '  exit "${FAIL_VERSION:-0}"\n'
        'elif [[ "$2" == venv ]]; then\n'
        '  mkdir -p "$3/bin"; cp "$0" "$3/bin/python"; chmod +x "$3/bin/python"\n'
        'elif [[ "$2" == pip ]]; then\n'
        '  printf "%s\\n" "$@" >> "$INSTALL_LOG"\n'
        '  exit "${FAIL_INSTALL:-0}"\n'
        "else exit 99; fi\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AHAB_") and k != "DESTDIR"}
    env.update(
        DESTDIR=str(tmp_path / "stage with spaces"),
        AHAB_PYTHON=str(python),
        INSTALL_LOG=str(tmp_path / "pip.log"),
    )
    return checkout, env


def install(sandbox, *args):
    checkout, env = sandbox
    return subprocess.run(
        [BASH, str(checkout / "install.sh"), "--dev", "--no-systemd", *args],
        env=env,
        cwd=checkout.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_dev_install_preserves_config(sandbox):
    checkout, env = sandbox
    stage = Path(env["DESTDIR"])
    app = stage / "opt/ahab"
    config = stage / "etc/ahab/ahab.toml"
    result = install(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (app / ".venv/bin/python").exists()
    assert 'state_dir = "/var/lib/ahab/state"' in config.read_text()
    assert "[dev]" in Path(env["INSTALL_LOG"]).read_text()
    assert config.stat().st_mode & 0o077 == 0
    assert app.stat().st_mode & 0o005 == 0o005
    assert (app / ".venv/bin/python").stat().st_mode & 0o005 == 0o005
    assert "--config" in (stage / "usr/local/bin/ahab").read_text()
    config.write_text("custom settings")
    result = install(sandbox)
    assert result.returncode == 0, result.stderr
    assert config.read_text() == "custom settings"
    assert not (app / "deploy/deploy").exists()


@pytest.mark.parametrize("failure", ["FAIL_VERSION", "FAIL_INSTALL"])
def test_dev_failure_does_not_copy_config(sandbox, failure):
    _, env = sandbox
    env[failure] = "1"
    result = install(sandbox)
    assert result.returncode != 0
    assert not (Path(env["DESTDIR"]) / "etc/ahab/ahab.toml").exists()
    if failure == "FAIL_VERSION":
        assert "interpreter is required" in result.stderr
        assert not Path(env["INSTALL_LOG"]).exists()
    else:
        assert "pip" in Path(env["INSTALL_LOG"]).read_text()


def test_dev_rejects_incompatible_venv(sandbox):
    _, env = sandbox
    (Path(env["DESTDIR"]) / "opt/ahab/.venv").mkdir(parents=True)
    result = install(sandbox)
    assert result.returncode != 0
    assert "Existing .venv is incompatible" in result.stderr


def test_remote_install(sandbox, tmp_path):
    checkout, env = sandbox
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for name in (
            "ahab",
            "deploy",
            "pyproject.toml",
            "ahab.example.toml",
            "README.md",
            "LICENSE",
        ):
            output.add(checkout / name, arcname=f"snapshot/{name}")
    tools = tmp_path / "tools"
    tools.mkdir()
    curl = tools / "curl"
    curl.write_text(
        '#!/usr/bin/env bash\nwhile [[ "$1" != -o ]]; do shift; done\ncp "$SOURCE_ARCHIVE" "$2"\n'
    )
    curl.chmod(0o755)
    env.update(SOURCE_ARCHIVE=str(archive), PATH=str(tools) + os.pathsep + env["PATH"])
    config = Path(env["DESTDIR"]) / "etc/ahab/ahab.toml"
    for attempt in range(2):
        result = subprocess.run(
            [BASH],
            input=(ROOT / "install.sh").read_text(),
            env=env,
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if attempt == 0:
            assert 'state_dir = "/var/lib/ahab/state"' in config.read_text()
            config.write_text("custom settings")
        else:
            assert config.read_text() == "custom settings"
    assert not (Path(env["DESTDIR"]) / "opt/ahab/deploy/deploy").exists()


def test_install_sh_rejects_non_root():
    env = {k: v for k, v in os.environ.items() if not k.startswith("AHAB_") and k != "DESTDIR"}
    kwargs = {}
    if os.geteuid() == 0:
        import pwd

        nobody = pwd.getpwnam("nobody")
        kwargs.update(user=nobody.pw_uid, group=nobody.pw_gid, extra_groups=[])
    result = subprocess.run(
        [BASH],
        input=(ROOT / "install.sh").read_text(),
        cwd="/tmp",
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        **kwargs,
    )
    assert result.returncode != 0
    assert "must run as root" in result.stderr


def test_staging_rejects_systemd(sandbox):
    result = install(sandbox, "--systemd")
    assert result.returncode != 0
    assert "unavailable with DESTDIR" in result.stderr
