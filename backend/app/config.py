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


class SSOSettings(BaseSettings):
    """Feishu OAuth SSO settings."""

    enabled: bool = Field(default=False, alias="ENABLE_SSO")
    feishu_app_id: str = Field(default="", alias="SSO_FEISHU_APP_ID")
    feishu_app_secret: str = Field(default="", alias="SSO_FEISHU_APP_SECRET")
    feishu_redirect_uri: str = Field(
        default="https://apollo.nicebuild.click/api/auth/feishu/callback",
        alias="SSO_FEISHU_REDIRECT_URI",
    )
    jwt_secret: str = Field(default="", alias="SSO_JWT_SECRET")
    cookie_name: str = Field(default="jarvis_session", alias="SSO_COOKIE_NAME")
    cookie_days: int = Field(default=365, alias="SSO_COOKIE_DAYS")
    cookie_secure: bool = Field(default=True, alias="SSO_COOKIE_SECURE")
    cookie_domain: str = Field(default="", alias="SSO_COOKIE_DOMAIN")

    allowed_domains_raw: str = Field(default="plaud.ai", alias="SSO_ALLOWED_DOMAINS")
    admin_emails_raw: str = Field(default="", alias="ADMIN_EMAILS")
    exempt_paths_raw: str = Field(
        default="/api/health,/api/linear/webhook,/api/v1/,/api/auth/",
        alias="SSO_EXEMPT_PATHS",
    )

    model_config = {
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "populate_by_name": True,
    }

    @property
    def allowed_domains(self) -> List[str]:
        return [d.strip() for d in self.allowed_domains_raw.split(",") if d.strip()]

    @property
    def admin_emails(self) -> List[str]:
        return [e.strip().lower() for e in self.admin_emails_raw.split(",") if e.strip()]

    @property
    def exempt_paths(self) -> List[str]:
        return [p.strip() for p in self.exempt_paths_raw.split(",") if p.strip()]


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
    # ── claude_api specific (ignored by CLI providers) ──
    base_url: str = ""             # Vertex proxy URL, e.g. http://34.216.169.232:30001/vertex
    per_turn_timeout: int = 120    # seconds per messages.create call
    max_tokens: int = 8192         # max output tokens per turn
    enable_cache: bool = True      # apply cache_control on system prompt


class AgentSettings(BaseSettings):
    default: str = "claude_code"
    call_mode: str = "cli"         # "cli" | "api" — kept for backward compat; use api_traffic_ratio instead
    api_traffic_ratio: float = 0.0  # 0.0=100% CLI, 0.2=20% API, 1.0=100% API
    timeout: int = 600
    max_turns: int = 25
    # 方案 C：prompt chars 超过此阈值 → 强制走 claude_code (CLI 1M context)。
    # 默认 500K bytes ≈ 140K tokens；预留 ~60K 给 system prompt + tool 中间产物 + 8K output。
    # API claude-sonnet-4-6 context 仅 200K tokens；超大日志工单走 API 装不下 → 直走 CLI 1M 兜底。
    # 0 = 关闭此路由（不推荐；除非 L1.5 condensation 已稳定能保证所有 prompt < 200K）。
    cli_route_above_chars: int = 500_000
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


class JenkinsServerConfig(BaseSettings):
    """One Jenkins endpoint. Each has its own independent account."""
    url: str = ""                # "http://10.0.52.101:8080"
    user: str = ""               # e.g. "jarvis-bot"
    token_env: str = ""          # name of env var that holds the API token
    api_token: str = ""          # resolved at load time from os.environ[token_env]


