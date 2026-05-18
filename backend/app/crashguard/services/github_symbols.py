"""
从 GitHub releases 自动下载 Plaud App 符号文件。

标签格式: v{semver}+{build}-{date}-{time}-global
Datadog 版本格式: {semver}-{build}（如 3.18.0-708）

只处理 global flavor（含 cn 的跳过）。
下载文件缓存到 /data/symbols/github_cache/{app_version}/，避免重复下载。
"""
from __future__ import annotations

import logging
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crashguard.github_symbols")

_REPO = "Plaud-AI/Plaud-App"
_GITHUB_API = "https://api.github.com"

_ASSET_IOS_DSYM = "PLAUD.dSYMs.zip"
_ASSET_ANDROID_MAPPING = "mapping_globalRelease.txt"
_ASSET_DART_SYMBOLS = "flutter_symbols.tar.gz"
_ASSET_ANDROID_NATIVE_SYMBOLS = "native_symbols.tar.gz"  # libflutter.so / libapp.so 带 debug 符号


def _github_token() -> Optional[str]:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


_GITHUB_CACHE_KEEP_VERSIONS = 10  # fallback，优先使用 crashguard config


def _github_cache_dir() -> Path:
    base = Path(os.environ.get("DATA_DIR", "/data"))
    p = base / "symbols" / "github_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cleanup_github_cache(keep: int = _GITHUB_CACHE_KEEP_VERSIONS) -> None:
    """按 mtime 保留最新 keep 个版本目录，删除多余的。"""
    cache_dir = _github_cache_dir()
    version_dirs = [d for d in cache_dir.iterdir() if d.is_dir()]
    if len(version_dirs) <= keep:
        return
    # 按目录最后修改时间降序，保留最新的
    version_dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    to_remove = version_dirs[keep:]
    for d in to_remove:
        try:
            import shutil as _shutil
            _shutil.rmtree(d)
            logger.info("github_cache: removed old version dir %s", d.name)
        except Exception as exc:
            logger.warning("github_cache: failed to remove %s: %s", d, exc)


def _version_to_tag_prefix(app_version: str) -> Optional[str]:
    """3.18.0-708 → 'v3.18.0+708-'"""
    parts = app_version.rsplit("-", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    return f"v{parts[0]}+{parts[1]}-"


async def find_release_tag(app_version: str) -> Optional[str]:
    """
    查找对应 app_version 的 GitHub release tag（仅 global flavor）。
    结果缓存到本地，避免每次调 API。
    """
    prefix = _version_to_tag_prefix(app_version)
    if not prefix:
        return None

    cache_dir = _github_cache_dir() / app_version
    tag_cache = cache_dir / ".release_tag"
    if tag_cache.exists():
        cached = tag_cache.read_text().strip()
        if cached:
            return cached

    token = _github_token()
    headers: dict = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            # 最多翻 3 页，每页 100 条，覆盖近 300 个 release
            for page in range(1, 4):
                resp = await client.get(
                    f"{_GITHUB_API}/repos/{_REPO}/releases",
                    headers=headers,
                    params={"per_page": 100, "page": page},
                )
                resp.raise_for_status()
                releases = resp.json()
                if not releases:
                    break
                for release in releases:
                    tag = release.get("tag_name", "")
                    if tag.startswith(prefix) and tag.endswith("-global"):
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        tag_cache.write_text(tag)
                        logger.info("found GitHub release %s for version %s", tag, app_version)
                        try:
                            from app.crashguard.config import get_crashguard_settings as _gs
                            _keep = _gs().github_cache_keep_versions
                        except Exception:
                            _keep = _GITHUB_CACHE_KEEP_VERSIONS
                        _cleanup_github_cache(_keep)
                        return tag
    except Exception as exc:
        logger.warning("find_release_tag failed for %s: %s", app_version, exc)

    return None


async def _download_asset(tag: str, asset_name: str, dest: Path) -> Optional[Path]:
    """下载单个 release asset 到 dest。已存在则直接返回。"""
    if dest.exists():
        return dest

    token = _github_token()
    headers: dict = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GITHUB_API}/repos/{_REPO}/releases/tags/{tag}",
                headers=headers,
            )
            resp.raise_for_status()
            assets = resp.json().get("assets", [])

        asset = next((a for a in assets if a["name"] == asset_name), None)
        if not asset:
            logger.warning("asset %s not found in release %s", asset_name, tag)
            return None

        size_mb = asset["size"] // 1024 // 1024
        logger.info("downloading %s from %s (%dMB) ...", asset_name, tag, size_mb)

        dest.parent.mkdir(parents=True, exist_ok=True)
        dl_headers = {**headers, "Accept": "application/octet-stream"}

        async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:
            async with client.stream("GET", asset["browser_download_url"], headers=dl_headers) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        f.write(chunk)

        logger.info("downloaded %s → %s", asset_name, dest)
        return dest

    except Exception as exc:
        logger.warning("download_asset %s failed: %s", asset_name, exc)
        if dest.exists():
            dest.unlink(missing_ok=True)
        return None


