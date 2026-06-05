"""Coreguard 配置（demo 阶段最小集）。

env > defaults。env 前缀 `COREGUARD_`。Datadog/Feishu 凭据默认复用 crashguard 的值，
避免重复维护两套；后续正式上线可独立。
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings

from app.config import PROJECT_ROOT


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
    # 演示阶段：email 优先于 chat_id（点对点不打扰群）
    # 2026-05-28：已废弃，新路由用 group_daily_quota + overflow_email；保留字段避免 env 校验失败
    feishu_prefer_email: bool = False
    # 2026-05-28 群配额：每日最多发到群 N 条，超出转 overflow_email（不打扰群里其他人）
    feishu_group_daily_quota: int = 2
    # 群配额溢出的个人邮箱；不设回落 feishu_target_email
    feishu_overflow_email: str = ""

    # Demo dashboard 锁定
    dashboard_id: str = "4h8-qff-zra"

    # Demo 阈值（Crash-free sessions 收紧到 0.5pp）
    demo_threshold_pp: float = 0.5

    # Scheduler — hourly_watch cron 每小时第 15 分钟跑 22 指标 SHoW 对比
    # 底层逻辑：Datadog RUM 入仓延迟实测 0-10min 才稳定，给 15min 缓冲
    # 避免漏掉窗口末段 5-13% 的 events（fact-check 见 commit 描述）
    scheduler_enabled: bool = True
    hourly_watch_cron: str = "15 * * * *"

    # 样本量地板（共用一次 cardinality(@usr.id) 查询）：
    # 当前窗口 distinct user < min_users → 静默写快照，不发飞书
    # 2026-05-25 实测填充率 92.7%（与 crashguard `latest_version_min_sessions=300` 对齐颗粒度）
    min_users: int = 300

    # P1 N=2 防抖：单点 breach 仅写快照，连续 2 次才入飞书卡（P0 不防抖立刻报）
    # 上一个窗口的 breached 状态从 CoreguardMetricSnapshot 历史快照读
    p1_consecutive_breach: int = 2

    # ── 预测带引擎（design 2026-06-05；回验确定 k=3）──────────────────
    # 带 = median(同时段近 N 天) ± k × (1.4826·MAD)，方向感知穿带 → 告警
    band_enabled: bool = True              # False → 回退旧单点 SHoW + 固定阈值
    band_k: float = 3.0                    # 全局 k（回验 §6.3：4.6/周 @k=3）
    band_window_hours: int = 3             # 滚动评估窗口（样本量 ×3，比单小时稳）
    band_baseline_days: int = 14           # 方案 B：同时段近 N 天（RUM 仅留 ~30 天）
    band_min_points: int = 3               # 有效历史点 < N → 不报、只记录
    # 带宽地板（防零宽带）：百分比类用绝对 pp；其余用相对 μ 比例
    band_sigma_floor_pp: float = 0.05
    band_sigma_floor_rel: float = 0.005
    # 防抖：连续 N 个窗口同向穿带才入飞书卡（统一所有 tier，design §5.2）
    band_consecutive: int = 2

    model_config = {
        "env_prefix": "COREGUARD_",
        # 用绝对路径（同 crashguard 模式），避免 cwd 在 backend/ 时找不到根目录 .env
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


def _load_crashguard_feishu_from_yaml() -> dict:
    """从 config.yaml::crashguard.feishu 段读取 target_chat_id / target_email / alert_email。

    Crashguard 实际 chat_id 在 yaml 不在 env（见 config.yaml line 150）。coreguard demo
    阶段直接读 yaml 公共配置（不 import crashguard 模块，保隔离合约）。
    """
    try:
        from app.config import _load_yaml
        data = _load_yaml() or {}    # 注意：无参数，全局 PROJECT_ROOT/config.yaml
        feishu = (data.get("crashguard") or {}).get("feishu") or {}
        return {
            "target_chat_id": feishu.get("target_chat_id", "") or "",
            "target_email": feishu.get("target_email", "") or "",
            "alert_email": feishu.get("alert_email", "") or "",
        }
    except Exception:
        return {"target_chat_id": "", "target_email": "", "alert_email": ""}


@lru_cache(maxsize=1)
def get_coreguard_settings() -> CoreguardSettings:
    s = CoreguardSettings()
    # Demo 阶段：未配 COREGUARD_* 时回落到 CRASHGUARD_*，方便快速验证
    if not s.datadog_api_key:
        s.datadog_api_key = os.environ.get("CRASHGUARD_DATADOG_API_KEY", "")
    if not s.datadog_app_key:
        s.datadog_app_key = os.environ.get("CRASHGUARD_DATADOG_APP_KEY", "")

    # Feishu chat_id / email 回落顺序：
    #   COREGUARD_* env → CRASHGUARD_* env → config.yaml::crashguard.feishu.*
    yaml_feishu = _load_crashguard_feishu_from_yaml()
    if not s.feishu_target_chat_id:
        s.feishu_target_chat_id = (
            os.environ.get("CRASHGUARD_FEISHU_TARGET_CHAT_ID", "")
            or yaml_feishu["target_chat_id"]
        )
    if not s.feishu_target_email:
        # 告警优先用 alert_email（点对点），其次 target_email（兜底）
        s.feishu_target_email = (
            os.environ.get("CRASHGUARD_FEISHU_ALERT_EMAIL", "")
            or os.environ.get("CRASHGUARD_FEISHU_TARGET_EMAIL", "")
            or yaml_feishu["alert_email"]
            or yaml_feishu["target_email"]
        )
    # overflow_email：未单独配置时回落 target_email；保证"群配额满 → 总有去处"
    if not s.feishu_overflow_email:
        s.feishu_overflow_email = s.feishu_target_email
    return s
