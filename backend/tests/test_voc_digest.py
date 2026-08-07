"""Tests for app.services.voc_digest — pure aggregation functions (no DB,
no LLM). generate_weekly_digest() (LLM orchestration) is tested separately
in test_voc_digest_generate.py once Task 8 adds it."""
from __future__ import annotations

import json
from datetime import date, datetime

from app.services import voc_digest


def _row(day: str, tag_id="ai-01", group="蓝牙连接", label="配对失败", root_cause="", needs_engineer=False, device_type=""):
    return {
        "created_at": datetime.fromisoformat(day),
        "voc_tags_json": json.dumps([{
            "tag_id": tag_id, "level_1_category": group, "level_2_label": label,
            "level_3_diagnosis": "", "role": "primary", "confidence": "high", "reason": "x",
        }]),
        "root_cause": root_cause,
        "needs_engineer": needs_engineer,
        "device_type": device_type,
    }


def _untagged_row(day: str):
    return {"created_at": datetime.fromisoformat(day), "voc_tags_json": "[]",
            "root_cause": "", "needs_engineer": False, "device_type": ""}


# ---------------------------------------------------------------------------
# default_week_start
# ---------------------------------------------------------------------------

def test_default_week_start_mid_week_returns_prior_complete_week():
    # Wed 2026-08-12 -> this week's Monday is 08-10 (not yet complete) ->
    # most recent COMPLETE week is 08-03 (Mon) .. 08-09 (Sun).
    assert voc_digest.default_week_start(date(2026, 8, 12)) == "2026-08-03"


def test_default_week_start_on_monday_returns_previous_week():
    assert voc_digest.default_week_start(date(2026, 8, 10)) == "2026-08-03"


# ---------------------------------------------------------------------------
# aggregate_trend
# ---------------------------------------------------------------------------

def test_aggregate_trend_by_group_buckets_by_date_and_key():
    rows = [_row("2026-08-03"), _row("2026-08-03", group="固件升级"), _row("2026-08-04")]
    trend = voc_digest.aggregate_trend(rows, level="group")
    assert trend["2026-08-03"] == {"蓝牙连接": 1, "固件升级": 1}
    assert trend["2026-08-04"] == {"蓝牙连接": 1}


def test_aggregate_trend_by_label_combines_group_and_label():
    rows = [_row("2026-08-03", group="蓝牙连接", label="配对失败")]
    trend = voc_digest.aggregate_trend(rows, level="label")
    assert trend["2026-08-03"] == {"蓝牙连接 › 配对失败": 1}


def test_aggregate_trend_skips_untagged_rows():
    rows = [_row("2026-08-03"), _untagged_row("2026-08-03")]
    trend = voc_digest.aggregate_trend(rows, level="group")
    assert trend["2026-08-03"] == {"蓝牙连接": 1}


def test_aggregate_trend_empty_rows_returns_empty_dict():
    assert voc_digest.aggregate_trend([], level="group") == {}


# ---------------------------------------------------------------------------
# aggregate_movers
# ---------------------------------------------------------------------------

def test_aggregate_movers_computes_delta_and_pct():
    cur = [_row("2026-08-10")] * 6
    prev = [_row("2026-08-03")] * 4
    movers = voc_digest.aggregate_movers(cur, prev, level="group", min_base=3)
    assert len(movers) == 1
    m = movers[0]
    assert m["key"] == "蓝牙连接"
    assert m["cur"] == 6 and m["prev"] == 4
    assert m["delta"] == 2
    assert m["delta_pct"] == 50.0


def test_aggregate_movers_filters_below_min_base():
    """1 -> 3 is +200% but both counts are tiny — must be filtered out by
    the min_base floor, this is the noise-suppression the design calls for."""
    cur = [_row("2026-08-10", group="A")] * 3
    prev = [_row("2026-08-03", group="A")] * 1
    movers = voc_digest.aggregate_movers(cur, prev, level="group", min_base=5)
    assert movers == []


