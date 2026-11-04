import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")


@pytest.fixture
def checkout(tmp_path):
    """Create a minimal repo checkout with all required directories."""
    directory = tmp_path / "checkout with spaces"
    directory.mkdir()
    # Files
    for name in (
        "install.sh",
        "ahab.example.toml",
        "pyproject.toml",
        "README.md",
        "LICENSE",
    ):
        shutil.copy2(ROOT / name, directory / name)
    # Directories needed by installer
    (directory / "ahab").mkdir()
    (directory / "ahab" / "__init__.py").write_text("", encoding="utf-8")
    (directory / "deploy").mkdir()
    (directory / "deploy" / "ahab.service").write_text(
        "[Unit]\nDescription=Test\n[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=multi-user.target\n",
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def fake_python(checkout):
    """A fake Python that passes version/platform checks and mocks venv/pip."""
    path = checkout / "fake python"
    path.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == -c ]]; then\n'
        '  if [[ "$2" == "import venv, ensurepip" ]]; then exit 0; fi\n'
        '  exit "${FAIL_VERSION:-0}";\n'
        "fi\n"
        'if [[ "$2" == venv ]]; then\n'
        '  mkdir -p "$3/bin"\n'
        '  cp "$0" "$3/bin/python"\n'
        '  chmod +x "$3/bin/python"\n'
        'elif [[ "$2" == pip ]]; then\n'
        '  printf "%s\\n" "$@" >> "$INSTALL_LOG"\n'
        '  exit "${FAIL_INSTALL:-0}"\n'
        "else exit 99; fi\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def install(checkout, fake_python, *args, **extra_env):
    env = {
        **os.environ,
        "AHAB_SKIP_ROOT": "1",
        "INSTALL_LOG": (checkout / "pip.log").as_posix(),
        "AHAB_ALLOW_NONROOT": "1",
        **extra_env,
    }
    return subprocess.run(
        [
            BASH,
            (checkout / "install.sh").as_posix(),
            "--python",
            fake_python.as_posix(),
            "--dev",
            *args,
        ],
        cwd=checkout.parent,
        env=env,
        text=True,
        capture_output=True,
    )


@pytest.mark.skipif(
    not BASH,
    reason="Installer tests require bash and Linux/macOS (skipped on Windows without WSL)",
)
def test_dev_install_preserves_config(checkout, fake_python, tmp_path):
    """Test installer with AHAB_SKIP_ROOT=1 uses temp dirs."""
    target = tmp_path / "test-ahab"
    env = {
        **os.environ,
        "AHAB_INSTALL_DIR": target.as_posix(),
        "AHAB_SKIP_ROOT": "1",
        "INSTALL_LOG": (checkout / "pip.log").as_posix(),
    }
    result = subprocess.run(
        [BASH, (checkout / "install.sh").as_posix(), "--python", fake_python.as_posix(), "--dev"],
        cwd=checkout.parent,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert target.joinpath(".venv/bin/python").exists()
    config = target / "ahab.toml"
    assert config.read_bytes() == (checkout / "ahab.example.toml").read_bytes()
    assert "[dev]" in (checkout / "pip.log").read_text(encoding="utf-8")

    # Re-run preserves config
    config.write_text("custom settings", encoding="utf-8")
    result = subprocess.run(
        [BASH, (checkout / "install.sh").as_posix(), "--python", fake_python.as_posix(), "--dev"],
        cwd=checkout.parent,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
    assert config.read_text(encoding="utf-8") == "custom settings"


@pytest.mark.skipif(
    not BASH,
    reason="Installer tests require bash and Linux/macOS (skipped on Windows without WSL)",
)
@pytest.mark.parametrize("failure", ["FAIL_VERSION", "FAIL_INSTALL"])
def test_dev_failure_does_not_copy_config(checkout, fake_python, failure, tmp_path):
    target = tmp_path / "test-ahab"
    env = {
        **os.environ,
        "AHAB_INSTALL_DIR": target.as_posix(),
        failure: "1",
        "AHAB_SKIP_ROOT": "1",
        "INSTALL_LOG": (checkout / "pip.log").as_posix(),
    }
    result = subprocess.run(
        [BASH, (checkout / "install.sh").as_posix(), "--python", fake_python.as_posix(), "--dev"],
        cwd=checkout.parent,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert not (target / "ahab.toml").exists()


@pytest.mark.skipif(
    not BASH,
    reason="Installer tests require bash and Linux/macOS (skipped on Windows without WSL)",
)
def test_dev_rejects_incompatible_venv(checkout, fake_python, tmp_path):
    target = tmp_path / "test-ahab"
    target.mkdir(parents=True)
    (target / ".venv").mkdir()
    env = {
        **os.environ,
        "AHAB_INSTALL_DIR": target.as_posix(),
        "AHAB_SKIP_ROOT": "1",
        "INSTALL_LOG": (checkout / "pip.log").as_posix(),
    }
    result = subprocess.run(
        [BASH, (checkout / "install.sh").as_posix(), "--python", fake_python.as_posix(), "--dev"],
        cwd=checkout.parent,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "Existing .venv is incompatible" in result.stderr


@pytest.mark.skipif(
    not BASH,
    reason="Installer tests require bash and Linux/macOS (skipped on Windows without WSL)",
)
def test_remote_install(checkout, fake_python, tmp_path):
    # Create source tarball with full structure
    source = checkout / "ahab"
    source.mkdir(exist_ok=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
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
        '#!/usr/bin/env bash\nwhile [[ "$1" != -o ]]; do shift; done\ncp "$SOURCE_ARCHIVE" "$2"\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    target = home / "test-ahab"
    env = {
        **os.environ,
        "HOME": home.as_posix(),
        "AHAB_INSTALL_DIR": target.as_posix(),
        "AHAB_PYTHON": fake_python.as_posix(),
        "SOURCE_ARCHIVE": archive.as_posix(),
        "INSTALL_LOG": (tmp_path / "pip.log").as_posix(),
        "AHAB_SKIP_ROOT": "1",
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
    }
    for _ in range(2):
        result = subprocess.run(
            [BASH],
            input=(ROOT / "install.sh").read_text(encoding="utf-8"),
            env=env,
            cwd=tmp_path,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        config = target / "ahab.toml"
        if config.read_text(encoding="utf-8") != "custom settings":
            assert config.read_bytes() == (checkout / "ahab.example.toml").read_bytes()
            config.write_text("custom settings", encoding="utf-8")
    assert (target / ".venv/bin/python").exists()
    assert not (target / "state").exists()


@pytest.mark.skipif(
    not BASH,
    reason="Installer tests require bash and Linux/macOS (skipped on Windows without WSL)",
)
def test_install_sh_rejects_non_root(checkout, fake_python):
    # Use the fake python (which passes checks) but without AHAB_SKIP_ROOT
    env = {**os.environ}
    result = subprocess.run(
        [BASH, (checkout / "install.sh").as_posix(), "--dev", "--python", fake_python.as_posix()],
        cwd=checkout.parent,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "must run as root" in result.stderr
