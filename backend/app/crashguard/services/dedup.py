"""
Stack fingerprint 算法 — 跨版本同 bug 去重抓手。

归一化规则:
1. 取 stack trace 前 5 帧
2. 剥离行号: foo.dart:123 → foo.dart
3. 剥离匿名闭包/生成代码: <anonymous>, _$xxxx, closure_at_
4. 剥离版本号路径: pub-cache/.../package-1.2.3/ → package-*
5. 剥离 SDK/framework 噪音帧 (dart:async, Flutter framework, libsystem)
6. 剩余规范化文本拼接 → SHA1
"""
from __future__ import annotations

import hashlib
import re
from typing import List

# 噪音帧黑名单（substring 匹配，case-insensitive）
_NOISE_PATTERNS = [
    "dart:async",
    "dart:core",
    "dart:io",
    "package:flutter/src/",
    "libsystem",
    "libdyld",
    "libobjc",
    "java.lang.Thread",
    "java.util.concurrent",
    "kotlin.coroutines",
    "<anonymous>",
]

_LINE_NUM_RE = re.compile(r":\d+(?=[\s\)]|$)")
_CLOSURE_RE = re.compile(r"_\$[a-zA-Z0-9]+(_closure)?")
_VERSIONED_PATH_RE = re.compile(r"(pub-cache|node_modules|\.gradle/caches)/[^/]+-\d+\.\d+\.\d+", re.IGNORECASE)


def normalize_stack_frames(stack_trace: str, top_n: int = 5) -> List[str]:
    """
    把堆栈拆成帧列表，归一化噪音，返回前 top_n 个有效帧。
    """
    if not stack_trace:
        return []

    # 1. 拆行
    lines = [ln.strip() for ln in stack_trace.splitlines() if ln.strip()]

    # 2. 跳过非帧行（如错误标题）— 启发式: 包含 "at " 或 "  at "
    frames = [ln for ln in lines if ln.startswith("at ") or " at " in ln or ln.startswith("- ")]
    if not frames:
        # 兜底: 取所有非空行（异常情况）
        frames = lines[1:] if len(lines) > 1 else lines

    # 3. 归一化每帧
    normalized: List[str] = []
    for frame in frames:
        # 跳过噪音帧
        if any(p.lower() in frame.lower() for p in _NOISE_PATTERNS):
            continue

        # 剥离行号
        f = _LINE_NUM_RE.sub("", frame)
        # 剥离匿名闭包
        f = _CLOSURE_RE.sub("", f)
        # 版本号路径替换
        f = _VERSIONED_PATH_RE.sub(r"\1/*", f)
        # 折叠多余空白
        f = " ".join(f.split())

        normalized.append(f)

        if len(normalized) >= top_n:
            break

    return normalized


def compute_fingerprint(stack_trace: str, top_n: int = 5) -> str:
    """
    计算 stack_fingerprint (SHA1)。

    空栈/异常输入仍返回稳定哈希（避免上游中断）。
    """
    frames = normalize_stack_frames(stack_trace or "", top_n=top_n)
    payload = "\n".join(frames) if frames else "empty"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


import json
from typing import List as _List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_fingerprint_link(
    session: AsyncSession,
    fingerprint: str,
    datadog_issue_id: str,
    first_seen_version: str,
    events_count: int,
    normalized_top_frames: _List[str],
) -> None:
    """
    把 (fingerprint, datadog_issue_id) 关系写入 crash_fingerprints 表。

    - 不存在 → 新建
    - 已存在 → 把 datadog_issue_id 追加到 list；累加 events count；
              first_seen_version 取早版本（字符串字典序兜底）
    """
    from app.crashguard.models import CrashFingerprint

    row = (await session.execute(
        select(CrashFingerprint).where(CrashFingerprint.fingerprint == fingerprint)
    )).scalar_one_or_none()

    if row is None:
        row = CrashFingerprint(
            fingerprint=fingerprint,
            datadog_issue_ids=json.dumps([datadog_issue_id]),
            first_seen_version=first_seen_version,
            total_events_across_versions=events_count,
            normalized_top_frames=json.dumps(normalized_top_frames),
        )
        session.add(row)
        return

    ids = json.loads(row.datadog_issue_ids or "[]")
    if datadog_issue_id not in ids:
        ids.append(datadog_issue_id)
        row.datadog_issue_ids = json.dumps(ids)

    row.total_events_across_versions = (row.total_events_across_versions or 0) + events_count

    # 取更早的版本（简单字典序，足够大多数 semver 场景）
    if first_seen_version and (
        not row.first_seen_version or first_seen_version < row.first_seen_version
    ):
        row.first_seen_version = first_seen_version
