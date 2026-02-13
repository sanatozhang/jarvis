"""
API for reading/writing .env configuration.
Admin-only: validates username before allowing writes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import PROJECT_ROOT
from app.db import database as db

logger = logging.getLogger("jarvis.api.env_settings")
router = APIRouter()

ENV_PATH = PROJECT_ROOT / ".env"

# Fields exposed to the UI (grouped), with display labels
# Sensitive fields show masked values when reading
ENV_FIELDS = {
    "feishu": {
        "label": "飞书 API",
        "fields": {
            "FEISHU_APP_ID": {"label": "App ID", "sensitive": False},
            "FEISHU_APP_SECRET": {"label": "App Secret", "sensitive": True},
        },
    },
    "openai": {
        "label": "OpenAI",
        "fields": {
            "OPENAI_API_KEY": {"label": "API Key", "sensitive": True},
            "OPENAI_SUMMARY_MODEL": {"label": "Summary Model", "sensitive": False},
        },
    },
    "zendesk": {
        "label": "Zendesk",
        "fields": {
            "ZENDESK_SUBDOMAIN": {"label": "Subdomain", "sensitive": False},
            "ZENDESK_EMAIL": {"label": "Email", "sensitive": False},
            "ZENDESK_API_TOKEN": {"label": "API Token", "sensitive": True},
        },
    },
    "code": {
        "label": "代码仓库",
        "fields": {
            "CODE_REPO_PATH": {"label": "本地源码路径", "sensitive": False},
        },
    },
    "api": {
        "label": "Public API",
        "fields": {
            "JARVIS_API_KEY": {"label": "API Key", "sensitive": True},
        },
    },
    "server": {
        "label": "服务器",
        "fields": {
            "LOG_LEVEL": {"label": "日志级别", "sensitive": False},
            "SECRET_KEY": {"label": "Secret Key", "sensitive": True},
        },
    },
}


def _read_env() -> Dict[str, str]:
    """Read .env file into a dict."""
    result = {}
    if not ENV_PATH.exists():
        return result
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _write_env(updates: Dict[str, str]):
    """Update specific keys in .env file, preserving comments and order."""
    if not ENV_PATH.exists():
        return

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    updated_keys = set()
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # Append any new keys not found in existing file
    for key, val in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _mask(value: str) -> str:
    """Mask sensitive values for display."""
    if not value or len(value) <= 8:
        return "••••••••" if value else ""
    return value[:4] + "••••" + value[-4:]


@router.get("")
async def get_env_settings(username: str = Query(...)):
    """Get current .env settings (sensitive values masked)."""
    user = await db.get_user(username)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can view settings")

    env = _read_env()
    groups = []

    for group_key, group_cfg in ENV_FIELDS.items():
        fields = []
        for field_key, field_cfg in group_cfg["fields"].items():
            raw_value = env.get(field_key, "")
            fields.append({
                "key": field_key,
                "label": field_cfg["label"],
                "value": _mask(raw_value) if field_cfg["sensitive"] else raw_value,
                "has_value": bool(raw_value),
                "sensitive": field_cfg["sensitive"],
            })
        groups.append({
            "key": group_key,
            "label": group_cfg["label"],
            "fields": fields,
        })

    return {"groups": groups}


class EnvUpdateRequest(BaseModel):
    updates: Dict[str, str]  # { "OPENAI_API_KEY": "sk-xxx", ... }


@router.put("")
async def update_env_settings(req: EnvUpdateRequest, username: str = Query(...)):
    """Update .env settings. Only non-empty values are written. Requires admin."""
    user = await db.get_user(username)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can update settings")

    # Validate: only allow known fields
    all_keys = set()
    for g in ENV_FIELDS.values():
        all_keys.update(g["fields"].keys())

    filtered = {}
    for k, v in req.updates.items():
        if k not in all_keys:
            continue
        # Skip masked values (user didn't change sensitive field)
        if "••••" in v:
            continue
        filtered[k] = v

    if not filtered:
        return {"status": "no_changes"}

    _write_env(filtered)
    logger.info("Env settings updated by %s: %s", username, list(filtered.keys()))

    return {"status": "updated", "keys": list(filtered.keys()), "note": "部分配置需要重启服务才能生效"}
