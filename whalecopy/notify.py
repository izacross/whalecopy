"""Optional alerts to a Slack or Discord incoming webhook."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, webhook_url: str = ""):
        self.url = webhook_url.strip()

    def send(self, text: str) -> None:
        log.info("NOTIFY %s", text)
        if not self.url:
            return
        payload = {"content": text[:1900]} if "discord" in self.url else {"text": text}
        try:
            requests.post(self.url, json=payload, timeout=10)
        except requests.RequestException as e:
            log.warning("webhook failed: %s", e)
