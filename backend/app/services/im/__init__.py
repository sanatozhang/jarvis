"""IM 传输层注册表。

**闸门是"实现了吗"，不是字面量白名单。** 把一个没实现的 provider 名放过去，
后果不是报错而是：配置写进去了、启动没报错、然后每一次告警在
`resolve_transport()` 抛异常被上游的 try/except 吞掉 —— 表现是"这个模块的
告警悄悄没了"。所以 `implemented_providers()` 直接从这张表派生，校验方
问它，不要各自维护一份 `("feishu", "slack")` 的字面量。
"""
from __future__ import annotations

from typing import Dict, Tuple

from app.services.im.base import Fold, IMTransport, NotifyTarget, Rendered
from app.services.im.feishu import FeishuTransport
from app.services.im.slack import SlackTransport

_TRANSPORTS: Dict[str, IMTransport] = {
    "feishu": FeishuTransport(),
    "slack": SlackTransport(),
}

DEFAULT_PROVIDER = "feishu"


def implemented_providers() -> Tuple[str, ...]:
    """按字母序返回已实现的 provider 名。配置校验用这个，不要写字面量。"""
    return tuple(sorted(_TRANSPORTS))


def resolve_transport(provider: str) -> IMTransport:
    """provider 名 → transport 实例。

    每次调用现查，**不在调用方缓存实例** —— 各模块的 provider 是可以热切的
    （改 yaml + 重载配置），缓存会让"已经加载过这个模块的代码路径"继续用旧
    渠道，而没加载过的用新渠道，同一次切换出现两种行为。

    未知 provider 抛 `ValueError` 而不是回落到飞书：回落会让"配错了"表现成
    "一切正常"，而这正是整个迁移里最贵的一类 bug。
    """
    key = (provider or "").strip().lower()
    transport = _TRANSPORTS.get(key)
    if transport is None:
        raise ValueError(
            f"未知的通知 provider {provider!r}；已实现的是 {implemented_providers()}"
        )
    return transport


def validate_provider(module: str, value: str) -> str:
    """启动期校验一个模块配的 provider。非法就 **raise**，不回落。

    启动期 fail-fast 和运行期回落是刻意的分工：
    - 这里配错了立刻炸，运维在部署那一刻就看见；
    - 运行期（各模块 `notify.provider()`）回落到默认渠道 + 记 error，
      因为那时候抛异常等于**把一条告警丢掉**，而告警本身可能正在报线上故障。

    判据是「实现了吗」（注册表），不是字面量白名单——接第三个渠道时不用
    回来改这里。
    """
    name = (value or DEFAULT_PROVIDER).strip().lower()
    if name not in _TRANSPORTS:
        raise ValueError(
            f"{module} 的通知 provider={value!r} 不是已实现的渠道；"
            f"可选：{implemented_providers()}"
        )
    return name


__all__ = [
    "DEFAULT_PROVIDER",
    "Fold",
    "IMTransport",
    "NotifyTarget",
    "Rendered",
    "implemented_providers",
    "resolve_transport",
    "validate_provider",
]
