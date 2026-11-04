import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@pytest.fixture
def checkout(tmp_path):
    directory = tmp_path / "checkout with spaces"
    directory.mkdir()
    for name in (
        "install.sh",
        "install.ps1",
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


@pytest.mark.skipif(not POWERSHELL or os.name != "nt", reason="Windows PowerShell required")
def test_iex_remote_install(tmp_path):
    target = tmp_path / "installed app"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(target / ".venv")], check=True
    )
    modules = tmp_path / "modules"
    modules.mkdir()
    (modules / "pip.py").write_text("", encoding="utf-8")
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in (
            "pyproject.toml",
            "ahab.example.toml",
            "ahab/__init__.py",
            "README.md",
            "LICENSE",
        ):
            output.write(ROOT / name, f"snapshot/{name}")
    # Keep the user PATH unchanged while exercising the remote extraction and launcher.
    bin_dir = str(target / "bin")
    old_path = os.environ["PATH"]
    command = (
        "function Invoke-WebRequest { param($UseBasicParsing, $Uri, $OutFile) "
        "Copy-Item $env:SOURCE_ARCHIVE $OutFile }; "
        '$old = [Environment]::GetEnvironmentVariable("Path", "User"); '
        'try { [Environment]::SetEnvironmentVariable("Path", $env:TEST_BIN, "User"); '
        "Get-Content -Raw $env:INSTALLER | Invoke-Expression } "
        'finally { [Environment]::SetEnvironmentVariable("Path", $old, "User") }'
    ).replace("param($UseBasicParsing,", "param([switch]$UseBasicParsing,")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", command],
        env={
            **os.environ,
            "AHAB_INSTALL_DIR": str(target),
            "AHAB_PYTHON": sys.executable,
            "SOURCE_ARCHIVE": str(archive),
            "INSTALLER": str(ROOT / "install.ps1"),
            "PYTHONPATH": str(modules),
            "TEST_BIN": bin_dir,
            "PATH": old_path,
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (target / "ahab.toml").exists()
    assert (target / "bin/ahab.cmd").exists()
    assert not (target / "state").exists()


@pytest.mark.skipif(not POWERSHELL, reason="PowerShell is unavailable")
def test_powershell_syntax():
    script = ROOT / "install.ps1"
    command = (
        "$tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{str(script).replace(chr(39), chr(39) * 2)}', [ref]$tokens, [ref]$errors) > $null; "
        "if ($errors.Count) { $errors; exit 1 }"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", command], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not POWERSHELL or os.name != "nt", reason="Windows PowerShell required")
def test_powershell_install_and_preserve_config(checkout):
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(checkout / ".venv")], check=True
    )
    modules = checkout / "fake modules"
    modules.mkdir()
    (modules / "pip.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        'Path(os.environ["INSTALL_LOG"]).write_text(repr(sys.argv), encoding="utf-8")\n'
        'sys.exit(int(os.environ.get("FAIL_INSTALL", "0")))\n',
        encoding="utf-8",
    )
    command = [
        POWERSHELL,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(checkout / "install.ps1"),
        "-Python",
        sys.executable,
        "-Dev",
    ]
    env = {**os.environ, "PYTHONPATH": str(modules), "INSTALL_LOG": str(checkout / "pip.log")}
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[dev]" in (checkout / "pip.log").read_text(encoding="utf-8")
    config = checkout / "ahab.toml"
    assert config.read_bytes() == (checkout / "ahab.example.toml").read_bytes()
    config.write_text("custom settings", encoding="utf-8")
    assert subprocess.run(command, env=env, capture_output=True).returncode == 0
    assert config.read_text(encoding="utf-8") == "custom settings"
    assert (
        subprocess.run(command, env={**env, "FAIL_INSTALL": "1"}, capture_output=True).returncode
        != 0
    )
    assert config.read_text(encoding="utf-8") == "custom settings"


@pytest.mark.skipif(not POWERSHELL, reason="PowerShell is unavailable")
def test_powershell_rejects_missing_python(checkout):
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(checkout / "install.ps1"),
            "-Python",
            "ahab-python-not-found",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not (checkout / ".venv").exists()
    assert not (checkout / "ahab.toml").exists()
