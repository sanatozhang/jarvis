"""Graygate 配置 — 灰度期临时模块，独立配置段。

加载顺序: env (`GRAYGATE_*`) > 默认值。Datadog 凭证不单独维护一套，未显式配置时
回落到 `CRASHGUARD_DATADOG_*`（同一对 key，避免重复维护）——照抄
`coreguard/config.py::get_coreguard_settings` 的回落写法。
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings

from app.config import PROJECT_ROOT


class GraygateSettings(BaseSettings):
    enabled: bool = True              # 总开关
    feishu_enabled: bool = True       # 发送开关（False = 只算不发）
    scheduler_enabled: bool = True    # 该实例是否跑 cron（多机部署兜底）

    dashboard_id: str = "mbn-8h9-m2p"
    version_pattern: str = "4.0.3*"   # 灰度批次，可随版本推进改
    feishu_chat_id: str = ""
    report_hour_bjt: int = 9
    min_sessions: int = 50            # 样本地板，低于此不出该单元格
    # 2026-08-23：报告构建/发送失败时私聊告警的收件人（跟 crashguard 那几个
    # fallback_email 是同一个模式）——之前失败只是悄悄写进心跳表，没人会主动
    # 去查，导致连续两天没发都没人发现。
    alert_email: str = "sanato.zhang@plaud.ai"

    # Datadog（未显式配置时回落 CRASHGUARD_DATADOG_*，见 get_graygate_settings）
    datadog_api_key: str = ""
    datadog_app_key: str = ""
    datadog_site: str = "datadoghq.com"

    model_config = {
        "env_prefix": "GRAYGATE_",
        # 用绝对路径（同 crashguard/coreguard 模式），避免 cwd 在 backend/ 时找不到根目录 .env
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


@lru_cache(maxsize=1)
def get_graygate_settings() -> GraygateSettings:
    s = GraygateSettings()
    # 未配 GRAYGATE_DATADOG_* 时回落到 CRASHGUARD_DATADOG_*，两个模块共用一对 key
    if not s.datadog_api_key:
        s.datadog_api_key = os.environ.get("CRASHGUARD_DATADOG_API_KEY", "")
    if not s.datadog_app_key:
        s.datadog_app_key = os.environ.get("CRASHGUARD_DATADOG_APP_KEY", "")
    return s
