"""崩溃类型预分类 — 纯函数，无 IO。
在 deep_analyzer 调用 agent 之前预判 crash_type，注入 prompt 指引专项调查路径。
"""
from __future__ import annotations
import re
from typing import Dict

_ANR_TITLE_RE = re.compile(
    r"\bANR\b|Application Not Responding|appNotResponding", re.IGNORECASE
)
_ANR_STACK_RE = re.compile(
    r"android\.app\.ActivityManagerNative|android\.os\.Process\.(sendSignal|killProcess)"
    r"|ActivityThread\.handleBindApplication|ANRError",
    re.IGNORECASE,
)
_FREEZE_RE = re.compile(
    r"\bfreeze\b|卡顿|hang\b|Watchdog|WatchDog|CADisplayLink|runloop.*stall",
    re.IGNORECASE,
)
_OOM_RE = re.compile(
    r"\bOOM\b|OutOfMemory|out.of.memory|low.memory|MemoryError", re.IGNORECASE
)
_NATIVE_STACK_RE = re.compile(
    r"SIGSEGV|SIGABRT|SIGBUS|EXC_BAD_ACCESS|EXC_CRASH|fatal signal",
    re.IGNORECASE,
)


def classify_crash_type(title: str, stack: str, tags: Dict) -> str:
    """返回 anr | freeze | oom | native_crash | crash。

    优先级：anr > freeze > oom > native_crash > crash（默认）。
    title 和 stack 都检查，title 权重略高（先检查）。
    """
    text_title = title or ""
    text_stack = stack or ""

    if _ANR_TITLE_RE.search(text_title) or _ANR_STACK_RE.search(text_stack):
        return "anr"
    if _FREEZE_RE.search(text_title) or _FREEZE_RE.search(text_stack):
        return "freeze"
    if _OOM_RE.search(text_title) or _OOM_RE.search(text_stack):
        return "oom"
    if _NATIVE_STACK_RE.search(text_title) or _NATIVE_STACK_RE.search(text_stack):
        return "native_crash"
    return "crash"
