"""
Plaud .plaud file decryption service.

Implements ChaCha20 decryption inline (no external dependency on plaudDecryptor.py).
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("jarvis.decrypt")

# ---------------------------------------------------------------------------
# ChaCha20 implementation (matching the original plaudDecryptor.py)
# ---------------------------------------------------------------------------
CHACHA20_KEY = b"plaud2023_log_chacha20_key_32bit"  # exactly 32 bytes
CHACHA20_NONCE = b"\x01" * 12
BLOCK_SIZE = 8192


class _ChaCha20:
    """Pure-Python ChaCha20 (matches the Flutter / HTML / Python originals)."""

    def __init__(self, key: bytes, nonce: bytes):
        self.key = bytearray(key[:32].ljust(32, b"\x00"))
        self.nonce = bytearray(nonce[:12])
        self.counter = 0

    def _quarter_round(self, a, b, c, d, x):
        x[a] = (x[a] + x[b]) & 0xFFFFFFFF
        x[d] ^= x[a]
        x[d] = ((x[d] << 16) | (x[d] >> 16)) & 0xFFFFFFFF
        x[c] = (x[c] + x[d]) & 0xFFFFFFFF
        x[b] ^= x[c]
        x[b] = ((x[b] << 12) | (x[b] >> 20)) & 0xFFFFFFFF
        x[a] = (x[a] + x[b]) & 0xFFFFFFFF
        x[d] ^= x[a]
        x[d] = ((x[d] << 8) | (x[d] >> 24)) & 0xFFFFFFFF
        x[c] = (x[c] + x[d]) & 0xFFFFFFFF
        x[b] ^= x[c]
        x[b] = ((x[b] << 7) | (x[b] >> 25)) & 0xFFFFFFFF

    def _block(self) -> bytearray:
        x = [0] * 16
        x[0], x[1], x[2], x[3] = 0x61707865, 0x3320646E, 0x79622D32, 0x6B206574
        for i in range(8):
            x[4 + i] = (
                self.key[i * 4]
                | (self.key[i * 4 + 1] << 8)
                | (self.key[i * 4 + 2] << 16)
                | (self.key[i * 4 + 3] << 24)
            ) & 0xFFFFFFFF
        x[12] = self.counter & 0xFFFFFFFF
        for i in range(3):
            x[13 + i] = (
                self.nonce[i * 4]
                | (self.nonce[i * 4 + 1] << 8)
                | (self.nonce[i * 4 + 2] << 16)
                | (self.nonce[i * 4 + 3] << 24)
            ) & 0xFFFFFFFF

        w = x[:]
        for _ in range(10):
            self._quarter_round(0, 4, 8, 12, w)
            self._quarter_round(1, 5, 9, 13, w)
            self._quarter_round(2, 6, 10, 14, w)
            self._quarter_round(3, 7, 11, 15, w)
            self._quarter_round(0, 5, 10, 15, w)
            self._quarter_round(1, 6, 11, 12, w)
            self._quarter_round(2, 7, 8, 13, w)
            self._quarter_round(3, 4, 9, 14, w)

        out = bytearray(64)
        for i in range(16):
            val = (w[i] + x[i]) & 0xFFFFFFFF
            out[i * 4] = val & 0xFF
            out[i * 4 + 1] = (val >> 8) & 0xFF
            out[i * 4 + 2] = (val >> 16) & 0xFF
            out[i * 4 + 3] = (val >> 24) & 0xFF
        self.counter += 1
        return out

    def decrypt(self, data: bytes) -> bytearray:
        self.counter = 0
        out = bytearray(len(data))
        pos = 0
        while pos < len(data):
            ks = self._block()
            end = min(pos + 64, len(data))
            for i in range(end - pos):
                out[pos + i] = data[pos + i] ^ ks[i]
            pos = end
        return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def is_plaud_log_format(path: Path) -> bool:
    """Check if a .log file looks like a Plaud device log."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(2048)
        return "INFO:" in head and bool(
            re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", head)
        )
    except Exception:
        return False


def is_zip_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"PK"
    except Exception:
        return False


