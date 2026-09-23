"""IM 传输层的中立契约。

分层：

    各模块 alerter/report
      ├─ 自己解析出 NotifyTarget（读自己模块的配置）
      ├─ 自己渲染出 Rendered（provider-native，见设计文档「不做中立 IR」）
      └─ resolve_transport(target.provider).send(target, rendered)

**这个包刻意不 import 任何模块的配置**（`app.crashguard.config` 等）。
依赖方向单向向下：模块 → services/im → services/{feishu_cli,slack_cli}。
反过来会让 services 依赖 crashguard/coreguard/graygate，把"通用基础设施"
变成"知道所有业务模块的东西"。

所以三级回落链（`alert_email → target_chat_id → target_email`）的收口
发生在**各模块自己的** notify_target 里，不在这一层。这一层只认最终结果。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence


@dataclass(frozen=True)
class NotifyTarget:
    """一次发送的最终去处。

    `channel` 和 `email` **互斥且有优先级**：`channel` 非空就发频道/群，
    否则发给 `email` 对应的人。两个都空 = 没配，调用方应当跳过并记 warning
    （**不要静默返回成功**——那正是"切了渠道但没人收到"的形状）。
    """

    provider: str          # "feishu" | "slack"
    channel: str = ""      # feishu chat_id（oc_...）/ slack channel id（C.../D...）
    email: str = ""        # 点对点兜底

    @property
    def configured(self) -> bool:
        return bool(self.channel or self.email)


@dataclass(frozen=True)
class Fold:
    """一个「折叠段」。

    飞书编译成 `collapsible_panel`（由各模块的飞书 builder 自己塞进 card，
    这一层看不到）；**Slack 编译成一条 thread 回复** —— Block Kit 没有折叠区，
    thread 是层次同构的替代物（见设计文档「折叠层落地」）。
    """

    title: str
    blocks: List[Dict[str, Any]] = field(default_factory=list)
    text: str = ""          # 通知栏/降级文本；空则用 title


@dataclass
class Rendered:
    """provider-native 的渲染结果。transport 只管发，不管长什么样。

    - 飞书：`payload` 是完整的 card dict，`folds` 恒为空（折叠已经在 card 里）
    - Slack：`payload` 是 blocks 列表，`folds` 是要发成 thread 回复的段，
      `color` 是 legacy attachment 色条（对应飞书 card 的 `template`）

    `text` 两边都要：飞书的纯文本降级、Slack 的通知栏摘要。
    """

    payload: Any = None
    folds: Sequence[Fold] = ()
    text: str = ""
    color: str = ""


class IMTransport(ABC):
    """一个 IM 渠道。实现类不做渲染，只做投递。"""

    provider: str = ""

    @abstractmethod
    async def send(self, target: NotifyTarget, msg: Rendered) -> bool:
        """投递一条已渲染的消息。

        返回 `True` 仅表示**主消息**发出去了。thread 回复（`folds`）失败
        **不影响返回值** —— 见 `slack.py::SlackTransport.send` 里那段关于
        发送锁的注释，这不是偷懒，是防重发。
        """

    @abstractmethod
    async def send_text(self, target: NotifyTarget, text: str) -> bool:
        """纯文本消息（心跳失败告警这类不需要卡片的场景）。"""
