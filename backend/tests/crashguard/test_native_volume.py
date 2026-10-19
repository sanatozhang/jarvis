"""早报 4.0 Native 会话 / 用户数（大盘 + 主要版本，带 RUM 采样还原）。"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

from app.crashguard.services.daily_report import _native_volume_md, build_native_volume
from app.crashguard.services.datadog_client import DatadogClient
from app.crashguard.services.feishu_card import _build_crash_free_columns
from app.crashguard.services.slack_daily import build_daily_slack

_CJK = re.compile(r"[㐀-鿿＀-￯]")


def _volume(rate_ios: float = 100.0):
    return {
        "ios": {
            "4.0.302-1203": {"sampled": 1808, "estimated": int(1808 * 100 / rate_ios),
                             "min_sample_rate": rate_ios},
            "4.0.302-1143": {"sampled": 22, "estimated": 22, "min_sample_rate": 100.0},
        },
        "android": {
            "4.0.0-1236": {"sampled": 20, "estimated": 20, "min_sample_rate": 100.0},
            "4.0.302-1203": {"sampled": 140, "estimated": 140, "min_sample_rate": 100.0},
        },
    }


_UBV = {"ios": {"4.0.302-1203": 214, "4.0.302-1143": 5},
        "android": {"4.0.302-1203": 51, "4.0.0-1236": 7}}


def test_build_picks_main_by_sessions_and_sums():
    n = build_native_volume(_volume(), {"ios": 220, "android": 60}, _UBV)
    ios = n["platforms"]["IOS"]
    assert ios["overall"] == {"sessions": 1830, "sampled_sessions": 1830, "users": 220,
                              "min_sample_rate": 100.0}
    assert ios["main"]["version"] == "4.0.302-1203"
    assert ios["main"]["sessions"] == 1808 and ios["main"]["users"] == 214
    assert n["platforms"]["ANDROID"]["main"]["version"] == "4.0.302-1203"
    assert n["summary"]["overall"]["sessions"] == 1830 + 160
    assert n["summary"]["main"]["users"] == 214 + 51
    assert [r["version"] for r in ios["versions"]] == ["4.0.302-1203", "4.0.302-1143"]
    assert ios["versions"][1]["users"] == 5


def test_build_empty_volume_returns_empty():
    assert build_native_volume({}, {}, {}) == {}
    assert build_native_volume({"ios": {}}, {"ios": 3}, {}) == {}


def test_sampled_sessions_extrapolated_and_flagged():
    n = build_native_volume(_volume(rate_ios=20.0), {"ios": 220}, {"ios": {"4.0.302-1203": 214}})
    ios = n["platforms"]["IOS"]
    assert ios["main"]["sessions"] == 9040 and ios["main"]["sampled_sessions"] == 1808
    assert ios["overall"]["min_sample_rate"] == 20.0
    md = "\n".join(_native_volume_md(n))
    assert "≈9,062" in md  # 9040 + 22
    assert "sampling" in md and "undercounted" in md
    assert not _CJK.search(md)


def test_full_sampling_has_no_sampling_note():
    md = "\n".join(_native_volume_md(build_native_volume(_volume(), {"ios": 220}, {"ios": {"4.0.302-1203": 214}})))
    assert "4.0 Native" in md and "1,830" in md and "`4.0.302-1203`" in md
    # 完整数据：各平台全版本明细
    assert "all 4.0 versions" in md and "`4.0.302-1143`" in md
    assert "≈" not in md and "sampling" not in md
    assert not _CJK.search(md.replace("🍎", "").replace("📱", ""))


def test_slack_column_english_with_native():
    native = build_native_volume(_volume(rate_ios=50.0), {"ios": 220, "android": 60},
                                 _UBV)
    payload = {
        "tldr": {"severity": "green"},
        "crash_free_detail": {
            "all_versions": {"platforms": {"IOS": {"crash_free_users_pct": 99.0, "total_users": 100,
                                                   "crashed_users": 1}}},
            "native_40": native,
        },
    }
    r = build_daily_slack(report_type="morning", target_date="2026-10-19", payload=payload,
                          frontend_base_url="http://h/")
    text = json.dumps(r.payload, ensure_ascii=False)
    assert "4.0 Native" in text and "≈3,638 sessions" in text and "220 users" in text
    assert "Primary `4.0.302-1203`" in text and "160 sessions" in text
    assert "RUM sampled 50%" in text
    assert not _CJK.search(text)


def test_feishu_columns_include_native():
    native = build_native_volume(_volume(), {"ios": 220, "android": 60}, _UBV)
    detail = {"all_versions": {"platforms": {"IOS": {"total_sessions": 10, "crash_free_pct": 99.0}}},
              "native_40": native}
    text = json.dumps(_build_crash_free_columns(detail), ensure_ascii=False)
    assert "🆕 4.0 Native" in text
    assert "Primary `4.0.302-1203`" in text and "**214** users" in text
    assert "4.0 Native total" in text


def test_session_volume_by_version_rate_restore(monkeypatch):
    """缺 sample_rate 的 session 按 100% 计；不同采样率逐桶还原。"""
    def bucket(by, n):
        return SimpleNamespace(by=by, computes={"c0": n})

    total = [bucket({"@os.name": "iOS", "version": "4.0.302-1203"}, 120),
             bucket({"@os.name": "Android", "version": "4.0.302-1203"}, 10)]
    rated = [bucket({"@os.name": "iOS", "version": "4.0.302-1203",
                     "@_dd.configuration.session_sample_rate": 20}, 100),
             bucket({"@os.name": "Android", "version": "4.0.302-1203",
                     "@_dd.configuration.session_sample_rate": 100}, 10)]
    calls = []

    def fake_aggregate(self, body):
        calls.append(body)
        buckets = rated if len(body.group_by) == 3 else total
        return SimpleNamespace(data=SimpleNamespace(buckets=buckets))

    monkeypatch.setattr("datadog_api_client.v2.api.rum_api.RUMApi.aggregate_rum_events", fake_aggregate)
    c = DatadogClient(api_key="k", app_key="a", service_filter="(service:plaud_ios)")
    out = c._sync_session_volume_by_version(24)
    ios = out["ios"]["4.0.302-1203"]
    assert ios == {"sampled": 120, "estimated": 520, "min_sample_rate": 20.0}  # 100/0.2 + 20
    assert out["android"]["4.0.302-1203"]["estimated"] == 10
    assert all(b.filter.query.startswith("(service:plaud_ios)") for b in calls)
