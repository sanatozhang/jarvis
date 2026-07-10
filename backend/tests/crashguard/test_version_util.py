"""Tests for crashguard.services.version_util."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.crashguard.services.version_util import (
    classify_generation,
    collect_recent_versions,
    derive_latest_release_from_crashes,
    derive_top_user_version_from_crashes,
    max_version,
    parse_semver,
    resolve_effective_latest_release,
)


@pytest.fixture
async def patched_session(db_engine):
    """复用 conftest db_engine，建表后返回 session factory。"""
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


class TestParseSemver:
    def test_full_semver(self):
        assert parse_semver("3.17.0") == (3, 17, 0, "")

    def test_two_parts(self):
        assert parse_semver("3.17") == (3, 17, 0, "")

    def test_one_part(self):
        assert parse_semver("3") == (3, 0, 0, "")

    def test_with_v_prefix(self):
        assert parse_semver("v3.17.0") == (3, 17, 0, "")

    def test_with_build_suffix(self):
        assert parse_semver("3.17.0-702") == (3, 17, 0, "702")

    def test_with_plus_suffix(self):
        assert parse_semver("3.17.0+build") == (3, 17, 0, "build")

    def test_unparseable(self):
        assert parse_semver("abc") is None
        assert parse_semver("") is None
        assert parse_semver(None) is None  # type: ignore[arg-type]


class TestClassifyGeneration:
    # service tag 为主（SDK 直接盖的真相）
    def test_native_by_service_ios(self):
        assert classify_generation("plaud_ios") == "native"

    def test_native_by_service_android(self):
        assert classify_generation("plaud_android") == "native"

    def test_flutter_by_service(self):
        assert classify_generation("plaud-flutter") == "flutter"

    def test_service_case_insensitive(self):
        assert classify_generation("PLAUD_IOS") == "native"

    # service 优先于 version（即使 version 给了相反信号，service 是真相）
    def test_service_wins_over_version(self):
        assert classify_generation("plaud_ios", "3.16.0") == "native"
        assert classify_generation("plaud-flutter", "4.0.0") == "flutter"

    # version 兜底（service 缺失）
    def test_native_by_version_fallback(self):
        assert classify_generation("", "4.0.100") == "native"
        assert classify_generation("", "4.0.0") == "native"
        assert classify_generation("", "5.2.1-700") == "native"

    def test_flutter_by_version_fallback(self):
        assert classify_generation("", "3.16.0-634") == "flutter"
        assert classify_generation("", "3.99.99") == "flutter"

    def test_cutover_boundary(self):
        # 切线 4.0.0：3.x 全是 flutter，4.0.0 起是 native
        assert classify_generation("", "3.999.999") == "flutter"
        assert classify_generation("", "4.0.0") == "native"

    # 非 app service（web）且无版本 → 不标
    def test_web_service_unknown(self):
        assert classify_generation("plaud-web", "") == ""

    # web service 带版本时按版本兜底（web 版本通常很大，会判 native——但 web issue
    # 在早报里已被 _resolve_real_os 过滤掉，分类器只需不崩；这里只断言不抛异常）
    def test_unknown_service_falls_back_to_version(self):
        assert classify_generation("plaud-web", "3.16.0") == "flutter"

    def test_both_empty_unknown(self):
        assert classify_generation("", "") == ""
        assert classify_generation() == ""

    def test_unparseable_version_unknown(self):
        assert classify_generation("", "abc") == ""


class TestMaxVersion:
    def test_picks_highest_semver_not_lexicographic(self):
        # 3.9 lexicographic > 3.10, semver 3.10 > 3.9
        assert max_version(["3.10.0", "3.9.0"]) == "3.10.0"

    def test_handles_two_part(self):
        assert max_version(["3.16.0", "3.17"]) == "3.17"

    def test_empty(self):
        assert max_version([]) == ""

    def test_skips_empty_strings(self):
        assert max_version(["", " ", "3.17.0"]) == "3.17.0"

    def test_unparseable_loses_to_parseable(self):
        # 不可解析的 "abc" 排在可解析的后面，即使字典序大
        assert max_version(["abc", "3.17.0"]) == "3.17.0"


class TestCollectRecentVersions:
    def test_descending_unique(self):
        out = collect_recent_versions(
            ["3.15.0", "3.17.0", "3.16.0", "3.17.0", "3.14.0"],
            latest="3.17.0", n=3,
        )
        assert out == ["3.17.0", "3.16.0", "3.15.0"]

    def test_filters_unparseable(self):
        out = collect_recent_versions(["abc", "3.17.0", "xyz", "3.16.0"], "3.17.0", 5)
        assert out == ["3.17.0", "3.16.0"]

    def test_empty_falls_back_to_latest(self):
        assert collect_recent_versions([], "3.17.0", 3) == ["3.17.0"]


@pytest.mark.asyncio
async def test_resolve_uses_override_when_set(patched_session):
    async with patched_session() as s:  # patched_session is a session factory
        v = await resolve_effective_latest_release(
            session=s, platform="flutter", override="3.18.0", min_events=300,
        )
    assert v == "3.18.0"


@pytest.mark.asyncio
async def test_derive_picks_max_semver_above_threshold(patched_session):
    from app.crashguard.models import CrashIssue

    async with patched_session() as s:  # patched_session is a session factory
        # 阈值 1000：3.17.0 累计 1500（过线），3.18.0 仅 500（未过线），3.16.0 累计 5000
        s.add(CrashIssue(
            datadog_issue_id="i1", platform="flutter",
            last_seen_version="3.17.0", total_events=1500,
        ))
        s.add(CrashIssue(
            datadog_issue_id="i2", platform="flutter",
            last_seen_version="3.18.0", total_events=500,  # 未达阈值
        ))
        s.add(CrashIssue(
            datadog_issue_id="i3", platform="flutter",
            last_seen_version="3.16.0", total_events=5000,
        ))
        await s.commit()
        v = await derive_latest_release_from_crashes(
            session=s, platform="flutter", min_events=1000,
        )
    # 应该选 3.17.0（最大且 events ≥ 1000），过滤掉灰度的 3.18.0
    assert v == "3.17.0"


@pytest.mark.asyncio
async def test_derive_case_insensitive_platform(patched_session):
    """DB 里 platform 大小写不一时（如 ANDROID/android），应当统一识别。"""
    from app.crashguard.models import CrashIssue

    async with patched_session() as s:  # patched_session is a session factory
        s.add(CrashIssue(
            datadog_issue_id="i1", platform="ANDROID",
            last_seen_version="3.17.0", total_events=2000,
        ))
        await s.commit()
        v = await derive_latest_release_from_crashes(
            session=s, platform="android", min_events=300,
        )
    assert v == "3.17.0"


@pytest.mark.asyncio
async def test_derive_returns_empty_when_no_data(patched_session):
    async with patched_session() as s:  # patched_session is a session factory
        v = await derive_latest_release_from_crashes(
            session=s, platform="flutter", min_events=300,
        )
    assert v == ""


# ---------------------------------------------------------------------------
# 「用户量最大版本」fallback —— 从 crash_issues.top_app_version 加权聚合
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_derive_top_user_version_picks_weighted_max(patched_session):
    """跨 issue 加权聚合：3.17.0 的总贡献 = 1000*0.6 + 500*0.5 = 850 > 3.16.0 的 1000*0.4 + 500*0.5 = 650
    ⚠️ 权重源是 total_events（total_users_affected 当前 = 0，已知 data hole）"""
    from app.crashguard.models import CrashIssue

    async with patched_session() as s:
        s.add(CrashIssue(
            datadog_issue_id="i1", platform="android",
            top_app_version="3.17.0 (60%), 3.16.0 (40%)",
            total_events=1000,
        ))
        s.add(CrashIssue(
            datadog_issue_id="i2", platform="android",
            top_app_version="3.17.0 (50%), 3.16.0 (50%)",
            total_events=500,
        ))
        await s.commit()
        out = await derive_top_user_version_from_crashes(s, platform="android")
    assert out is not None
    assert out["version"] == "3.17.0"
    assert out["users"] == 850  # round(1000*0.6 + 500*0.5)


@pytest.mark.asyncio
async def test_derive_top_user_version_returns_none_on_empty(patched_session):
    async with patched_session() as s:
        out = await derive_top_user_version_from_crashes(s, platform="ios")
    assert out is None


@pytest.mark.asyncio
async def test_derive_top_user_version_skips_malformed_entries(patched_session):
    """top_app_version 解析失败的条目应当 silent skip，不污染聚合"""
    from app.crashguard.models import CrashIssue

    async with patched_session() as s:
        s.add(CrashIssue(
            datadog_issue_id="i1", platform="ios",
            top_app_version="garbage_no_percent, 3.17.0 (80%)",
            total_events=1000,
        ))
        s.add(CrashIssue(
            datadog_issue_id="i2", platform="ios",
            top_app_version="",  # 空值
            total_events=500,
        ))
        await s.commit()
        out = await derive_top_user_version_from_crashes(s, platform="ios")
    assert out is not None
    assert out["version"] == "3.17.0"
    assert out["users"] == 800   # 仅 i1 的 80% 贡献


@pytest.mark.asyncio
async def test_derive_top_user_version_case_insensitive_platform(patched_session):
    """DB 里 platform 大小写不一时应识别"""
    from app.crashguard.models import CrashIssue

    async with patched_session() as s:
        s.add(CrashIssue(
            datadog_issue_id="i1", platform="ANDROID",
            top_app_version="3.17.0 (100%)",
            total_events=1234,
        ))
        await s.commit()
        out = await derive_top_user_version_from_crashes(s, platform="android")
    assert out == {"version": "3.17.0", "users": 1234}


def test_gen_badge_has_native_and_flutter_entries():
    from app.crashguard.services.version_util import GEN_BADGE

    assert GEN_BADGE["native"] == "🆕4.0"
    assert GEN_BADGE["flutter"] == "🦋3.x"