async def get_ios_dsyms_dir(app_version: str) -> Optional[str]:
    """
    返回 iOS dSYMs 目录路径（含 .dSYM bundles）。
    第一次调用从 GitHub release 下载 PLAUD.dSYMs.zip 并解压，后续走缓存。
    """
    cache_dir = _github_cache_dir() / app_version / "ios"
    marker = cache_dir / ".extracted"
    if marker.exists():
        return str(cache_dir)

    tag = await find_release_tag(app_version)
    if not tag:
        return None

    zip_path = cache_dir / _ASSET_IOS_DSYM
    result = await _download_asset(tag, _ASSET_IOS_DSYM, zip_path)
    if not result:
        return None

    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(cache_dir)
        zip_path.unlink(missing_ok=True)
        marker.touch()
        logger.info("iOS dSYMs extracted to %s", cache_dir)
        return str(cache_dir)
    except Exception as exc:
        logger.warning("failed to extract iOS dSYMs: %s", exc)
        return None


async def get_android_mapping(app_version: str) -> Optional[str]:
    """
    返回 Android ProGuard mapping 文件路径。
    第一次调用从 GitHub release 下载，后续走缓存。
    """
    cache_dir = _github_cache_dir() / app_version / "android"
    dest = cache_dir / _ASSET_ANDROID_MAPPING
    if dest.exists():
        return str(dest)

    tag = await find_release_tag(app_version)
    if not tag:
        return None

    result = await _download_asset(tag, _ASSET_ANDROID_MAPPING, dest)
    return str(result) if result else None


async def get_android_native_symbols_dir(app_version: str) -> Optional[str]:
    """
    返回 Android native_symbols 目录路径（带 debug 符号的 libflutter.so / libapp.so 等）。
    第一次调用从 GitHub release 下载 native_symbols.tar.gz 并解压（~661MB），后续走缓存。

    这是 Plan C for Android native crash 的关键 — Plaud 自己打包了带符号版本的 .so 文件。
    """
    cache_dir = _github_cache_dir() / app_version / "native"
    marker = cache_dir / ".extracted"
    if marker.exists():
        return str(cache_dir)

    tag = await find_release_tag(app_version)
    if not tag:
        return None

    tar_path = cache_dir / _ASSET_ANDROID_NATIVE_SYMBOLS
    result = await _download_asset(tag, _ASSET_ANDROID_NATIVE_SYMBOLS, tar_path)
    if not result:
        return None

    try:
        with tarfile.open(tar_path) as tf:
            tf.extractall(cache_dir)
        tar_path.unlink(missing_ok=True)
        marker.touch()
        logger.info("Android native symbols extracted to %s", cache_dir)
        return str(cache_dir)
    except Exception as exc:
        logger.warning("failed to extract Android native symbols: %s", exc)
        return None


async def get_dart_symbols_dir(app_version: str) -> Optional[str]:
    """
    返回 Dart debug symbols 目录路径（flutter_symbols.tar.gz 解压后）。
    第一次调用从 GitHub release 下载，后续走缓存。
    """
    cache_dir = _github_cache_dir() / app_version / "dart"
    marker = cache_dir / ".extracted"
    if marker.exists():
        return str(cache_dir)

    tag = await find_release_tag(app_version)
    if not tag:
        return None

    tar_path = cache_dir / _ASSET_DART_SYMBOLS
    result = await _download_asset(tag, _ASSET_DART_SYMBOLS, tar_path)
    if not result:
        return None

    try:
        with tarfile.open(tar_path) as tf:
            tf.extractall(cache_dir)
        tar_path.unlink(missing_ok=True)
        marker.touch()
        logger.info("Dart symbols extracted to %s", cache_dir)
        return str(cache_dir)
    except Exception as exc:
        logger.warning("failed to extract Dart symbols: %s", exc)
        return None
