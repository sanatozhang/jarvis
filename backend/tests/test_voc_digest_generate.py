"""Tests for app.services.voc_digest.generate_weekly_digest — the LLM
narrative orchestration on top of the pure stats layer (test_voc_digest.py)
and the DB cache (test_voc_digest_db.py). Mocks claude_headless.run_json."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services import voc_digest


def _seed_row(created_at):
    return {
        "created_at": created_at, "voc_tags_json": json.dumps([{
            "tag_id": "ai-01", "level_1_category": "蓝牙连接", "level_2_label": "配对失败",
            "level_3_diagnosis": "", "role": "primary", "confidence": "high", "reason": "x",
        }]),
        "root_cause": "设备断电导致时间戳归零", "needs_engineer": False, "device_type": "Note",
        "problem_type": "蓝牙连接失败", "platform": "app", "issue_id": "i1",
    }


@pytest.fixture(autouse=True)
def digest_settings(monkeypatch):
    fake_settings = SimpleNamespace(
        voc=SimpleNamespace(digest_enabled=True, digest_model="claude-test", digest_timeout_seconds=5),
    )
    monkeypatch.setattr(voc_digest, "get_settings", lambda: fake_settings)
    yield


async def _patch_db(monkeypatch, cur_rows, prev_rows, existing=None, recurrence_rows=None):
    import app.db.database as db_mod
    async def fake_get_rows(date_from, date_to):
        # First call in generate_weekly_digest is the current week, second is previous.
        return cur_rows if not fake_get_rows.called else prev_rows
    fake_get_rows.called = False
    async def wrapper(date_from, date_to):
        result = cur_rows if not wrapper.calls else prev_rows
        wrapper.calls += 1
        return result
    wrapper.calls = 0
    monkeypatch.setattr(db_mod, "get_voc_analysis_rows", wrapper)

    async def fake_get_existing(week_start):
        return existing
    monkeypatch.setattr(db_mod, "get_voc_weekly_digest", fake_get_existing)

    async def fake_get_recurrence_rows(date_from, date_to):
        return recurrence_rows or []
    monkeypatch.setattr(db_mod, "get_recurrence_rows", fake_get_recurrence_rows)

    captured = {}
    async def fake_upsert(**kwargs):
        captured.update(kwargs)
        return {"week_start": kwargs["week_start"], "stats": kwargs["stats"],
                "narrative": kwargs["narrative"], "markdown": kwargs["markdown"],
                "model": kwargs.get("model", ""), "total_tokens": 0, "total_cost_usd": 0.0,
                "generated_at": "2026-08-10T10:00:00"}
    monkeypatch.setattr(db_mod, "upsert_voc_weekly_digest", fake_upsert)
    return captured


async def test_generate_weekly_digest_returns_cached_when_not_forced(monkeypatch):
    existing = {"week_start": "2026-08-03", "stats": {}, "narrative": None, "markdown": "cached"}
    await _patch_db(monkeypatch, [], [], existing=existing)
    result = await voc_digest.generate_weekly_digest("2026-08-03", force=False)
    assert result == existing


async def test_generate_weekly_digest_calls_llm_and_stores_narrative(monkeypatch):
    captured = await _patch_db(monkeypatch, [_seed_row(datetime(2026, 8, 10))], [])

    async def fake_run_json(**kwargs):
        return {
            "headline": "Bluetooth pairing dominates this week",
            "key_findings": [{"scope": "蓝牙连接", "finding": "spike in pairing failures"}],
            "product_opportunities": [{
                "area": "Bluetooth", "problem": "device timestamp resets on power loss",
                "suggestion": "warn users during file transfer",
            }],
        }
    monkeypatch.setattr(voc_digest.claude_headless, "run_json", fake_run_json)

    result = await voc_digest.generate_weekly_digest("2026-08-03", force=False)
    assert result["narrative"]["headline"] == "Bluetooth pairing dominates this week"
    assert "Bluetooth pairing dominates this week" in result["markdown"]
    assert "设备断电导致时间戳归零" not in captured["markdown"]  # raw root_cause isn't dumped verbatim into markdown


async def test_generate_weekly_digest_llm_failure_still_caches_deterministic_stats(monkeypatch):
    captured = await _patch_db(monkeypatch, [_seed_row(datetime(2026, 8, 10))], [])

    async def fake_run_json(**kwargs):
        return None
    monkeypatch.setattr(voc_digest.claude_headless, "run_json", fake_run_json)

    result = await voc_digest.generate_weekly_digest("2026-08-03", force=False)
    assert result["narrative"] is None
    assert captured["stats"]["total_cur"] == 1
    assert "洞察生成失败" in result["markdown"]


async def test_generate_weekly_digest_narrative_missing_required_key_is_discarded(monkeypatch):
    await _patch_db(monkeypatch, [_seed_row(datetime(2026, 8, 10))], [])

    async def fake_run_json(**kwargs):
        return {"headline": "incomplete"}  # missing key_findings/product_opportunities
    monkeypatch.setattr(voc_digest.claude_headless, "run_json", fake_run_json)

    result = await voc_digest.generate_weekly_digest("2026-08-03", force=False)
    assert result["narrative"] is None


async def test_generate_weekly_digest_digest_disabled_skips_llm_call(monkeypatch):
    fake_settings = SimpleNamespace(
        voc=SimpleNamespace(digest_enabled=False, digest_model="claude-test", digest_timeout_seconds=5),
    )
    monkeypatch.setattr(voc_digest, "get_settings", lambda: fake_settings)
    await _patch_db(monkeypatch, [_seed_row(datetime(2026, 8, 10))], [])

    called = False
    async def fake_run_json(**kwargs):
        nonlocal called
        called = True
        return {"headline": "x", "key_findings": [], "product_opportunities": []}
    monkeypatch.setattr(voc_digest.claude_headless, "run_json", fake_run_json)

    result = await voc_digest.generate_weekly_digest("2026-08-03", force=False)
    assert called is False
    assert result["narrative"] is None


# ---------------------------------------------------------------------------
# Recurrence section — "only show anomalies": absent when zero red hits,
# present (and never silently dropped) when there's at least one.
# ---------------------------------------------------------------------------

async def test_digest_markdown_omits_recurrence_section_with_no_red_hits(monkeypatch):
    await _patch_db(monkeypatch, [_seed_row(datetime(2026, 8, 10))], [], recurrence_rows=[
        {"new_issue_id": "n1", "prior_issue_id": "p1", "severity": "yellow",
         "fix_target": "", "fix_version": ""},
    ])
    result = await voc_digest.generate_weekly_digest("2026-08-03", force=False)
    assert result["stats"]["recurrence"]["red_count"] == 0
    assert result["stats"]["recurrence"]["yellow_count"] == 1
    assert "修复复发" not in result["markdown"]


async def test_digest_markdown_includes_recurrence_section_with_red_hits(monkeypatch):
    await _patch_db(monkeypatch, [_seed_row(datetime(2026, 8, 10))], [], recurrence_rows=[
        {"new_issue_id": "n1", "prior_issue_id": "p1", "severity": "red",
         "fix_target": "app", "fix_version": "3.16.0"},
    ])
    result = await voc_digest.generate_weekly_digest("2026-08-03", force=False)
    assert result["stats"]["recurrence"]["red_count"] == 1
    assert "修复复发" in result["markdown"]
    assert "n1" in result["markdown"]
    assert "p1" in result["markdown"]
