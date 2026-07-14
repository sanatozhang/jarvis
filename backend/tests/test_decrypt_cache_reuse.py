"""decrypt cache 命中路径的 log_paths 现场重扫单测（2026-07-14，fb_f2fb6cb886）。

背景：decrypt.py 修复了"只返回 plaud.log、丢弃 plaud_backup.log"的 bug 之后，
fb_f2fb6cb886 重新触发分析依旧只看到 213,907 行——因为 issue 级 decrypt 缓存命中时，
analysis_worker.py 靠 replay `decrypt_manifest.json` 里冻结的文件名列表还原 log_paths，
那份 manifest 是修复前写的，永远只记着 plaud.log 一个文件，即使 processed/ 目录里
plaud_backup.log 其实一直都在。改成命中缓存时直接从磁盘现场枚举，不再相信过期记录。
"""
from __future__ import annotations

from pathlib import Path

from app.workers.analysis_worker import _resolve_decrypted_log_paths


def test_resolve_decrypted_log_paths_finds_all_non_empty_logs(tmp_path: Path):
    decrypted = tmp_path / "log_1_decrypted"
    decrypted.mkdir()
    (decrypted / "plaud.log").write_text("current session")
    (decrypted / "plaud_backup.log").write_text("older rotated buffer")

    result = _resolve_decrypted_log_paths(tmp_path)

    assert {p.name for p in result} == {"plaud.log", "plaud_backup.log"}


def test_resolve_decrypted_log_paths_ignores_stale_manifest_style_omission(tmp_path: Path):
    """核心回归：即使一份历史 manifest 只记录了 plaud.log，现场扫描也必须同时找到
    此后一直躺在磁盘上、从未被 manifest 记录过的 plaud_backup.log。"""
    decrypted = tmp_path / "log_1783995685_decrypted"
    decrypted.mkdir()
    (decrypted / "plaud.log").write_text("current session")
    (decrypted / "plaud_backup.log").write_text("[SafeMode] Entering safe mode; bootFailCount=2")

    # 模拟历史遗留的 manifest 依然只知道一个文件——这里故意不去读它，
    # 断言现场扫描的结果本身就与 manifest 内容无关。
    stale_manifest_log_paths = ["log_1783995685_decrypted/plaud.log"]

    result = _resolve_decrypted_log_paths(tmp_path)

    result_names = {p.name for p in result}
    assert "plaud_backup.log" in result_names
    assert len(stale_manifest_log_paths) == 1  # 佐证：manifest 确实不完整，仍能全找到


def test_resolve_decrypted_log_paths_skips_empty_files(tmp_path: Path):
    decrypted = tmp_path / "log_1_decrypted"
    decrypted.mkdir()
    (decrypted / "plaud.log").write_text("content")
    (decrypted / "empty.log").write_text("")

    result = _resolve_decrypted_log_paths(tmp_path)

    assert [p.name for p in result] == ["plaud.log"]


def test_resolve_decrypted_log_paths_orders_plaud_log_first(tmp_path: Path):
    decrypted = tmp_path / "log_1_decrypted"
    decrypted.mkdir()
    (decrypted / "plaud_backup.log").write_text("x" * 1000)
    (decrypted / "plaud.log").write_text("y" * 10)

    result = _resolve_decrypted_log_paths(tmp_path)

    assert result[0].name == "plaud.log"


def test_resolve_decrypted_log_paths_empty_dir_returns_empty_list(tmp_path: Path):
    assert _resolve_decrypted_log_paths(tmp_path) == []
