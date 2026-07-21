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


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并两个 dict：override 的标量值覆盖 base；嵌套 dict 递归合并（而非整段替换）。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml() -> Dict[str, Any]:
    """加载 config.yaml（git 版本控制的默认值/模板）叠加 config.local.yaml（每台服务器
    各自的运行时覆盖，不进 git，见根目录 config.local.yaml.example）。

    背景：`/settings` 页面里"无需重启即可持久化"的开关（如 crashguard.qa_capture_enabled）
    以前直接写回 config.yaml——但 config.yaml 是 git 追踪文件，docker 部署时又把它挂载成
    只读（避免容器写入把 git 工作区弄脏），导致写入静默失败（try/except 吞掉了
    OSError: Read-only file system），设置页显示"已保存"但重启/重新部署后又变回默认值
    （2026-07-21 生产环境实测发现）。config.local.yaml 专门承接这类运行时覆盖，各服务器
    独立、部署时 `git pull` 不会碰它。
    """
    global _yaml_config
    if _yaml_config:
        return _yaml_config
    merged: Dict[str, Any] = {}
    yaml_path = PROJECT_ROOT / "config.yaml"
    if yaml_path.exists():
        with open(yaml_path, "r", encoding="utf-8") as f:
            merged = yaml.safe_load(f) or {}
    local_path = PROJECT_ROOT / "config.local.yaml"
    # is_file()（不只 exists()）+ try/except：bind mount 源路径若在宿主机意外建成目录
    # （2026-07-21 生产环境踩过——docker 单文件 bind mount 在源路径不存在时的自动创建
    # 行为不总是建普通文件），open() 会抛 IsADirectoryError，绝不能让这个基础设施层面
    # 的意外崩掉整个 app 启动；读取失败就跳过覆盖，退化成只用 config.yaml 默认值。
    if local_path.is_file():
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                local_overrides = yaml.safe_load(f) or {}
            merged = _deep_merge(merged, local_overrides)
        except Exception as exc:
            import logging
            logging.getLogger("jarvis.config").warning(
                "failed to load config.local.yaml overrides, falling back to config.yaml defaults: %s", exc,
            )
    _yaml_config = merged
    return _yaml_config


