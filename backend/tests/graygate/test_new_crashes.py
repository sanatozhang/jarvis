"""graygate.services.new_crashes 单测。

全部 mock `DatadogClient.list_issues_for_window`，不打真实 Datadog API。核心
锁定点（详见 task-4-brief.md）：

- 「昨日新增」判定必须同时满足 first_seen_at 落在目标窗口内 **且**
  first_seen_version 匹配 version_pattern —— 两者缺一不可。
- 老 bug 在新版本上复现（first_seen_at 早于窗口，但 first_seen_version 恰好
  也匹配 pattern）必须被排除，这是本任务实测验证过的真实场景，专门测试锁定。
- 窗口内首次出现但版本号不匹配 pattern 的也要排除。
- 结果按 events_count 降序，最多 10 条。
- CircuitBreakerOpen / 任意异常 → 返回空 list，不抛异常。
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.crashguard.services.datadog_client import CircuitBreakerOpen
from app.graygate.services import new_crashes as nc

_BJT = ZoneInfo("Asia/Shanghai")

TARGET_DATE = date(2026, 8, 18)


def _raw_issue(
    issue_id: str,
    first_seen_bjt: datetime,
    first_seen_version: str,
    events_count: int = 100,
    platform: str = "IOS",
    title: str = "some crash",
) -> dict:
    ts_ms = int(first_seen_bjt.replace(tzinfo=_BJT).timestamp() * 1000)
    return {
        "id": issue_id,
        "attributes": {
            "title": title,
            "platform": platform,
            "first_seen_timestamp": ts_ms,
            "first_seen_version": first_seen_version,
            "events_count": events_count,
        },
    }


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    fake = SimpleNamespace(
        datadog_api_key="test-key",
        datadog_app_key="test-app-key",
        datadog_site="datadoghq.com",
        version_pattern="4.0.3*",
    )
    monkeypatch.setattr(nc, "get_graygate_settings", lambda: fake)


def _mock_list_issues(monkeypatch, raw_issues=None, side_effect=None):
    async def fake_list_issues_for_window(self, start_ms, end_ms, query="*", **kwargs):
        if side_effect is not None:
            raise side_effect
        return raw_issues or []

    monkeypatch.setattr(
        nc.DatadogClient, "list_issues_for_window", fake_list_issues_for_window
    )


# ---------------------------------------------------------------------------
# a) in-window + matching version -> included
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_crash_in_window_and_matching_version_included(monkeypatch):
    raw = [
        _raw_issue(
            "issue-a",
            datetime(2026, 8, 18, 8, 22),
            "4.0.301-1013",
            events_count=42,
            platform="IOS",
            title="crash A",
        )
    ]
    _mock_list_issues(monkeypatch, raw_issues=raw)

    result = await nc.find_new_crashes(TARGET_DATE)

    assert len(result) == 1
    crash = result[0]
    assert crash.platform == "ios"
    assert crash.version == "4.0.301-1013"
    assert crash.events_count == 42
    assert crash.title == "crash A"
    assert crash.datadog_url == "https://app.datadoghq.com/error-tracking/issue/issue-a"


# ---------------------------------------------------------------------------
# b) old bug recurrence: first_seen_at BEFORE the window, even though
#    first_seen_version happens to match the pattern -> must NOT be included.
#    This is the real scenario found during live testing (task-4-brief.md).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_old_bug_recurrence_excluded_even_if_version_matches(monkeypatch):
    raw = [
        _raw_issue(
            "issue-old",
            datetime(2026, 5, 1, 10, 0),  # long before target_date's window
            "4.0.301-1013",              # matches pattern, but that's not enough
            events_count=999,
            platform="ANDROID",
            title="old bug recurring on 4.0.3*",
        )
    ]
    _mock_list_issues(monkeypatch, raw_issues=raw)

    result = await nc.find_new_crashes(TARGET_DATE)

    assert result == []


# ---------------------------------------------------------------------------
# c) in-window but version does NOT match pattern -> excluded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_in_window_but_version_mismatch_excluded(monkeypatch):
    raw = [
        _raw_issue(
            "issue-c",
            datetime(2026, 8, 18, 12, 0),
            "3.9.5",
            events_count=10,
            platform="IOS",
            title="unrelated old-gen crash",
        )
    ]
    _mock_list_issues(monkeypatch, raw_issues=raw)

    result = await nc.find_new_crashes(TARGET_DATE)

    assert result == []


# ---------------------------------------------------------------------------
# Sorting: descending by events_count, capped at 10.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sorted_descending_and_capped_at_ten(monkeypatch):
    raw = [
        _raw_issue(
            f"issue-{i}",
            datetime(2026, 8, 18, 12, 0),
            "4.0.301-1013",
            events_count=i,  # 0..14, ascending in list order
            platform="ANDROID",
            title=f"crash {i}",
        )
        for i in range(15)
    ]
    _mock_list_issues(monkeypatch, raw_issues=raw)

    result = await nc.find_new_crashes(TARGET_DATE)

    assert len(result) == 10
    events = [c.events_count for c in result]
    assert events == sorted(events, reverse=True)
    assert events == list(range(14, 4, -1))  # top 10 highest: 14..5


# ---------------------------------------------------------------------------
# CircuitBreakerOpen / generic failure -> empty list, no exception.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_breaker_open_returns_empty(monkeypatch):
    _mock_list_issues(monkeypatch, side_effect=CircuitBreakerOpen("breaker open"))

    result = await nc.find_new_crashes(TARGET_DATE)

    assert result == []


@pytest.mark.asyncio
async def test_generic_failure_returns_empty(monkeypatch):
    _mock_list_issues(monkeypatch, side_effect=RuntimeError("http boom"))

    result = await nc.find_new_crashes(TARGET_DATE)

    assert result == []


# ---------------------------------------------------------------------------
# Empty result from Datadog -> empty list.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_datadog_result_returns_empty(monkeypatch):
    _mock_list_issues(monkeypatch, raw_issues=[])

    result = await nc.find_new_crashes(TARGET_DATE)

    assert result == []
