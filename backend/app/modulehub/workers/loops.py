"""Periodic loops: build poller and release-branch reconciliation."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("jarvis.modulehub")


async def run_forever(step: Callable[[], Awaitable[object]], interval_seconds: float, *,
                      sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, name: str = "modulehub") -> None:
    """Call `step` forever; a failing step is logged and retried on the next tick."""
    while True:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s step failed", name)
        await sleep(interval_seconds)
