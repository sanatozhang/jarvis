"""源码仓库路由 —— 单一真相源。

输入 (platform, version)，输出 RepoResolution（源码路径 / 子仓 / GitHub 仓 /
符号化 profile / family）。纯函数，零副作用（path_exists 可注入便于测试）。

配置形态见 config.yaml `repo_routing` 段。切换线：3.x=flutter，4.0.0 起=native。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("jarvis.repo_router")

_VER_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


@dataclass
class RepoResolution:
    family: str
    platform: str
    wrapper_path: str
    sub_repo_path: str
    logical_name: str
    github_repo: str
    symbol_profile: str
    confidence: str  # "high" | "low"


def parse_version(v: Optional[str]) -> Optional[tuple[int, int, int]]:
    """'3.16.0-634' → (3,16,0)；无法解析 → None。"""
    if not v:
        return None
    m = _VER_RE.match(str(v))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def normalize_platform(raw: str, os_name: str = "") -> Optional[str]:
    """把 jarvis 的 'app' / crashguard 的 'flutter' 按 os_name 细分到 android/ios；
    web/desktop/android/ios 原样小写。无法归一 → None。"""
    p = (raw or "").strip().lower()
    if p in ("android", "ios", "web", "desktop"):
        return p
    if p in ("app", "flutter"):
        o = (os_name or "").strip().lower()
        if "android" in o:
            return "android"
        if "ios" in o or "iphone" in o or "ipad" in o:
            return "ios"
        return None  # app/flutter 但拿不到 os → 调用方降级
    return None


def select_band(bands: list[dict], version: Optional[str]) -> Optional[tuple[dict, str]]:
    """按 min_version 降序取第一个 version >= min_version 的 band。
    version 缺失 → 最新 band（min_version 最大）+ confidence='low'。"""
    if not bands:
        return None
    ordered = sorted(bands, key=lambda b: parse_version(b.get("min_version", "0")) or (0, 0, 0), reverse=True)
    pv = parse_version(version)
    if pv is None:
        return ordered[0], "low"
    for b in ordered:
        mv = parse_version(b.get("min_version", "0")) or (0, 0, 0)
        if pv >= mv:
            return b, "high"
    # version is parseable but below every band's min_version → unmatched fallback (low confidence)
    return ordered[-1], "low"


def resolve(
    platform: str,
    version: Optional[str],
    routing: dict,
    *,
    sub_hint: str = "",
    stack_text: str = "",
    os_name: str = "",
    path_exists: Callable[[str], bool] = os.path.exists,
) -> Optional[RepoResolution]:
    # sub_hint / stack_text: reserved for later tasks (flutter sub-repo override + crash-text heuristics)
    norm = normalize_platform(platform, os_name=os_name)
    if not norm:
        logger.info("repo_router: cannot normalize platform=%r os=%r", platform, os_name)
        return None
    cfg = routing.get(norm)
    if not cfg or not cfg.get("bands"):
        logger.info("repo_router: platform %s not configured", norm)
        return None
    picked = select_band(cfg["bands"], version)
    if not picked:
        logger.warning("repo_router: select_band returned None for platform=%s version=%s", norm, version)
        return None
    band, confidence = picked

    wrapper = os.path.expanduser(band.get("wrapper", "") or "")
    sub = (band.get("sub", "") or "").strip()
    if not wrapper or not path_exists(wrapper):
        logger.warning("repo_router: wrapper missing for %s: %s", norm, wrapper)
        return None
    if sub:
        sub_path = os.path.join(wrapper, sub)
        logical = sub
    else:
        sub_path = wrapper
        logical = os.path.basename(wrapper.rstrip("/"))
    if not path_exists(sub_path):
        logger.warning("repo_router: sub_repo missing for %s: %s", norm, sub_path)
        return None

    logger.info(
        "repo_router.resolved platform=%s version=%s family=%s repo=%s sub=%s symbol_profile=%s confidence=%s",
        norm, version or "?", band.get("family"), band.get("github_repo"), logical,
        band.get("symbol_profile"), confidence,
    )

    return RepoResolution(
        family=band.get("family", ""),
        platform=norm,
        wrapper_path=wrapper,
        sub_repo_path=sub_path,
        logical_name=logical,
        github_repo=band.get("github_repo", "") or "",
        symbol_profile=band.get("symbol_profile", "none") or "none",
        confidence=confidence,
    )
