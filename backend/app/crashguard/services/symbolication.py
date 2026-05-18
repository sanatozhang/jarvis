"""
Flutter Engine + 用户自上传符号包 符号化服务。

支持：
  - Android: addr2line / llvm-symbolizer + libflutter.so
  - iOS:     atos + Flutter.dSYM
  - 用户上传的 dart_symbols / proguard_mapping / dsym 包（Plan B fallback）

容错优先：任何子步骤失败都不影响主调用方，原始地址原样保留。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("crashguard.symbolication")

# ── 工具可用性缓存（进程级，启动时探测一次）──────────────────────────────────
_ADDR2LINE: Optional[str] = None   # addr2line 或 llvm-symbolizer 路径
_ATOS: Optional[str] = None        # atos 路径（仅 macOS）
_TOOLS_PROBED = False

def _probe_tools() -> None:
    global _ADDR2LINE, _ATOS, _TOOLS_PROBED
    if _TOOLS_PROBED:
        return
    _ADDR2LINE = shutil.which("llvm-symbolizer") or shutil.which("addr2line")
    _ATOS = shutil.which("atos")
    _TOOLS_PROBED = True
    logger.info(
        "symbolication tools: addr2line/llvm-symbolizer=%s  atos=%s",
        _ADDR2LINE, _ATOS,
    )


# ── 缓存目录 ──────────────────────────────────────────────────────────────────
def _flutter_engine_cache_dir() -> Path:
    base = Path(os.environ.get("DATA_DIR", "/data"))
    p = base / "symbols" / "flutter_engine_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _user_symbols_dir() -> Path:
    base = Path(os.environ.get("DATA_DIR", "/data"))
    p = base / "symbols"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── 公开入口 ──────────────────────────────────────────────────────────────────

async def symbolicate_stack(
    stack: str,
    binary_images: list,
    platform: str,
    app_version: str = "",
) -> str:
    """
    尝试符号化 stack 中的帧，返回增强后的 stack 字符串。

    优先级：
      1. Flutter engine 帧（Plan A：从公开存储自动下载）
      2. 用户上传的符号包（Plan B）
      3. GitHub release 符号包（Plan C：自动按版本下载）

    Args:
        stack:         原始堆栈字符串
        binary_images: Datadog RUM 事件里的 binary_images 列表（可为空 list）
        platform:      "ios" | "android" | "flutter" 等
        app_version:   Datadog @application.version，如 "3.18.0-708"（可为空）

    Returns:
        符号化后的堆栈字符串（失败时原样返回 stack）
    """
    _probe_tools()
    if not stack:
        return stack
    try:
        # Plan A + Plan B（同步，在线程里跑）
        result = await asyncio.to_thread(_symbolicate_stack_sync, stack, binary_images, platform)
        # Plan C：GitHub release 符号（异步，按需下载）
        if app_version:
            result = await _symbolicate_with_github(result, platform, app_version)
        return result
    except Exception as exc:
        logger.warning("symbolicate_stack failed (non-fatal): %s", exc)
        return stack


def _symbolicate_stack_sync(stack: str, binary_images: list, platform: str) -> str:
    plat = (platform or "").lower()
    if "ios" in plat or "iphone" in plat or "ipados" in plat:
        return _symbolicate_ios(stack, binary_images)
    if "android" in plat:
        return _symbolicate_android(stack, binary_images)
    # flutter 在 Android/iOS 底层跑，尝试两者
    out = _symbolicate_android(stack, binary_images)
    if out != stack:
        return out
    return _symbolicate_ios(out, binary_images)


async def _symbolicate_with_github(stack: str, platform: str, app_version: str) -> str:
    """Plan C：利用 Plaud GitHub release 里的符号文件对 stack 做进一步增强。

    Android：
      1. 优先用 native_symbols.tar.gz 里的 libflutter.so / libapp.so（带 debug 符号）解 native 帧
      2. 用 mapping_globalRelease.txt 做 ProGuard 反混淆 Java 帧
    iOS：用 PLAUD.dSYMs.zip 里的 dSYM bundle 用 atos 解析
    """
    from app.crashguard.services.github_symbols import (
        get_ios_dsyms_dir, get_android_mapping, get_android_native_symbols_dir,
    )
    plat = (platform or "").lower()
    try:
        if "ios" in plat or "iphone" in plat or "ipados" in plat:
            dsyms_dir = await get_ios_dsyms_dir(app_version)
            if dsyms_dir:
                stack = await asyncio.to_thread(_symbolicate_ios_with_dir, stack, dsyms_dir)
        elif "android" in plat or "flutter" in plat:
            # 1. native 符号（关键：libflutter.so / libapp.so 带 debug 符号）
            native_dir = await get_android_native_symbols_dir(app_version)
            if native_dir:
                stack = await asyncio.to_thread(
                    _symbolicate_android_with_dir, stack, native_dir,
                )
            # 2. ProGuard mapping（Java 帧反混淆）
            mapping_path = await get_android_mapping(app_version)
            if mapping_path:
                stack = await asyncio.to_thread(_retrace_proguard, stack, mapping_path)
    except Exception as exc:
        logger.debug("github symbolication failed (non-fatal): %s", exc)
    return stack


def _symbolicate_android_with_dir(stack: str, native_dir: str) -> str:
    """
    用 Plaud release 解压出的 native_symbols 目录里的 .so 文件（含 debug 符号）
    对 Android native crash 帧做 addr2line 符号化。

    匹配策略：从 stack 文本提 BuildId → 遍历 native_dir 下所有 .so 用 file 命令验 BuildId → addr2line。
    """
    if not _ADDR2LINE:
        return stack

    build_ids = set(m.group(4) for m in _ANDROID_FLUTTER_FRAME_RE.finditer(stack) if m.group(4))
    if not build_ids:
        return stack

    # 也匹配非 libflutter.so 的 native 帧（如 libapp.so）
    build_ids_all = set()
    extra_re = re.compile(
        r"(#\d+\s+pc\s+)([0-9a-fA-F]+)\s+.*?(\S+\.so).*?(?:BuildId:\s*([0-9a-fA-F]+))",
        re.MULTILINE,
    )
    for m in extra_re.finditer(stack):
        if m.group(4):
            build_ids_all.add(m.group(4).lower())

    # 扫 native_dir 找所有 .so，建 build_id → so_path 映射
    so_map: dict = {}
    for so_path in Path(native_dir).rglob("*.so"):
        try:
            r = subprocess.run(
                ["file", str(so_path)], capture_output=True, text=True, timeout=5,
            )
            for bid in build_ids_all:
                if bid in r.stdout.lower():
                    so_map[bid] = str(so_path)
        except Exception:
            continue

    if not so_map:
        return stack

    def replace_frame(m: re.Match) -> str:
        bid = (m.group(4) or "").lower()
        if not bid or bid not in so_map:
            return m.group(0)
        offset = m.group(2)
        sym = _addr2line_lookup(so_map[bid], offset)
        if not sym:
            return m.group(0)
        # 替换 (???) 为 [function:line]
        return m.group(0).replace("(???)", f"[{sym}]")

    return extra_re.sub(replace_frame, stack)


# ── iOS 符号化 ─────────────────────────────────────────────────────────────────

# Flutter iOS 帧格式：
#   1   Flutter  0x00000001076c6100 0x1071dc000 + 5153024
_IOS_FLUTTER_FRAME_RE = re.compile(
    r"^(\s*\d+\s+Flutter\s+)(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+\+\s+(\d+)(.*)",
    re.MULTILINE,
)

def _symbolicate_ios(stack: str, binary_images: list) -> str:
    # 找 Flutter binary_image entry
    flutter_entry = _find_ios_flutter_image(binary_images)
    if not flutter_entry:
        return stack

    uuid = (flutter_entry.get("uuid") or "").replace("-", "").lower()
    load_addr = flutter_entry.get("load_address") or flutter_entry.get("load") or ""

    if not uuid:
        return stack

    dsym_path = _get_or_download_ios_dsym(uuid)
    if not dsym_path:
        # 尝试用户上传的符号包
        dsym_path = _find_user_dsym(uuid, "ios")
    if not dsym_path or not _ATOS:
        return stack

    dwarf_path = _find_dwarf_in_dsym(dsym_path)
    if not dwarf_path:
        return stack

    def replace_frame(m: re.Match) -> str:
        prefix = m.group(1)
        addr = m.group(2)
        base = m.group(3) if not load_addr else load_addr
        suffix = m.group(5)
        sym = _atos_lookup(dwarf_path, base, addr)
        if sym:
            return f"{prefix}{sym}{suffix}"
        return m.group(0)

    return _IOS_FLUTTER_FRAME_RE.sub(replace_frame, stack)


def _find_ios_flutter_image(binary_images: list) -> Optional[dict]:
    for img in (binary_images or []):
        if not isinstance(img, dict):
            continue
        name = (img.get("name") or img.get("image") or "").lower()
        if "flutter" in name:
            return img
    return None


def _atos_lookup(dwarf_path: str, load_addr: str, addr: str) -> Optional[str]:
    if not _ATOS:
        return None
    try:
        result = subprocess.run(
            [_ATOS, "-arch", "arm64", "-o", dwarf_path, "-l", str(load_addr), str(addr)],
            capture_output=True, text=True, timeout=10,
        )
        out = result.stdout.strip()
        if out and out != addr and "???" not in out:
            return out
    except Exception as exc:
        logger.debug("atos failed for %s: %s", addr, exc)
    return None


def _find_dwarf_in_dsym(dsym_path: str) -> Optional[str]:
    p = Path(dsym_path)
    # Flutter.dSYM/Contents/Resources/DWARF/Flutter
    dwarf_dir = p / "Contents" / "Resources" / "DWARF"
    if dwarf_dir.exists():
        candidates = list(dwarf_dir.iterdir())
        if candidates:
            return str(candidates[0])
    return None


# ── Android 符号化 ─────────────────────────────────────────────────────────────

# Android flutter 帧格式：
#   #00 pc 00897954  /data/app/.../libflutter.so (???) (BuildId: 0a7fde9baaf490ad50a8480ebc422ea4ee862a2e)
_ANDROID_FLUTTER_FRAME_RE = re.compile(
    r"(#\d+\s+pc\s+)([0-9a-fA-F]+)(\s+.*?libflutter\.so.*?(?:BuildId:\s*([0-9a-fA-F]+)).*?)$",
    re.MULTILINE,
)

def _symbolicate_android(stack: str, binary_images: list) -> str:
    # 提取所有 BuildId
    build_ids_in_stack = set(m.group(4) for m in _ANDROID_FLUTTER_FRAME_RE.finditer(stack) if m.group(4))

    if not build_ids_in_stack:
        return stack

    # 对每个 BuildId 找符号文件
    so_map: dict[str, Optional[str]] = {}
    for bid in build_ids_in_stack:
        so_map[bid] = _get_or_download_android_so(bid) or _find_user_so(bid, "android")

    def replace_frame(m: re.Match) -> str:
        build_id = m.group(4)
        if not build_id:
            return m.group(0)
        so_path = so_map.get(build_id)
        if not so_path:
            return m.group(0)
        offset = m.group(2)
        sym = _addr2line_lookup(so_path, offset)
        if sym:
            prefix = m.group(1)
            rest = m.group(3)
            return f"{prefix}{offset}  [{sym}]{rest}"
        return m.group(0)

    return _ANDROID_FLUTTER_FRAME_RE.sub(replace_frame, stack)


def _addr2line_lookup(so_path: str, offset: str) -> Optional[str]:
    if not _ADDR2LINE:
        return None
    try:
        cmd = [_ADDR2LINE, "-f", "-e", so_path, "-a", f"0x{offset}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        lines = result.stdout.strip().splitlines()
        # llvm-symbolizer 输出：地址 / 函数名 / 文件:行
        # addr2line 输出：函数名 / 文件:行
        sym_parts = [l.strip() for l in lines if l.strip() and "??" not in l]
        if sym_parts:
            return " ".join(sym_parts[:2])
    except Exception as exc:
        logger.debug("addr2line failed for offset %s: %s", offset, exc)
    return None


# ── Flutter Engine 符号下载 ──────────────────────────────────────────────────

def _normalize_uuid(uuid: str) -> str:
    return uuid.replace("-", "").lower()


def _get_or_download_ios_dsym(uuid: str) -> Optional[str]:
    """按 UUID 查找或下载 Flutter.dSYM。"""
    uid = _normalize_uuid(uuid)
    cache = _flutter_engine_cache_dir() / f"ios_{uid}"
    dsym_marker = cache / "Flutter.dSYM"
    if dsym_marker.exists():
        return str(dsym_marker)

    engine_hash = _find_flutter_engine_hash(uid, "ios")
    if not engine_hash:
        return None

    url = (
        f"https://storage.googleapis.com/flutter_infra_release/flutter/"
        f"{engine_hash}/ios-release/Flutter.dSYM.zip"
    )
    return _download_and_extract(url, cache, "Flutter.dSYM")


def _get_or_download_android_so(build_id: str) -> Optional[str]:
    """按 BuildId 查找或下载 libflutter.so（带符号）。"""
    bid = build_id.lower()
    cache = _flutter_engine_cache_dir() / f"android_{bid}"
    so_path = cache / "libflutter.so"
    if so_path.exists():
        return str(so_path)

    engine_hash = _find_flutter_engine_hash(bid, "android")
    if not engine_hash:
        return None

    url = (
        f"https://storage.googleapis.com/flutter_infra_release/flutter/"
        f"{engine_hash}/android-arm64/symbols.zip"
    )
    return _download_and_extract(url, cache, "libflutter.so")


def _find_flutter_engine_hash(uuid_or_build_id: str, platform: str) -> Optional[str]:
    """
    从 UUID / BuildId 推导 Flutter engine commit hash。

    策略（按优先级）：
    1. 本地 engine_hash_index.json 索引（命中即用）
    2. 遍历 Flutter releases.json 最近 N 个 stable channel 的 engine hash，
       下载对应平台 symbols.zip，验 build-id 是否匹配（命中后写回 index 缓存）
    """
    key = uuid_or_build_id.lower()
    index_path = _flutter_engine_cache_dir() / "engine_hash_index.json"
    index: dict = {}
    if index_path.exists():
        try:
            import json as _json
            index = _json.loads(index_path.read_text(encoding="utf-8")) or {}
            if key in index:
                return index[key]
        except Exception:
            index = {}

    # 自动遍历：从 Flutter 官方 releases 路由查 engine hash
    # max_versions 提高到 40 + 包含 stable / beta channels（Plaud 不一定用 stable，
    # 灰度包可能在 beta channel）；逐个验证 build-id 匹配
    try:
        hashes = _fetch_recent_flutter_engine_hashes(max_versions=40)
    except Exception as exc:
        logger.debug("fetch_recent_flutter_engine_hashes failed: %s", exc)
        return None

    for engine_hash in hashes:
        if not engine_hash:
            continue
        verified = _verify_engine_hash_against_build_id(engine_hash, key, platform)
        if verified:
            # 命中，写回索引缓存
            index[key] = engine_hash
            try:
                import json as _json
                index_path.write_text(_json.dumps(index, indent=2), encoding="utf-8")
                logger.info("cached engine_hash mapping: %s → %s", key, engine_hash)
            except Exception:
                pass
            return engine_hash
    return None


# Module-level cache for Flutter releases meta（防止每次重复拉）
_FLUTTER_RELEASES_CACHE: Optional[list] = None
_FLUTTER_RELEASES_CACHE_AT: float = 0.0


def _fetch_recent_flutter_engine_hashes(max_versions: int = 8) -> List[str]:
    """从 Flutter 官方 releases.json 拉最近 N 个 stable SDK 版本，按 SDK hash → engine.version 派生 engine commit hash。

    Flutter releases JSON 字段：每个 release 有 `hash`(Flutter SDK commit) + `channel`。
    engine commit 单独存在 GitHub `flutter/flutter@{sdk_hash}:bin/internal/engine.version` 文件里。
    """
    import time as _time
    import urllib.request as _ureq

    global _FLUTTER_RELEASES_CACHE, _FLUTTER_RELEASES_CACHE_AT
    now = _time.time()
    # 6h 缓存防 Flutter API 速率限制
    if _FLUTTER_RELEASES_CACHE is not None and (now - _FLUTTER_RELEASES_CACHE_AT) < 6 * 3600:
        stable_hashes = _FLUTTER_RELEASES_CACHE
    else:
        url = "https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json"
        try:
            with _ureq.urlopen(url, timeout=15) as resp:  # noqa: S310
                import json as _json
                data = _json.loads(resp.read().decode("utf-8"))
            # 包含 stable + beta（Plaud 灰度可能用 beta channel）；按 releases 顺序遍历
            allowed_channels = {"stable", "beta"}
            stable_hashes = []
            seen = set()
            for r in (data.get("releases") or []):
                if r.get("channel") not in allowed_channels:
                    continue
                h = (r.get("hash") or "").strip()
                if h and h not in seen:
                    seen.add(h)
                    stable_hashes.append(h)
                if len(stable_hashes) >= max_versions * 2:  # 多取一些防部分 engine 查询失败
                    break
            _FLUTTER_RELEASES_CACHE = stable_hashes
            _FLUTTER_RELEASES_CACHE_AT = now
        except Exception as exc:
            logger.warning("fetch_flutter_releases failed: %s", exc)
            return []

    # SDK hash → engine commit
    engine_hashes: List[str] = []
    seen_engines = set()
    for sdk_hash in stable_hashes:
        engine_hash = _sdk_hash_to_engine_hash(sdk_hash)
        if engine_hash and engine_hash not in seen_engines:
            seen_engines.add(engine_hash)
            engine_hashes.append(engine_hash)
        if len(engine_hashes) >= max_versions:
            break
    return engine_hashes


def _sdk_hash_to_engine_hash(sdk_hash: str) -> Optional[str]:
    """通过 GitHub raw 拉 flutter/flutter@{sdk_hash}:bin/internal/engine.version，返回 engine commit。"""
    import urllib.request as _ureq

    url = f"https://raw.githubusercontent.com/flutter/flutter/{sdk_hash}/bin/internal/engine.version"
    try:
        with _ureq.urlopen(url, timeout=10) as resp:  # noqa: S310
            content = resp.read().decode("utf-8").strip()
        # 文件里通常就是一行 hash
        if content and re.match(r"^[0-9a-f]{40}$", content):
            return content
    except Exception as exc:
        logger.debug("sdk_hash_to_engine_hash failed for %s: %s", sdk_hash, exc)
    return None


def _verify_engine_hash_against_build_id(engine_hash: str, build_id: str, platform: str) -> bool:
    """下载 engine_hash 对应的 Android symbols.zip，解压找 libflutter.so，验 build-id 匹配。"""
    plat = (platform or "").lower()
    if "android" not in plat and plat != "" and "flutter" not in plat:
        # iOS UUID 校验目前不支持自动反查（dSYM zip 太大，先跳过）
        return False

    cache = _flutter_engine_cache_dir() / f"android_engine_{engine_hash[:12]}"
    so_path = cache / "libflutter.so"
    if not so_path.exists():
        url = (
            f"https://storage.googleapis.com/flutter_infra_release/flutter/"
            f"{engine_hash}/android-arm64/symbols.zip"
        )
        result = _download_and_extract(url, cache, "libflutter.so")
        if not result or not Path(result).exists():
            return False
        so_path = Path(result)

    # 读 so 的 BuildId 与 stack 里给的对比
    try:
        out = subprocess.run(
            ["file", str(so_path)], capture_output=True, text=True, timeout=5,
        ).stdout.lower()
        if build_id.lower() in out:
            # 把这个 .so 同时链到 android_{build_id} 目录，让 _get_or_download_android_so 命中
            target_dir = _flutter_engine_cache_dir() / f"android_{build_id.lower()}"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_so = target_dir / "libflutter.so"
            if not target_so.exists():
                try:
                    target_so.symlink_to(so_path)
                except Exception:
                    import shutil as _sh
                    _sh.copy(so_path, target_so)
            return True
    except Exception as exc:
        logger.debug("verify_engine_hash_against_build_id failed: %s", exc)
    return False


def _download_and_extract(url: str, dest_dir: Path, target_name: str) -> Optional[str]:
    """下载 zip 并解压，返回 target_name 的路径，失败返回 None。"""
    import urllib.request
    import tempfile

    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / target_name
    if target.exists():
        return str(target)

    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name
        logger.info("downloading flutter engine symbols: %s", url)
        urllib.request.urlretrieve(url, tmp_path)  # noqa: S310

        with zipfile.ZipFile(tmp_path) as zf:
            members = zf.namelist()
            # 找 target_name（可能在子目录里）
            matched = [m for m in members if m.endswith(target_name) or target_name in m]
            if not matched:
                # 解压全部，再找
                zf.extractall(dest_dir)
            else:
                for m in matched:
                    zf.extract(m, dest_dir)

        # 递归找 target 文件
        candidates = list(dest_dir.rglob(target_name))
        if candidates:
            # 如果不在 dest_dir 根，建软链接方便后续访问
            if str(candidates[0]) != str(target):
                target.symlink_to(candidates[0])
            return str(target)
        return None
    except Exception as exc:
        logger.warning("failed to download/extract %s: %s", url, exc)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ── 用户上传符号包查找 ────────────────────────────────────────────────────────

def _find_user_dsym(uuid: str, platform: str) -> Optional[str]:
    """在用户上传的 dsym 包里按 UUID 查找 dSYM bundle。"""
    symbols_dir = _user_symbols_dir() / platform / "dsym"
    if not symbols_dir.exists():
        return None
    uid = _normalize_uuid(uuid)
    for version_dir in symbols_dir.iterdir():
        if not version_dir.is_dir():
            continue
        for p in version_dir.rglob("*.dSYM"):
            plist = p / "Contents" / "Info.plist"
            if plist.exists():
                try:
                    text = plist.read_text(encoding="utf-8")
                    if uid in text.replace("-", "").lower():
                        return str(p)
                except Exception:
                    continue
    return None


def _find_user_so(build_id: str, platform: str) -> Optional[str]:
    """在用户上传的包里按 BuildId 查找 libflutter.so（简单目录扫描）。"""
    symbols_dir = _user_symbols_dir() / platform
    if not symbols_dir.exists():
        return None
    bid = build_id.lower()
    for so in symbols_dir.rglob("libflutter.so"):
        # 尝试用 file 命令检查 build-id（可选，不影响功能）
        try:
            r = subprocess.run(
                ["file", str(so)], capture_output=True, text=True, timeout=3,
            )
            if bid in r.stdout.lower():
                return str(so)
        except Exception:
            pass
    return None


# ── Plan C：GitHub release 符号 ────────────────────────────────────────────────

def _symbolicate_ios_with_dir(stack: str, dsyms_dir: str) -> str:
    """
    用 GitHub release 里解压出的 dSYMs 目录对 iOS stack 做符号化。
    遍历目录下所有 .dSYM bundle，逐一尝试 atos 解析未符号化的帧。
    """
    if not _ATOS:
        return stack

    import re as _re
    # 匹配尚未符号化的 iOS 帧：函数名为十六进制地址或 "???"
    _unsym_re = _re.compile(
        r"^(\s*\d+\s+\S+\s+)(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+\+\s+(\d+)(.*)",
        _re.MULTILINE,
    )

    dsyms = list(Path(dsyms_dir).rglob("*.dSYM"))
    if not dsyms:
        return stack

    lines = stack.splitlines(keepends=True)
    result = []
    for line in lines:
        m = _unsym_re.match(line)
        if not m:
            result.append(line)
            continue
        addr = m.group(2)
        base = m.group(3)
        resolved = False
        for dsym in dsyms:
            dwarf = _find_dwarf_in_dsym(str(dsym))
            if not dwarf:
                continue
            sym = _atos_lookup(dwarf, base, addr)
            if sym:
                result.append(f"{m.group(1)}{sym}{m.group(5)}\n")
                resolved = True
                break
        if not resolved:
            result.append(line)
    return "".join(result)


# ProGuard mapping 行格式：
#   com.original.Class -> a.b.C:
#       returnType originalMethod(params) -> x
_PG_CLASS_RE = re.compile(r"^(\S+)\s+->\s+(\S+):$")
_PG_METHOD_RE = re.compile(r"^\s+\S+\s+(\S+)\(.*?\)\s+->\s+(\S+)$")

def _build_proguard_index(mapping_path: str) -> dict:
    """解析 mapping.txt，构建 {obfuscated → original} 映射表（类名 + 方法名）。"""
    index: dict = {}  # obfuscated_class → original_class
    method_index: dict = {}  # (obfuscated_class, obfuscated_method) → original_method
    current_obf = ""
    try:
        with open(mapping_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                cm = _PG_CLASS_RE.match(line)
                if cm:
                    orig, obf = cm.group(1), cm.group(2).rstrip(":")
                    index[obf] = orig
                    current_obf = obf
                    continue
                if current_obf:
                    mm = _PG_METHOD_RE.match(line)
                    if mm:
                        orig_m, obf_m = mm.group(1), mm.group(2)
                        method_index[(current_obf, obf_m)] = orig_m
    except Exception as exc:
        logger.warning("failed to parse ProGuard mapping %s: %s", mapping_path, exc)
    return {"classes": index, "methods": method_index}


# 缓存 mapping 解析结果（按文件路径），避免每次重复解析 50MB 文件
_PG_INDEX_CACHE: dict = {}

def _get_proguard_index(mapping_path: str) -> dict:
    if mapping_path not in _PG_INDEX_CACHE:
        logger.info("parsing ProGuard mapping %s ...", mapping_path)
        _PG_INDEX_CACHE[mapping_path] = _build_proguard_index(mapping_path)
        logger.info("ProGuard mapping loaded: %d classes", len(_PG_INDEX_CACHE[mapping_path]["classes"]))
    return _PG_INDEX_CACHE[mapping_path]


# Android Java/Kotlin 堆栈帧格式：
#   at a.b.c.d(SourceFile:123)
#   at a.b.c.d(Unknown Source)
_ANDROID_FRAME_RE = re.compile(r"(\s+at\s+)([\w.$]+)\.([\w$]+)\(([^)]*)\)")

def _retrace_proguard(stack: str, mapping_path: str) -> str:
    """用 ProGuard mapping 对 Android stack 做 retrace（纯 Python，无需 retrace 工具）。"""
    idx = _get_proguard_index(mapping_path)
    classes = idx.get("classes", {})
    methods = idx.get("methods", {})
    if not classes:
        return stack

    def replace_frame(m: re.Match) -> str:
        prefix = m.group(1)
        obf_class = m.group(2)
        obf_method = m.group(3)
        rest = m.group(4)
        orig_class = classes.get(obf_class, obf_class)
        orig_method = methods.get((obf_class, obf_method), obf_method)
        return f"{prefix}{orig_class}.{orig_method}({rest})"

    return _ANDROID_FRAME_RE.sub(replace_frame, stack)
