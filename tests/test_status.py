import json
from unittest.mock import Mock

import pytest

from ahab import cli
from ahab.storage import Store


def test_status_does_not_create_directory(tmp_path):
    directory = tmp_path / "missing"
    assert Store(directory, create=False).status() == {"phase": "not_started", "attempts": 0}
    assert not directory.exists()


@pytest.mark.parametrize(
    "extra,now,phase",
    [
        ({}, 100, "ready"),
        ({"next_at": 200}, 100, "waiting"),
        ({"next_at": 50}, 100, "ready"),
        ({"pending": {"created_at": 0, "token": "SECRET"}}, 100, "uncertain"),
        ({"instance_id": "vm1", "notified": True}, 100, "accepted"),
    ],
)
def test_status_redacts_journal_and_never_writes(tmp_path, extra, now, phase):
    store = Store(tmp_path)
    store.save({"attempts": 3, "plan": {"ssh_public_key": "SECRET"}, **extra})
    before = (tmp_path / "state.json").read_bytes()
    result = store.status(now)
    assert result["phase"] == phase
    assert result["attempts"] == 3
    assert "SECRET" not in json.dumps(result)
    assert "plan" not in result
    assert (tmp_path / "state.json").read_bytes() == before
    assert not (tmp_path / "hunter.lock").exists()


def test_status_flags_expired_pending_without_revealing_payload(tmp_path):
    store = Store(tmp_path)
    store.save({"pending": {"created_at": 0, "payload": "SECRET", "token": "SECRET"}})
    result = store.status(23 * 3600)
    assert result["manual_reconciliation_required"] is True
    assert "SECRET" not in json.dumps(result)


def test_status_cli_needs_no_credentials_or_notification_file(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    directory = tmp_path / "state"
    config.write_text(f"state_dir = {json.dumps(str(directory))}\n")
    monkeypatch.setattr(
        "sys.argv", ["ahab", "status", "--config", str(config), "--env-file", "missing.env"]
    )
    api = Mock(side_effect=AssertionError("status must not construct OCI clients"))
    monkeypatch.setattr(cli, "Oracle", api)
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "not_started"
    api.assert_not_called()
    assert not directory.exists()


def test_status_corrupt_journal_fails_without_replacing_it(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json")
    with pytest.raises(ValueError):
        Store(tmp_path, create=False).status()
    assert path.read_text() == "not json"
