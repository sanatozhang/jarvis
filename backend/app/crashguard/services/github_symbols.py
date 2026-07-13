"""
从 GitHub releases 自动下载 Plaud App 符号文件。

标签格式: v{semver}+{build}-{date}-{time}-global
Datadog 版本格式: {semver}-{build}（如 3.18.0-708）

只处理 global flavor（含 cn 的跳过）。
下载文件缓存到 /data/symbols/github_cache/{app_version}/，避免重复下载。
"""
from __future__ import annotations

import asyncio
import logging
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crashguard.github_symbols")

# 同一 (tag, asset) 的并发下载锁：661MB 文件被多 task 并发 stream 写同一 dest
# 会互相 truncate 导致全失败（实战教训 — 102 服务器部署后 N 个 issue 同时触发
# 符号化，所有 download_asset 都返回 4MB 残骸）。按 (tag, asset_name) 复用同一把锁，
# 后到的 task 等前面那个跑完 → 看到完整文件直接复用。
_DOWNLOAD_LOCKS: "dict[tuple[str, str], asyncio.Lock]" = {}
_DOWNLOAD_LOCK_GUARD = asyncio.Lock()


async def _get_download_lock(tag: str, asset_name: str) -> asyncio.Lock:
    async with _DOWNLOAD_LOCK_GUARD:
        key = (tag, asset_name)
        lock = _DOWNLOAD_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _DOWNLOAD_LOCKS[key] = lock
        return lock

# NOTE: 本地 GitHub 符号化是 FLUTTER 期机制（补 Datadog 解不了的 Dart AOT libapp.so 帧）。
# native(4.0) 不走这里：native 发布流水线把 iOS dSYM(UUID)/Android mapping+NDK(build_id)
# 直接传 Datadog，Datadog 服务端已符号化 native 栈；crashguard 读到的 native 栈本就符号化过。
# 下面的默认仓/资产名都是 flutter 约定；native band 的 github_repo 是 no-op 占位（找不到即
# 静默保留原栈）。详见 config.yaml repo_routing 段注释与 memory。
_DEFAULT_REPO = "Plaud-AI/Plaud-App"
_GITHUB_API = "https://api.github.com"

_ASSET_IOS_DSYM = "PLAUD.dSYMs.zip"
_ASSET_ANDROID_MAPPING = "mapping_globalRelease.txt"
_ASSET_DART_SYMBOLS = "flutter_symbols.tar.gz"
_ASSET_ANDROID_NATIVE_SYMBOLS = "native_symbols.tar.gz"  # libflutter.so / libapp.so 带 debug 符号


def _github_token() -> Optional[str]:
    """优先用 `gh auth token`（服务器上已登录的 OAuth token，hosts.yml gho_*，长期
    有效、有 org 权限）；GH_TOKEN/GITHUB_TOKEN env 常是个人 fine-grained PAT，超过
    Plaud-AI org 90 天生命周期策略会被硬拒绝（2026-07-13 实测：release 列表接口全
    403），只作 gh 不可用时的最后兜底。和 pr_drafter/pr_sync/pr_reviewer 里"剥
    GH_TOKEN 走 OAuth"是同一个道理，这里因为走的是 httpx 直连而不是 gh 子进程，
    没法靠剥 env 让 gh 自己接管，只能反过来主动问 gh 要它当前用的 token。
    """
    try:
        import subprocess
        r = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            tok = (r.stdout or "").strip()
            if tok:
                return tok
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


_GITHUB_CACHE_KEEP_VERSIONS = 10  # fallback，优先使用 crashguard config


def _github_cache_dir() -> Path:
    env = os.environ.get("DATA_DIR")
    if env:
        base = Path(env)
    elif os.access("/data", os.W_OK):
        base = Path("/data")
    else:
        base = Path(__file__).resolve().parents[4] / "data"
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


