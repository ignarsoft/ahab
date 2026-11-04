import json
import logging
import os
from urllib.parse import urlparse
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)


def validate_environment():
    if bool(os.getenv("TELEGRAM_BOT_TOKEN")) != bool(os.getenv("TELEGRAM_CHAT_ID")):
        raise ValueError("Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    webhook = os.getenv("AHAB_WEBHOOK_URL")
    if webhook:
        parsed = urlparse(webhook)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("AHAB_WEBHOOK_URL requires an HTTPS host without embedded credentials")


def send(result):
    validate_environment()
    text = (
        f"Ahab: {result['display_name']} - {result['state']}\n"
        f"{result['instance_id']}\nAD: {result['availability_domain']}\n"
        f"Public IP: {result['public_ip'] or 'unavailable'}\n{result['console_url']}"
    )
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    webhook = os.getenv("AHAB_WEBHOOK_URL")
    targets = []
    if token and chat:
        targets.append(
            (
                f"https://api.telegram.org/bot{token}/sendMessage",
                {"chat_id": chat, "text": text},
                True,
            )
        )
    if webhook:
        targets.append((webhook, {"text": text, "content": text}, False))
    failed = False
    for url, payload, telegram in targets:
        try:
            request = Request(
                url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
            )
            with urlopen(request, timeout=20) as response:
                if telegram and not json.load(response).get("ok"):
                    raise ValueError("Telegram rejected message")
        except Exception:
            # Do not log secret URLs or exception details.
            LOG.warning("Notification failed; the instance will not be launched again")
            failed = True
    if failed:
        raise RuntimeError("Notification pending; execute run to retry")
