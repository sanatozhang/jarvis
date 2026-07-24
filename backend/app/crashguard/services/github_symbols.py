"""
从 GitHub releases 自动下载 Plaud App 符号文件。

标签格式: v{semver}+{build}-{date}-{time}-global
Datadog 版本格式: {semver}-{build}（如 3.18.0-708）

只处理 global flavor（含 cn 的跳过）。
下载文件缓存到 /data/symbols/github_cache/{app_version}/，避免重复下载。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import plistlib
import re
import struct
import tarfile
import zipfile
import zlib
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


# ── native(4.0) 按真实 build 号精确匹配 GitHub release（2026-07-24）─────────────
#
# 背景：native app 仓库（Plaud-AI/plaud-native-app）的 tag 后缀 `+NNN` 不是真实
# app build 号（几十个 build 才冻结换一次，如 +813/+910/+999），不能像 flutter 仓库
# 那样直接拿 tag 后缀当 build 号用（_version_to_tag_prefix 那套）。真实 build 号
# （单调递增，如 908→914→…→950→952）要么在 Android .aab 资产名里，要么在 dSYM 包
# 自带的 Contents/Info.plist 的 CFBundleVersion 里——iOS 与 Android 在同一个
# release 内共用同一个真实 build 号（已用 gh api + 本地 atos 交叉验证）。

_AAB_NAME_RE = re.compile(r"^PLAUD_v[\d.]+_(\d+)_.*\.aab$")
_DSYM_INFO_PLIST_RE = re.compile(r"\.app\.dSYM/Contents/Info\.plist$")
_EOCD_SIGNATURE = b"PK\x05\x06"
_CD_SIGNATURE = b"PK\x01\x02"
_LOCAL_SIGNATURE = b"PK\x03\x04"


def _parse_semver_build(app_version: str) -> Optional["tuple[str, str]"]:
    """'4.0.100-950' → ('4.0.100', '950')。

    Android jank 的 symbol_key 是 build_id UUID（如 '196bae40-…-e2dc315b3bbb'），
    最后一段含十六进制字母，`.isdigit()` 天然为 False → 返回 None，不会被误当成
    build 号进入匹配逻辑（与 _version_to_tag_prefix 用同一套判定，保持一致）。
    """
    if not app_version:
        return None
    parts = app_version.rsplit("-", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    return parts[0], parts[1]


def _aab_build_from_assets(assets: list) -> Optional[str]:
    """从 release 的 assets[].name 里抠出 Android .aab 文件名自带的真实 build 号。

    零额外请求——release 列表接口本身已经带 assets，这是最便宜的 build 号来源。
    但不是每个 release 都有这个资产（实测 3/18 个 release 没有 aab），没有就交给
    调用方去尝试更贵的 dSYM Info.plist Range 读取路径。
    """
    for asset in assets or []:
        name = asset.get("name", "") if isinstance(asset, dict) else ""
        m = _AAB_NAME_RE.match(name)
        if m:
            return m.group(1)
    return None


async def _http_range_get(client, url: str, headers: dict, start: int, end: int) -> Optional[bytes]:
    """GET 一段字节范围 [start, end]（闭区间）。

    用 streaming 请求：服务端返回 206 才读 body；返回 200（不支持 Range，会给整个
    文件）时立刻中止连接、不读 body，返回 None——绝不整包下载兜底，这是本模块的
    硬约束（相比整包省 4 个数量级流量）。
    """
    range_headers = {**headers, "Range": f"bytes={start}-{end}"}
    async with client.stream("GET", url, headers=range_headers) as resp:
        if resp.status_code == 200:
            await resp.aclose()
            return None
        if resp.status_code != 206:
            return None
        return await resp.aread()


async def _read_dsym_build_via_range(
    asset_id, asset_size: int, repo: str, headers: dict,
) -> Optional[str]:
    """对 dSYM zip 做标准 HTTP Range 读，只取出 `*.app.dSYM/Contents/Info.plist`
    这一个 zip member（653 字节级别），不下载整个 ~90MB 包。

    步骤：① 末尾一段找 EOCD + 中央目录（entries 不多时通常一次覆盖，覆盖不到再单独
    Range 取中央目录）② 在中央目录里定位目标 member 的 local header 偏移/压缩大小
    ③ 一次 Range 读出 local header + 压缩数据，本地 zlib 解压 + plistlib 解析拿
    CFBundleVersion。任一步失败或服务端不支持 Range → 干净返回 None。
    """
    if not asset_id or not asset_size or asset_size <= 0:
        return None
    try:
        import httpx
        asset_api_url = f"{_GITHUB_API}/repos/{repo}/releases/assets/{asset_id}"
        dl_headers = {**headers, "Accept": "application/octet-stream"}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            tail_size = min(65536, asset_size)
            tail_start = asset_size - tail_size
            tail = await _http_range_get(client, asset_api_url, dl_headers, tail_start, asset_size - 1)
            if not tail:
                return None

            eocd_idx = tail.rfind(_EOCD_SIGNATURE)
            if eocd_idx == -1:
                return None
            eocd = tail[eocd_idx:eocd_idx + 22]
            if len(eocd) < 22:
                return None
            cd_size, cd_offset = struct.unpack("<II", eocd[12:20])

            if cd_offset >= tail_start:
                cd_bytes = tail[cd_offset - tail_start: cd_offset - tail_start + cd_size]
            else:
                cd_bytes = await _http_range_get(
                    client, asset_api_url, dl_headers, cd_offset, cd_offset + cd_size - 1,
                )
                if not cd_bytes:
                    return None

            pos = 0
            target: Optional[dict] = None
            while pos + 46 <= len(cd_bytes):
                if cd_bytes[pos:pos + 4] != _CD_SIGNATURE:
                    break
                (compression,) = struct.unpack("<H", cd_bytes[pos + 10:pos + 12])
                (compressed_size,) = struct.unpack("<I", cd_bytes[pos + 20:pos + 24])
                name_len, extra_len, comment_len = struct.unpack("<HHH", cd_bytes[pos + 28:pos + 34])
                (local_header_offset,) = struct.unpack("<I", cd_bytes[pos + 42:pos + 46])
                name_start = pos + 46
                name = cd_bytes[name_start:name_start + name_len].decode("utf-8", errors="replace")
                if _DSYM_INFO_PLIST_RE.search(name):
                    target = {
                        "compression": compression,
                        "compressed_size": compressed_size,
                        "local_header_offset": local_header_offset,
                        "name_len": name_len,
                    }
                    break
                pos = name_start + name_len + extra_len + comment_len

            if not target:
                return None

            lh_offset = target["local_header_offset"]
            # 一次 Range 读出 local header（固定 30 字节）+ filename + extra（用中央目录
            # 的 name_len 做基准，extra 段给 256 字节安全余量，不够就放弃，不二次整包兜底）
            margin = 256
            read_len = 30 + target["name_len"] + margin + target["compressed_size"]
            local_and_data = await _http_range_get(
                client, asset_api_url, dl_headers, lh_offset, lh_offset + read_len - 1,
            )
            if not local_and_data or len(local_and_data) < 30 or local_and_data[:4] != _LOCAL_SIGNATURE:
                return None
            lh_name_len, lh_extra_len = struct.unpack("<HH", local_and_data[26:30])
            data_start = 30 + lh_name_len + lh_extra_len
            data_end = data_start + target["compressed_size"]
            if data_end > len(local_and_data):
                return None
            raw = local_and_data[data_start:data_end]

            if target["compression"] == 0:
                plist_bytes = raw
            elif target["compression"] == 8:
                try:
                    plist_bytes = zlib.decompressobj(-15).decompress(raw)
                except Exception:
                    return None
            else:
                return None

            try:
                plist = plistlib.loads(plist_bytes)
            except Exception:
                return None
            build = plist.get("CFBundleVersion")
            return str(build) if build else None
    except Exception as exc:
        logger.warning("_read_dsym_build_via_range failed for asset %s: %r", asset_id, exc)
        return None


async def _resolve_release_build(release: dict, repo: str, headers: dict) -> Optional[str]:
    """解析一个 release 的真实 app build 号：优先免费的 aab 文件名，没有再 Range 读 dSYM。"""
    assets = release.get("assets") or []
    build = _aab_build_from_assets(assets)
    if build:
        return build
    dsym_asset = next((a for a in assets if a.get("name") == _ASSET_IOS_DSYM_NATIVE), None)
    if not dsym_asset:
        return None
    return await _read_dsym_build_via_range(
        dsym_asset.get("id"), int(dsym_asset.get("size") or 0), repo, headers,
    )


def _build_index_path(repo: str) -> Path:
    safe = repo.replace("/", "_")
    p = _github_cache_dir() / "_build_index"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{safe}.json"


def _load_build_index(repo: str) -> dict:
    path = _build_index_path(repo)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_build_index(repo: str, index: dict) -> None:
    try:
        _build_index_path(repo).write_text(json.dumps(index))
    except Exception as exc:
        logger.warning("failed to persist build index for %s: %r", repo, exc)


def _dsym_bundle_build(dsym_dir: "Path | str") -> Optional[str]:
    """从解压后的 dSYM 目录里找 `*.app.dSYM/Contents/Info.plist`，读 CFBundleVersion。

    用作解压后的运行期校验闸门：把"iOS/Android 同 release 共用 build"这条实证假设
    变成硬校验，任何反例都安全退化为 miss，绝不给错误符号（见 2026-07-23 生产实测：
    错 build 的 dSYM 会静默解出一个"看起来合理但完全错误"的符号）。
    """
    try:
        for plist_path in Path(dsym_dir).rglob("Contents/Info.plist"):
            bundle_dir = plist_path.parent.parent
            if not bundle_dir.name.endswith(".app.dSYM"):
                continue
            try:
                with open(plist_path, "rb") as f:
                    plist = plistlib.load(f)
                build = plist.get("CFBundleVersion")
                if build:
                    return str(build)
            except Exception:
                continue
        return None
    except Exception as exc:
        logger.warning("_dsym_bundle_build failed for %s: %r", dsym_dir, exc)
        return None


async def _find_release_tag_by_build(app_version: str, repo: str) -> Optional[str]:
    """按真实 app build 号（而非 tag 后缀）精确匹配 GitHub release。

    永远不做跨 build 回落——找不到就干净返回 None（如 4.0.201-951：该 semver 系列
    在 GitHub 上真实 build 上限只到 917，是打包侧真实 gap，不该被误判成"能符号化"）。
    命中写 `.release_tag` 正向缓存；**不缓存 miss**（避免"还没发布"的 build 以后
    补发了也被永久钉死）。每个 release 的 build 解析结果另写永久 `_build_index`
    缓存，避免重复 API/Range 请求。
    """
    parsed = _parse_semver_build(app_version)
    if not parsed:
        return None
    semver, target_build = parsed

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

    build_index = _load_build_index(repo)
    index_dirty = False
    tag_prefix = f"v{semver}+"

    def _flush_index() -> None:
        if index_dirty:
            _save_build_index(repo, build_index)

    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
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
                    if not tag.startswith(tag_prefix) or not tag.endswith("-global"):
                        continue
                    release_build = build_index.get(tag)
                    if release_build is None:
                        release_build = await _resolve_release_build(release, repo, headers)
                        if release_build:
                            build_index[tag] = release_build
                            index_dirty = True
                    if release_build == target_build:
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        tag_cache.write_text(tag)
                        logger.info(
                            "found GitHub release %s for version %s via build match (build=%s)",
                            tag, app_version, target_build,
                        )
                        _flush_index()
                        try:
                            from app.crashguard.config import get_crashguard_settings as _gs
                            _keep = _gs().github_cache_keep_versions
                        except Exception:
                            _keep = _GITHUB_CACHE_KEEP_VERSIONS
                        _cleanup_github_cache(_keep)
                        return tag
    except Exception as exc:
        logger.warning("find_release_tag(match_by_build) failed for %s: %s", app_version, exc)
        _flush_index()
        return None

    _flush_index()
    return None


async def find_release_tag(
    app_version: str,
    allow_fallback: bool = True,
    repo: str = _DEFAULT_REPO,
    match_by_build: bool = False,
) -> Optional[str]:
    """
    查找对应 app_version 的 GitHub release tag（仅 global flavor）。

    若精确版本未找到且 allow_fallback=True，回落到最近的 global release。
    底层逻辑：Plaud Android libflutter.so 是 fork engine，多 build 共用同一份；
    libapp.so 每 build 重新 AOT 编译，BuildId 不同。fallback 用最近 release 的 libflutter.so，
    BuildId 仍能匹配，能解出 Dart engine / GC 帧；libapp.so 自然 BuildId 不对会跳过——安全。
    结果缓存到本地，避免每次调 API。

    match_by_build=True 时改走"按真实 app build 号精确匹配"（native app 仓库的 tag
    后缀是粗粒度冻结号，不是真实 build 号，见 _find_release_tag_by_build），永远
    忽略 allow_fallback，不做任何跨 build 回落——flutter 调用点不传这个参数，
    以下 tag 前缀匹配逻辑逐字节不变。
    """
    if match_by_build:
        return await _find_release_tag_by_build(app_version, repo=repo)

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

    # native app 自己的二进制(Plaud-Global.dSYMs.zip)每次 build 都是独立的内存布局/
    # 符号表，不像 Flutter 的 libflutter.so 是多 build 共享的 fork engine（那边靠 BuildId
    # 验证跨 build 复用安全）。找不到精确版本的 release 时落到"最近 global release"对
    # native 是不安全的——用错 build 的 dSYM 查地址不会报错，只会静默解出一个"看起来
    # 合理但实际上完全错误"的符号（2026-07-23 生产实测：3 个不同 build、不同地址的
    # jank issue 全部被误判成同一个 "main"，因为都 fallback 到了同一个无关 release）。
    #
    # 2026-07-24 修复：native 分支进一步改按真实 app build 号精确匹配 release
    # （而不是"猜 tag 后缀 == build 号"，那套对 native 仓库根本对不上），并在解压后
    # 加一道运行期校验闸门——见下方 _dsym_bundle_build。
    is_native = asset_name == _ASSET_IOS_DSYM_NATIVE
    allow_fallback = not is_native
    tag = await find_release_tag(
        app_version, repo=repo, allow_fallback=allow_fallback, match_by_build=is_native,
    )
    if not tag:
        return None

    # 不同 asset_name 解压到不同子目录，避免 flutter/native 复用同一 tag 时互相覆盖
    cache_dir = _tag_cache_dir(tag) / "ios" / asset_name
    marker = cache_dir / ".extracted"
    if not marker.exists():
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
        except Exception as exc:
            logger.warning("failed to extract iOS dSYMs: %s", exc)
            return None

    if is_native:
        parsed = _parse_semver_build(app_version)
        if not parsed:
            return None
        _, target_build = parsed
        actual_build = _dsym_bundle_build(cache_dir)
        if actual_build != target_build:
            logger.warning(
                "iOS native dSYM build mismatch after extraction: expected build=%s, got=%s "
                "(tag=%s, app_version=%s) — refusing to symbolicate to avoid false-positive symbols",
                target_build, actual_build, tag, app_version,
            )
            return None

    return str(cache_dir)


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

    # native(4.0) 走按真实 build 号精确匹配（同 iOS native 分支根因，见
    # get_ios_dsyms_dir 上方注释）；flutter（repo==_DEFAULT_REPO）行为不变。
    is_native = repo != _DEFAULT_REPO
    tag = await find_release_tag(app_version, repo=repo, match_by_build=is_native)
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


async def _find_uploaded_android_native_symbols_dir(app_version: str) -> Optional[str]:
    """查已上传的 Android native_symbols.tar.gz（platform=android, symbol_type=native_symbols）。

    解压逻辑与现有 GitHub 那份一致：只保留 arm64-v8a + merged_native_libs 下的
    libflutter.so / libapp.so（复用 _is_native_lib_tar_member，不重复实现体积决策）。
    """
    src_dir = _uploaded_package_dir("android", "native_symbols", app_version)
    if not src_dir:
        return None

    extracted_dir = src_dir / ".extracted"
    marker = extracted_dir / ".done"
    if marker.exists():
        return str(extracted_dir)

    tars = list(src_dir.glob("*.tar.gz")) or list(src_dir.glob("*.tgz"))
    if not tars:
        return None

    lock = await _get_extract_lock("android", "native_symbols", app_version)
    async with lock:
        if marker.exists():
            return str(extracted_dir)
        try:
            from app.crashguard.config import get_crashguard_settings as _gs
            allowlist = getattr(_gs(), "android_extract_so_allowlist", None) \
                or ["libflutter.so", "libapp.so"]
        except Exception:
            allowlist = ["libflutter.so", "libapp.so"]

        try:
            extracted_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tars[0]) as tf:
                members = [m for m in tf.getmembers() if _is_native_lib_tar_member(m.name, allowlist)]
                if not members:
                    # 上传的 tar 里一个匹配白名单的 member 都没有（如打包侧目录布局出错）——
                    # 不能悄悄返回一个空目录当成"命中"，否则调用方永远不会回落到 GitHub。
                    # 不 touch marker、不返回目录，当成 miss 处理。
                    logger.warning(
                        "uploaded native_symbols.tar.gz for app_version=%s had 0 members "
                        "matching allowlist %s — treating as miss, falling back to GitHub",
                        app_version, allowlist,
                    )
                    return None
                tf.extractall(extracted_dir, members=members)
            marker.touch()
            logger.info(
                "uploaded Android native symbols extracted to %s (app_version=%s, kept=%d)",
                extracted_dir, app_version, len(members),
            )
            return str(extracted_dir)
        except Exception as exc:
            logger.warning("failed to extract uploaded Android native symbols for %s: %s", app_version, exc)
            return None


async def get_android_native_symbols_dir(app_version: str, repo: str = _DEFAULT_REPO) -> Optional[str]:
    """
    返回 Android native_symbols 目录路径（带 debug 符号的 libflutter.so / libapp.so 等）。
    优先查打包机已上传的包（精确 app_version 匹配），查不到再走 GitHub（按 tag 共享 cache）。

    这是 Plan C for Android native crash 的关键 — Plaud 自己打包了带符号版本的 .so 文件。
    """
    uploaded = await _find_uploaded_android_native_symbols_dir(app_version)
    if uploaded:
        return uploaded

    # native(4.0) 走按真实 build 号精确匹配，理由同 get_android_mapping。
    is_native = repo != _DEFAULT_REPO
    tag = await find_release_tag(app_version, repo=repo, match_by_build=is_native)
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