async def find_release_tag(app_version: str, allow_fallback: bool = True, repo: str = _DEFAULT_REPO) -> Optional[str]:
    """
    查找对应 app_version 的 GitHub release tag（仅 global flavor）。

    若精确版本未找到且 allow_fallback=True，回落到最近的 global release。
    底层逻辑：Plaud Android libflutter.so 是 fork engine，多 build 共用同一份；
    libapp.so 每 build 重新 AOT 编译，BuildId 不同。fallback 用最近 release 的 libflutter.so，
    BuildId 仍能匹配，能解出 Dart engine / GC 帧；libapp.so 自然 BuildId 不对会跳过——安全。
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

    latest_global_tag: Optional[str] = None  # fallback 候选

    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            # 最多翻 3 页，每页 100 条，覆盖近 300 个 release
            for page in range(1, 4):
                resp = await client.get(
                    f"{_GITHUB_API}/repos/{repo}/releases",
                    headers=headers,
                    params={"per_page": 100, "page": page},
                )
                resp.raise_for_status()
                releases = resp.json()
                if not releases:
                    break
                for release in releases:
                    tag = release.get("tag_name", "")
                    # 第一个见到的 global tag（API 默认按 published_at desc）作为 fallback
                    if latest_global_tag is None and tag.endswith("-global"):
                        latest_global_tag = tag
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

    # 精确版本未命中：回落到最近 global release（仅当 allow_fallback）
    if allow_fallback and latest_global_tag:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tag_cache.write_text(latest_global_tag)
        logger.info(
            "no exact GitHub release for %s, fallback to latest global %s "
            "(libflutter.so 共用 fork engine 时 BuildId 仍可匹配)",
            app_version, latest_global_tag,
        )
        try:
            from app.crashguard.config import get_crashguard_settings as _gs
            _keep = _gs().github_cache_keep_versions
        except Exception:
            _keep = _GITHUB_CACHE_KEEP_VERSIONS
        _cleanup_github_cache(_keep)
        return latest_global_tag

    return None


async def _download_asset(tag: str, asset_name: str, dest: Path, repo: str = _DEFAULT_REPO) -> Optional[Path]:
    """下载单个 release asset 到 dest。已存在且大小匹配则直接返回；不完整则重下。

    并发安全：同一 (tag, asset_name) 加锁——多 task 同时触发符号化时不再互相
    truncate 同一个 dest 文件（实战根因）。锁内先复检 dest 是否已被前一个 task
    下完，避免重复下载。
    """
    lock = await _get_download_lock(tag, asset_name)
    async with lock:
        token = _github_token()
        headers: dict = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            import httpx
            from urllib.parse import quote
            encoded_tag = quote(tag, safe="")  # `+` 必须编码为 %2B，否则 GitHub API 404
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{_GITHUB_API}/repos/{repo}/releases/tags/{encoded_tag}",
                    headers=headers,
                )
                resp.raise_for_status()
                assets = resp.json().get("assets", [])

            asset = next((a for a in assets if a["name"] == asset_name), None)
            if not asset:
                logger.warning("asset %s not found in release %s", asset_name, tag)
                return None

            expected_size = int(asset.get("size") or 0)
            # 锁内复检：前一个等锁的 task 已经下完整 → 直接复用，不重下
            if dest.exists():
                actual_size = dest.stat().st_size
                if expected_size and actual_size == expected_size:
                    return dest
                logger.warning(
                    "cache %s size mismatch (have %d, expect %d) — re-downloading",
                    dest, actual_size, expected_size,
                )
                dest.unlink(missing_ok=True)

            size_mb = expected_size // 1024 // 1024
            logger.info("downloading %s from %s (%dMB) ...", asset_name, tag, size_mb)

            dest.parent.mkdir(parents=True, exist_ok=True)
            # 私有 repo 必须用 API URL `releases/assets/{id}` + Accept: octet-stream，
            # browser_download_url 对私有 repo 直接 404（GitHub 鉴权策略）
            dl_headers = {**headers, "Accept": "application/octet-stream"}
            asset_api_url = f"{_GITHUB_API}/repos/{repo}/releases/assets/{asset['id']}"

            # 先写 .part，全量写完再 rename → 即使中途崩溃，dest 也不会留下半截垃圾
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.unlink(missing_ok=True)
            async with httpx.AsyncClient(timeout=1800, follow_redirects=True) as client:
                async with client.stream("GET", asset_api_url, headers=dl_headers) as resp:
                    resp.raise_for_status()
                    with open(tmp, "wb") as f:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            f.write(chunk)
            # 大小校验后再 atomic rename
            if expected_size and tmp.stat().st_size != expected_size:
                logger.warning(
                    "download_asset %s size mismatch after stream (got %d expect %d)",
                    asset_name, tmp.stat().st_size, expected_size,
                )
                tmp.unlink(missing_ok=True)
                return None
            tmp.replace(dest)

            logger.info("downloaded %s → %s", asset_name, dest)
            return dest

        except Exception as exc:
            # repr(exc) 比 str(exc) 多带类型名，便于排查空消息异常
            logger.warning("download_asset %s failed: %r", asset_name, exc)
            # 清掉残骸防下次复用脏数据；.part 也清掉
            if dest.exists():
                dest.unlink(missing_ok=True)
            part = dest.with_suffix(dest.suffix + ".part")
            if part.exists():
                part.unlink(missing_ok=True)
            return None


def _tag_cache_dir(tag: str) -> Path:
    """按 GitHub tag（而非 app_version）建 cache 目录，避免 fallback 时重复下载。

    底层逻辑：多个 app_version 可能 fallback 到同一个 release tag（如 3.18.1-715 与
    3.19.102-711 都用 v3.18.0+708-...），按 app_version 分目录会让同一个 661MB 文件
    被存 N 份。按 tag 分目录可让所有 fallback 共享同一份解压结果。
    """
    # tag 含 + 符号在文件系统上合法但易引起 shell 问题，统一替换为 -
    safe = tag.replace("+", "-")
    return _github_cache_dir() / "_by_tag" / safe


async def get_ios_dsyms_dir(app_version: str, repo: str = _DEFAULT_REPO) -> Optional[str]:
    """
    返回 iOS dSYMs 目录路径（含 .dSYM bundles）。
    按 tag 共享 cache：多个 app_version 命中同一 release 时不重复下载/解压。
    """
    tag = await find_release_tag(app_version, repo=repo)
    if not tag:
        return None

    cache_dir = _tag_cache_dir(tag) / "ios"
    marker = cache_dir / ".extracted"
    if marker.exists():
        return str(cache_dir)

    zip_path = cache_dir / _ASSET_IOS_DSYM
    result = await _download_asset(tag, _ASSET_IOS_DSYM, zip_path, repo=repo)
    if not result:
        return None

    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(cache_dir)
        zip_path.unlink(missing_ok=True)
        marker.touch()
        logger.info("iOS dSYMs extracted to %s (tag=%s, shared by app_version=%s)",
                    cache_dir, tag, app_version)
        return str(cache_dir)
    except Exception as exc:
        logger.warning("failed to extract iOS dSYMs: %s", exc)
        return None


async def get_android_mapping(app_version: str, repo: str = _DEFAULT_REPO) -> Optional[str]:
    """
    返回 Android ProGuard mapping 文件路径。按 tag 共享 cache。
    """
    tag = await find_release_tag(app_version, repo=repo)
    if not tag:
        return None

    cache_dir = _tag_cache_dir(tag) / "android"
    dest = cache_dir / _ASSET_ANDROID_MAPPING
    if dest.exists():
        return str(dest)

    result = await _download_asset(tag, _ASSET_ANDROID_MAPPING, dest, repo=repo)
    return str(result) if result else None


async def get_android_native_symbols_dir(app_version: str, repo: str = _DEFAULT_REPO) -> Optional[str]:
    """
    返回 Android native_symbols 目录路径（带 debug 符号的 libflutter.so / libapp.so 等）。
    按 tag 共享 cache：661MB 文件不会被多个 app_version 重复下载。

    这是 Plan C for Android native crash 的关键 — Plaud 自己打包了带符号版本的 .so 文件。
    """
    tag = await find_release_tag(app_version, repo=repo)
    if not tag:
        return None

    cache_dir = _tag_cache_dir(tag) / "native"
    marker = cache_dir / ".extracted"
    if marker.exists():
        return str(cache_dir)

    tar_path = cache_dir / _ASSET_ANDROID_NATIVE_SYMBOLS
    result = await _download_asset(tag, _ASSET_ANDROID_NATIVE_SYMBOLS, tar_path, repo=repo)
    if not result:
        return None

    try:
        # 选择性解压：只保留 global_apk merged_native_libs arm64-v8a 下的
        # libflutter.so 和 libapp.so（占 crash 帧 99%+），其他全丢。
        # 原 661MB tar → 全解 2GB → 仅 arm64 merged 380MB → 仅 flutter+app ~172MB
        #
        # 决策依据：
        #   - libflutter.so 144MB: Dart engine / GC / Skia / Impeller 全在这里
        #   - libapp.so 28MB: Plaud Dart AOT 代码
        #   - 其余 33 个 .so（rive/onnx/avcodec 等）每个独立 BuildId，可能出现在
        #     stack 里但概率 <1%；不保留时这些帧会原样保留（不影响主流分析）
        # 想覆盖更多 .so 时 config 改 android_extract_so_allowlist
        kept = 0
        skipped = 0
        try:
            from app.crashguard.config import get_crashguard_settings as _gs
            allowlist = getattr(_gs(), "android_extract_so_allowlist", None) \
                or ["libflutter.so", "libapp.so"]
        except Exception:
            allowlist = ["libflutter.so", "libapp.so"]

        with tarfile.open(tar_path) as tf:
            members_to_extract = []
            for member in tf.getmembers():
                name = member.name
                if (
                    "global_apk/merged_native_libs" in name
                    and "/arm64-v8a/" in name
                    and any(name.endswith("/" + so) for so in allowlist)
                ):
                    members_to_extract.append(member)
                    kept += 1
                else:
                    skipped += 1
            tf.extractall(cache_dir, members=members_to_extract)
        tar_path.unlink(missing_ok=True)
        marker.touch()
        logger.info(
            "Android native symbols extracted to %s (tag=%s, kept=%d/%d, skipped=%d)",
            cache_dir, tag, kept, kept + skipped, skipped,
        )
        return str(cache_dir)
    except Exception as exc:
        logger.warning("failed to extract Android native symbols: %s", exc)
        return None


async def get_dart_symbols_dir(app_version: str, repo: str = _DEFAULT_REPO) -> Optional[str]:
    """
    返回 Dart debug symbols 目录路径（flutter_symbols.tar.gz 解压后）。按 tag 共享。
    """
    tag = await find_release_tag(app_version, repo=repo)
    if not tag:
        return None

    cache_dir = _tag_cache_dir(tag) / "dart"
    marker = cache_dir / ".extracted"
    if marker.exists():
        return str(cache_dir)

    tar_path = cache_dir / _ASSET_DART_SYMBOLS
    result = await _download_asset(tag, _ASSET_DART_SYMBOLS, tar_path, repo=repo)
    if not result:
        return None

    try:
        with tarfile.open(tar_path) as tf:
            tf.extractall(cache_dir)
        tar_path.unlink(missing_ok=True)
        marker.touch()
        logger.info("Dart symbols extracted to %s (tag=%s)", cache_dir, tag)
        return str(cache_dir)
    except Exception as exc:
        logger.warning("failed to extract Dart symbols: %s", exc)
        return None
