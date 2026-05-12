"""
Crashguard 模块配置 — 独立配置段，与 jarvis 全局配置解耦。

加载顺序: env (CRASHGUARD_*) > config.yaml crashguard 段 > 默认值
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List, Tuple, Type

from pydantic import Field
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from app.config import PROJECT_ROOT, _load_yaml


def _autodetect_frontend_base_url() -> str:
    """探测本机出口 IP，构造默认 frontend URL。

    底层逻辑：多机部署（10.0.52.100 / 102 / ...）若不显式配置 frontend_base_url，
    飞书消息里的链接会回环到 localhost——其他人点开打不到当前部署的页面。
    用 UDP socket connect 8.8.8.8（不真发包，仅触发路由表查询）拿到本机出口 IP。

    ⚠️ Docker 容器里 socket 拿到的是 bridge 网段（172.x.x.x），对外不可达。
    Docker 部署必须显式设 env：`CRASHGUARD_FRONTEND_BASE_URL=http://10.0.52.x:3000`，
    本函数仅作 native dev / 单机部署的便利默认。
    """
    import os
    import socket
    # 优先读 env：HOST_IP / DEPLOY_HOST （docker-compose 可注入宿主 IP）
    for key in ("CRASHGUARD_HOST_IP", "HOST_IP", "DEPLOY_HOST"):
        v = (os.environ.get(key) or "").strip()
        if v:
            if v.startswith("http://") or v.startswith("https://"):
                return v
            return f"http://{v}:3000"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip != "127.0.0.1":
            return f"http://{ip}:3000"
    except Exception:
        pass
    return "http://localhost:3000"


class _YamlSource(PydanticBaseSettingsSource):
    """从 config.yaml crashguard 段读取的低优先级 source"""

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> Tuple[Any, str, bool]:
        # 不实现单字段读取（用 __call__ 批量返回）
        return None, field_name, False

    def __call__(self) -> Dict[str, Any]:
        return _yaml_overrides()


class CrashguardSettings(BaseSettings):
    # Kill switches
    enabled: bool = True
    pr_enabled: bool = True
    feishu_enabled: bool = True
    # 多实例部署时，仅一台机器开启 scheduler，避免双发（兜底；DB 锁是主要去重）
    scheduler_enabled: bool = True

    # Datadog
    datadog_api_key: str = ""
    datadog_app_key: str = ""
    datadog_site: str = "datadoghq.com"
    datadog_window_hours: int = 24
    # 哪个 track 含有崩溃数据：rum / logs / trace。Plaud 移动端崩溃在 RUM。
    # 多 track 用逗号分隔（如 "rum,logs"），空 = 单 track。
    datadog_tracks: str = "rum"
    # 搜索 query（event search 语法）。
    # ⚠️ 双路口径（C 路线，对齐 Datadog UI "Crashes" 与 "Errors" 两个独立看板）：
    #   - fatal  → 真崩溃 + ANR + App Hang（App 死/卡）
    #   - non_fatal → 业务侧捕获异常（runZonedGuarded / addError，App 没挂但流程中断）
    # 旧字段 datadog_query 保留兼容（单路全量），新代码请用 fatal/non_fatal。
    datadog_query: str = "*"
    datadog_query_fatal: str = "@error.is_crash:true OR @error.category:ANR OR @error.category:\"App Hang\""
    datadog_query_nonfatal: str = "@type:error -@error.is_crash:true -@error.category:ANR -@error.category:\"App Hang\""

    # Schedule
    morning_cron: str = "0 7 * * *"
    evening_cron: str = "0 17 * * *"
    # 晚报数据窗口（小时）。早报固定用 datadog_window_hours=24h，晚报用此值。
    # 默认 10h = 早报到晚报之间的工作日内增量；基线 = SHoW 上周同 weekday 同 10h 段。
    # 设计意图：早报=昨日 24h 总览，晚报=日内增量信号，两份卡片**不再冗余**。
    evening_window_hours: int = 10

    # Top N + thresholds
    max_top_n: int = 20
    # 批量自动 AI 分析的 Top N 上限
    analyze_top_n: int = 20
    surge_multiplier: float = 1.5
    surge_min_events: int = 10
    regression_silent_versions: int = 3
    feasibility_pr_threshold: float = 0.7
    # 早晚报关注点阈值（vs 昨日变化率）
    daily_surge_threshold: float = 0.10   # +10%
    daily_drop_threshold: float = -0.10   # -10%
    # 噪声治理：events 量级下限。低于此值的 surge / drop 不进 attention，
    # 但「新增 issue」(is_new_in_version) 不受此限制（新代码崩溃永远是信号）。
    daily_attention_min_events: int = 100

    # Feishu
    feishu_target_chat_id: str = ""
    # 测试阶段可改用点对点推送给指定邮箱（优先级高于 chat_id）
    feishu_target_email: str = ""
    feishu_admin_open_ids: List[str] = Field(default_factory=list)
    # 飞书消息中链接前缀（指向 frontend）
    # 优先级：env CRASHGUARD_FRONTEND_BASE_URL > yaml.frontend_base_url > env HOST_IP 派生
    #        > 本机出口 IP 自动探测 > http://localhost:3000
    # 多机部署/Docker：建议显式 env 设值，避免容器内拿 bridge IP
    frontend_base_url: str = Field(default_factory=_autodetect_frontend_base_url)

    # 半自动 PR 仓库映射（按平台覆盖，未设回落 jarvis code_repo_app）
    repo_path_flutter: str = ""
    repo_path_android: str = ""
    repo_path_ios: str = ""
    # PR 去重窗口（同一 issue+platform 30 天内只允许一个 draft PR）
    pr_dedup_days: int = 30
    # PR 状态同步 cron（拉 GitHub 现态回填 DB）；默认每 30 分钟
    # 关闭/合并后 30min 内同步到 jarvis，DRAFT → CLOSED 不会残留
    pr_sync_cron: str = "*/30 * * * *"
    # 启动后延迟一次性跑 pipeline + auto-analyze（避免重启等到 07:00 才开始）
    warmup_on_startup: bool = True
    # 周期 pipeline cron（与早晚报解耦）；默认每 4 小时整点
    pipeline_cron: str = "0 */4 * * *"

    # 「线上最新版本」手动覆盖（按平台），留空则按崩溃数据自动派生
    current_release_flutter: str = ""
    current_release_android: str = ""
    current_release_ios: str = ""
    # 数据派生阈值：某版本累计 events 不足该值则不视作"线上版本"（过滤灰度/测试包）
    latest_version_min_events: int = 300
    # AI 分析去重窗口（小时）：自动触发场景下，若 issue 在该窗口内已有 success 分析，
    # 直接复用——避免 warmup/cron/batch 多入口重复烧 token。UI 重新分析按钮始终强制重跑。
    analysis_dedup_hours: int = 6
    # AI 分析定时小步分批：避免一次跑 20 个被杀。每 N 分钟 tick 一次，每次最多 K 个。
    # 默认每 5 分钟 1 个 → 20 个 issue 约 100 分钟跑完；崩溃只损失当前 1 个，下 tick 自动续跑。
    analyze_cron: str = "*/5 * * * *"
    analyze_max_per_tick: int = 1

    # === 3h 告警（SHoW-3h 同周同 3 小时块对比）===
    # 每 3 小时拉 Datadog，对比上周同 weekday 同 3h 块 events，超过阈值或新增 issue 发飞书告警。
    # 3h 块对齐到 UTC 00/03/06/09/12/15/18/21；小时颗粒度噪声大，工作日/周末活跃差异大时
    # 3 小时块是 P&L 平衡点。早晚报和此告警都不含 PR 修复内容。
    hourly_alert_enabled: bool = True
    # cron 每 3 小时块的第 5 分钟触发：Datadog ingest 延迟 3-5 分钟，避开数据未到位
    hourly_alert_cron: str = "5 */3 * * *"
    # 上涨阈值（百分比，默认 10%）
    hourly_alert_growth_threshold_pct: float = 10.0
    # 「新增」窗口：最近 N 天首次出现的 issue 视为新增
    hourly_alert_new_window_days: int = 30
    # SHoW 基线最小 events（< 此值不参与百分比计算，防小基数噪声）
    hourly_alert_min_baseline_events: int = 20
    # 卡片最多展示 issue 数（聚合 digest）
    hourly_alert_max_items: int = 10
    # 绝对量级阈值：单 issue 在窗口内 sessions_affected < 此值不入告警（脏数据/极低频噪声过滤）
    # 注：Plaud RUM 未 setUser，users_affected 全 0（已知 data hole），用 sessions 代理 user
    # （24h 内典型 1-3 sessions/user，相关性高）。卡片文案显示「受影响会话 ≥ N」。
    hourly_alert_min_sessions: int = 60

    # === 核心指标报警（10 分钟粒度 crash-free sessions % 监控）===
    # 底层逻辑：早晚报是 24h 大盘，hourly_alert 是单 issue 突增/新增；核心指标补的是
    # "整体健康度"颗粒度——即使没有单 issue 飙升，整体 crash-free 跌穿基线也要报警。
    # 用 Datadog Mobile RUM 原生口径：(1 - crashed_sessions/total_sessions) * 100。
    # 对比基线：当前 10min 窗口 vs 前 1h 平均 crash_free_pct。
    core_metric_enabled: bool = True
    core_metric_cron: str = "*/10 * * * *"
    # 报警触发阈值：crash_free_pct 相对前 1h 变化绝对值 >= N pp（percentage points）
    # 例：基线 99.5%，当前 99.0% → 变化 0.5 pp，>=0.3 触发
    core_metric_change_threshold_pp: float = 0.3
    # 绝对量级阈值：当前 10min 窗口 total_sessions < N 不告警（小流量噪声）
    core_metric_min_sessions: int = 100
    # 监控平台白名单（小写逗号串），空 = 不限制
    core_metric_platforms: str = "android,ios"

    model_config = {
        "env_prefix": "CRASHGUARD_",
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        # 优先级（左 > 右）: init_kwargs > env > dotenv > yaml > defaults
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls),
            file_secret_settings,
        )


def _yaml_overrides() -> Dict[str, Any]:
    """从 config.yaml crashguard 段读取覆盖项"""
    cfg = _load_yaml().get("crashguard") or {}
    flat: Dict[str, Any] = {}
    for k in (
        "enabled", "pr_enabled", "feishu_enabled", "scheduler_enabled",
        "max_top_n", "analyze_top_n",
    ):
        if k in cfg:
            flat[k] = cfg[k]
    if "thresholds" in cfg:
        t = cfg["thresholds"] or {}
        for k_yaml, k_py in [
            ("surge_multiplier", "surge_multiplier"),
            ("surge_min_events", "surge_min_events"),
            ("regression_silent_versions", "regression_silent_versions"),
            ("feasibility_pr_threshold", "feasibility_pr_threshold"),
            ("daily_surge_threshold", "daily_surge_threshold"),
            ("daily_drop_threshold", "daily_drop_threshold"),
            ("daily_attention_min_events", "daily_attention_min_events"),
        ]:
            if k_yaml in t:
                flat[k_py] = t[k_yaml]
    if "datadog" in cfg:
        d = cfg["datadog"] or {}
        if "site" in d:
            flat["datadog_site"] = d["site"]
        if "tracks" in d:
            v = d["tracks"]
            flat["datadog_tracks"] = ",".join(v) if isinstance(v, list) else str(v)
        if "query" in d:
            flat["datadog_query"] = d["query"]
        if "query_fatal" in d:
            flat["datadog_query_fatal"] = d["query_fatal"]
        if "query_nonfatal" in d:
            flat["datadog_query_nonfatal"] = d["query_nonfatal"]
        if "query_non_fatal" in d:
            flat["datadog_query_nonfatal"] = d["query_non_fatal"]
        if "window_hours" in d:
            flat["datadog_window_hours"] = int(d["window_hours"])
    if "feishu" in cfg:
        f = cfg["feishu"] or {}
        if "target_chat_id" in f:
            flat["feishu_target_chat_id"] = f["target_chat_id"]
        if "target_email" in f:
            flat["feishu_target_email"] = f["target_email"]
        if "admin_open_ids" in f:
            flat["feishu_admin_open_ids"] = f["admin_open_ids"]
        if "morning_cron" in f:
            flat["morning_cron"] = f["morning_cron"]
        if "evening_cron" in f:
            flat["evening_cron"] = f["evening_cron"]
        if "evening_window_hours" in f:
            flat["evening_window_hours"] = int(f["evening_window_hours"])
    if "repo_paths" in cfg:
        rp = cfg["repo_paths"] or {}
        if "flutter" in rp:
            flat["repo_path_flutter"] = rp["flutter"]
        if "android" in rp:
            flat["repo_path_android"] = rp["android"]
        if "ios" in rp:
            flat["repo_path_ios"] = rp["ios"]
    if "frontend_base_url" in cfg:
        flat["frontend_base_url"] = cfg["frontend_base_url"]
    if "pr_dedup_days" in cfg:
        flat["pr_dedup_days"] = int(cfg["pr_dedup_days"])
    if "pr_sync_cron" in cfg:
        flat["pr_sync_cron"] = str(cfg["pr_sync_cron"])
    if "warmup_on_startup" in cfg:
        flat["warmup_on_startup"] = bool(cfg["warmup_on_startup"])
    if "pipeline_cron" in cfg:
        flat["pipeline_cron"] = str(cfg["pipeline_cron"])
    if "current_release" in cfg:
        cr = cfg["current_release"] or {}
        if isinstance(cr, dict):
            if "flutter" in cr:
                flat["current_release_flutter"] = str(cr["flutter"] or "")
            if "android" in cr:
                flat["current_release_android"] = str(cr["android"] or "")
            if "ios" in cr:
                flat["current_release_ios"] = str(cr["ios"] or "")
    if "latest_version_min_events" in cfg:
        flat["latest_version_min_events"] = int(cfg["latest_version_min_events"])
    if "analysis_dedup_hours" in cfg:
        flat["analysis_dedup_hours"] = int(cfg["analysis_dedup_hours"])
    if "analyze_cron" in cfg:
        flat["analyze_cron"] = str(cfg["analyze_cron"])
    if "analyze_max_per_tick" in cfg:
        flat["analyze_max_per_tick"] = int(cfg["analyze_max_per_tick"])
    if "hourly_alert" in cfg:
        ha = cfg["hourly_alert"] or {}
        if isinstance(ha, dict):
            if "enabled" in ha:
                flat["hourly_alert_enabled"] = bool(ha["enabled"])
            if "cron" in ha:
                flat["hourly_alert_cron"] = str(ha["cron"])
            if "growth_threshold_pct" in ha:
                flat["hourly_alert_growth_threshold_pct"] = float(ha["growth_threshold_pct"])
            if "new_window_days" in ha:
                flat["hourly_alert_new_window_days"] = int(ha["new_window_days"])
            if "min_baseline_events" in ha:
                flat["hourly_alert_min_baseline_events"] = int(ha["min_baseline_events"])
            if "max_items" in ha:
                flat["hourly_alert_max_items"] = int(ha["max_items"])
            if "min_sessions" in ha:
                flat["hourly_alert_min_sessions"] = int(ha["min_sessions"])
    if "core_metric" in cfg:
        cm = cfg["core_metric"] or {}
        if isinstance(cm, dict):
            if "enabled" in cm:
                flat["core_metric_enabled"] = bool(cm["enabled"])
            if "cron" in cm:
                flat["core_metric_cron"] = str(cm["cron"])
            if "change_threshold_pp" in cm:
                flat["core_metric_change_threshold_pp"] = float(cm["change_threshold_pp"])
            if "min_sessions" in cm:
                flat["core_metric_min_sessions"] = int(cm["min_sessions"])
            if "platforms" in cm:
                v = cm["platforms"]
                flat["core_metric_platforms"] = ",".join(v) if isinstance(v, list) else str(v)
    return flat


@lru_cache
def get_crashguard_settings() -> CrashguardSettings:
    """获取 crashguard 配置（cached singleton）

    优先级由 ``settings_customise_sources`` 注册：env > dotenv > yaml > defaults。
    """
    return CrashguardSettings()
