"""github_symbols.py 已上传包查找基础设施单测（2026-07-22）。"""
from __future__ import annotations


def test_uploaded_package_dir_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    assert G._uploaded_package_dir("ios", "dsym", "4.0.201-941") is None


def test_uploaded_package_dir_returns_none_when_empty_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    empty_dir = tmp_path / "symbols" / "ios" / "dsym" / "4.0.201-941"
    empty_dir.mkdir(parents=True)
    assert G._uploaded_package_dir("ios", "dsym", "4.0.201-941") is None


def test_uploaded_package_dir_returns_path_when_nonempty(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    d = tmp_path / "symbols" / "ios" / "dsym" / "4.0.201-941"
    d.mkdir(parents=True)
    (d / "PLAUD.dSYMs.zip").write_bytes(b"fake")

    result = G._uploaded_package_dir("ios", "dsym", "4.0.201-941")
    assert result == d


async def test_get_extract_lock_returns_same_instance_for_same_key():
    from app.crashguard.services import github_symbols as G

    lock1 = await G._get_extract_lock("ios", "dsym", "4.0.201-941")
    lock2 = await G._get_extract_lock("ios", "dsym", "4.0.201-941")
    assert lock1 is lock2


async def test_get_extract_lock_returns_different_instance_for_different_key():
    from app.crashguard.services import github_symbols as G

    lock1 = await G._get_extract_lock("ios", "dsym", "4.0.201-941")
    lock2 = await G._get_extract_lock("android", "native_symbols", "4.0.201-941")
    assert lock1 is not lock2
