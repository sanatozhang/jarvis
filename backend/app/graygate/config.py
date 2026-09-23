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
    # ⚠️ 历史名。它是**发送总开关**（False = 只算不发），跟走哪个渠道无关
    # ——切到 slack 之后它照样管用。不改名是因为 env `GRAYGATE_FEISHU_ENABLED`
    # 已经配在各台机器上，改名要同步改部署；代码里一律通过 `send_enabled`
    # 这个属性读它，让调用点的名字是对的。
    feishu_enabled: bool = True
    scheduler_enabled: bool = True    # 该实例是否跑 cron（多机部署兜底）

    dashboard_id: str = "mbn-8h9-m2p"
    version_pattern: str = "4.0.3*"   # 灰度批次，可随版本推进改
    feishu_chat_id: str = ""

    # --- 通知渠道（Slack 迁移，2026-09）---------------------------------
    # 模块粒度开关：graygate 可以先于 crashguard/coreguard 切到 Slack。
    # 合法值由 `services/im.implemented_providers()` 决定，不写字面量白名单。
    notify_provider: str = "feishu"
    # Slack 频道 id（`C...`）。**刻意没有默认值、没有硬编码兜底**：
    # 渠道专属 id 带默认值的后果见设计文档——切过去时拿到的是另一个渠道的 id，
    # 发送方看起来成功、收件人是空气。没配就是没配，启动期会报出来。
    slack_channel: str = ""
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

    # 2026-09-16：主要版本写接口调用方鉴权。背景——102 上实测发现有不明调用方
    # 持续把 focus-version 摁回一个值，changed_by 全是匿名（无 SSO 也无身份），
    # 审计表能记录"变了"但记录不了"谁"。改成每个调用方一把独立密钥，写请求必须
    # 带上其中一把才放行；SSO 登录态（浏览器 /settings 页面）不受影响，仍然
    # 用邮箱识别，不需要额外带 key。见 api/graygate.py::_resolve_caller。
    api_key_jarvis: str = ""   # jarvis 自己（脚本/技能直接调 API）用
    api_key_runway: str = ""  # Runway 发版工具用，独立于 jarvis 的 key

    @property
    def send_enabled(self) -> bool:
        """发送总开关。读的是历史名 `feishu_enabled`，但语义与渠道无关。"""
        return self.feishu_enabled

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
