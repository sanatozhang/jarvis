"""
Application configuration.

Loads settings from:
1. config.yaml (project-level defaults)
2. .env / environment variables (secrets & overrides)
"""

from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache
from typing import Any, Dict, List, Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # jarvis/
BACKEND_ROOT = Path(__file__).resolve().parent.parent          # jarvis/backend/
RULES_DIR = BACKEND_ROOT / "rules"

_yaml_config: Dict[str, Any] = {}


def _load_yaml() -> Dict[str, Any]:
    global _yaml_config
    if _yaml_config:
        return _yaml_config
    yaml_path = PROJECT_ROOT / "config.yaml"
    if yaml_path.exists():
        with open(yaml_path, "r", encoding="utf-8") as f:
            _yaml_config = yaml.safe_load(f) or {}
    return _yaml_config


# ---------------------------------------------------------------------------
# Pydantic Settings (env vars take precedence)
# ---------------------------------------------------------------------------
class FeishuSettings(BaseSettings):
    app_id: str = ""
    app_secret: str = ""
    app_token: str = "BmjmbSpxxabP2dsuxbtcUTYAn4g"
    table_id: str = "tblWQRIvZq74MhRT"
    view_id: str = "vewu36X0Gx"
    base_url: str = "https://nicebuild.feishu.cn/base/BmjmbSpxxabP2dsuxbtcUTYAn4g"

    model_config = {
        "env_prefix": "FEISHU_",
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class LinearSettings(BaseSettings):
    api_key: str = ""                       # Linear API key
    webhook_secret: str = ""                # Webhook signing secret
    trigger_keyword: str = "@ai-agent"      # Keyword in comment to trigger analysis
    team_id: str = ""                       # Default team ID (optional)

    model_config = {
        "env_prefix": "LINEAR_",
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class AgentProviderConfig(BaseSettings):
    enabled: bool = False
    model: str = ""
    timeout: int = 300
    max_turns: int = 25
    allowed_tools: List[str] = Field(default_factory=list)
    approval_mode: str = "auto-edit"


class AgentSettings(BaseSettings):
    default: str = "claude_code"
    timeout: int = 300
    max_turns: int = 25
    providers: Dict[str, AgentProviderConfig] = Field(default_factory=dict)
    routing: Dict[str, str] = Field(default_factory=dict)


class ConcurrencySettings(BaseSettings):
    max_workers: int = 3
    max_agent_sessions: int = 3
    max_downloads: int = 5
    task_timeout: int = 600


class StorageSettings(BaseSettings):
    workspace_dir: str = "./workspaces"
    data_dir: str = "./data"


class Settings(BaseSettings):
    # --- Env-based settings ---
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "sqlite+aiosqlite:///./data/jarvis.db"
    code_repo_path: str = ""
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
    log_level: str = "info"
    secret_key: str = "change-me"

    # --- Sub-configs (populated from yaml + env) ---
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    linear: LinearSettings = Field(default_factory=LinearSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    concurrency: ConcurrencySettings = Field(default_factory=ConcurrencySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)

    model_config = {
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",  # Ignore unknown env vars
    }


def _merge_yaml_into_settings(settings: Settings) -> Settings:
    """Overlay config.yaml values onto settings (env vars still take precedence)."""
    cfg = _load_yaml()

    # Feishu
    fs = cfg.get("feishu", {})
    for k, v in fs.items():
        if hasattr(settings.feishu, k) and not os.getenv(f"FEISHU_{k.upper()}"):
            setattr(settings.feishu, k, v)

    # Linear
    ls = cfg.get("linear", {})
    for k, v in ls.items():
        if hasattr(settings.linear, k) and not os.getenv(f"LINEAR_{k.upper()}"):
            setattr(settings.linear, k, v)

    # Agent
    ag = cfg.get("agent", {})
    for k in ("default", "timeout", "max_turns"):
        if k in ag:
            setattr(settings.agent, k, ag[k])

    providers_cfg = ag.get("providers", {})
    for name, pcfg in providers_cfg.items():
        settings.agent.providers[name] = AgentProviderConfig(**pcfg)

    routing_cfg = ag.get("routing", {})
    settings.agent.routing = routing_cfg

    # Concurrency
    cc = cfg.get("concurrency", {})
    for k, v in cc.items():
        if hasattr(settings.concurrency, k):
            setattr(settings.concurrency, k, v)

    # Storage
    sc = cfg.get("storage", {})
    for k, v in sc.items():
        if hasattr(settings.storage, k):
            setattr(settings.storage, k, v)

    return settings


@lru_cache()
def get_settings() -> Settings:
    settings = Settings()
    settings = _merge_yaml_into_settings(settings)

    # Resolve relative paths
    ws = Path(settings.storage.workspace_dir)
    if not ws.is_absolute():
        settings.storage.workspace_dir = str(PROJECT_ROOT / ws)
    dd = Path(settings.storage.data_dir)
    if not dd.is_absolute():
        settings.storage.data_dir = str(PROJECT_ROOT / dd)

    # Ensure directories exist
    Path(settings.storage.workspace_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.storage.data_dir).mkdir(parents=True, exist_ok=True)

    return settings
