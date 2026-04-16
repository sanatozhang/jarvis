"""
Feishu (Lark) Open API client.

DEPRECATED: This module is replaced by feishu_cli.py (lark-cli based).
Kept as compatibility shim — all names re-exported from feishu_cli.
"""
from __future__ import annotations

# Re-export CLI-based client as FeishuClient for backward compatibility
from app.services.feishu_cli import FeishuCLI as FeishuClient  # noqa: F401

__all__ = ["FeishuClient"]
