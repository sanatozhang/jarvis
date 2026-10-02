"""Notifier port on direct messages (飞书/Slack). Never raises: a lost notification must not fail a release.

The channel is not modulehub's call: it follows the jarvis-wide "system" notify switch
(`Settings.system_notify_provider`, settings page), via `app.services.system_notify`.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger("jarvis.modulehub")


class DmNotifier:
    def __init__(self, emails: List[str], send: Optional[Callable[..., Awaitable[bool]]] = None):
        self._emails = emails
        self._send = send

    async def notify(self, text: str) -> None:
        send = self._send
        if send is None:
            from app.services.system_notify import send_text as send  # late import: only when actually sending
        for email in self._emails:
            try:
                if not await send(email=email, text=text):
                    logger.warning("modulehub notify to %s was not delivered", email)
            except Exception as e:
                logger.warning("modulehub notify to %s failed: %s", email, e)
