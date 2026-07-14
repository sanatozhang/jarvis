"""decrypt.py::decrypt_plaud_file() 多日志文件单测（2026-07-14）。

背景：工单 fb_f2fb6cb886 的服务器分析找不到根因，本地 CLI 却能找到——真因是
.plaud 解压出的 ZIP 常常含 2 个日志文件（plaud.log 当前会话 + plaud_backup.log/
truncated_backup_*.log 更早的滚动缓冲），decrypt_plaud_file() 只要找到 plaud.log
就直接返回，第二个文件被静默丢弃，从未进入 agent 能 grep 到的 logs/ 目录。
本次实测这个 bug 与 Flutter/Native 无关——两边共用同一份 decrypt.py。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.services.decrypt import decrypt_plaud_bytes, decrypt_plaud_file, process_log_file


def _make_plaud_file(tmp_path: Path, zip_members: dict[str, bytes], name: str = "log_1.plaud") -> Path:
    """构造一个合法的 .plaud 文件：明文 ZIP → ChaCha20 加密（对称，加密函数=解密函数）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for member_name, content in zip_members.items():
            zf.writestr(member_name, content)
    plaintext_zip = buf.getvalue()
    encrypted = decrypt_plaud_bytes(plaintext_zip)  # ChaCha20 是流密码，加解密同一函数
    plaud_path = tmp_path / name
    plaud_path.write_bytes(encrypted)
    return plaud_path


def test_decrypt_plaud_file_returns_only_primary_log_files_when_no_backup(tmp_path: Path):
    plaud_path = _make_plaud_file(tmp_path, {"plaud.log": b"INFO: 2026-01-01 00:00:00.000000: hello\n"})
    result = decrypt_plaud_file(plaud_path)
    assert [p.name for p in result] == ["plaud.log"]


def test_decrypt_plaud_file_keeps_backup_log_not_just_primary(tmp_path: Path):
    """核心回归测试：ZIP 里的 plaud_backup.log 必须和 plaud.log 一起被保留，不能丢。"""
    plaud_path = _make_plaud_file(tmp_path, {
        "plaud.log": b"INFO: 2026-07-14 02:50:26.000000: current session\n",
        "plaud_backup.log": b"WARN: 2026-07-14 01:59:36.000000: [SafeMode] Entering safe mode; bootFailCount=2\n",
    })

    result = decrypt_plaud_file(plaud_path)

    names = {p.name for p in result}
    assert names == {"plaud.log", "plaud_backup.log"}
    backup = next(p for p in result if p.name == "plaud_backup.log")
    assert b"SafeMode" in backup.read_bytes()


def test_decrypt_plaud_file_keeps_arbitrarily_named_backup_log(tmp_path: Path):
    """truncated_backup_<ts>.log 这种非固定命名的备份日志同样不能丢。"""
    plaud_path = _make_plaud_file(tmp_path, {
        "plaud.log": b"INFO: 2026-07-14 02:50:26.000000: current session\n",
        "truncated_backup_1783991746342.log": b"INFO: 2026-07-13 10:00:00.000000: older rotated buffer\n",
    })

    result = decrypt_plaud_file(plaud_path)

    names = {p.name for p in result}
    assert names == {"plaud.log", "truncated_backup_1783991746342.log"}


def test_decrypt_plaud_file_empty_zip_returns_empty_list(tmp_path: Path):
    plaud_path = _make_plaud_file(tmp_path, {"readme.txt": b"no logs here"})
    result = decrypt_plaud_file(plaud_path)
    assert result == []


def test_decrypt_plaud_file_invalid_data_returns_empty_list(tmp_path: Path):
    bogus = tmp_path / "bogus.plaud"
    bogus.write_bytes(b"\x00" * 32)
    result = decrypt_plaud_file(bogus)
    assert result == []


def test_process_log_file_propagates_all_logs_for_dot_plaud(tmp_path: Path):
    plaud_path = _make_plaud_file(tmp_path, {
        "plaud.log": b"INFO: 2026-07-14 02:50:26.000000: current session\n",
        "plaud_backup.log": b"WARN: 2026-07-14 01:59:36.000000: [SafeMode] Entering safe mode; bootFailCount=2\n",
    })
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    log_paths, incorrect, reason = process_log_file(plaud_path, work_dir)

    assert incorrect is False
    assert reason is None
    assert {p.name for p in log_paths} == {"plaud.log", "plaud_backup.log"}
