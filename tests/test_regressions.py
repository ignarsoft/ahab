from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import oci
import pytest

from ahab.config import Config
from ahab.engine import Engine, retry_after
from ahab.notify import validate_environment
from ahab.oracle import Oracle
from ahab.storage import Store


@pytest.mark.parametrize("header", ["inf", "-inf", "nan", "invalid"])
def test_invalid_retry_after(header):
    error = oci.exceptions.ServiceError(429, "TooManyRequests", {"Retry-After": header}, "wait")
    assert retry_after(error, 0) == 0


def test_reconciliation_failure_retries_without_launch(tmp_path):
    api = Mock()
    api.find_existing.side_effect = [oci.exceptions.RequestException("offline"), None]
    api.capacity.return_value = "unavailable"
    clock = [0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    engine = Engine(
        replace(Config(), max_attempts=2),
        api,
        Store(tmp_path),
        sleep=sleep,
        clock=lambda: clock[0],
        jitter=lambda *_: 0,
    )
    assert engine.run({"ads": ["AD1"]}) is None
    assert sleeps == [240]
    api.launch.assert_not_called()
    assert api.find_existing.call_count == 2


def test_deadline_caps_sleep(tmp_path):
    api = Mock()
    api.find_existing.return_value = None
    api.capacity.return_value = "unavailable"
    clock = [0]

    def sleep(seconds):
        clock[0] += seconds

    engine = Engine(
        replace(Config(), deadline_hours=0.01),
        api,
        Store(tmp_path),
        sleep=sleep,
        clock=lambda: clock[0],
        jitter=lambda *_: 0,
    )
    assert engine.run({"ads": ["AD1"]}) is None
    assert clock[0] == 36
    assert api.capacity.call_count == 1
    api.launch.assert_not_called()


def test_stopped_instance_does_not_wait_for_running():
    api = Oracle.__new__(Oracle)
    api.config = {"region": "region1"}
    api.compartment = "compartment1"
    api.compute = Mock()
    api.all = Mock(return_value=[])
    api.get_instance = Mock(side_effect=AssertionError("must not poll a stopped instance"))
    instance = SimpleNamespace(
        id="vm1", display_name="ahab-a1", lifecycle_state="STOPPED", availability_domain="AD1"
    )
    assert api.result(instance, 600)["state"] == "STOPPED"
    api.get_instance.assert_not_called()


@pytest.mark.parametrize(
    "token,chat,url",
    [
        ("token", "", ""),
        ("", "chat", ""),
        ("", "", "https://"),
        ("", "", "http://example.com"),
        ("", "", "https://user:secret@example.com"),
    ],
)
def test_invalid_notification_settings(monkeypatch, token, chat, url):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat)
    monkeypatch.setenv("AHAB_WEBHOOK_URL", url)
    with pytest.raises(ValueError):
        validate_environment()
