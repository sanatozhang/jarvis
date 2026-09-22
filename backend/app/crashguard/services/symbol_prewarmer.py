"""符号包预热（2026-09-22）。

**目标**：让 QA 查刚发的灰度包时永远命中 `cached`，而不是等 1–3 分钟下载
~90MB 的 dSYM。按 graygate 人工指定的「主要版本」提前把符号包拉到本地缓存。

## 依赖方向：crashguard → graygate（单向）

graygate 是独立子模块，不应该知道 crashguard 存在。所以由 crashguard 主动读
`graygate.services.focus_version.get_all_focus_versions()`，**graygate 零改动**。

## 必须走 job queue，不能直接 await

`workers/scheduler.py` 的 `_tick_once()` 是 60s 单线程顺序 await（见该文件
line 161 注释：一个长任务会拖垮整个 loop）。预热要下 90MB，所以调用方必须用
`_enqueue_job()` 把它丢进串行 worker 队列。

## 幂等

preflight 判为 `cached` 就跳过，不重复下载。否则每 30 分钟重下 90MB。

## 与 keep_versions 的相互作用

预热会主动占满 `github_cache` 名额。`_cleanup_github_cache` 按 mtime 淘汰，
预热刚下的是最新的不会被自己挤掉，但**会加速老版本淘汰** —— 这正是
`keep_versions` 从 10 提到 20 的实际价值（预热占掉的名额需要更大的 keep
才不挤掉研发正在回查的老版本）。两个改动是互补的。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("jarvis.crashguard.symbol_prewarmer")


async def _get_focus_versions() -> dict:
    """读 graygate 人工指定的「主要版本」→ {"ios": str|None, "android": str|None}。"""
    from app.graygate.services.focus_version import get_all_focus_versions

    return await get_all_focus_versions()


async def _preflight(platform: str, app_version: str, **kw) -> dict:
    from app.crashguard.services.symbol_catalog import preflight_symbols

    return await preflight_symbols(platform, app_version, **kw)


def _resolve_routing(platform: str, app_version: str) -> tuple:
    """(symbol_profile, github_repo)。

    path_exists=lambda _p: True 是必需的 —— resolve() 默认校验源码 wrapper 目录，
    容器内裸机路径不存在会返回 None，导致 symbol_profile 丢失、iOS 下错误的
    dSYM 资产（详见 docs/crashguard/symbolication.md）。
    """
    from app.config import get_repo_routing
    from app.services import repo_router

    res = repo_router.resolve(
        platform, app_version, get_repo_routing(), path_exists=lambda _p: True,
    )
    if res is None:
        return "", ""
    return res.symbol_profile, res.github_repo


async def _download_symbols(
    platform: str, app_version: str, symbol_profile: str, github_repo: str,
) -> None:
    """按 symbol_profile 只调该调的 getter。

    盲调四个 getter 是纯浪费：native_android 没有 Dart，flutter_ios 用的是
    PLAUD.dSYMs.zip 而非 native 的 Plaud-Global.dSYMs.zip（2026-07-14 实测
    确认两者资产名不同，套错会下到错误的 dSYM）。
    """
    from app.crashguard.services import github_symbols as gs
    from app.crashguard.services.symbolication import _profile_strategy

    repo = github_repo or gs._DEFAULT_REPO
    st = _profile_strategy(symbol_profile)

    if st.get("use_flutter_dsym") or st.get("use_app_dsym"):
        asset = (
            gs._ASSET_IOS_DSYM_NATIVE if st.get("use_app_dsym")
            else gs._ASSET_IOS_DSYM
        )
        await gs.get_ios_dsyms_dir(app_version, repo=repo, asset_name=asset)
    if st.get("use_native_so"):
        await gs.get_android_native_symbols_dir(app_version, repo=repo)
    if st.get("use_dart_symbols"):
        await gs.get_dart_symbols_dir(app_version, repo=repo)
    if st.get("use_proguard"):
        await gs.get_android_mapping(app_version, repo=repo)


async def prewarm_focus_versions() -> dict:
    """为 graygate 的主要版本预热符号包。

    Returns:
        {"prewarmed": [...], "skipped": [...], "missing": [...],
         "errors": [...], "duration_ms": int}
        每项形如 "ios:4.0.201-941"。
    """
    start = time.monotonic()
    prewarmed: list = []
    skipped: list = []
    missing: list = []
    errors: list = []

    try:
        focus = (await _get_focus_versions()) or {}
    except Exception as exc:
        logger.warning("prewarm: read focus_version failed: %s", exc)
        return {
            "prewarmed": [], "skipped": [], "missing": [],
            "errors": [f"focus_version 读取失败：{exc}"],
            "duration_ms": int((time.monotonic() - start) * 1000),
        }

    for platform in ("ios", "android"):
        version = (focus.get(platform) or "").strip()
        if not version:
            continue
        tag = f"{platform}:{version}"
        try:
            profile, repo = _resolve_routing(platform, version)
            pf = await _preflight(
                platform, version, symbol_profile=profile, github_repo=repo,
            )
            status = pf.get("status")
            if status == "cached":
                skipped.append(tag)
                continue
            if status == "missing":
                # 灰度包未上传符号包是常态，告警会变噪音 —— 只记日志
                logger.info(
                    "prewarm: %s has no symbols (%s) — 灰度包未传符号包是常态，不告警",
                    tag, (pf.get("suggestions") or {}).get("reason", ""),
                )
                missing.append(tag)
                continue
            await _download_symbols(platform, version, profile, repo)
            prewarmed.append(tag)
            logger.info("prewarm: %s symbols downloaded", tag)
        except Exception as exc:
            logger.warning("prewarm: %s failed: %s", tag, exc)
            errors.append(f"{tag}: {exc}")

    return {
        "prewarmed": prewarmed, "skipped": skipped, "missing": missing,
        "errors": errors, "duration_ms": int((time.monotonic() - start) * 1000),
    }
