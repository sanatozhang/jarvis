"""Notifier port on Feishu direct messages. Never raises: a lost notification must not fail a release."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger("jarvis.modulehub")


class FeishuNotifier:
    def __init__(self, emails: List[str], send: Optional[Callable[..., Awaitable[bool]]] = None):
        self._emails = emails
        self._send = send

    async def notify(self, text: str) -> None:
        send = self._send
        if send is None:
            from app.services.feishu_cli import send_message as send  # late import: only when actually sending
        for email in self._emails:
            try:
                await send(email=email, text=text)
            except Exception as e:
                logger.warning("modulehub notify to %s failed: %s", email, e)
