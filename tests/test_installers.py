import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")


@pytest.fixture
def checkout(tmp_path):
    directory = tmp_path / "checkout with spaces"
    directory.mkdir()
    for name in (
        "install.sh",
        "ahab.example.toml",
        "pyproject.toml",
        "README.md",
        "LICENSE",
    ):
        shutil.copy2(ROOT / name, directory / name)
    return directory


@pytest.fixture
def fake_python(checkout):
    path = checkout / "fake python"
    path.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == -c ]]; then exit "${FAIL_VERSION:-0}"; fi\n'
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
    return subprocess.run(
        [BASH, (checkout / "install.sh").as_posix(), "--python", fake_python.as_posix(), *args],
        cwd=checkout.parent,
        env={**os.environ, "INSTALL_LOG": (checkout / "pip.log").as_posix(), **extra_env},
        text=True,
        capture_output=True,
    )


@pytest.mark.skipif(not BASH, reason="Bash is unavailable")
def test_shell_install_and_preserve_config(checkout, fake_python):
    result = install(checkout, fake_python, "--dev")
    assert result.returncode == 0, result.stderr
    assert (checkout / ".venv/bin/python").exists()
    config = checkout / "ahab.toml"
    assert config.read_bytes() == (checkout / "ahab.example.toml").read_bytes()
    assert "[dev]" in (checkout / "pip.log").read_text(encoding="utf-8")
    config.write_text("custom settings", encoding="utf-8")
    assert install(checkout, fake_python).returncode == 0
    assert config.read_text(encoding="utf-8") == "custom settings"
    assert not (checkout / "state").exists()


@pytest.mark.skipif(not BASH, reason="Bash is unavailable")
@pytest.mark.parametrize("failure", ["FAIL_VERSION", "FAIL_INSTALL"])
def test_shell_failure_does_not_copy_config(checkout, fake_python, failure):
    result = install(checkout, fake_python, **{failure: "1"})
    assert result.returncode != 0
    assert not (checkout / "ahab.toml").exists()


@pytest.mark.skipif(not BASH, reason="Bash is unavailable")
def test_shell_rejects_incompatible_venv(checkout, fake_python):
    (checkout / ".venv").mkdir()
    result = install(checkout, fake_python)
    assert result.returncode != 0
    assert "Existing .venv is incompatible" in result.stderr


@pytest.mark.skipif(not BASH, reason="Bash is unavailable")
def test_piped_shell_remote_install(checkout, fake_python, tmp_path):
    source = checkout / "ahab"
    source.mkdir()
    (source / "__init__.py").write_text("", encoding="utf-8")
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for name in ("ahab", "pyproject.toml", "ahab.example.toml", "README.md", "LICENSE"):
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
    target = home / "installed app"
    env = {
        **os.environ,
        "HOME": home.as_posix(),
        "AHAB_INSTALL_DIR": target.as_posix(),
        "AHAB_PYTHON": fake_python.as_posix(),
        "SOURCE_ARCHIVE": archive.as_posix(),
        "INSTALL_LOG": (tmp_path / "pip.log").as_posix(),
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
    assert (home / ".local/bin/ahab").exists()
    assert (home / ".bashrc").read_text(encoding="utf-8").count("# Ahab PATH") == 1
    assert not (target / "state").exists()