def decrypt_plaud_bytes(encrypted: bytes) -> bytes:
    """Decrypt raw .plaud bytes → ZIP bytes."""
    result = bytearray()
    for offset in range(0, len(encrypted), BLOCK_SIZE):
        chunk = encrypted[offset : offset + BLOCK_SIZE]
        cipher = _ChaCha20(CHACHA20_KEY, CHACHA20_NONCE)
        cipher.counter = offset // 64
        result.extend(cipher.decrypt(chunk))
    return bytes(result)


def decrypt_plaud_file(plaud_path: Path, output_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Decrypt a .plaud file → extract ZIP → return path to plaud.log.
    Returns None on failure.
    """
    if output_dir is None:
        output_dir = plaud_path.parent / f"{plaud_path.stem}_decrypted"

    try:
        encrypted = plaud_path.read_bytes()
        logger.info("[plaud] Reading %s: %d bytes, magic: %s",
                     plaud_path.name, len(encrypted), encrypted[:8].hex() if encrypted else "empty")

        decrypted = decrypt_plaud_bytes(encrypted)
        logger.info("[plaud] Decrypted %d bytes → %d bytes, first 4 bytes: %s",
                     len(encrypted), len(decrypted), decrypted[:4].hex() if decrypted else "empty")

        if not decrypted[:2] == b"PK":
            logger.warning("[plaud] ✗ Decrypted data is NOT a valid ZIP (expected PK, got %s) for %s",
                          decrypted[:4].hex() if len(decrypted) >= 4 else "?", plaud_path.name)
            return None

        logger.info("[plaud] ✓ Decrypted data is a valid ZIP, extracting...")
        output_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(decrypted), "r") as zf:
            file_list = zf.namelist()
            logger.info("[plaud] ZIP contains %d files: %s", len(file_list), file_list[:20])
            zf.extractall(output_dir)

        log_file = output_dir / "plaud.log"
        if log_file.exists():
            logger.info("[plaud] ✓ Found plaud.log (%d bytes)", log_file.stat().st_size)
            return log_file

        # Fallback: find any .log in the extracted files
        all_logs = list(output_dir.rglob("*.log"))
        logger.info("[plaud] plaud.log not found, searching for other .log files: found %d", len(all_logs))
        for p in all_logs:
            if p.is_file() and p.stat().st_size > 0:
                logger.info("[plaud] ✓ Using fallback log: %s (%d bytes)", p.name, p.stat().st_size)
                return p

        logger.warning("[plaud] ✗ No .log files found after extraction in %s", output_dir)
        return None
    except Exception as e:
        logger.error("[plaud] ✗ Decrypt failed for %s: %s", plaud_path.name, e, exc_info=True)
        return None


def process_log_file_for_platform(
    file_path: Path,
    work_dir: Path,
    platform: str = "",
) -> Tuple[Optional[Path], bool, Optional[str]]:
    """Dispatch to the correct platform-specific decryption handler.

    - app (or empty/unknown): full Plaud .plaud ChaCha20 decryption
    - web: placeholder — currently passes through as plain log
    - desktop: placeholder — currently passes through as plain log
    """
    plat = (platform or "").lower().strip()

    if plat == "web":
        return _process_log_web(file_path, work_dir)
    elif plat == "desktop":
        return _process_log_desktop(file_path, work_dir)
    else:
        # Default: app (original Plaud .plaud decryption)
        return process_log_file(file_path, work_dir)


def _process_log_web(
    file_path: Path,
    work_dir: Path,
) -> Tuple[Optional[Path], bool, Optional[str]]:
    """Web platform log processing — placeholder.

    Web logs do not use .plaud encryption. Currently just passes the file
    through with basic format detection. Extend with web-specific decryption
    when the format is defined.
    """
    logger.info("=== process_log_web: %s ===", file_path.name)
    name = file_path.name.lower()

    # Handle ZIP
    if name.endswith(".zip") or is_zip_file(file_path):
        return _process_zip(file_path, work_dir)

    # Plain log / unknown — pass through
    if file_path.exists() and file_path.stat().st_size > 0:
        return file_path, False, None

    return None, True, f"Web 日志文件无法处理: {file_path.name}"


def _process_log_desktop(
    file_path: Path,
    work_dir: Path,
) -> Tuple[Optional[Path], bool, Optional[str]]:
    """Desktop platform log processing — placeholder.

    Desktop logs do not use .plaud encryption. Currently just passes the file
    through with basic format detection. Extend with desktop-specific decryption
    when the format is defined.
    """
    logger.info("=== process_log_desktop: %s ===", file_path.name)
    name = file_path.name.lower()

    # Handle ZIP
    if name.endswith(".zip") or is_zip_file(file_path):
        return _process_zip(file_path, work_dir)

    # Plain log / unknown — pass through
    if file_path.exists() and file_path.stat().st_size > 0:
        return file_path, False, None

    return None, True, f"Desktop 日志文件无法处理: {file_path.name}"


def process_log_file(
    file_path: Path,
    work_dir: Path,
) -> Tuple[Optional[Path], bool, Optional[str]]:
    """
    Process a downloaded log file:
    - .plaud → decrypt (ChaCha20 → ZIP → plaud.log)
    - .zip → extract and look for plaud content
    - .log → use directly (or merge if non-plaud format)

    Returns: (log_path, log_parse_incorrect, reason)
    """
    name = file_path.name.lower()
    size = file_path.stat().st_size if file_path.exists() else 0

    # Read magic bytes for detection
    magic = b""
    if size > 0:
        with open(file_path, "rb") as f:
            magic = f.read(16)

    logger.info("=== process_log_file: %s ===", file_path.name)
    logger.info("  size: %d bytes | extension: %s | magic: %s",
                size, Path(name).suffix or "(none)", magic[:8].hex() if magic else "empty")

    # --- .plaud files ---
    if name.endswith(".plaud"):
        logger.info("  Strategy: .plaud extension detected")
        if is_zip_file(file_path):
            logger.info("  → File is actually a ZIP (PK magic), processing as ZIP...")
            return _process_zip(file_path, work_dir)
        logger.info("  → Attempting ChaCha20 decryption...")
        log_path = decrypt_plaud_file(file_path, work_dir / f"{file_path.stem}_decrypted")
        if log_path:
            logger.info("  ✓ .plaud decryption succeeded → %s (%d bytes)", log_path.name, log_path.stat().st_size)
            return log_path, False, None
        logger.warning("  ✗ .plaud decryption failed")
        return None, True, ".plaud 解密失败"

    # --- .zip files ---
    if name.endswith(".zip") or is_zip_file(file_path):
        logger.info("  Strategy: ZIP file detected (ext=%s, magic_PK=%s)", name.endswith(".zip"), magic[:2] == b"PK")
        return _process_zip(file_path, work_dir)

    # --- .log files ---
    if name.endswith(".log"):
        is_plaud = is_plaud_log_format(file_path)
        logger.info("  Strategy: .log extension detected, is_plaud_format=%s", is_plaud)
        if is_plaud:
            logger.info("  ✓ Using .log file directly → %s (%d bytes)", file_path.name, size)
            return file_path, False, None
        logger.info("  ✓ Using non-plaud .log file (still useful for analysis) → %s (%d bytes)", file_path.name, size)
        return file_path, True, ".log 文件非 plaud 日志格式"

    # --- Unknown extension: try all strategies ---
    logger.info("  Strategy: unknown extension, trying all detection methods...")

    # Try 1: ZIP detection by magic bytes
    if is_zip_file(file_path):
        logger.info("  → PK magic detected, processing as ZIP...")
        return _process_zip(file_path, work_dir)

    # Try 2: plain text log detection
    if is_plaud_log_format(file_path):
        logger.info("  ✓ Plaud log format detected, using directly → %s (%d bytes)", file_path.name, size)
        return file_path, False, None

    # Try 3: .plaud decryption as last resort (Linear CDN strips file extensions)
    logger.info("  → Attempting .plaud decryption (last resort)...")
    log_path = decrypt_plaud_file(file_path, work_dir / f"{file_path.stem}_decrypted")
    if log_path:
        logger.info("  ✓ .plaud decryption succeeded → %s (%d bytes)", log_path.name, log_path.stat().st_size)
        return log_path, False, None

    logger.warning("  ✗ All strategies failed for %s", file_path.name)
    return None, True, f"无法识别的文件格式: {file_path.name}"


def _process_zip(
    zip_path: Path,
    work_dir: Path,
) -> Tuple[Optional[Path], bool, Optional[str]]:
    extract_dir = work_dir / f"{zip_path.stem}_unzipped"
    extract_dir.mkdir(parents=True, exist_ok=True)

    logger.info("[zip] Extracting %s (%d bytes) → %s", zip_path.name, zip_path.stat().st_size, extract_dir)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            file_list = zf.namelist()
            logger.info("[zip] ZIP contains %d entries: %s", len(file_list), file_list[:30])
            zf.extractall(extract_dir)
            logger.info("[zip] ✓ Extraction complete")
    except Exception as e:
        logger.warning("[zip] Python zipfile failed (%s), trying system unzip...", e)
        import subprocess
        try:
            result = subprocess.run(
                ["unzip", "-o", "-q", str(zip_path), "-d", str(extract_dir)],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                logger.warning("[zip] system unzip stderr: %s", result.stderr[:500])
        except Exception as e2:
            logger.error("[zip] ✗ Both extraction methods failed: %s / %s", e, e2)
            return None, True, f"解压失败: {e} (system unzip also failed: {e2})"

    # List all extracted files for debugging
    all_files = list(extract_dir.rglob("*"))
    all_files = [f for f in all_files if f.is_file()]
    logger.info("[zip] Extracted %d files:", len(all_files))
    for f in all_files[:30]:
        logger.info("[zip]   %s (%d bytes)", f.relative_to(extract_dir), f.stat().st_size)

    # 1. Look for .plaud files inside (highest priority)
    plaud_files = list(extract_dir.rglob("*.plaud"))
    if plaud_files:
        logger.info("[zip] Found %d .plaud files, decrypting first one: %s", len(plaud_files), plaud_files[0].name)
        log_path = decrypt_plaud_file(plaud_files[0])
        if log_path:
            logger.info("[zip] ✓ .plaud inside ZIP decrypted → %s (%d bytes)", log_path.name, log_path.stat().st_size)
            return log_path, False, None
        logger.warning("[zip] ✗ .plaud inside ZIP decryption failed")
        return None, True, "zip 内含 .plaud 但解密失败"

    # 2. Look for plaud-format .log (device logs)
    log_files = list(extract_dir.rglob("*.log"))
    logger.info("[zip] Found %d .log files", len(log_files))
    for p in log_files:
        if p.is_file() and is_plaud_log_format(p):
            logger.info("[zip] ✓ Found plaud-format log: %s (%d bytes)", p.name, p.stat().st_size)
            return p, False, None

    # 3. Decompress any .log.gz files
    import gzip
    gz_files = list(extract_dir.rglob("*.log.gz"))
    if gz_files:
        logger.info("[zip] Found %d .log.gz files, decompressing...", len(gz_files))
    for gz_path in gz_files:
        try:
            out_path = gz_path.with_suffix("")
            with gzip.open(gz_path, "rb") as f_in:
                out_path.write_bytes(f_in.read())
            logger.info("[zip] Decompressed %s → %s (%d bytes)", gz_path.name, out_path.name, out_path.stat().st_size)
            if is_plaud_log_format(out_path):
                logger.info("[zip] ✓ Decompressed file is plaud-format")
                return out_path, False, None
        except Exception as e:
            logger.warning("[zip] Failed to decompress %s: %s", gz_path.name, e)

    # 4. Collect ALL available .log files (even non-plaud format)
    all_logs = sorted(extract_dir.rglob("*.log"), key=lambda p: p.stat().st_size, reverse=True)
    if all_logs:
        logger.info("[zip] No plaud-format logs found, merging all %d .log files as fallback...", len(all_logs))
        merged = work_dir / "merged_logs.log"
        with open(merged, "w", encoding="utf-8", errors="replace") as out:
            for lp in all_logs:
                if lp.stat().st_size == 0:
                    continue
                out.write(f"\n{'='*60}\n")
                out.write(f"=== FILE: {lp.name} (size: {lp.stat().st_size}) ===\n")
                out.write(f"{'='*60}\n\n")
                try:
                    out.write(lp.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    pass
        if merged.stat().st_size > 0:
            logger.info("[zip] ✓ Merged %d log files → %s (%d bytes)", len(all_logs), merged.name, merged.stat().st_size)
            return merged, False, None

    logger.warning("[zip] ✗ No usable log files found after extraction")
    return None, True, "解压后未发现可用日志文件"