class JenkinsSettings(BaseSettings):
    """Jenkins release-build automation."""

    enabled: bool = False
    servers: List[JenkinsServerConfig] = Field(default_factory=list)
    job_cn: str = "plaud-app-publish-cn"
    job_global: str = "plaud-app-publish-global"
    poll_interval_seconds: int = 30
    build_timeout_seconds: int = 3600
    mt_bin: str = "mt"                                 # override if installed under custom path
    common_subdir: str = "plaud-flutter-common"        # sub-repo holding pubspec.yaml
    # 不参与 release 分支的子仓（mt 工具自身 / 脚本仓等）—— `mt checkout -b` /
    # `mt push` / 审计快照都跳过这些。
    exclude_subrepos: List[str] = Field(default_factory=lambda: ["mt", "plaud-app-scripts"])
    notify_emails: List[str] = Field(default_factory=list)  # 飞书额外抄送（除创建者外）

    model_config = {
        "env_prefix": "JENKINS_",
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


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
    frontend_base_url: str = ""    # Jarvis 前端 URL，用于告警深链（env: APPLLO_BASE_URL）

    # --- Sub-configs (populated from yaml + env) ---
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    linear: LinearSettings = Field(default_factory=LinearSettings)
    sso: SSOSettings = Field(default_factory=SSOSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    context_condensation: ContextCondensationSettings = Field(default_factory=ContextCondensationSettings)
    concurrency: ConcurrencySettings = Field(default_factory=ConcurrencySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    jenkins: JenkinsSettings = Field(default_factory=JenkinsSettings)

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
    for k in ("default", "call_mode", "timeout", "max_turns", "api_traffic_ratio", "cli_route_above_chars"):
        if k in ag:
            setattr(settings.agent, k, ag[k])

    providers_cfg = ag.get("providers", {})
    for name, pcfg in providers_cfg.items():
        settings.agent.providers[name] = AgentProviderConfig(**pcfg)

    routing_cfg = ag.get("routing", {})
    settings.agent.routing = routing_cfg

    # AGENT_DEFAULT env var overrides config.yaml default and all routing
    _valid_agents = {"codex", "claude_code", "claude_api"}
    env_agent = os.getenv("AGENT_DEFAULT", "").strip()
    if env_agent in _valid_agents:
        settings.agent.default = env_agent
        settings.agent.routing = {k: env_agent for k in settings.agent.routing}

    # AGENT_CALL_MODE env var overrides config.yaml call_mode
    env_call_mode = os.getenv("AGENT_CALL_MODE", "").strip().lower()
    if env_call_mode in ("api", "cli"):
        settings.agent.call_mode = env_call_mode

    # Context condensation (L1.5)
    ccc = cfg.get("context_condensation", {})
    for k, v in ccc.items():
        if hasattr(settings.context_condensation, k) and not os.getenv(f"CONDENSER_{k.upper()}"):
            setattr(settings.context_condensation, k, v)

    # L1.5 api_key: 没显式设 → 从 ANTHROPIC_API_KEY 取（公司环境通常是 vertex proxy key）。
    if (
        settings.context_condensation.provider == "anthropic"
        and not settings.context_condensation.api_key
    ):
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        if anthropic_key:
            settings.context_condensation.api_key = anthropic_key

    # L1.5 Vertex proxy 继承：与 claude_api 走同一条 vertex 代理（顶层设计统一）。
    # 治本（2026-05-20 实测）：Vertex 代理已支持 claude-haiku-4-5（rawPredict 200 OK）。
    # 旧逻辑"有直连 api_key 就走 anthropic.com"会导致 vertex key + 直连 URL → 401 必败，
    # 因为公司环境的 ANTHROPIC_API_KEY 几乎都是 vertex 代理 key（sk-Mobile_...），不是
    # Anthropic 直连真 key。删除 `not api_key` 拦截 → 默认走 vertex 拉通整个 LLM 栈。
    # 用户显式设 CONDENSER_API_BASE_URL（含 env 或 yaml）才会覆盖此默认。
    if (
        settings.context_condensation.provider == "anthropic"
        and not settings.context_condensation.api_base_url
        and not os.getenv("CONDENSER_API_BASE_URL")
    ):
        claude_api_provider = settings.agent.providers.get("claude_api")
        if claude_api_provider and claude_api_provider.base_url:
            settings.context_condensation.api_base_url = (
                claude_api_provider.base_url.rstrip("/") + "/v1/messages"
            )

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

    # Jenkins (release automation). Server list is rendered into
    # JenkinsServerConfig objects; per-server API tokens are pulled from
    # env vars whose names are spelled in `token_env`.
    jk = cfg.get("jenkins", {})
    for k, v in jk.items():
        if k == "servers":
            servers: List[JenkinsServerConfig] = []
            for srv in v or []:
                if isinstance(srv, str):
                    # Legacy bare-URL form — kept so we don't break old configs.
                    servers.append(JenkinsServerConfig(url=srv))
                    continue
                cfg_obj = JenkinsServerConfig(
                    url=srv.get("url", ""),
                    user=srv.get("user", ""),
                    token_env=srv.get("token_env", ""),
                )
                if cfg_obj.token_env:
                    cfg_obj.api_token = os.getenv(cfg_obj.token_env, "")
                servers.append(cfg_obj)
            settings.jenkins.servers = servers
            continue
        if hasattr(settings.jenkins, k) and not os.getenv(f"JENKINS_{k.upper()}"):
            setattr(settings.jenkins, k, v)

    # frontend_base_url: yaml 优先，其次 env APPLLO_BASE_URL，再次 CRASHGUARD_FRONTEND_BASE_URL
    if not settings.frontend_base_url:
        settings.frontend_base_url = (
            cfg.get("frontend_base_url", "")
            or os.getenv("APPLLO_BASE_URL", "")
            or os.getenv("CRASHGUARD_FRONTEND_BASE_URL", "")
        ).rstrip("/")

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
