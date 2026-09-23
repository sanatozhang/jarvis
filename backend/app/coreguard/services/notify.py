"""Coreguard 的通知出口 —— target 解析 + 群配额路由 + 按 provider 渲染。

**所有 coreguard 的发送都走这里**，不要再直接 import `feishu_cli`。

## 群配额路由是「策略」，不是「渠道」

`feishu_group_daily_quota` / `overflow_email` 那套（每天最多往群里发 N 条，
超出转个人）是**跟渠道无关的产品策略**，迁移不动它一个字节：

    1) 有群 + 今日群配额未满 → 发群（target_kind='group'）
    2) 群配额已满 或 无群     → 发 overflow 邮箱（target_kind='email'）
    3) 两者都没配            → skip

`coreguard_alert_dispatches` 表也照旧：`target_kind` 记的是 group/email 这个
**语义**，不是 feishu/slack，所以切换渠道之后历史配额计数仍然连续
——不会出现"切完当天配额被清零、于是把群刷一遍"。

## 溢出为什么不需要并行的 slack 字段

溢出目标是**邮箱**，而邮箱寻址在两个渠道下都成立（Slack 侧走
`users.lookupByEmail`）。只有"群"需要并行字段（`feishu_target_chat_id`
vs `slack_channel`）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.services.im import NotifyTarget, Rendered, resolve_transport

logger = logging.getLogger("coreguard.notify")


def _settings():
    """⚠️ 延迟 import，不要改成模块级。

    测试普遍 `patch("app.coreguard.config.get_coreguard_settings", ...)`，
    模块级 import 是早绑定，那种 patch 碰不到这里——表现是测试里拿到本机
    `.env` 里**真实的生产群 chat_id**。同 graygate 的 `notify._settings()`。
    """
    from app.coreguard.config import get_coreguard_settings

    return get_coreguard_settings()


def _provider(s=None) -> str:
    s = s or _settings()
    return (getattr(s, "notify_provider", "") or "feishu").strip().lower()


def group_target(s=None) -> NotifyTarget:
    """「群」这个去处。provider=slack 时是频道，feishu 时是群 chat_id。"""
    s = s or _settings()
    prov = _provider(s)
    if prov == "slack":
        return NotifyTarget(provider="slack", channel=getattr(s, "slack_channel", "") or "")
    return NotifyTarget(provider="feishu", channel=s.feishu_target_chat_id or "")


def overflow_target(s=None) -> NotifyTarget:
    """群配额溢出的去处 —— 两个渠道都用邮箱寻址。"""
    s = s or _settings()
    email = s.feishu_overflow_email or s.feishu_target_email or ""
    return NotifyTarget(provider=_provider(s), email=email)


def render_summary(**kwargs) -> Rendered:
    """按当前 provider 渲染告警。入参跟 `build_summary_card` 逐字对齐。"""
    if _provider() == "slack":
        from app.coreguard.services.slack_summary import build_summary_message

        return build_summary_message(**kwargs)

    from app.coreguard.services.feishu_summary_card import build_summary_card

    return Rendered(payload=build_summary_card(**kwargs))


def _extract_title(msg: Rendered) -> str:
    """从已渲染的消息里抠出标题，用于 dispatch 审计行。

    两个 provider 的结构不同，所以这里按形状取：飞书是
    `header.title.content`，Slack 是 `Rendered.text`（渲染时就是标题）。
    取不到返回空串——审计行的标题为空不该把告警本身搞挂。
    """
    if msg.text:
        return msg.text
    try:
        return ((msg.payload.get("header") or {}).get("title") or {}).get("content", "") or ""
    except Exception:
        return ""


def _today_local_date():
    """配额按 Asia/Shanghai 自然日切（容器 TZ=Asia/Shanghai → date.today() 即可）。"""
    from datetime import date as _date_t

    return _date_t.today()


async def _count_group_sent_today(today) -> int:
    """今日已成功发到**群**的告警数（配额判定用）。

    只数 `sent_ok=True` —— 发送失败不该消耗配额，否则渠道抖一下就会把当天
    剩下的告警全挤去个人邮箱。
    """
    from sqlalchemy import func, select

    from app.coreguard.models import CoreguardAlertDispatch
    from app.db.database import get_session

    async with get_session() as session:
        n = (await session.execute(
            select(func.count(CoreguardAlertDispatch.id)).where(
                CoreguardAlertDispatch.sent_date == today,
                CoreguardAlertDispatch.target_kind == "group",
                CoreguardAlertDispatch.sent_ok.is_(True),
            )
        )).scalar_one()
    return int(n or 0)


async def _record_dispatch(
    *, today, target_kind: str, target_value: str, sent_ok: bool,
    alert_title: str, breach_count: int, overflow_from_group: bool,
) -> None:
    from app.coreguard.models import CoreguardAlertDispatch
    from app.db.database import get_session

    async with get_session() as session:
        session.add(CoreguardAlertDispatch(
            sent_date=today,
            target_kind=target_kind,
            target_value=(target_value or "")[:128],
            sent_ok=sent_ok,
            alert_title=(alert_title or "")[:256],
            breach_count=int(breach_count or 0),
            overflow_from_group=overflow_from_group,
        ))
        await session.commit()


async def send_alert(msg: Rendered, breach_count: int = 0) -> bool:
    """按群配额路由投递一条已渲染的告警。每次成功/失败都写 dispatch 审计行。"""
    s = _settings()
    if not s.feishu_enabled:
        logger.info("feishu_enabled=false, skip send")
        return False

    group = group_target(s)
    overflow = overflow_target(s)
    if not group.configured and not overflow.configured:
        logger.warning(
            "coreguard 没有任何可用的通知去处（provider=%s，群和溢出邮箱都空）", _provider(s),
        )
        return False

    today = _today_local_date()
    title = _extract_title(msg)
    quota = int(s.feishu_group_daily_quota or 0)
    sent_today = await _count_group_sent_today(today) if group.configured else 0

    try:
        transport = resolve_transport(_provider(s))

        # 1) 优先群（配额内）
        if group.configured and sent_today < quota:
            ok = await transport.send(group, msg)
            logger.info("coreguard send → group %s (today %d/%d, ok=%s)",
                        group.channel, sent_today + (1 if ok else 0), quota, ok)
            await _record_dispatch(
                today=today, target_kind="group", target_value=group.channel,
                sent_ok=bool(ok), alert_title=title, breach_count=breach_count,
                overflow_from_group=False,
            )
            return bool(ok)

        # 2) 群配额已满 / 无群 → 转个人
        if overflow.configured:
            ok = await transport.send(overflow, msg)
            logger.info("coreguard send → overflow email %s (group_sent_today=%d quota=%d, ok=%s)",
                        overflow.email, sent_today, quota, ok)
            await _record_dispatch(
                today=today, target_kind="email", target_value=overflow.email,
                sent_ok=bool(ok), alert_title=title, breach_count=breach_count,
                overflow_from_group=(sent_today >= quota and group.configured),
            )
            return bool(ok)

        logger.warning("group quota exhausted but no overflow email configured, dropping alert")
        return False
    except Exception as e:
        logger.error("coreguard send failed: %s", e)
        return False


async def send_summary(*, breach_count: int = 0, **kwargs) -> bool:
    """渲染 + 投递。`kwargs` 逐字转给 `build_summary_card` / `build_summary_message`。"""
    return await send_alert(render_summary(**kwargs), breach_count=breach_count)


async def send_simple_card(feishu_card: Dict[str, Any],
                           slack_blocks: Optional[List[dict]] = None,
                           *, text: str = "", color: str = "") -> bool:
    """给 demo / 一次性小卡片用：直接发到「群」，**不走配额路由**。

    demo 是人手动触发的一次性动作，不该消耗当天给真实告警留的群配额。
    """
    s = _settings()
    if not s.feishu_enabled:
        logger.info("feishu_enabled=false, skip send")
        return False
    target = group_target(s)
    if not target.configured:
        target = overflow_target(s)
    if not target.configured:
        logger.warning("coreguard demo：没有任何可用的通知去处")
        return False

    msg = (Rendered(payload=slack_blocks or [], text=text, color=color)
           if target.provider == "slack" else Rendered(payload=feishu_card))
    try:
        return await resolve_transport(target.provider).send(target, msg)
    except Exception as e:
        logger.error("coreguard demo send failed: %s", e)
        return False
