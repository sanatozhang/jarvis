"""删除工单时清理 per-issue 日志/解密缓存，避免重新导入后复用旧日志。

复现 (102 rec27CyKMwcZ5l)：工单因日志格式非法被删除 → 用户重新导入新日志 →
worker 命中 _cache/{issue_id}/ 旧缓存直接复用，新日志永不被下载/解密。
"""
from pathlib import Path


def _seed_issue_cache(root: Path, issue_id: str):
    """造出一份 issue 的三类 per-issue 缓存目录。"""
    (root / "_cache" / issue_id / "raw").mkdir(parents=True, exist_ok=True)
    (root / "_cache" / issue_id / "raw" / "old.log").write_text("OLD")
    (root / "_cache" / issue_id / "processed").mkdir(parents=True, exist_ok=True)
    (root / "_cache" / issue_id / "decrypt_manifest.json").write_text("{}")
    (root / issue_id / "raw").mkdir(parents=True, exist_ok=True)
    (root / issue_id / "raw" / "old.log").write_text("OLD")


def test_purge_issue_cache_removes_all_per_issue_dirs(tmp_path):
    from app.workers.analysis_worker import purge_issue_cache

    _seed_issue_cache(tmp_path, "rec_bad")
    _seed_issue_cache(tmp_path, "rec_other")

    purge_issue_cache(str(tmp_path), "rec_bad")

    # 目标 issue 的所有缓存目录都没了
    assert not (tmp_path / "_cache" / "rec_bad").exists()
    assert not (tmp_path / "rec_bad").exists()
    # 别的 issue 的缓存原封不动
    assert (tmp_path / "_cache" / "rec_other" / "raw" / "old.log").exists()
    assert (tmp_path / "rec_other" / "raw" / "old.log").exists()


def test_purge_issue_cache_noop_when_absent(tmp_path):
    from app.workers.analysis_worker import purge_issue_cache
    # 无缓存时不抛错
    purge_issue_cache(str(tmp_path), "rec_never_seen")
