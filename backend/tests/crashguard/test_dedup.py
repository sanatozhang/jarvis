"""stack_fingerprint 算法测试"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def stacks() -> dict:
    return json.loads((FIXTURES / "stack_traces.json").read_text())


def test_normalize_strips_line_numbers(stacks):
    """归一化剥离行号"""
    from app.crashguard.services.dedup import normalize_stack_frames

    frames = normalize_stack_frames(stacks["flutter_v1"], top_n=5)
    # 不应包含 :42, :18 这类行号
    for f in frames:
        assert ":" not in f or f.endswith(".dart")  # 行号被剥离
        assert not any(c.isdigit() and i > 0 and f[i - 1] == ":" for i, c in enumerate(f))


def test_normalize_strips_anonymous_closures(stacks):
    """归一化剥离 <anonymous> / _$xxxxx_closure"""
    from app.crashguard.services.dedup import normalize_stack_frames

    frames = normalize_stack_frames(stacks["flutter_v1"], top_n=5)
    for f in frames:
        assert "_$xxxxx" not in f
        assert "<anonymous>" not in f
        assert "closure" not in f.lower() or "_$" not in f


def test_same_bug_same_fingerprint_across_versions(stacks):
    """同一 bug 不同版本（行号变了）→ 同一 fingerprint"""
    from app.crashguard.services.dedup import compute_fingerprint

    fp1 = compute_fingerprint(stacks["flutter_v1"])
    fp2 = compute_fingerprint(stacks["flutter_v2_same_bug"])
    assert fp1 == fp2


def test_different_bugs_different_fingerprint(stacks):
    """不同 bug → 不同 fingerprint"""
    from app.crashguard.services.dedup import compute_fingerprint

    fp1 = compute_fingerprint(stacks["flutter_v1"])
    fp_other = compute_fingerprint(stacks["different_bug"])
    assert fp1 != fp_other


def test_empty_stack_returns_stable_fingerprint():
    """空字符串/异常输入不应崩溃"""
    from app.crashguard.services.dedup import compute_fingerprint

    fp = compute_fingerprint("")
    assert isinstance(fp, str)
    assert len(fp) == 40  # SHA1


def test_ios_stack_strips_libsystem(stacks):
    """iOS 栈归一化剥离 libsystem 噪音"""
    from app.crashguard.services.dedup import normalize_stack_frames

    frames = normalize_stack_frames(stacks["ios_native"], top_n=5)
    assert all("libsystem" not in f.lower() for f in frames)


@pytest.mark.asyncio
async def test_link_issue_to_fingerprint_creates_new_record(tmp_path, monkeypatch):
    """fingerprint 不存在 → 新建 record"""
    from app.crashguard.services.dedup import upsert_fingerprint_link
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa
    import os

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    # 重新初始化 settings 缓存
    from app.config import get_settings
    get_settings.cache_clear()

    await init_db()
    async with get_session() as s:
        await upsert_fingerprint_link(
            s, fingerprint="abc", datadog_issue_id="issue1",
            first_seen_version="1.4.7", events_count=100,
            normalized_top_frames=["frame1", "frame2"],
        )
        await s.commit()

    async with get_session() as s:
        from sqlalchemy import select
        from app.crashguard.models import CrashFingerprint
        row = (await s.execute(
            select(CrashFingerprint).where(CrashFingerprint.fingerprint == "abc")
        )).scalar_one()
        import json as _json
        assert _json.loads(row.datadog_issue_ids) == ["issue1"]
        assert row.total_events_across_versions == 100


@pytest.mark.asyncio
async def test_link_issue_appends_existing_fingerprint(tmp_path, monkeypatch):
    """同 fingerprint 第二个 issue → 数组追加，count 累加"""
    from app.crashguard.services.dedup import upsert_fingerprint_link
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test2.db'}")
    from app.config import get_settings
    get_settings.cache_clear()

    await init_db()

    async with get_session() as s:
        await upsert_fingerprint_link(
            s, fingerprint="xyz", datadog_issue_id="issue_a",
            first_seen_version="1.4.6", events_count=50,
            normalized_top_frames=["fA"],
        )
        await s.commit()
    async with get_session() as s:
        await upsert_fingerprint_link(
            s, fingerprint="xyz", datadog_issue_id="issue_b",
            first_seen_version="1.4.7", events_count=80,
            normalized_top_frames=["fA"],
        )
        await s.commit()

    async with get_session() as s:
        from sqlalchemy import select
        from app.crashguard.models import CrashFingerprint
        row = (await s.execute(
            select(CrashFingerprint).where(CrashFingerprint.fingerprint == "xyz")
        )).scalar_one()
        import json as _json
        ids = _json.loads(row.datadog_issue_ids)
        assert set(ids) == {"issue_a", "issue_b"}
        assert row.total_events_across_versions == 130
        assert row.first_seen_version == "1.4.6"  # 早的版本
