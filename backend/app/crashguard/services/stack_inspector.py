"""裸堆栈文本 → 符号化所需元数据（2026-09-22）。

**纯函数模块：零 IO、零网络、零 DB。** 这是它能被 pytest 完整覆盖的前提，
也是它可以被 crashguard pipeline 复用的前提。

## 为什么需要这个模块

`POST /api/crash/symbolicate` 要求调用方显式给出 platform + app_version，
但用户手上往往只有一段裸文本。本模块尽力从文本里把这些元数据挖出来做预填。

## 版本号能否自动解析——分场景

| 堆栈来源 | app 版本号 | UUID/BuildId |
|---|---|---|
| Apple .ips（iOS 15+，两段式 JSON） | ✅ app_version + build_version | ✅ usedImages[].uuid |
| Apple .crash（旧文本） | ✅ `Version: 4.0.201 (941)` | ✅ Binary Images 段 |
| Datadog RUM 复制的堆栈 | ❌ | ❌ |
| Android Java/Kotlin ProGuard 混淆栈 | ❌ **物理不可能** | ❌ |
| Android logcat | ⚠️ 偶有 versionName | ❌ |
| Android tombstone / native | ❌ | ✅ BuildId |

最高频的 Android 输入就是一段 `at a.b.c(Unknown Source:12)`，里面没有任何版本
信息。所以「手动指定版本号」必须保留为主路径，本模块只做预填，不做保证。

## 契约

`inspect_stack()` **绝不抛异常**。任何解析失败都降级为 stack_format="unknown"，
用户仍可手选平台/版本走通符号化。宁可不预填，也不要因为解析器崩了就整个功能不可用。
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Optional

logger = logging.getLogger("jarvis.crashguard.stack_inspector")

# 复用 symbolication 的 arm64e 指针认证(PAC)掩码语义，不重新发明。
# 生产实测依据见 symbolication._strip_ptr_auth 的 docstring：arm64e 设备上
# 栈回溯得到的返回地址高 24 bit 会被 PAC 签名位污染，不掩掉会算出 10^19 量级
# 的天文数字 offset。
_PTR_AUTH_MASK = 0xFFFFFFFFFF  # 保留低 40 bit

# iOS/Apple 帧：`<idx> <module> <addr> <base> + <off>`
# api/crash.py 的 _compute_frame_stats 复用此正则做逐帧统计，故导出为模块级常量。
FRAME_RE_IOS = re.compile(
    r"^(\s*\d+\s+)(\S+)(\s+)(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+\+\s+(\d+)(.*)$",
    re.MULTILINE,
)

_TOMBSTONE_FRAME_RE = re.compile(
    r"#\d{2}\s+pc\s+([0-9a-fA-F]+)\s+(\S+)(?:\s+\(BuildId:\s*([0-9a-fA-F]+)\))?"
)
_JAVA_FRAME_RE = re.compile(r"^\s*at\s+\S+\(", re.MULTILINE)
_APPLE_VERSION_RE = re.compile(r"^Version:\s*(\S+)\s*\((\S+)\)\s*$", re.MULTILINE)
_APPLE_BINIMG_RE = re.compile(
    r"^\s*(0x[0-9a-fA-F]+)\s*-\s*(0x[0-9a-fA-F]+)\s+(\S+)\s+\S+\s+<([0-9a-fA-F]+)>\s+(\S+)",
    re.MULTILINE,
)
_LOGCAT_VERSION_RE = re.compile(r"versionName[=:\s]+(\S+)")

_ALL_FORMATS = (
    "unknown", "apple_ips_json", "apple_crash_text", "datadog_rum",
    "android_java", "android_tombstone", "android_logcat",
)


@dataclass
class StackInsight:
    """解析结果。所有字段都有默认值，支持部分解析成功。"""

    stack_format: str = "unknown"
    platform: str = ""
    app_version: str = ""
    uuids: list = field(default_factory=list)
    build_ids: list = field(default_factory=list)
    binary_images: list = field(default_factory=list)
    normalized_stack: str = ""
    frame_count: int = 0
    confidence: str = "low"
    notes: list = field(default_factory=list)


def _norm_uuid(u: str) -> str:
    """UUID 规范化：去 '-'、小写。与 symbolication._normalize_uuid 语义一致。"""
    return (u or "").replace("-", "").lower()


def _strip_pac(addr: int) -> int:
    return addr & _PTR_AUTH_MASK


_MANUAL_VERSION_NOTE = "必须手动指定版本号"


def inspect_stack(raw: str) -> StackInsight:
    """识别堆栈格式并尽力提取符号化所需元数据。

    **绝不抛异常**：任何解析失败都降级为 stack_format="unknown"，
    用户仍可手选平台/版本走通符号化。
    """
    if not raw or not raw.strip():
        return StackInsight(notes=["输入为空"])
    try:
        return _inspect_inner(raw)
    except Exception as exc:  # noqa: BLE001 — 降级是本模块的明确契约
        logger.warning("inspect_stack failed, degrading to unknown: %s", exc)
        return StackInsight(
            normalized_stack=raw,
            notes=[f"解析异常，已降级为手动模式：{exc}"],
        )


def _inspect_inner(raw: str) -> StackInsight:
    """识别链。**顺序本身就是优先级**：先严格后宽松，避免误判。

    两个必须守住的顺序约束：
      - apple_crash_text 必须在 datadog_rum 之前：两者帧形状完全相同，
        只能靠 Apple header 区分。
      - android_logcat 必须在 android_java 之前：logcat 里也含 `\\tat ` 帧。
    """
    stripped = raw.lstrip()

    # 1) Apple .ips（iOS 15+）：两段式 JSON
    if stripped.startswith("{"):
        got = _try_apple_ips(raw)
        if got is not None:
            return got

    # 2) Apple .crash（旧文本格式）
    if "Incident Identifier:" in raw or re.search(r"^Binary Images:\s*$", raw, re.MULTILINE):
        return _parse_apple_crash_text(raw)

    # 3) Android tombstone / native
    if re.search(r"#\d{2}\s+pc\s", raw) or "backtrace:" in raw:
        return _parse_android_tombstone(raw)

    # 4) Android logcat（必须在 android_java 之前——logcat 里也含 at 帧）
    if "E AndroidRuntime:" in raw or "Build fingerprint:" in raw:
        return _parse_android_logcat(raw)

    # 5) Datadog RUM / 裸 iOS 帧（帧形状同 Apple，但无 header）
    if FRAME_RE_IOS.search(raw):
        return _parse_datadog_rum(raw)

    # 6) Android Java/Kotlin 混淆栈
    if _JAVA_FRAME_RE.search(raw):
        return _parse_android_java(raw)

    return StackInsight(
        normalized_stack=raw,
        notes=["无法识别堆栈格式，请手动选择平台和版本号"],
    )


# ── Apple .ips（iOS 15+，两段式 JSON）──────────────────────────────────────

def _try_apple_ips(raw: str) -> Optional[StackInsight]:
    """解析 .ips。header 能解出来就算命中（payload 坏掉仍返回版本号）。

    .ips 是两段式：第一行 header JSON，之后是 payload JSON。必须按第一个
    换行切开分别 parse —— 整体 json.loads 一定失败。
    """
    head_raw, _, payload_raw = raw.partition("\n")
    try:
        header = json.loads(head_raw)
    except Exception:
        return None
    if not isinstance(header, dict):
        return None
    # 必须有 .ips 的特征字段，否则可能是别的 JSON（不要抢别人的格式）
    if not any(k in header for k in ("app_version", "bundleID", "app_name", "incident_id")):
        return None

    r = StackInsight(stack_format="apple_ips_json", platform="ios", normalized_stack=raw)

    app_v = str(header.get("app_version") or "").strip()
    build_v = str(header.get("build_version") or "").strip()
    if app_v and build_v:
        # 对齐 Datadog @application.version 的写法
        r.app_version = f"{app_v}-{build_v}"
    elif app_v:
        r.app_version = app_v
    r.confidence = "high" if r.app_version else "medium"

    try:
        payload = json.loads(payload_raw) if payload_raw.strip() else {}
    except Exception as exc:
        r.notes.append(f".ips payload 解析失败（版本号仍可用）：{exc}")
        if not r.app_version:
            r.notes.append(_MANUAL_VERSION_NOTE)
        return r
    if not isinstance(payload, dict):
        r.notes.append(".ips payload 不是对象，已跳过帧归一化")
        return r

    images = payload.get("usedImages") or []
    for img in images:
        if not isinstance(img, dict):
            continue
        base = img.get("base")
        size = img.get("size") or 0
        name = img.get("name") or ""
        if not name and img.get("path"):
            name = PurePosixPath(str(img["path"])).name
        uid = _norm_uuid(str(img.get("uuid") or ""))
        if uid:
            r.uuids.append(uid)
        entry: dict = {
            "uuid": uid,
            "name": name,
            "source": img.get("source") or "",
        }
        if isinstance(base, int):
            entry["load_address"] = hex(base)
            entry["max_address"] = hex(base + int(size or 0))
        r.binary_images.append(entry)

    r.normalized_stack = _normalize_ips_frames(payload, images) or raw
    r.frame_count = len(
        [l for l in r.normalized_stack.splitlines() if FRAME_RE_IOS.match(l)]
    )
    return r


def _normalize_ips_frames(payload: dict, images: list) -> str:
    """.ips 的结构化帧 → `_symbolicate_ios_with_dir` 正则认的文本帧形状。

    符号化引擎的正则只认 `<idx> <module> <addr> <base> + <off>`，
    所以结构化帧必须在这里摊平，否则 .ips 根本没法符号化。
    """
    threads = payload.get("threads") or []
    if not isinstance(threads, list) or not threads:
        return ""
    picked = None
    for t in threads:
        if isinstance(t, dict) and t.get("triggered"):
            picked = t
            break
    if picked is None:
        picked = threads[0] if isinstance(threads[0], dict) else None
    if not isinstance(picked, dict):
        return ""

    out = []
    for idx, f in enumerate(picked.get("frames") or []):
        if not isinstance(f, dict):
            continue
        ii = f.get("imageIndex")
        off = f.get("imageOffset")
        if not isinstance(ii, int) or not isinstance(off, int):
            continue
        if ii < 0 or ii >= len(images):
            continue
        img = images[ii] if isinstance(images[ii], dict) else {}
        base = img.get("base")
        if not isinstance(base, int):
            continue
        name = img.get("name") or ""
        if not name and img.get("path"):
            name = PurePosixPath(str(img["path"])).name
        addr = _strip_pac(base + off)
        out.append(f"{idx}   {name}   {hex(addr)} {hex(base)} + {off}")
    return "\n".join(out) + ("\n" if out else "")


# ── Apple .crash（旧文本格式）──────────────────────────────────────────────

def _parse_apple_crash_text(raw: str) -> StackInsight:
    r = StackInsight(
        stack_format="apple_crash_text", platform="ios", normalized_stack=raw,
    )
    m = _APPLE_VERSION_RE.search(raw)
    if m:
        # `Version: 4.0.201 (941)` → `4.0.201-941`
        r.app_version = f"{m.group(1)}-{m.group(2)}"
    r.confidence = "high" if r.app_version else "medium"
    if not r.app_version:
        r.notes.append(f"未能从 Version: 行解析出版本号，{_MANUAL_VERSION_NOTE}")

    for bm in _APPLE_BINIMG_RE.finditer(raw):
        load_addr, max_addr, name, uid, path = bm.groups()
        uid_n = _norm_uuid(uid)
        if uid_n:
            r.uuids.append(uid_n)
        r.binary_images.append({
            "uuid": uid_n,
            "name": name,
            "load_address": load_addr,
            "max_address": max_addr,
            "path": path,
        })

    r.frame_count = len([l for l in raw.splitlines() if FRAME_RE_IOS.match(l)])
    _note_pac_if_any(raw, r)
    return r


# ── Datadog RUM / 裸 iOS 帧 ────────────────────────────────────────────────

def _parse_datadog_rum(raw: str) -> StackInsight:
    r = StackInsight(
        stack_format="datadog_rum",
        platform="ios",           # 这个帧形状是 iOS 特有的
        normalized_stack=raw,
        confidence="medium",
    )
    r.frame_count = len([l for l in raw.splitlines() if FRAME_RE_IOS.match(l)])
    r.notes.append(f"Datadog 复制的堆栈不含版本号，{_MANUAL_VERSION_NOTE}")
    _note_pac_if_any(raw, r)
    return r


def _note_pac_if_any(raw: str, r: StackInsight) -> None:
    """检测 arm64e 指针认证污染的地址，记一条 note。

    不改写 normalized_stack —— symbolication 侧的 _strip_ptr_auth 自己会掩码，
    在这里改会造成两处逻辑重复且可能不一致。这里只负责告知用户。
    """
    for m in FRAME_RE_IOS.finditer(raw):
        try:
            addr = int(m.group(4), 16)
        except (ValueError, TypeError):
            continue
        if _strip_pac(addr) != addr:
            r.notes.append(
                "检测到 arm64e 指针认证(PAC)污染地址，符号化时由引擎掩码处理"
            )
            return


# ── Android ────────────────────────────────────────────────────────────────

def _parse_android_tombstone(raw: str) -> StackInsight:
    r = StackInsight(
        stack_format="android_tombstone",
        platform="android",
        normalized_stack=raw,
        confidence="medium",
    )
    seen = set()
    for m in _TOMBSTONE_FRAME_RE.finditer(raw):
        r.frame_count += 1
        bid = (m.group(3) or "").lower()
        if bid and bid not in seen:
            seen.add(bid)
            r.build_ids.append(bid)
    r.notes.append(f"tombstone 不含 app 版本号，{_MANUAL_VERSION_NOTE}")
    return r


def _parse_android_logcat(raw: str) -> StackInsight:
    r = StackInsight(
        stack_format="android_logcat",
        platform="android",
        normalized_stack=raw,
        confidence="medium",
    )
    m = _LOGCAT_VERSION_RE.search(raw)
    if m:
        r.app_version = m.group(1)
        r.confidence = "high"
    else:
        r.notes.append(f"logcat 里没有 versionName，{_MANUAL_VERSION_NOTE}")
    r.frame_count = len(_JAVA_FRAME_RE.findall(raw))
    return r


def _parse_android_java(raw: str) -> StackInsight:
    r = StackInsight(
        stack_format="android_java",
        platform="android",
        normalized_stack=raw,
        confidence="medium",
    )
    r.frame_count = len(_JAVA_FRAME_RE.findall(raw))
    r.notes.append(f"ProGuard 混淆栈不含版本信息，{_MANUAL_VERSION_NOTE}")
    return r