def test_aggregate_movers_new_key_with_no_prior_baseline_has_none_pct():
    cur = [_row("2026-08-10", group="新问题")] * 5
    prev = []
    movers = voc_digest.aggregate_movers(cur, prev, level="group", min_base=3)
    assert movers[0]["key"] == "新问题"
    assert movers[0]["prev"] == 0
    assert movers[0]["delta_pct"] is None


def test_aggregate_movers_sorted_by_absolute_delta_descending():
    cur = [_row("2026-08-10", group="A")] * 10 + [_row("2026-08-10", group="B")] * 4
    prev = [_row("2026-08-03", group="A")] * 4 + [_row("2026-08-03", group="B")] * 3
    movers = voc_digest.aggregate_movers(cur, prev, level="group", min_base=3)
    assert [m["key"] for m in movers] == ["A", "B"]  # A's delta=6 > B's delta=1


# ---------------------------------------------------------------------------
# compute_weekly_stats
# ---------------------------------------------------------------------------

def test_compute_weekly_stats_shape_and_totals():
    cur = [_row("2026-08-10", needs_engineer=True), _row("2026-08-11", group="固件升级")]
    prev = [_row("2026-08-03")]
    stats = voc_digest.compute_weekly_stats(cur, prev, min_base=1)
    assert stats["total_cur"] == 2
    assert stats["total_prev"] == 1
    assert stats["total_delta"] == 1
    assert stats["total_delta_pct"] == 100.0
    assert stats["needs_engineer_rate"] == 50.0
    groups = {g["group"]: g["count"] for g in stats["groups"]}
    assert groups == {"蓝牙连接": 1, "固件升级": 1}
    assert isinstance(stats["top_movers"], list)
    assert isinstance(stats["devices"], list)


def test_compute_weekly_stats_zero_prev_total_has_none_delta_pct():
    cur = [_row("2026-08-10")]
    stats = voc_digest.compute_weekly_stats(cur, [], min_base=1)
    assert stats["total_prev"] == 0
    assert stats["total_delta_pct"] is None


def test_compute_weekly_stats_empty_input_does_not_raise():
    stats = voc_digest.compute_weekly_stats([], [])
    assert stats["total_cur"] == 0
    assert stats["groups"] == []
    assert stats["top_movers"] == []


# ---------------------------------------------------------------------------
# sample_root_causes
# ---------------------------------------------------------------------------

def test_sample_root_causes_groups_dedupes_and_caps():
    rows = (
        [_row("2026-08-10", group="蓝牙连接", root_cause="token mismatch")] * 3
        + [_row("2026-08-10", group="蓝牙连接", root_cause="设备断电导致时间戳归零")]
        + [_row("2026-08-10", group="固件升级", root_cause="OTA 传输中断")]
    )
    samples = voc_digest.sample_root_causes(rows, top_n_groups=5, max_per_group=8)
    assert set(samples["蓝牙连接"]) == {"token mismatch", "设备断电导致时间戳归零"}  # deduped
    assert samples["固件升级"] == ["OTA 传输中断"]


def test_sample_root_causes_skips_empty_root_cause():
    rows = [_row("2026-08-10", root_cause=""), _row("2026-08-10", root_cause="real cause")]
    samples = voc_digest.sample_root_causes(rows)
    assert samples["蓝牙连接"] == ["real cause"]


def test_sample_root_causes_only_includes_top_n_groups_by_volume():
    rows = (
        [_row("2026-08-10", group="A", root_cause="a-cause")] * 3
        + [_row("2026-08-10", group="B", root_cause="b-cause")]
    )
    samples = voc_digest.sample_root_causes(rows, top_n_groups=1, max_per_group=8)
    assert set(samples.keys()) == {"A"}


def test_sample_root_causes_caps_at_max_per_group():
    rows = [
        _row("2026-08-10", group="蓝牙连接", root_cause=f"cause-{i}")
        for i in range(12)  # 12 distinct root causes, well over max_per_group=8
    ]
    samples = voc_digest.sample_root_causes(rows, top_n_groups=5, max_per_group=8)
    assert len(samples["蓝牙连接"]) == 8
