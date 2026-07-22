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

# NOTE（2026-07-14 修正）：之前以为 native(4.0) 符号化全部由 Datadog 服务端完成、
# crashguard 读到的栈本就符号化过——today 实测证伪（Android r8-map-id 占位符 / iOS
# 原始地址栈均未解析）。native 符号包实际发布在独立仓 Plaud-AI/plaud-native-app 的
# Release assets 里（tag 格式与 flutter 一致：v{semver}+{build}-{date}-{time}-global），
# 资产名 Android 侧（mapping_globalRelease.txt / native_symbols.tar.gz）与 flutter 相同，
# iOS 侧不同（native 是 Plaud-Global.dSYMs.zip，见 _ASSET_IOS_DSYM_NATIVE）。
# repo_routing 里 native band 的 github_repo 已指到 plaud-native-app（见 config.yaml）。
_DEFAULT_REPO = "Plaud-AI/Plaud-App"
_GITHUB_API = "https://api.github.com"

_ASSET_IOS_DSYM = "PLAUD.dSYMs.zip"
_ASSET_IOS_DSYM_NATIVE = "Plaud-Global.dSYMs.zip"
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

    2026-07-20 修复：上面这段调 `gh auth token` 子进程时忘了剥离
    GH_TOKEN/GITHUB_TOKEN env——`gh` 二进制本身会尊重这两个 env var，于是又把
    过期 PAT 取了回来，102 上实测所有符号包下载全 403。和其余 3 个 gh 子进程调用
    点（pr_drafter._github_open_crashguard_pr / pr_reviewer.fetch_pr_diff_via_gh /
    check_review_status_from_gh）保持同款处理：调用前从子进程 env 里剥掉这两个 key。
    """
    try:
        import subprocess
        sub_env = dict(os.environ)
        for k in ("GH_TOKEN", "GITHUB_TOKEN"):
            sub_env.pop(k, None)
        r = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10,
            env=sub_env,
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


# ── 已上传符号包查找（打包机上传优先，GitHub 兜底）───────────────────────────
# 与 api/crash.py::upload_symbol_package 的落盘路径保持完全一致：
# <DATA_DIR>/symbols/<platform>/<symbol_type>/<app_version>/<原始文件名>

_EXTRACT_LOCKS: "dict[tuple[str, str, str], asyncio.Lock]" = {}
_EXTRACT_LOCK_GUARD = asyncio.Lock()


def _uploaded_symbols_root() -> Path:
    """与 api/crash.py::upload_symbol_package 的 dest_dir 解析方式保持一致
    （同样直接用 DATA_DIR 环境变量，默认 /data，不做额外可写性探测）。"""
    return Path(os.environ.get("DATA_DIR", "/data")) / "symbols"


def _uploaded_package_dir(platform: str, symbol_type: str, app_version: str) -> Optional[Path]:
    """精确匹配 (platform, symbol_type, app_version) 的已上传包目录。

    只做精确字符串匹配，不做模糊/最近版本回退——查不到就让调用方原样走 GitHub 逻辑
    （GitHub 那条路径自己已有 fallback，这里重蹈"错误 dSYM 硬套"覆辙的代价太高）。
    """
    d = _uploaded_symbols_root() / platform / symbol_type / app_version
    if not d.exists() or not d.is_dir():
        return None
    if not any(d.iterdir()):
        return None
    return d


async def _get_extract_lock(platform: str, symbol_type: str, app_version: str) -> asyncio.Lock:
    """按 (platform, symbol_type, app_version) 复用同一把锁，防止并发解压同一个上传包
    互相踩踏（同款模式见 _get_download_lock，用于 GitHub 下载侧）。"""
    async with _EXTRACT_LOCK_GUARD:
        key = (platform, symbol_type, app_version)
        lock = _EXTRACT_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _EXTRACT_LOCKS[key] = lock
        return lock


async def _find_uploaded_ios_dsyms_dir(app_version: str) -> Optional[str]:
    """查已上传的 iOS dSYM 包（platform=ios, symbol_type=dsym），精确 app_version 匹配。

    上传目录里通常是原始 zip（首次使用时解压到 .extracted/ 子目录，marker 文件标记，
    之后直接返回缓存目录，不重复解压）；也兼容"目录里已经是解压后的 .dSYM bundle"的
    情况（万一未来上传接口改成直接存目录）。按 (platform, symbol_type, app_version)
    加锁防止并发解压互相踩踏。
    """
    src_dir = _uploaded_package_dir("ios", "dsym", app_version)
    if not src_dir:
        return None

    extracted_dir = src_dir / ".extracted"
    marker = extracted_dir / ".done"
    if marker.exists():
        return str(extracted_dir)

    zips = list(src_dir.glob("*.zip"))
    if not zips:
        if any(src_dir.rglob("*.dSYM")):
            return str(src_dir)
        return None

    lock = await _get_extract_lock("ios", "dsym", app_version)
    async with lock:
        if marker.exists():  # 锁内复检：等锁期间可能已被前一个 task 解压完
            return str(extracted_dir)
        try:
            extracted_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zips[0]) as zf:
                zf.extractall(extracted_dir)
            marker.touch()
            logger.info(
                "uploaded iOS dSYMs extracted to %s (app_version=%s)", extracted_dir, app_version,
            )
            return str(extracted_dir)
        except Exception as exc:
            logger.warning("failed to extract uploaded iOS dSYMs for %s: %s", app_version, exc)
            return None


async def get_ios_dsyms_dir(
    app_version: str, repo: str = _DEFAULT_REPO, asset_name: str = _ASSET_IOS_DSYM,
) -> Optional[str]:
    """
    返回 iOS dSYMs 目录路径（含 .dSYM bundles）。
    优先查打包机已上传的包（精确 app_version 匹配），查不到再走 GitHub release 下载
    （按 tag 共享 cache：多个 app_version 命中同一 release 时不重复下载/解压）。

    asset_name：flutter 用 PLAUD.dSYMs.zip，native 用 Plaud-Global.dSYMs.zip
    （见 _ASSET_IOS_DSYM_NATIVE），由调用方按 symbol_profile 选择。
    """
    uploaded = await _find_uploaded_ios_dsyms_dir(app_version)
    if uploaded:
        return uploaded

    tag = await find_release_tag(app_version, repo=repo)
    if not tag:
        return None

    # 不同 asset_name 解压到不同子目录，避免 flutter/native 复用同一 tag 时互相覆盖
    cache_dir = _tag_cache_dir(tag) / "ios" / asset_name
    marker = cache_dir / ".extracted"
    if marker.exists():
        return str(cache_dir)

    zip_path = cache_dir / asset_name
    result = await _download_asset(tag, asset_name, zip_path, repo=repo)
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


def _find_uploaded_android_mapping(app_version: str) -> Optional[str]:
    """查已上传的 Android ProGuard mapping（platform=android, symbol_type=proguard_mapping）。

    上传的就是原始 .txt，找到该目录下第一个 .txt 文件直接返回路径，不需要解压/加锁。
    """
    src_dir = _uploaded_package_dir("android", "proguard_mapping", app_version)
    if not src_dir:
        return None
    txts = sorted(src_dir.glob("*.txt"))
    return str(txts[0]) if txts else None


async def get_android_mapping(app_version: str, repo: str = _DEFAULT_REPO) -> Optional[str]:
    """
    返回 Android ProGuard mapping 文件路径。
    优先查打包机已上传的包（精确 app_version 匹配），查不到再走 GitHub（按 tag 共享 cache）。
    """
    uploaded = _find_uploaded_android_mapping(app_version)
    if uploaded:
        return uploaded

    tag = await find_release_tag(app_version, repo=repo)
    if not tag:
        return None

    cache_dir = _tag_cache_dir(tag) / "android"
    dest = cache_dir / _ASSET_ANDROID_MAPPING
    if dest.exists():
        return str(dest)

    result = await _download_asset(tag, _ASSET_ANDROID_MAPPING, dest, repo=repo)
    return str(result) if result else None


def _is_native_lib_tar_member(name: str, allowlist: list) -> bool:
    """挑出 native_symbols.tar.gz 里带 debug 符号的 arm64 .so。

    flutter 打包路径带一层 global_apk 前缀（global_apk/merged_native_libs/...），
    native(4.0) 打包脚本没有这层（merged_native_libs/globalRelease/
    mergeGlobalReleaseNativeLibs/out/lib/arm64-v8a/...）——只认 "merged_native_libs"
    子串，两种布局都能命中；同 tar 里还有 stripped_native_libs（release 产物，
    已 strip 掉 debug_info），子串不同不会被误选中。
    """
    return (
        "merged_native_libs" in name
        and "/arm64-v8a/" in name
        and any(name.endswith("/" + so) for so in allowlist)
    )


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
                if _is_native_lib_tar_member(member.name, allowlist):
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


async def _find_uploaded_dart_symbols_dir(app_version: str) -> Optional[str]:
    """查已上传的 Dart debug symbols 包（platform=flutter, symbol_type=dart_symbols）。

    比照 iOS dSYM 的 tar.gz 解压模式：首次使用时解压到 .extracted/，marker 标记后
    复用；按 (platform, symbol_type, app_version) 加锁防并发解压互相踩踏。
    """
    src_dir = _uploaded_package_dir("flutter", "dart_symbols", app_version)
    if not src_dir:
        return None

    extracted_dir = src_dir / ".extracted"
    marker = extracted_dir / ".done"
    if marker.exists():
        return str(extracted_dir)

    tars = list(src_dir.glob("*.tar.gz")) or list(src_dir.glob("*.tgz"))
    if not tars:
        if any(src_dir.rglob("*.symbols")):
            return str(src_dir)
        return None

    lock = await _get_extract_lock("flutter", "dart_symbols", app_version)
    async with lock:
        if marker.exists():
            return str(extracted_dir)
        try:
            extracted_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tars[0]) as tf:
                tf.extractall(extracted_dir)
            marker.touch()
            logger.info(
                "uploaded Dart symbols extracted to %s (app_version=%s)", extracted_dir, app_version,
            )
            return str(extracted_dir)
        except Exception as exc:
            logger.warning("failed to extract uploaded Dart symbols for %s: %s", app_version, exc)
            return None


async def get_dart_symbols_dir(app_version: str, repo: str = _DEFAULT_REPO) -> Optional[str]:
    """
    返回 Dart debug symbols 目录路径（flutter_symbols.tar.gz 解压后）。
    优先查打包机已上传的包（精确 app_version 匹配），查不到再走 GitHub（按 tag 共享）。
    """
    uploaded = await _find_uploaded_dart_symbols_dir(app_version)
    if uploaded:
        return uploaded

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
