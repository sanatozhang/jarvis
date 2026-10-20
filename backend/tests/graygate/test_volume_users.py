"""2026-10-19：灰度日报加用户数 + RUM 采样还原 + 网页端全版本明细。"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.graygate.services import version_resolver as vr
from app.graygate.services.card_builder import GraygateReportData, TierSummary, VersionRow
from app.graygate.services.report_markdown import assemble_report_markdown
from app.graygate.services.slack_report import assemble_slack_message

_CJK = re.compile(r"[㐀-鿿]")


def _data(*, rate: float = 100.0) -> GraygateReportData:
    cells = {"crash_free": "🟩 99.90%"}
    est = lambda n: int(n * 100 / rate)  # noqa: E731
    tiers = {
        p: [
            TierSummary("大盘", "4.0.3*", 1702, False, dict(cells),
                        users=230, est_sessions=est(1702), sample_rate=rate),
            TierSummary("主要版本", "4.0.302-1203", 1670, True, dict(cells),
                        users=214, est_sessions=est(1670), sample_rate=rate),
        ]
        for p in ("ios", "android")
    }
    rows = [VersionRow("4.0.302-1203", 1670, est(1670), 214, rate),
            VersionRow("4.0.301-1058", 32, est(32), 6, rate)]
    return GraygateReportData(
        target_date=date(2026, 10, 18), d1_day=date(2026, 10, 17), version_pattern="4.0.3*",
        worsen_lines=[], columns_md=["", ""], new_crash_md=None, top_crash_md=None, top_jank_md=None,
        metric_rows=[("crash_free", "Crash-free sessions", True)],
        tiers=tiers, version_rows={"ios": rows, "android": []},
    )


def _slack_text(data, url="https://j/graygate/reports?date=2026-10-18") -> str:
    r = assemble_slack_message(data, url)
    return json.dumps([r.payload, r.text], ensure_ascii=False)


def test_slack_shows_sessions_and_users_for_primary_and_overall():
    text = _slack_text(_data())
    assert "_Primary_ `4.0.302-1203` (manual) · 1,670 sessions · 214 users" in text
    assert "_Overall (4.0.3*)_ · 1,702 sessions · 230 users" in text
    assert "sampled" not in text
    assert not _CJK.search(text)


def test_slack_sampled_marks_approx_and_note():
    text = _slack_text(_data(rate=20.0))
    assert "8,350 sessions" in text and "≈8,350" in text  # 1670 / 0.2
    assert "RUM sampled 20%" in text
    assert not _CJK.search(text)


def test_web_markdown_has_users_row_and_all_versions_table():
    md = assemble_report_markdown(_data())
    assert "| Users | 214 | 214 | 230 | 230 |" in md
    assert "All `4.0.3*` versions" in md
    assert "| `4.0.302-1203` | 1,670 | 98.1% | 214 | 100% |" in md
    assert "| `4.0.301-1058` | 32 | 1.9% | 6 | 100% |" in md
    assert "sampling" not in md


def test_web_markdown_sampled_note():
    md = assemble_report_markdown(_data(rate=50.0))
    assert "| Sessions | ≈3,340 |" in md
    assert "≈3,340" in md and "sampling" in md


def test_old_tiersummary_without_new_fields_still_renders():
    t = TierSummary("大盘", "4.0.3*", 12345, False, {})
    assert t.display_sessions == "12,345" and t.users is None


@pytest.mark.asyncio
async def test_restore_sampling_per_version_and_unrated(monkeypatch):
    buckets = [
        {"by": {"version": "4.0.302-1203", "@_dd.configuration.session_sample_rate": 20}, "computes": {"c0": 100}},
        {"by": {"version": "4.0.301-1058", "@_dd.configuration.session_sample_rate": 100}, "computes": {"c0": 30}},
    ]

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": {"buckets": buckets}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            assert json["group_by"][1]["facet"] == "@_dd.configuration.session_sample_rate"
            return _Resp()

    monkeypatch.setattr(vr.httpx, "AsyncClient", _Client)
    est, rates = await vr._restore_sampling(
        "u", {}, {"filter": {}}, {"4.0.302-1203": 120, "4.0.301-1058": 32}, "ios",
    )
    assert est == {"4.0.302-1203": 520, "4.0.301-1058": 32}  # 100/0.2 + 20 未带 rate；30 + 2
    assert rates == {"4.0.302-1203": 20.0, "4.0.301-1058": 100.0}

    pv = vr.PlatformVersions("ios", [("4.0.302-1203", 120), ("4.0.301-1058", 32)],
                             "4.0.302-1203", 120, 152, est, rates)
    assert pv.est_sessions() == 552 and pv.est_sessions("4.0.302-1203") == 520
    assert pv.sample_rate() == 20.0 and pv.sample_rate("4.0.301-1058") == 100.0


def test_all_app_query_covers_flutter_and_native_per_platform():
    from app.graygate.services.user_counts import all_app_query
    ios = all_app_query("ios")
    assert "@service:plaud-flutter @os.name:(iOS OR iPadOS)" in ios and "@service:plaud_ios" in ios
    assert "version:" not in ios                      # 不限版本：3.x + 4.x 全部
    android = all_app_query("android")
    assert "@os.name:Android" in android and "@service:plaud_android" in android
