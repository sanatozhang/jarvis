"""
Notification service — sends alerts to oncall engineers.

DEPRECATED: Feishu notifications now use feishu_cli.py.
This module re-exports from feishu_cli for backward compatibility.
"""
from __future__ import annotations

from app.services.feishu_cli import (  # noqa: F401
    create_escalation_group,
    notify_oncall,
    send_message,
)

__all__ = ["create_escalation_group", "notify_oncall", "send_message"]
