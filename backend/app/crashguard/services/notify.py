"""Crashguard 的通知出口 —— target 解析 + 按 provider 渲染 + 投递。

**所有 crashguard 的发送都走这里**，不要再直接 import `feishu_cli`。

## 这个文件最大的收益跟 Slack 无关

`alert_email → target_chat_id → target_email` 这条三级回落链，迁移前在
**六个地方**各写了一遍：

    job_health_alerter.py（×2）/ core_metric_alerter.py / hourly_alerter.py
    / symbol_coverage_monitor.py / daily_report.py

六份拷贝意味着：改路由要改六处，漏一处就是"某一类告警切了渠道、另一类没切"，
而且这种漏是**静默**的——那一类告警只是继续发去旧渠道，没人会收到报错。
收口成 `alert_target()` 一个函数之后，这条链只有一份。

## 两类去处

| | 函数 | 语义 |
|---|---|---|
| 早晚报 | `report_target()` | 进**群/频道**，全组可见 |
| 告警 | `alert_target()` | 默认**点对点**，不打扰群里其他人 |

告警为什么默认点对点：hourly / core_metric / job_health / symbol 这几类是
高频的，进群会把群刷成噪音——这是飞书时代就定下的策略（`feishu_alert_email`
这个字段本身就是为它加的），迁移不动它。

## 调用方应当把自己的 settings 传进来（`s=s`）

每个 alerter 手上都已经有一份 `get_crashguard_settings()` 的结果。让它们
`s=s` 传进来有两个好处：少一次读配置；以及**测试里各模块 patch 自己的
`get_crashguard_settings` 就能生效**——不传的话这里会从
`app.crashguard.config` 再读一遍，绕过那些 patch，拿到本机 `.env` 的真值
（这正是 2026-09-23 那次把假卡片发进真实飞书群的形状）。

## 邮箱在两个渠道下都能寻址

所以 `feishu_alert_email` / `feishu_target_email` **不需要并行的 slack 字段**
（Slack 侧走 `users.lookupByEmail`）。只有"群"需要（`feishu_target_chat_id`
vs `slack_channel`）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from app.services.im import NotifyTarget, Rendered, resolve_transport

logger = logging.getLogger("crashguard.notify")


def _settings():
    """⚠️ 延迟 import，不要改成模块级。

    测试普遍按模块属性 patch settings，模块级 import 是早绑定会让那种 patch
    失效——表现是测试里拿到本机 `.env` 里**真实的生产群 chat_id**。
    同 graygate / coreguard 的 `notify._settings()`。
    """
    from app.crashguard.config import get_crashguard_settings

    return get_crashguard_settings()


def provider(s=None) -> str:
    """当前渠道名。**永远返回一个已实现的 provider**。

    非法值在这里**不抛异常、回落 feishu 并记 error**，跟启动期校验分工：

    - 启动期（`validate_notify_config()`）配错了就 fail-fast，让人立刻看见；
    - 运行期走到这里说明配置在进程存活期间被改成了非法值，这时候抛异常等于
      **把这条告警丢掉**——而告警本身可能正是在报一个线上故障。回落到当前
      还在用的渠道 + 一条 error 日志，是这两害里轻的那个。

    也兼容测试里的 MagicMock settings（`notify_provider` 是个 Mock 而不是
    字符串）：取不出字符串就按默认走，不让桩对象把告警路径炸掉。
    """
    from app.services.im import DEFAULT_PROVIDER, implemented_providers

    raw = getattr(s or _settings(), "notify_provider", None)
    if not isinstance(raw, str) or not raw.strip():
        return DEFAULT_PROVIDER
    name = raw.strip().lower()
    if name not in implemented_providers():
        logger.error("crashguard notify_provider=%r 不是已实现的渠道（%s），"
                     "本次按 %s 发送", raw, implemented_providers(), DEFAULT_PROVIDER)
        return DEFAULT_PROVIDER
    return name


def report_target(s=None) -> NotifyTarget:
    """早晚报的去处：群/频道。群没配时退化成 `target_email`（沿用旧行为）。"""
    s = s or _settings()
    prov = provider(s)
    channel = (getattr(s, "slack_channel", "") if prov == "slack"
               else getattr(s, "feishu_target_chat_id", "")) or ""
    return NotifyTarget(provider=prov, channel=channel,
                        email=getattr(s, "feishu_target_email", "") or "")


def alert_target(s=None) -> NotifyTarget:
    """告警的去处 —— **这里就是那条被复制了六遍的回落链**。

        alert_email（点对点，默认）> 群 > target_email（旧路径兼容）

    顺序不能改：把群提到最前面等于让每条小时级告警都去吵全组，而
    `feishu_alert_email` 这个字段当初就是为了避免这件事才加的。
    """
    s = s or _settings()
    prov = provider(s)
    alert_email = getattr(s, "feishu_alert_email", "") or ""
    if alert_email:
        return NotifyTarget(provider=prov, email=alert_email)
    channel = (getattr(s, "slack_channel", "") if prov == "slack"
               else getattr(s, "feishu_target_chat_id", "")) or ""
    if channel:
        return NotifyTarget(provider=prov, channel=channel)
    return NotifyTarget(provider=prov, email=getattr(s, "feishu_target_email", "") or "")


async def _send(target: NotifyTarget, msg: Rendered, *, what: str) -> bool:
    """统一的投递 + 日志 + 吞异常。

    吞异常是**刻意**的：告警发不出去不该把产生告警的那个 job 也搞崩（心跳
    还要写、快照还要落表）。但必须留 `exception` 级日志——静默失败在这条链上
    已经吃过亏（graygate 连续两天没发都没人发现）。
    """
    if not target.configured:
        logger.warning("crashguard %s: 没有可用的通知去处（provider=%s），跳过发送",
                       what, target.provider)
        return False
    try:
        ok = await resolve_transport(target.provider).send(target, msg)
    except Exception:
        logger.exception("crashguard %s: 发送异常", what)
        return False
    if not ok:
        logger.warning("crashguard %s: 发送失败（transport 返回 False）", what)
    return bool(ok)


async def send_alert(
    feishu_card: Dict[str, Any],
    slack: Optional[Callable[[], Rendered]] = None,
    *,
    s=None,
    what: str = "alert",
) -> bool:
    """发一条告警（点对点优先）。

    `slack` 是个**惰性**的渲染回调而不是现成的 `Rendered`：provider=feishu 时
    根本不该构造 Slack 那份，白构造不只是浪费——有些渲染要查 settings/DB，
    在没切过去的环境里可能直接抛。
    """
    target = alert_target(s)
    msg = (slack() if (target.provider == "slack" and slack) else Rendered(payload=feishu_card))
    return await _send(target, msg, what=what)


async def send_report(
    feishu_card: Dict[str, Any],
    slack: Optional[Callable[[], Rendered]] = None,
    *,
    text_fallback: str = "",
    what: str = "daily_report",
) -> bool:
    """发早晚报（进群/频道）。

    `text_fallback` 沿用飞书路径的降级：卡片发失败时再试一条纯文本。
    """
    target = report_target()
    msg = (slack() if (target.provider == "slack" and slack) else Rendered(payload=feishu_card))
    ok = await _send(target, msg, what=what)
    if not ok and text_fallback:
        logger.warning("crashguard %s: 卡片发送失败，降级成纯文本再试一次", what)
        try:
            return await resolve_transport(target.provider).send_text(target, text_fallback)
        except Exception:
            logger.exception("crashguard %s: 纯文本降级也失败", what)
    return ok


async def send_text(text: str, *, email: str = "", s=None, what: str = "text") -> bool:
    """纯文本点对点（PR 相关的几条通知用）。

    `email` 显式给收件人（PR reviewer / owner 这类是按人算出来的，不是配置里
    的固定地址）；不给则回落到 `alert_target()`。
    """
    target = (NotifyTarget(provider=provider(s), email=email) if email else alert_target(s))
    if not target.configured:
        logger.warning("crashguard %s: 没有收件人，跳过", what)
        return False
    try:
        return await resolve_transport(target.provider).send_text(target, text)
    except Exception:
        logger.exception("crashguard %s: 发送异常", what)
        return False


async def send_card_to(email: str, feishu_card: Dict[str, Any],
                       slack: Optional[Callable[[], Rendered]] = None,
                       *, s=None, what: str = "card") -> bool:
    """把卡片点对点发给一个**算出来的**收件人（PR reviewer 等）。"""
    target = NotifyTarget(provider=provider(s), email=email)
    msg = (slack() if (target.provider == "slack" and slack) else Rendered(payload=feishu_card))
    return await _send(target, msg, what=what)
