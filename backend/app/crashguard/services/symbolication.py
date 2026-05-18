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
) -> str:
    """
    尝试符号化 stack 中的 Flutter Engine 帧，返回增强后的 stack 字符串。

    Args:
        stack:         原始堆栈字符串
        binary_images: Datadog RUM 事件里的 binary_images 列表（可为空 list）
        platform:      "ios" | "android" | "flutter" 等

    Returns:
        符号化后的堆栈字符串（失败时原样返回 stack）
    """
    _probe_tools()
    if not stack:
        return stack
    try:
        return await asyncio.to_thread(_symbolicate_stack_sync, stack, binary_images, platform)
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

    策略（最简可行方案）：
    1. 检查本地 engine_hash_index（JSON 文件，运营可手动维护）
    2. 无法查到时返回 None（跳过，不报错）

    更完整的方案：
    - 从 https://storage.googleapis.com/flutter_infra_release/flutter/<hash>/... 遍历
      已知发布 hash 列表（太慢，暂不实现）
    - 通过 Flutter releases JSON 查版本-hash 映射（需要先有 app_version → Flutter SDK 版本映射）
    """
    index_path = _flutter_engine_cache_dir() / "engine_hash_index.json"
    if index_path.exists():
        import json
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            key = uuid_or_build_id.lower()
            if key in index:
                return index[key]
        except Exception:
            pass
    return None


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
