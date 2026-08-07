"""Tests for app.services.voc_classifier — the flat-list validator shared with
the CLI-agent path (base.py's _safe_voc_tags) and the dedicated classify_ticket()
LLM call used by the backfill script."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import voc_classifier, voc_taxonomy

ACTIVE_TAGS = [
    {
        "id": "ai-01", "level_1_category": "蓝牙连接", "level_2_label": "配对失败",
        "level_3_diagnosis": "Token不匹配", "definition": "本地 token 与云端不一致",
        "positive_examples": [], "mece_rules": [], "negative_examples": [],
        "updated_by": "", "retired": False,
    },
    {
        "id": "ai-02", "level_1_category": "录音问题", "level_2_label": "录音丢失",
        "level_3_diagnosis": "", "definition": "录音文件在设备/云端均找不到",
        "positive_examples": [], "mece_rules": [], "negative_examples": [],
        "updated_by": "", "retired": False,
    },
    {
        "id": "ai-03", "level_1_category": "会员与支付", "level_2_label": "购买失败",
        "level_3_diagnosis": "", "definition": "支付未到账",
        "positive_examples": [], "mece_rules": [], "negative_examples": [],
        "updated_by": "", "retired": False,
    },
]


@pytest.fixture(autouse=True)
def active_taxonomy(monkeypatch):
    """All tests in this file see the same fixed active-tag set, regardless
    of DB state — classify_ticket/validate_flat_voc_tags only ever read
    voc_taxonomy.active_tags(), never the DB directly."""
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: ACTIVE_TAGS)


# ---------------------------------------------------------------------------
# validate_flat_voc_tags — the flat-list shape base.py._safe_voc_tags feeds in
# ---------------------------------------------------------------------------

def test_validate_flat_voc_tags_legal_output():
    raw = [
        {"tag_id": "ai-01", "role": "primary", "confidence": "high", "reason": "matches"},
        {"tag_id": "ai-02", "role": "secondary", "confidence": "low", "reason": "also relevant"},
    ]
    out = voc_classifier.validate_flat_voc_tags(raw)
    assert [t["tag_id"] for t in out] == ["ai-01", "ai-02"]
    assert out[0]["role"] == "primary"
    assert out[1]["role"] == "secondary"
    assert out[0]["level_1_category"] == "蓝牙连接"  # enriched from active_tags


def test_validate_flat_voc_tags_unknown_tag_id_dropped():
    raw = [
        {"tag_id": "not-a-real-tag", "role": "primary", "confidence": "high", "reason": "hallucinated"},
        {"tag_id": "ai-02", "role": "secondary", "confidence": "medium", "reason": "ok"},
    ]
    # No valid primary survives -> whole result is empty (base.py path omits
    # voc_tags entirely rather than forcing a fallback tag; that distinction
    # belongs to classify_ticket(), not this shared validator).
    out = voc_classifier.validate_flat_voc_tags(raw)
    assert out == []


def test_validate_flat_voc_tags_zero_primary_returns_empty():
    raw = [{"tag_id": "ai-01", "role": "secondary", "confidence": "high", "reason": "no primary given"}]
    out = voc_classifier.validate_flat_voc_tags(raw)
    assert out == []


def test_validate_flat_voc_tags_caps_secondary_at_two():
    raw = [
        {"tag_id": "ai-01", "role": "primary", "confidence": "high", "reason": "x"},
        {"tag_id": "ai-02", "role": "secondary", "confidence": "medium", "reason": "x"},
        {"tag_id": "ai-03", "role": "secondary", "confidence": "medium", "reason": "x"},
        {"tag_id": "not-real", "role": "secondary", "confidence": "medium", "reason": "x"},
    ]
    out = voc_classifier.validate_flat_voc_tags(raw)
    assert len(out) == 3  # 1 primary + at most 2 secondary
    assert [t["tag_id"] for t in out] == ["ai-01", "ai-02", "ai-03"]


def test_validate_flat_voc_tags_retired_tag_dropped(monkeypatch):
    """A tag_id that used to be active but got retired must not survive
    validation even if the model still names it."""
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: ACTIVE_TAGS[:2])  # ai-03 "retired"
    raw = [{"tag_id": "ai-03", "role": "primary", "confidence": "high", "reason": "stale"}]
    out = voc_classifier.validate_flat_voc_tags(raw)
    assert out == []


def test_validate_flat_voc_tags_empty_active_taxonomy_returns_empty(monkeypatch):
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: [])
    out = voc_classifier.validate_flat_voc_tags([{"tag_id": "ai-01", "role": "primary"}])
    assert out == []


def test_validate_flat_voc_tags_duplicate_secondary_ignored():
    raw = [
        {"tag_id": "ai-01", "role": "primary", "confidence": "high", "reason": "x"},
        {"tag_id": "ai-01", "role": "secondary", "confidence": "high", "reason": "dup of primary"},
        {"tag_id": "ai-02", "role": "secondary", "confidence": "high", "reason": "x"},
    ]
    out = voc_classifier.validate_flat_voc_tags(raw)
    assert [t["tag_id"] for t in out] == ["ai-01", "ai-02"]


# ---------------------------------------------------------------------------
# classify_ticket() — the dedicated LLM-call path. Mocks
# voc_classifier.claude_headless.run_json directly (the subprocess-level
# behavior of run_json itself is covered by tests/test_claude_headless.py).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def classifier_settings(monkeypatch):
    fake_settings = SimpleNamespace(
        voc=SimpleNamespace(classifier_model="claude-test", classifier_timeout_seconds=5),
    )
    monkeypatch.setattr(voc_classifier, "get_settings", lambda: fake_settings)
    yield


def _evidence():
    return voc_classifier.TicketEvidence(description="蓝牙配对一直失败，提示 token mismatch")


async def test_classify_ticket_valid_response(monkeypatch):
    async def fake_run_json(**kwargs):
        return {"primary": {"tag_id": "ai-01", "confidence": "high", "reason": "matches token mismatch"},
                "secondary": []}
    monkeypatch.setattr(voc_classifier.claude_headless, "run_json", fake_run_json)
    tags = await voc_classifier.classify_ticket(_evidence())
    assert len(tags) == 1
    assert tags[0]["tag_id"] == "ai-01"
    assert tags[0]["role"] == "primary"


async def test_classify_ticket_unknown_primary_tag_id_falls_back(monkeypatch):
    async def fake_run_json(**kwargs):
        return {"primary": {"tag_id": "does-not-exist", "confidence": "high", "reason": "hallucinated"},
                "secondary": []}
    monkeypatch.setattr(voc_classifier.claude_headless, "run_json", fake_run_json)
    tags = await voc_classifier.classify_ticket(_evidence())
    assert len(tags) == 1
    assert tags[0]["tag_id"] == voc_classifier.FALLBACK_TAG_ID
    assert tags[0]["confidence"] == "low"


async def test_classify_ticket_run_json_failure_falls_back(monkeypatch):
    async def fake_run_json(**kwargs):
        return None
    monkeypatch.setattr(voc_classifier.claude_headless, "run_json", fake_run_json)
    tags = await voc_classifier.classify_ticket(_evidence())
    assert tags[0]["tag_id"] == voc_classifier.FALLBACK_TAG_ID


async def test_classify_ticket_empty_active_taxonomy_falls_back_without_calling_run_json(monkeypatch):
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: [])
    called = False
    async def fake_run_json(**kwargs):
        nonlocal called
        called = True
        return {"primary": {"tag_id": "ai-01", "confidence": "high", "reason": "x"}, "secondary": []}
    monkeypatch.setattr(voc_classifier.claude_headless, "run_json", fake_run_json)
    tags = await voc_classifier.classify_ticket(_evidence())
    assert tags[0]["tag_id"] == voc_classifier.FALLBACK_TAG_ID
    assert called is False


async def test_classify_ticket_passes_taxonomy_and_evidence_through(monkeypatch):
    """The system prompt must embed the active taxonomy and the user input
    must be the evidence text — regression guard against silently passing
    the wrong strings into run_json after this refactor."""
    captured = {}
    async def fake_run_json(**kwargs):
        captured.update(kwargs)
        return {"primary": {"tag_id": "ai-01", "confidence": "high", "reason": "x"}, "secondary": []}
    monkeypatch.setattr(voc_classifier.claude_headless, "run_json", fake_run_json)
    await voc_classifier.classify_ticket(_evidence())
    assert "ai-01" in captured["system_prompt"]  # active tag id present in taxonomy payload
    assert "token mismatch" in captured["user_input"]
    assert captured["model"] == "claude-test"
    assert captured["timeout"] == 5.0
