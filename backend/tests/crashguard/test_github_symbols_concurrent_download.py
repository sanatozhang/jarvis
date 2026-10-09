"""并发符号化同一 tag 时只下载一次（2026-10-09 102 惊群重复下载回归）。

旧逻辑：锁外查 .extracted、`_download_asset` 锁内只复检归档本身；首个 task 解压后删除
归档 → 排队的 task 看不到归档全部重下。这里断言 N 个并发调用只触发 1 次下载。
"""
from __future__ import annotations

import asyncio
import io
import tarfile

import pytest

from app.crashguard.services import github_symbols as G


def _make_tar_gz(dest, member_name: str = "flutter_symbols/app.android-arm64.symbols") -> None:
    data = b"symbols"
    with tarfile.open(dest, "w:gz") as tf:
        info = tarfile.TarInfo(member_name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))


@pytest.mark.asyncio
async def test_concurrent_get_dart_symbols_dir_downloads_once(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def fake_find_uploaded(app_version):
        return None

    async def fake_find_release_tag(app_version, repo=G._DEFAULT_REPO, **kw):
        return "v3.29.1+739-2026_09_15-192150-global"

    calls = []

    async def fake_download_asset(tag, asset_name, dest, repo=G._DEFAULT_REPO):
        calls.append(asset_name)
        await asyncio.sleep(0.05)  # 让其他 task 有机会进入等锁
        dest.parent.mkdir(parents=True, exist_ok=True)
        _make_tar_gz(dest)
        return dest

    monkeypatch.setattr(G, "_find_uploaded_dart_symbols_dir", fake_find_uploaded)
    monkeypatch.setattr(G, "find_release_tag", fake_find_release_tag)
    monkeypatch.setattr(G, "_download_asset", fake_download_asset)

    results = await asyncio.gather(*[G.get_dart_symbols_dir("3.29.1-739") for _ in range(5)])

    assert len(calls) == 1
    assert len(set(results)) == 1 and results[0] is not None
    cache_dir = G._tag_cache_dir("v3.29.1+739-2026_09_15-192150-global") / "dart"
    assert (cache_dir / ".extracted").exists()
    assert not (cache_dir / G._ASSET_DART_SYMBOLS).exists()  # 归档解压后已清理


@pytest.mark.asyncio
async def test_extract_failure_does_not_touch_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def fake_download_asset(tag, asset_name, dest, repo=G._DEFAULT_REPO):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not a tar")
        return dest

    monkeypatch.setattr(G, "_download_asset", fake_download_asset)

    cache_dir = tmp_path / "c"

    def _extract(p):
        with tarfile.open(p) as tf:
            tf.extractall(cache_dir)

    ok = await G._ensure_tag_asset_extracted("t", "x.tar.gz", cache_dir, G._DEFAULT_REPO, _extract)
    assert ok is False
    assert not (cache_dir / ".extracted").exists()
