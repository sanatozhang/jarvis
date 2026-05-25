"""Coreguard 配置（demo 阶段最小集）。

env > defaults。env 前缀 `COREGUARD_`。Datadog/Feishu 凭据默认复用 crashguard 的值，
避免重复维护两套；后续正式上线可独立。
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class CoreguardSettings(BaseSettings):
    enabled: bool = True
    feishu_enabled: bool = True

    # Datadog（demo 默认复用 crashguard 同一对 key）
    datadog_api_key: str = ""
    datadog_app_key: str = ""
    datadog_site: str = "datadoghq.com"

    # Feishu（demo 默认复用 crashguard 群）
    feishu_target_chat_id: str = ""
    feishu_target_email: str = ""

    # Demo dashboard 锁定
    dashboard_id: str = "4h8-qff-zra"

    # Demo 阈值（Crash-free sessions 收紧到 0.5pp）
    demo_threshold_pp: float = 0.5

    model_config = {
        "env_prefix": "COREGUARD_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


@lru_cache(maxsize=1)
def get_coreguard_settings() -> CoreguardSettings:
    s = CoreguardSettings()
    # Demo 阶段：未配 COREGUARD_* 时回落到 CRASHGUARD_*，方便快速验证
    if not s.datadog_api_key:
        s.datadog_api_key = os.environ.get("CRASHGUARD_DATADOG_API_KEY", "")
    if not s.datadog_app_key:
        s.datadog_app_key = os.environ.get("CRASHGUARD_DATADOG_APP_KEY", "")
    if not s.feishu_target_chat_id:
        s.feishu_target_chat_id = os.environ.get("CRASHGUARD_FEISHU_TARGET_CHAT_ID", "")
    if not s.feishu_target_email:
        s.feishu_target_email = os.environ.get("CRASHGUARD_FEISHU_TARGET_EMAIL", "")
    return s
