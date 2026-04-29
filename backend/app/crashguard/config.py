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
    # 搜索 query（event search 语法）。"*" = 全量；可改成 "@type:error" 等。
    datadog_query: str = "*"

    # Schedule
    morning_cron: str = "0 7 * * *"
    evening_cron: str = "0 17 * * *"

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
    frontend_base_url: str = "http://localhost:3000"

    # 半自动 PR 仓库映射（按平台覆盖，未设回落 jarvis code_repo_app）
    repo_path_flutter: str = ""
    repo_path_android: str = ""
    repo_path_ios: str = ""
    # PR 去重窗口（同一 issue+platform 30 天内只允许一个 draft PR）
    pr_dedup_days: int = 30

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
    return flat


@lru_cache
def get_crashguard_settings() -> CrashguardSettings:
    """获取 crashguard 配置（cached singleton）

    优先级由 ``settings_customise_sources`` 注册：env > dotenv > yaml > defaults。
    """
    return CrashguardSettings()
