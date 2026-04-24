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
    # Separate IM app for group chat / messaging (can be same or different app)
    im_app_id: str = ""
    im_app_secret: str = ""

    model_config = {
        "env_prefix": "FEISHU_",
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def im_credentials(self) -> tuple:
        """Return (app_id, app_secret) for IM operations. Falls back to main app."""
        aid = self.im_app_id or self.app_id
        asecret = self.im_app_secret or self.app_secret
        return aid, asecret


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
    effort: str = ""               # "low", "medium", "high", "max" (empty = CLI default)
    fallback_model: str = ""       # auto-fallback when primary model is overloaded
    betas: List[str] = Field(default_factory=list)  # beta headers for API requests
    timeout: int = 600
    max_turns: int = 25
    allowed_tools: List[str] = Field(default_factory=list)
    approval_mode: str = "auto-edit"


class AgentSettings(BaseSettings):
    default: str = "claude_code"
    timeout: int = 600
    max_turns: int = 25
    providers: Dict[str, AgentProviderConfig] = Field(default_factory=dict)
    routing: Dict[str, str] = Field(default_factory=dict)


class ContextCondensationSettings(BaseSettings):
    """L1.5: LLM-powered context extraction from large logs."""
    enabled: bool = False                    # Enable via config.yaml or env
    provider: str = "gemini"                 # gemini, anthropic, openai
    model: str = ""                          # empty = use default per provider
    api_key: str = ""                        # API key (prefer env: CONDENSER_API_KEY)
    api_base_url: str = ""                   # Custom endpoint (optional)
    log_size_threshold_mb: float = 5.0       # Only condense logs > this size
    time_window_hours_before: int = 4        # Hours before problem_date
    time_window_hours_after: int = 2         # Hours after problem_date
    max_input_chars: int = 2_800_000         # ~800K tokens for Gemini Flash
    timeout: int = 120                       # LLM call timeout (seconds)
    temperature: float = 0.0                 # Deterministic extraction

    model_config = {
        "env_prefix": "CONDENSER_",
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


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
    database_url: str = "sqlite+aiosqlite:///./data/appllo.db"
    code_repo_path: str = ""              # Legacy: single repo (treated as app)
    code_repo_app: str = ""               # APP (Flutter) source code path
    code_repo_web: str = ""               # Web frontend source code path
    code_repo_desktop: str = ""           # Desktop (Electron) source code path
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
    log_level: str = "info"
    secret_key: str = "change-me"

    # --- Sub-configs (populated from yaml + env) ---
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    linear: LinearSettings = Field(default_factory=LinearSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    context_condensation: ContextCondensationSettings = Field(default_factory=ContextCondensationSettings)
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

    # AGENT_DEFAULT env var overrides config.yaml default and all routing
    _valid_agents = {"codex", "claude_code"}
    env_agent = os.getenv("AGENT_DEFAULT", "").strip()
    if env_agent in _valid_agents:
        settings.agent.default = env_agent
        settings.agent.routing = {k: env_agent for k in settings.agent.routing}

    # Context condensation (L1.5)
    ccc = cfg.get("context_condensation", {})
    for k, v in ccc.items():
        if hasattr(settings.context_condensation, k) and not os.getenv(f"CONDENSER_{k.upper()}"):
            setattr(settings.context_condensation, k, v)

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

    # Backfill: if legacy code_repo_path is set but code_repo_app is not, use it as app
    if settings.code_repo_path and not settings.code_repo_app:
        settings.code_repo_app = settings.code_repo_path

    return settings


def get_code_repo_for_platform(platform: str) -> Optional[str]:
    """Return the source code repo path for a given platform (app/web/desktop)."""
    s = get_settings()
    mapping = {
        "app": s.code_repo_app,
        "web": s.code_repo_web,
        "desktop": s.code_repo_desktop,
    }
    path = mapping.get(platform.lower(), "") if platform else ""
    # Fallback: if platform unknown or not configured, use app repo
    if not path:
        path = s.code_repo_app or s.code_repo_path
    return path if path else None


def get_all_code_repos() -> dict[str, str]:
    """Return all configured code repo paths (non-empty only)."""
    s = get_settings()
    repos = {}
    if s.code_repo_app:
        repos["app"] = s.code_repo_app
    if s.code_repo_web:
        repos["web"] = s.code_repo_web
    if s.code_repo_desktop:
        repos["desktop"] = s.code_repo_desktop
    return repos
