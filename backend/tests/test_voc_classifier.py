"""Tests for app.services.voc_classifier — the flat-list validator shared with
the CLI-agent path (base.py's _safe_voc_tags) and the dedicated classify_ticket()
LLM call used by the backfill script."""
from __future__ import annotations

import asyncio
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
# classify_ticket() — the dedicated LLM-call path, now a headless `claude` CLI
# subprocess (`-p --output-format json`) instead of a direct API call. Fake
# asyncio.create_subprocess_exec rather than httpx.
# ---------------------------------------------------------------------------

class _FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, delay=0.0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._delay = delay
        self.killed = False

    async def communicate(self, input=None):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


_next_process: "_FakeProcess | None" = None
_raise_not_found = False


async def _fake_create_subprocess_exec(*args, **kwargs):
    if _raise_not_found:
        raise FileNotFoundError("claude: command not found")
    return _next_process


def _set_fake_process(proc: "_FakeProcess | None", raise_not_found: bool = False):
    global _next_process, _raise_not_found
    _next_process = proc
    _raise_not_found = raise_not_found


def _envelope(result_obj) -> bytes:
    """Build a `claude -p --output-format json` stdout envelope wrapping the
    given object as the JSON-encoded `.result` string (structured output)."""
    import json as _json
    return _json.dumps({
        "type": "result", "is_error": False, "stop_reason": "tool_use",
        "result": _json.dumps(result_obj, ensure_ascii=False),
    }).encode("utf-8")


@pytest.fixture(autouse=True)
def classifier_settings(monkeypatch):
    fake_settings = SimpleNamespace(
        voc=SimpleNamespace(classifier_model="claude-test", classifier_timeout_seconds=5),
    )
    monkeypatch.setattr(voc_classifier, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(voc_classifier, "_make_cli_env", lambda: {})
    monkeypatch.setattr(voc_classifier.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    _set_fake_process(_FakeProcess())
    yield
    _set_fake_process(_FakeProcess())


def _evidence():
    return voc_classifier.TicketEvidence(description="蓝牙配对一直失败，提示 token mismatch")


async def test_classify_ticket_valid_response():
    _set_fake_process(_FakeProcess(stdout=_envelope({
        "primary": {"tag_id": "ai-01", "confidence": "high", "reason": "matches token mismatch"},
        "secondary": [],
    })))
    tags = await voc_classifier.classify_ticket(_evidence())
    assert len(tags) == 1
    assert tags[0]["tag_id"] == "ai-01"
    assert tags[0]["role"] == "primary"


async def test_classify_ticket_unknown_primary_tag_id_falls_back():
    _set_fake_process(_FakeProcess(stdout=_envelope({
        "primary": {"tag_id": "does-not-exist", "confidence": "high", "reason": "hallucinated"},
        "secondary": [],
    })))
    tags = await voc_classifier.classify_ticket(_evidence())
    assert len(tags) == 1
    assert tags[0]["tag_id"] == voc_classifier.FALLBACK_TAG_ID
    assert tags[0]["confidence"] == "low"


async def test_classify_ticket_non_json_output_falls_back():
    _set_fake_process(_FakeProcess(stdout=b"not a json envelope at all"))
    tags = await voc_classifier.classify_ticket(_evidence())
    assert tags[0]["tag_id"] == voc_classifier.FALLBACK_TAG_ID


async def test_classify_ticket_nonzero_exit_falls_back():
    _set_fake_process(_FakeProcess(stdout=b"", stderr=b"boom", returncode=1))
    tags = await voc_classifier.classify_ticket(_evidence())
    assert tags[0]["tag_id"] == voc_classifier.FALLBACK_TAG_ID


async def test_classify_ticket_timeout_falls_back():
    _set_fake_process(_FakeProcess(delay=999))
    fake_settings = SimpleNamespace(
        voc=SimpleNamespace(classifier_model="claude-test", classifier_timeout_seconds=0.05),
    )
    import app.services.voc_classifier as mod
    orig_get_settings = mod.get_settings
    mod.get_settings = lambda: fake_settings
    try:
        tags = await voc_classifier.classify_ticket(_evidence())
    finally:
        mod.get_settings = orig_get_settings
    assert tags[0]["tag_id"] == voc_classifier.FALLBACK_TAG_ID
    assert _next_process.killed is True


async def test_classify_ticket_cli_not_found_falls_back():
    _set_fake_process(None, raise_not_found=True)
    tags = await voc_classifier.classify_ticket(_evidence())
    assert tags[0]["tag_id"] == voc_classifier.FALLBACK_TAG_ID


async def test_classify_ticket_empty_active_taxonomy_falls_back_without_subprocess(monkeypatch):
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: [])
    # No fake process configured for this call — if the code tried to spawn
    # one it would get None back and crash on .communicate(); success here
    # proves it short-circuited before that.
    _set_fake_process(None)
    tags = await voc_classifier.classify_ticket(_evidence())
    assert tags[0]["tag_id"] == voc_classifier.FALLBACK_TAG_ID
