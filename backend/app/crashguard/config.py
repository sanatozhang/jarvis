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

    # Datadog
    datadog_api_key: str = ""
    datadog_app_key: str = ""
    datadog_site: str = "datadoghq.com"
    datadog_window_hours: int = 24

    # Schedule
    morning_cron: str = "0 7 * * *"
    evening_cron: str = "0 17 * * *"

    # Top N + thresholds
    max_top_n: int = 20
    surge_multiplier: float = 1.5
    surge_min_events: int = 10
    regression_silent_versions: int = 3
    feasibility_pr_threshold: float = 0.7

    # Feishu
    feishu_target_chat_id: str = ""
    feishu_admin_open_ids: List[str] = Field(default_factory=list)

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
        "enabled", "pr_enabled", "feishu_enabled",
        "max_top_n",
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
        ]:
            if k_yaml in t:
                flat[k_py] = t[k_yaml]
    if "datadog" in cfg:
        d = cfg["datadog"] or {}
        if "site" in d:
            flat["datadog_site"] = d["site"]
    if "feishu" in cfg:
        f = cfg["feishu"] or {}
        if "target_chat_id" in f:
            flat["feishu_target_chat_id"] = f["target_chat_id"]
        if "admin_open_ids" in f:
            flat["feishu_admin_open_ids"] = f["admin_open_ids"]
        if "morning_cron" in f:
            flat["morning_cron"] = f["morning_cron"]
        if "evening_cron" in f:
            flat["evening_cron"] = f["evening_cron"]
    return flat


@lru_cache
def get_crashguard_settings() -> CrashguardSettings:
    """获取 crashguard 配置（cached singleton）

    优先级由 ``settings_customise_sources`` 注册：env > dotenv > yaml > defaults。
    """
    return CrashguardSettings()
