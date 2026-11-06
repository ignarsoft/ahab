import os

import pytest

from ahab.notify import load_environment


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "AHAB_WEBHOOK_URL"):
        monkeypatch.delenv(key, raising=False)


def test_literal_settings_and_environment_precedence(tmp_path, monkeypatch):
    path = tmp_path / "notifications.env"
    path.write_text(
        '# Comment\n\nTELEGRAM_BOT_TOKEN="$(touch /tmp/not-executed)"\n'
        "TELEGRAM_CHAT_ID='123'\nAHAB_WEBHOOK_URL=https://example.org/a?b=c\n"
    )
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    load_environment(path)
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "$(touch /tmp/not-executed)"
    assert os.environ["TELEGRAM_CHAT_ID"] == "456"
    assert os.environ["AHAB_WEBHOOK_URL"] == "https://example.org/a?b=c"


@pytest.mark.parametrize(
    "content",
    [
        "export TELEGRAM_CHAT_ID=123",
        "PATH=secret",
        "TELEGRAM_CHAT_ID",
        'TELEGRAM_CHAT_ID="secret',
        "TELEGRAM_CHAT_ID=123\nTELEGRAM_CHAT_ID=secret",
    ],
)
def test_invalid_settings_do_not_leak_values(tmp_path, content):
    path = tmp_path / "notifications.env"
    path.write_text(content)
    with pytest.raises(ValueError, match="line") as error:
        load_environment(path)
    assert "secret" not in str(error.value)
    assert "TELEGRAM_CHAT_ID" not in os.environ