def write_local_override(section: str, updates: Dict[str, Any]) -> None:
    """把 {section: {...updates}} 合并写入 config.local.yaml（每台服务器独立、不进 git）。

    读-合并-写，保留该文件里已有的其它覆盖项 / 其它 section（不是整份重写覆盖）。
    写完立即失效 `_load_yaml()` 的模块级缓存，同进程内后续读取也能拿到最新值
    （调用方通常还会直接改运行中 Settings 单例的属性，这里的缓存失效是为了让
    `_load_yaml()` 本身保持一致，防止后续某处重新触发加载时读到写入前的旧缓存）。
    """
    global _yaml_config
    local_path = PROJECT_ROOT / "config.local.yaml"
    existing: Dict[str, Any] = {}
    if local_path.is_file():
        with open(local_path, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    section_dict = existing.get(section)
    if not isinstance(section_dict, dict):
        section_dict = {}
    section_dict.update(updates)
    existing[section] = section_dict
    with open(local_path, "w", encoding="utf-8") as f:
        yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)
    _yaml_config = {}


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

    enabled: bool = Field(default=True, alias="ENABLE_SSO")
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
    # 长日志档超时放宽到 task_timeout_large。应对 fb_26d82348bf / fb_b47f129711 类 case：
    # 50万+ 行 plaud.log 在 600s 内确定性超时 → 重试也超时。
    #
    # ⚠️ task_large_log_bytes 衡量的是 **解密前的原始 .plaud 文件**（_resolve_task_timeout 在
    # 解密之前就要决定 pipeline 超时，看不到解密后的大小）。原始 .plaud 解密后会膨胀约 6x
    # （实测 fb_b47f129711：23MB 原始 → 146MB / 533k 行解密）。所以阈值按原始大小设：
    # 8MB 原始 ≈ 解密后 ~50MB / ~150k+ 行，已属重负载档。旧值 30MB 原始 → 解密后近 200MB
    # 才触发，导致 23MB 原始这种重 case 溜过、确定性超时。
    task_timeout_large: int = 1200
    task_large_log_bytes: int = 8 * 1024 * 1024  # 8MB raw .plaud ≈ ~50MB decrypted
    # RC1 兜底：agent 自身超时 = pipeline 超时 − salvage_margin，保证 agent 先于 pipeline
    # 硬墙触发自己的 TimeoutError，走 salvage 路径捞回部分 result.json（而非被外层 cancel 丢弃）。
    salvage_margin: int = 60
    # ① 日志时效性预检：日志最新事件比问题发生时间早超过 N 天 → 判定"日志未覆盖问题时段"，
    # 直接出"需用户重传"结果、不再硬跑 agent（避免拿设备激活日的旧日志瞎猜根因，污染 inaccurate 桶）。
    # 阈值取保守值——正常日志离问题就几天，4 个月前激活日的旧日志才是要拦的（fb_f86c656539 类）。
    log_stale_gap_days: int = 30


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
    repo_routing: dict = {}              # repo_router bands（yaml repo_routing 段）
    support_web: bool = False            # 平台开关：是否支持 web 工单（默认关；submit 页据此 gating）
    support_desktop: bool = False        # 平台开关：是否支持 desktop 工单（默认关）
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
    log_level: str = "info"
    secret_key: str = "change-me"
    frontend_base_url: str = ""    # Jarvis 前端 URL，用于告警深链（env: APPLLO_BASE_URL）
    feedback_recipient: str = "sanato.zhang@plaud.ai"   # 反馈 widget 收件人（飞书邮箱）

    # --- Sub-configs (populated from yaml + env) ---
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    linear: LinearSettings = Field(default_factory=LinearSettings)
    sso: SSOSettings = Field(default_factory=SSOSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    context_condensation: ContextCondensationSettings = Field(default_factory=ContextCondensationSettings)
    concurrency: ConcurrencySettings = Field(default_factory=ConcurrencySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    jenkins: JenkinsSettings = Field(default_factory=JenkinsSettings)

    # 模型定价（每 Mtok USD），仅用于 API 路径成本估算（condenser haiku / claude_api agent）。
    # claude_code CLI 直接用 --output-format json 的 total_cost_usd，不查此表。
    # 来源：claude-api 定价（2026-06）；cache_read≈0.1×input，cache_write(5m)≈1.25×input。
    # 可在 config.yaml `pricing:` 段覆盖；定价变动时人工维护。
    pricing: Dict[str, Dict[str, float]] = Field(default_factory=lambda: {
        "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
        "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
        # 2026-07-03: intro 价 $2/$10（至 2026-08-31），到期后改回标准价 $3/$15、cache_read 0.3、cache_write 3.75
        "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5},
        "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    })

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

    # pricing：config.yaml `pricing:` 段按 model 合并覆盖默认表（缺省项保留默认）
    pr = cfg.get("pricing", {})
    if isinstance(pr, dict):
        for model_name, rates in pr.items():
            if isinstance(rates, dict):
                settings.pricing[model_name] = {**settings.pricing.get(model_name, {}), **rates}

    # repo_routing (repo_router bands)
    rr = cfg.get("repo_routing", {})
    if isinstance(rr, dict) and rr:
        settings.repo_routing = rr

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


def get_repo_routing() -> dict:
    """返回 repo_router 用的 routing dict。
    优先 yaml `repo_routing`；其缺失的平台用旧 env (code_repo_app/web/desktop)
    backfill 出一个 flutter-family band（min_version "0"），保证现有部署不炸。"""
    s = get_settings()
    routing = dict(s.repo_routing or {})

    def _flutter_band(wrapper: str, sub: str, gh: str, prof: str) -> dict:
        return {"min_version": "0", "family": "flutter", "wrapper": wrapper,
                "sub": sub, "github_repo": gh, "symbol_profile": prof}

    app_repo = s.code_repo_app or s.code_repo_path
    if "android" not in routing and app_repo:
        routing["android"] = {"bands": [_flutter_band(app_repo, "plaud-android", "Plaud-AI/Plaud-App", "flutter_android")]}
    if "ios" not in routing and app_repo:
        routing["ios"] = {"bands": [_flutter_band(app_repo, "plaud-ios", "Plaud-AI/Plaud-App", "flutter_ios")]}
    if "web" not in routing and s.code_repo_web:
        routing["web"] = {"bands": [{"min_version": "0", "family": "web", "wrapper": s.code_repo_web, "sub": "", "github_repo": "Plaud-AI/plaud-web", "symbol_profile": "none"}]}
    if "desktop" not in routing and s.code_repo_desktop:
        routing["desktop"] = {"bands": [{"min_version": "0", "family": "desktop", "wrapper": s.code_repo_desktop, "sub": "", "github_repo": "Plaud-AI/fe-nexus", "symbol_profile": "none"}]}
    return routing


def get_code_repo_for_platform(platform: str, version: Optional[str] = None,
                               os_name: str = "") -> Optional[str]:
    """[DEPRECATED] 旧接口降级：调 repo_router。无 version → 取最新 band 回落。
    新代码请直接用 app.services.repo_router.resolve。"""
    from app.services import repo_router
    res = repo_router.resolve(platform, version, get_repo_routing(), os_name=os_name)
    if res:
        return res.sub_repo_path
    # 兜底：旧静态映射（platform 无法归一 / 未配置时）
    s = get_settings()
    return (s.code_repo_app or s.code_repo_path) or None


def get_all_code_repos() -> dict[str, str]:
    """所有需要定时更新的 distinct wrapper（含 flutter/native/web/desktop）。"""
    routing = get_repo_routing()
    repos: dict[str, str] = {}
    for platform, cfg in routing.items():
        for band in cfg.get("bands", []):
            w = os.path.expanduser(band.get("wrapper", "") or "")
            if w:
                repos[w] = w   # key=去重后的 wrapper 路径
    return repos
