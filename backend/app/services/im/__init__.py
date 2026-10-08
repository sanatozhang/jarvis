"""IM 传输层注册表。

**闸门是"实现了吗"，不是字面量白名单。** 把一个没实现的 provider 名放过去，
后果不是报错而是：配置写进去了、启动没报错、然后每一次告警在
`resolve_transport()` 抛异常被上游的 try/except 吞掉 —— 表现是"这个模块的
告警悄悄没了"。所以 `implemented_providers()` 直接从这张表派生，校验方
问它，不要各自维护一份 `("feishu", "slack")` 的字面量。
"""
from __future__ import annotations

import contextvars
import functools
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from app.services.im.base import Fold, IMTransport, NotifyTarget, Rendered
from app.services.im.feishu import FeishuTransport
from app.services.im.slack import SlackTransport

_TRANSPORTS: Dict[str, IMTransport] = {
    "feishu": FeishuTransport(),
    "slack": SlackTransport(),
}

DEFAULT_PROVIDER = "feishu"

# 「双发」不是一个 transport，而是一个 provider 取值：配成 `both` 时，每个模块的
# 发送入口（`dual_send` 装饰的函数）依次用 feishu、slack 各跑一遍。
# 渲染是 provider-native 的（见 base.py），所以不能做成一个"复合 transport"——
# 它拿到的 Rendered 只有一种形状。
BOTH = "both"

logger = logging.getLogger("jarvis.im")

# 双发时当前正在跑的那一条腿（"feishu" / "slack"），None = 不在双发里。
_LEG: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("im_dual_leg", default=None)


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


def selectable_providers() -> Tuple[str, ...]:
    """配置里能写的取值 = 已实现的渠道 + `both`。"""
    return implemented_providers() + (BOTH,)


def effective_provider(raw: Any) -> str:
    """把配置里的 provider 取值解析成**这一次发送**该用的单个渠道。

    各模块读 provider 的地方都过这一层：双发的某条腿在跑时返回那条腿；
    `both` 但不在双发里（例如只读 target 判断"配没配"）返回默认渠道；
    其余原样返回。非字符串（测试里的 Mock）也原样返回，由调用方自己兜底。
    """
    if not isinstance(raw, str):
        return raw
    name = raw.strip().lower()
    if name == BOTH:
        return _LEG.get() or DEFAULT_PROVIDER
    return name


def is_secondary_leg() -> bool:
    """当前是不是双发里的**第二条腿**（slack）。

    有"计数/审计"副作用的发送点（coreguard 的群配额与 dispatch 审计）用它跳过
    第二条腿，避免一次告警被记两遍、配额被吃两份。
    """
    return _LEG.get() == "slack"


def _merge(results: list) -> Optional[bool]:
    """多条腿的结果合并：任一成功即成功；全是 None（没数据可报）仍是 None。"""
    if any(r is True for r in results):
        return True
    if results and all(r is None for r in results):
        return None
    return False


def dual_send(raw_getter: Callable[[], Any]) -> Callable:
    """装饰一个模块的**公开发送函数**：provider=both 时 feishu、slack 各跑一遍。

    - 每条腿独立 try/except：一边失败不拖累另一边，失败记 exception 日志；
    - 返回值：任一腿成功即成功（`_merge`）；
    - 非 both、或已经在某条腿里（嵌套调用）时原样直通，零开销。

    `raw_getter` 返回该模块**当前配置里**的 provider 取值（热切后立即生效）。
    """

    def deco(fn: Callable[..., Awaitable[Any]]):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            raw = raw_getter()
            if _LEG.get() is not None or not (
                isinstance(raw, str) and raw.strip().lower() == BOTH
            ):
                return await fn(*args, **kwargs)
            results = []
            for leg in ("feishu", "slack"):
                token = _LEG.set(leg)
                try:
                    results.append(await fn(*args, **kwargs))
                except Exception:
                    logger.exception("双发：%s 的 %s 腿发送异常", fn.__qualname__, leg)
                    results.append(False)
                finally:
                    _LEG.reset(token)
            return _merge(results)

        return wrapper

    return deco


def validate_provider(module: str, value: str, *, allow_both: bool = True) -> str:
    """启动期校验一个模块配的 provider。非法就 **raise**，不回落。

    启动期 fail-fast 和运行期回落是刻意的分工：
    - 这里配错了立刻炸，运维在部署那一刻就看见；
    - 运行期（各模块 `notify.provider()`）回落到默认渠道 + 记 error，
      因为那时候抛异常等于**把一条告警丢掉**，而告警本身可能正在报线上故障。

    判据是「实现了吗」（注册表），不是字面量白名单——接第三个渠道时不用
    回来改这里。
    """
    name = (value or DEFAULT_PROVIDER).strip().lower()
    allowed = selectable_providers() if allow_both else implemented_providers()
    if name not in allowed:
        raise ValueError(
            f"{module} 的通知 provider={value!r} 不是已实现的渠道；"
            f"可选：{allowed}"
        )
    return name


__all__ = [
    "DEFAULT_PROVIDER",
    "Fold",
    "IMTransport",
    "BOTH",
    "NotifyTarget",
    "Rendered",
    "dual_send",
    "effective_provider",
    "implemented_providers",
    "is_secondary_leg",
    "resolve_transport",
    "selectable_providers",
    "validate_provider",
]
