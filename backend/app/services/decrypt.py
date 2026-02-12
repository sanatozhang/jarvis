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
        decrypted = decrypt_plaud_bytes(encrypted)

        if not decrypted[:2] == b"PK":
            logger.warning("Decrypted data is not a valid ZIP for %s", plaud_path)
            return None

        output_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(decrypted), "r") as zf:
            zf.extractall(output_dir)

        log_file = output_dir / "plaud.log"
        if log_file.exists():
            logger.info("Decrypted %s → %s", plaud_path.name, log_file)
            return log_file

        # Fallback: find any .log in the extracted files
        for p in output_dir.rglob("*.log"):
            if p.is_file() and p.stat().st_size > 0:
                return p

        return None
    except Exception as e:
        logger.error("Decrypt failed for %s: %s", plaud_path, e)
        return None


def process_log_file(
    file_path: Path,
    work_dir: Path,
) -> Tuple[Optional[Path], bool, Optional[str]]:
    """
    Process a downloaded log file:
    - .plaud → decrypt
    - .zip → extract and look for plaud content
    - .log → use directly

    Returns: (log_path, log_parse_incorrect, reason)
    """
    name = file_path.name.lower()

    if name.endswith(".plaud"):
        if is_zip_file(file_path):
            # User may have zipped and renamed to .plaud
            return _process_zip(file_path, work_dir)
        log_path = decrypt_plaud_file(file_path, work_dir / f"{file_path.stem}_decrypted")
        if log_path:
            return log_path, False, None
        return None, True, ".plaud 解密失败"

    if name.endswith(".zip") or is_zip_file(file_path):
        return _process_zip(file_path, work_dir)

    if name.endswith(".log"):
        if is_plaud_log_format(file_path):
            return file_path, False, None
        return file_path, True, ".log 文件非 plaud 日志格式"

    # Unknown extension: try magic bytes
    if is_zip_file(file_path):
        return _process_zip(file_path, work_dir)
    if is_plaud_log_format(file_path):
        return file_path, False, None

    return None, True, f"无法识别的文件格式: {file_path.name}"


def _process_zip(
    zip_path: Path,
    work_dir: Path,
) -> Tuple[Optional[Path], bool, Optional[str]]:
    extract_dir = work_dir / f"{zip_path.stem}_unzipped"
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
    except Exception as e:
        # Fallback: use system `unzip` for formats Python can't handle (e.g. Deflate64)
        logger.warning("Python zipfile failed (%s), trying system unzip...", e)
        import subprocess
        try:
            subprocess.run(
                ["unzip", "-o", "-q", str(zip_path), "-d", str(extract_dir)],
                capture_output=True, text=True, timeout=60,
            )
        except Exception as e2:
            return None, True, f"解压失败: {e} (system unzip also failed: {e2})"

    # 1. Look for .plaud files inside (highest priority)
    for p in extract_dir.rglob("*.plaud"):
        log_path = decrypt_plaud_file(p)
        if log_path:
            return log_path, False, None
        return None, True, "zip 内含 .plaud 但解密失败"

    # 2. Look for plaud-format .log (device logs)
    for p in extract_dir.rglob("*.log"):
        if p.is_file() and is_plaud_log_format(p):
            return p, False, None

    # 3. Decompress any .log.gz files
    import gzip
    for gz_path in extract_dir.rglob("*.log.gz"):
        try:
            out_path = gz_path.with_suffix("")  # remove .gz
            with gzip.open(gz_path, "rb") as f_in:
                out_path.write_bytes(f_in.read())
            # Check if decompressed file is plaud format
            if is_plaud_log_format(out_path):
                return out_path, False, None
        except Exception as e:
            logger.warning("Failed to decompress %s: %s", gz_path, e)

    # 4. Collect ALL available .log files (even non-plaud format)
    # These could be Web/Desktop app logs, still useful for analysis
    all_logs = sorted(extract_dir.rglob("*.log"), key=lambda p: p.stat().st_size, reverse=True)
    if all_logs:
        # Merge all logs into a single file for the agent to analyze
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
            logger.info("Merged %d log files from zip into %s", len(all_logs), merged)
            return merged, False, None

    return None, True, "解压后未发现可用日志文件"
