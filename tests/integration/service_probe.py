"""Read/write probe for the real systemd sandbox; never contacts OCI."""

import os
import pwd
from pathlib import Path

import oci  # noqa: F401 -- also verify native dependency permissions

from ahab.config import Config
from ahab.notify import load_environment, validate_environment

assert os.geteuid() == pwd.getpwnam("ahab").pw_uid
assert os.environ["HOME"] == "/var/lib/ahab"
config = Config.load("/etc/ahab/ahab.toml")
load_environment("/etc/ahab/notifications.env")
validate_environment()
assert os.environ["TELEGRAM_CHAT_ID"] == "123"
assert Path(config.oci_config).read_text() == "test credentials\n"
assert Path(config.ssh_key_file).read_text() == "test public key\n"
assert Path("/var/lib/ahab/.oci/key.pem").read_text() == "test private key\n"
state = Path(config.state_dir)
state.mkdir(exist_ok=True)
(state / "integration-ok").write_text("ok")
