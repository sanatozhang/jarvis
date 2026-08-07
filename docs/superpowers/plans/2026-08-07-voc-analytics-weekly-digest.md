# VOC 分类可视化升级 + 周度工单洞察汇总 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make VOC Portal taxonomy the sole classification system driving `/analytics` (Top10/trend/pie all recomputed on VOC tags, page-level switch vs. legacy), add a weekly "上周焦点" insight digest (deterministic stats + LLM narrative grounded in `root_cause`), and close the taxonomy-drift gap in the seed→DB bootstrap path.

**Architecture:** Backend: a pure-function stats layer (`voc_digest.py`) sits on top of a new raw-row DB reader; a shared headless-CLI caller (`claude_headless.py`, extracted from the existing `voc_classifier.py`) is reused for LLM narrative generation; results cache in a new `voc_weekly_digests` table. Frontend: existing hand-rolled SVG chart code in `analytics/page.tsx` is reused as-is for VOC data instead of `problem_type`; a new digest card and a `/reports` tab render the cached narrative.

**Tech Stack:** FastAPI + SQLAlchemy (async) + SQLite, Next.js 15 / React 19 (no chart library — hand-rolled SVG), Python stdlib `asyncio.subprocess` to call the local `claude` CLI headless.

## Global Constraints

- Respond to the user in 中文 throughout (session-level instruction, not code).
- Never write/deploy without explicit per-instance user confirmation (existing project rule) — this plan produces code only; deploy/execute steps stay separate and are called out at the end.
- `cd backend && lint-imports` must keep passing after every task (crashguard isolation contract) — do not import from `app.crashguard.*` anywhere in this plan's files.
- All new backend modules follow the existing "no I/O in pure functions" pattern used by `voc_classifier.py` / `voc_taxonomy.py`.
- All frontend API calls go through `frontend/src/lib/api.ts` wrappers — components never `fetch` directly (frontend/CLAUDE.md).
- All new UI copy needs a `t("...")` key added to `frontend/src/lib/i18n.ts` (frontend/CLAUDE.md).

---

## Task 1: Extract shared headless-CLI JSON caller

**Files:**
- Create: `backend/app/services/claude_headless.py`
- Test: `backend/tests/test_claude_headless.py`

**Interfaces:**
- Produces: `async def run_json(system_prompt: str, user_input: str, schema: dict, model: str, timeout: float, *, log_prefix: str = "claude_headless") -> Optional[dict]` — spawns the local `claude` CLI headless, returns the parsed JSON object on success, `None` on any failure (CLI missing / timeout / non-zero exit / non-JSON / non-dict output). Never raises.
- Consumes: `app.agents.claude_code._make_cli_env`, `app.agents.claude_code.parse_cli_result_envelope` (both already exist and are used identically by `voc_classifier.py` today).

This is a pure extraction of the subprocess-calling logic already proven in `voc_classifier.classify_ticket()` (`backend/app/services/voc_classifier.py:280-335`) — Task 2 will point `classify_ticket()` at it. Task 8 (weekly digest narrative) reuses it a second time, which is the actual motivation: without this extraction we'd have two copies of the same ~50-line subprocess dance.

- [ ] **Step 1: Write the test file against the not-yet-existing module**

```python
"""Tests for app.services.claude_headless.run_json — the shared subprocess
caller extracted from voc_classifier.classify_ticket(). Mocks
asyncio.create_subprocess_exec directly (no real CLI binary needed)."""
from __future__ import annotations

import asyncio
import json as _json

import pytest

from app.services import claude_headless


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


def _set_fake_process(proc, raise_not_found: bool = False):
    global _next_process, _raise_not_found
    _next_process = proc
    _raise_not_found = raise_not_found


def _envelope(result_obj) -> bytes:
    return _json.dumps({
        "type": "result", "is_error": False, "stop_reason": "tool_use",
        "result": _json.dumps(result_obj, ensure_ascii=False),
    }).encode("utf-8")


@pytest.fixture(autouse=True)
def patched_subprocess(monkeypatch):
    monkeypatch.setattr(claude_headless, "_make_cli_env", lambda: {})
    monkeypatch.setattr(claude_headless.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    _set_fake_process(_FakeProcess())
    yield
    _set_fake_process(_FakeProcess())


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


async def test_run_json_success():
    _set_fake_process(_FakeProcess(stdout=_envelope({"ok": True})))
    result = await claude_headless.run_json(
        system_prompt="sys", user_input="hi", schema=SCHEMA, model="claude-test", timeout=5,
    )
    assert result == {"ok": True}


async def test_run_json_non_json_output_returns_none():
    _set_fake_process(_FakeProcess(stdout=b"not json"))
    result = await claude_headless.run_json(
        system_prompt="sys", user_input="hi", schema=SCHEMA, model="claude-test", timeout=5,
    )
    assert result is None


async def test_run_json_non_dict_json_returns_none():
    _set_fake_process(_FakeProcess(stdout=_envelope([1, 2, 3])))
    result = await claude_headless.run_json(
        system_prompt="sys", user_input="hi", schema=SCHEMA, model="claude-test", timeout=5,
    )
    assert result is None


async def test_run_json_nonzero_exit_returns_none():
    _set_fake_process(_FakeProcess(stdout=b"", stderr=b"boom", returncode=1))
    result = await claude_headless.run_json(
        system_prompt="sys", user_input="hi", schema=SCHEMA, model="claude-test", timeout=5,
    )
    assert result is None


async def test_run_json_timeout_kills_process_and_returns_none():
    _set_fake_process(_FakeProcess(delay=999))
    result = await claude_headless.run_json(
        system_prompt="sys", user_input="hi", schema=SCHEMA, model="claude-test", timeout=0.05,
    )
    assert result is None
    assert _next_process.killed is True


async def test_run_json_cli_not_found_returns_none():
    _set_fake_process(None, raise_not_found=True)
    result = await claude_headless.run_json(
        system_prompt="sys", user_input="hi", schema=SCHEMA, model="claude-test", timeout=5,
    )
    assert result is None
```

- [ ] **Step 2: Run the tests, confirm they fail with `ModuleNotFoundError` / `AttributeError`**

Run: `cd backend && source .venv/bin/activate && pytest tests/test_claude_headless.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.claude_headless'`

- [ ] **Step 3: Implement `claude_headless.py`**

```python
"""Shared headless `claude -p` CLI JSON-output caller.

Extracted from voc_classifier.classify_ticket() so other one-shot
structured-output LLM calls (voc_digest's weekly narrative generation, and
classify_ticket() itself) can reuse the same subprocess-invocation, timeout,
and envelope-parsing logic without duplicating it.

Why this goes through the local CLI (headless, OAuth-authenticated) rather
than a direct Messages API call: production runs `agent.call_mode: cli` with
no ANTHROPIC_API_KEY provisioned — see voc_classifier.py's module docstring
for the full explanation (an API key present in the CLI's env triggers an
interactive prompt that hangs a non-TTY subprocess).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from app.agents.claude_code import _make_cli_env, parse_cli_result_envelope

logger = logging.getLogger("jarvis.claude_headless")


async def run_json(
    system_prompt: str,
    user_input: str,
    schema: Dict[str, Any],
    model: str,
    timeout: float,
    *,
    log_prefix: str = "claude_headless",
) -> Optional[Dict[str, Any]]:
    """Run one headless `claude -p --output-format json` turn constrained to
    `schema` (piped via --json-schema), and return the parsed JSON object.

    Returns None — never raises — on any failure: CLI binary missing, call
    timed out, non-zero exit code, non-JSON `.result` text, or JSON that
    isn't an object. Callers apply their own fallback policy on None.
    """
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--model", model,
        "--tools", "",
        "--no-session-persistence",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--system-prompt", system_prompt,
        "--json-schema", json.dumps(schema),
    ]

    scratch = Path(tempfile.mkdtemp(prefix=f"{log_prefix}_"))
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(scratch),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_make_cli_env(),
            )
        except FileNotFoundError:
            logger.warning("%s: claude CLI binary not found", log_prefix)
            return None

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=user_input.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("%s: CLI call timed out after %ss", log_prefix, timeout)
            return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if proc.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        logger.warning("%s: CLI exited %d: %s", log_prefix, proc.returncode, stderr[:300])
        return None

    stdout_raw = stdout_bytes.decode("utf-8", errors="replace")
    text, _usage, _cost, _source = parse_cli_result_envelope(stdout_raw)

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("%s: CLI returned non-JSON output: %s", log_prefix, text[:300])
        return None

    if not isinstance(data, dict):
        logger.warning("%s: CLI JSON output was not an object", log_prefix)
        return None

    return data
```

- [ ] **Step 4: Run the tests again, confirm they pass**

Run: `cd backend && pytest tests/test_claude_headless.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/claude_headless.py backend/tests/test_claude_headless.py
git commit -m "feat(voc): extract shared headless-CLI JSON caller from voc_classifier"
```

---

## Task 2: Point `voc_classifier.classify_ticket()` at `claude_headless.run_json`

**Files:**
- Modify: `backend/app/services/voc_classifier.py:262-335` (the `classify_ticket` function body)
- Modify: `backend/tests/test_voc_classifier.py` (mock target moves from raw subprocess to `claude_headless.run_json`)

**Interfaces:**
- Consumes: `claude_headless.run_json` from Task 1.
- Produces: `classify_ticket()`'s public contract is unchanged — still `async def classify_ticket(evidence: TicketEvidence) -> List[Dict[str, Any]]`, still always returns a non-empty list (fallback tag on any failure). No caller elsewhere needs to change.

- [ ] **Step 1: Rewrite `classify_ticket()` to delegate to `claude_headless.run_json`**

In `backend/app/services/voc_classifier.py`, replace the imports at the top:

```python
from app.agents.claude_code import _make_cli_env, parse_cli_result_envelope
```

with:

```python
from app.services import claude_headless
```

(remove `asyncio`, `shutil`, `tempfile`, `Path` imports from this file if nothing else in it uses them — check with `grep -n "asyncio\.\|shutil\.\|tempfile\.\|Path(" backend/app/services/voc_classifier.py` after the edit; they were only used inside the subprocess block being replaced.)

Replace the entire body of `classify_ticket()` (lines 262-335) with:

```python
async def classify_ticket(evidence: TicketEvidence) -> List[Dict[str, Any]]:
    """Classify one ticket. Returns the voc_tags_json list (1 primary + <=2
    secondary), already validated against the active taxonomy — always a
    non-empty list (falls back to FALLBACK_TAG_ID rather than raising) so
    callers can write the result unconditionally.

    Delegates the actual CLI subprocess call to app.services.claude_headless
    (see that module's docstring for why this goes through the local CLI
    rather than a direct Messages API call).
    """
    active = voc_taxonomy.active_tags()
    active_ids = {t["id"] for t in active}
    if not active_ids:
        logger.warning("VOC classifier called with an empty active taxonomy — falling back")
        return _fallback("no active VOC taxonomy loaded")

    settings = get_settings()
    data = await claude_headless.run_json(
        system_prompt=_build_system_prompt(),
        user_input=evidence.to_prompt_text(),
        schema=_CLASSIFY_TOOL["input_schema"],
        model=settings.voc.classifier_model,
        timeout=float(settings.voc.classifier_timeout_seconds),
        log_prefix="voc_classify",
    )
    if data is None:
        return _fallback("classifier CLI call failed")

    return _validate_tags(data, active_ids)
```

- [ ] **Step 2: Rewrite `test_voc_classifier.py`'s classify_ticket() test section to mock `claude_headless.run_json` instead of subprocess**

Replace the entire block from `# classify_ticket() — the dedicated LLM-call path...` (line 114) through the end of the file with:

```python
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
```

Also remove the now-unused `import asyncio` from the top of the test file if nothing else there needs it (check with `grep -n "asyncio\." backend/tests/test_voc_classifier.py` after the edit — should show no remaining uses).

- [ ] **Step 3: Run the full VOC classifier test suite**

Run: `cd backend && pytest tests/test_voc_classifier.py -v`
Expected: all tests pass (validate_flat_voc_tags tests untouched, classify_ticket tests now mock `claude_headless.run_json`)

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/voc_classifier.py backend/tests/test_voc_classifier.py
git commit -m "refactor(voc): classify_ticket delegates to shared claude_headless.run_json"
```

---

## Task 3: `voc_digest.py` — pure aggregation functions

**Files:**
- Create: `backend/app/services/voc_digest.py` (this task only: the pure-function half; Task 8 adds the LLM-orchestration half to the same file)
- Test: `backend/tests/test_voc_digest.py`

**Interfaces:**
- Consumes: rows shaped like `{"created_at": datetime, "voc_tags_json": str, "root_cause": str, "device_type": str, "needs_engineer": bool, ...}` — this is exactly what Task 4's `get_voc_analysis_rows()` returns.
- Produces:
  - `default_week_start(today: Optional[date] = None) -> str` — ISO date string of the most recent COMPLETE week's Monday.
  - `aggregate_trend(rows: List[Dict], level: str = "group") -> Dict[str, Dict[str, int]]` — `{date_str: {key: count}}`.
  - `aggregate_movers(cur_rows: List[Dict], prev_rows: List[Dict], level: str = "label", min_base: int = 3) -> List[Dict]` — sorted by `abs(delta)` desc.
  - `compute_weekly_stats(cur_rows: List[Dict], prev_rows: List[Dict], min_base: int = 3) -> Dict[str, Any]`.
  - `sample_root_causes(rows: List[Dict], top_n_groups: int = 5, max_per_group: int = 8) -> Dict[str, List[str]]`.
  - These are the exact names Task 6 (API endpoints) and Task 8 (digest generation) will import.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `cd backend && pytest tests/test_voc_digest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.voc_digest'`

- [ ] **Step 3: Implement the pure-function half of `voc_digest.py`**

```python
"""VOC weekly insight digest — deterministic stats layer (this file) plus
LLM narrative generation (generate_weekly_digest, added by a later task).

Pure functions in this section take rows shaped like
app.db.database.get_voc_analysis_rows()'s return value and do no I/O — kept
separate from the DB/LLM-calling orchestration below so they're trivially
unit-testable and reusable from both the /api/voc/trend and /api/voc/movers
endpoints and the weekly digest generator.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("jarvis.voc_digest")


def default_week_start(today: Optional[date] = None) -> str:
    """Most recent COMPLETE week's Monday (ISO date string). If `today` is
    itself mid-week, that week isn't done yet, so this points at the week
    before it — e.g. on Wed 2026-08-12, returns 2026-08-03 (last Monday),
    not 2026-08-10 (this week's Monday, still in progress). On a Monday,
    "today" is the very start of a new week, so this still returns the
    prior Monday — the week that just finished."""
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    return last_monday.isoformat()


def _primary_tag(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The primary VOC tag dict for a row, or None if untagged/malformed."""
    try:
        tags = json.loads(row.get("voc_tags_json") or "[]")
    except (ValueError, TypeError):
        return None
    for t in tags:
        if isinstance(t, dict) and t.get("role") == "primary":
            return t
    return None


def _level_key(tag: Dict[str, Any], level: str) -> str:
    group = tag.get("level_1_category") or "未分类"
    if level == "group":
        return group
    label = tag.get("level_2_label") or ""
    return f"{group} › {label}" if label else group


def _row_date(row: Dict[str, Any]) -> str:
    v = row.get("created_at")
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]


def _count_by_key(rows: List[Dict[str, Any]], level: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        tag = _primary_tag(row)
        if not tag:
            continue
        key = _level_key(tag, level)
        counts[key] = counts.get(key, 0) + 1
    return counts


def aggregate_trend(rows: List[Dict[str, Any]], level: str = "group") -> Dict[str, Dict[str, int]]:
    """`{date_str: {key: count}}` for a multi-line trend chart. Rows with no
    primary VOC tag (never classified, or classifier fell back) are skipped
    — they carry no group/label to bucket by."""
    trend: Dict[str, Dict[str, int]] = {}
    for row in rows:
        tag = _primary_tag(row)
        if not tag:
            continue
        d = _row_date(row)
        key = _level_key(tag, level)
        trend.setdefault(d, {})
        trend[d][key] = trend[d].get(key, 0) + 1
    return trend


def aggregate_movers(
    cur_rows: List[Dict[str, Any]], prev_rows: List[Dict[str, Any]],
    level: str = "label", min_base: int = 3,
) -> List[Dict[str, Any]]:
    """Week-over-week movers, sorted by |delta| descending. A key is
    excluded unless `max(cur, prev) >= min_base` — at low ticket volumes,
    e.g. 1 -> 3 tickets is a +200% swing that's pure noise; min_base keeps
    the movers list meaningful rather than dominated by tiny denominators.

    delta_pct is None when prev == 0 (no baseline to compare against —
    frontend should render this as "new" rather than a percentage).
    """
    cur_counts = _count_by_key(cur_rows, level)
    prev_counts = _count_by_key(prev_rows, level)
    keys = set(cur_counts) | set(prev_counts)

    movers = []
    for key in keys:
        cur = cur_counts.get(key, 0)
        prev = prev_counts.get(key, 0)
        if max(cur, prev) < min_base:
            continue
        delta = cur - prev
        delta_pct = round(delta / prev * 100, 1) if prev else None
        movers.append({"key": key, "cur": cur, "prev": prev, "delta": delta, "delta_pct": delta_pct})

    movers.sort(key=lambda m: abs(m["delta"]), reverse=True)
    return movers


def compute_weekly_stats(
    cur_rows: List[Dict[str, Any]], prev_rows: List[Dict[str, Any]], min_base: int = 3,
) -> Dict[str, Any]:
    """Full deterministic stats package for one week: group distribution,
    total volume + week-over-week delta, top movers (label level), the
    needs_engineer rate, and device distribution. This is the ONLY source
    of numbers the LLM narrative (Task 8) is allowed to cite — see that
    task's system prompt."""
    group_counts = _count_by_key(cur_rows, level="group")
    groups = [{"group": g, "count": c} for g, c in sorted(group_counts.items(), key=lambda x: -x[1])]

    total_cur = len(cur_rows)
    total_prev = len(prev_rows)
    total_delta = total_cur - total_prev
    total_delta_pct = round(total_delta / total_prev * 100, 1) if total_prev else None

    top_movers = aggregate_movers(cur_rows, prev_rows, level="label", min_base=min_base)[:10]

    needs_engineer_cur = sum(1 for r in cur_rows if r.get("needs_engineer"))
    needs_engineer_rate = round(needs_engineer_cur / total_cur * 100, 1) if total_cur else 0.0

    device_counts: Dict[str, int] = {}
    for r in cur_rows:
        d = r.get("device_type") or "未知"
        device_counts[d] = device_counts.get(d, 0) + 1
    devices = [{"device_type": d, "count": c} for d, c in sorted(device_counts.items(), key=lambda x: -x[1])]

    return {
        "total_cur": total_cur,
        "total_prev": total_prev,
        "total_delta": total_delta,
        "total_delta_pct": total_delta_pct,
        "groups": groups,
        "top_movers": top_movers,
        "needs_engineer_rate": needs_engineer_rate,
        "devices": devices,
    }


def sample_root_causes(
    rows: List[Dict[str, Any]], top_n_groups: int = 5, max_per_group: int = 8,
) -> Dict[str, List[str]]:
    """Sample real root_cause text per top-volume VOC group — the only raw
    material the LLM narrative (Task 8) has for spotting "this is a product
    problem, not a user error" patterns (e.g. the timestamp/power-loss
    example from the design doc). Deduplicates identical root_cause strings
    within a group and truncates each to 500 chars. Groups with zero
    non-empty root_cause samples are omitted from the result."""
    group_counts = _count_by_key(rows, level="group")
    top_groups = [g for g, _ in sorted(group_counts.items(), key=lambda x: -x[1])[:top_n_groups]]

    samples: Dict[str, List[str]] = {g: [] for g in top_groups}
    seen: Dict[str, set] = {g: set() for g in top_groups}

    for row in rows:
        tag = _primary_tag(row)
        if not tag:
            continue
        group = tag.get("level_1_category") or "未分类"
        if group not in samples:
            continue
        rc = (row.get("root_cause") or "").strip()
        if not rc or rc in seen[group] or len(samples[group]) >= max_per_group:
            continue
        samples[group].append(rc[:500])
        seen[group].add(rc)

    return {g: v for g, v in samples.items() if v}
```

- [ ] **Step 4: Run the tests, confirm they pass**

Run: `cd backend && pytest tests/test_voc_digest.py -v`
Expected: all tests pass

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/voc_digest.py backend/tests/test_voc_digest.py
git commit -m "feat(voc): add pure-function weekly stats aggregation layer"
```

---

## Task 4: DB layer — raw row reader + weekly digest cache table

**Files:**
- Modify: `backend/app/db/database.py` (add `AnalysisRecord`-reading function, new ORM model, CRUD functions)
- Test: `backend/tests/test_voc_digest_db.py`

**Interfaces:**
- Produces:
  - `async def get_voc_analysis_rows(date_from: str, date_to: str) -> List[Dict[str, Any]]` — rows shaped `{issue_id, created_at, voc_tags_json, problem_type, root_cause, device_type, platform, needs_engineer}`.
  - ORM class `VocWeeklyDigest` (table `voc_weekly_digests`).
  - `async def get_voc_weekly_digest(week_start: str) -> Optional[Dict[str, Any]]`.
  - `async def list_voc_weekly_digests(limit: int = 12) -> List[Dict[str, Any]]`.
  - `async def upsert_voc_weekly_digest(week_start: str, stats: dict, narrative: Optional[dict], markdown: str, model: str = "", total_tokens: int = 0, total_cost_usd: float = 0.0) -> Dict[str, Any]`.
- Consumes: nothing new — uses the existing `get_session()`, `Base`, `AnalysisRecord` already in this file.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the VOC digest DB layer: get_voc_analysis_rows (raw row reader
for app.services.voc_digest) and the voc_weekly_digests cache table CRUD."""
from __future__ import annotations

import json

import pytest


async def test_get_voc_analysis_rows_shape_and_date_filter(db_engine, db_session):
    import app.db.database as db_mod
    from app.db.database import AnalysisRecord
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        async with db_session() as s:
            s.add(AnalysisRecord(
                task_id="t1", issue_id="i1", created_at=__import__("datetime").datetime(2026, 8, 10),
                voc_tags_json=json.dumps([{"tag_id": "ai-01", "role": "primary"}]),
                root_cause="token mismatch", device_type="Note", platform="app", needs_engineer=True,
            ))
            s.add(AnalysisRecord(
                task_id="t2", issue_id="i2", created_at=__import__("datetime").datetime(2026, 7, 1),
                voc_tags_json="[]",
            ))
            await s.commit()

        rows = await db_mod.get_voc_analysis_rows("2026-08-01", "2026-08-31")
        assert len(rows) == 1  # the July row is outside the range
        row = rows[0]
        assert row["issue_id"] == "i1"
        assert row["needs_engineer"] is True
        assert row["device_type"] == "Note"
        assert json.loads(row["voc_tags_json"])[0]["tag_id"] == "ai-01"
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_get_voc_analysis_rows_empty_range_returns_empty_list(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        rows = await db_mod.get_voc_analysis_rows("2026-01-01", "2026-01-31")
        assert rows == []
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_upsert_voc_weekly_digest_creates_and_updates(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        stats = {"total_cur": 10}
        narrative = {"headline": "test week"}
        record = await db_mod.upsert_voc_weekly_digest(
            week_start="2026-08-03", stats=stats, narrative=narrative,
            markdown="# hi", model="claude-test", total_tokens=100, total_cost_usd=0.01,
        )
        assert record["week_start"] == "2026-08-03"
        assert record["stats"] == stats
        assert record["narrative"] == narrative
        assert record["markdown"] == "# hi"

        fetched = await db_mod.get_voc_weekly_digest("2026-08-03")
        assert fetched["stats"] == stats

        # Second call with the same week_start updates in place, not a duplicate row
        updated = await db_mod.upsert_voc_weekly_digest(
            week_start="2026-08-03", stats={"total_cur": 20}, narrative=None, markdown="# updated",
        )
        assert updated["stats"] == {"total_cur": 20}
        assert updated["narrative"] is None
        all_digests = await db_mod.list_voc_weekly_digests(limit=10)
        assert len(all_digests) == 1
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_get_voc_weekly_digest_missing_returns_none(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        assert await db_mod.get_voc_weekly_digest("2099-01-01") is None
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_list_voc_weekly_digests_orders_newest_first(db_engine, db_session):
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        await db_mod.upsert_voc_weekly_digest(week_start="2026-07-27", stats={}, narrative=None, markdown="")
        await db_mod.upsert_voc_weekly_digest(week_start="2026-08-10", stats={}, narrative=None, markdown="")
        await db_mod.upsert_voc_weekly_digest(week_start="2026-08-03", stats={}, narrative=None, markdown="")
        digests = await db_mod.list_voc_weekly_digests(limit=10)
        assert [d["week_start"] for d in digests] == ["2026-08-10", "2026-08-03", "2026-07-27"]
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory
```

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `cd backend && pytest tests/test_voc_digest_db.py -v`
Expected: FAIL — `AttributeError: module 'app.db.database' has no attribute 'get_voc_analysis_rows'`

- [ ] **Step 3: Add `get_voc_analysis_rows()` right after `get_voc_classification_stats()`**

In `backend/app/db/database.py`, immediately after the end of `get_voc_classification_stats()` (the function ending around line 2420, right before the `=== voc_sync_loop ===` grep marker in earlier exploration — locate it by searching for `return {\n            "date_from": date_from,\n            "date_to": date_to,\n            "total": len(rows),\n            "total_tagged": total_tagged,\n            "groups": groups,\n        }` and insert after its closing blank line), add:

```python
async def get_voc_analysis_rows(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Raw per-analysis rows for VOC trend/movers/digest aggregation
    (app.services.voc_digest) — one row per analysis in [date_from, date_to]
    (inclusive, same local-date granularity as get_voc_classification_stats).
    Callers parse voc_tags_json themselves via voc_digest._primary_tag; this
    function is just the DB round-trip.
    """
    async with get_session() as session:
        from sqlalchemy import select, and_

        start = datetime.fromisoformat(date_from)
        end = datetime.fromisoformat(date_to + "T23:59:59")

        stmt = select(
            AnalysisRecord.issue_id,
            AnalysisRecord.created_at,
            AnalysisRecord.voc_tags_json,
            AnalysisRecord.problem_type,
            AnalysisRecord.root_cause,
            AnalysisRecord.device_type,
            AnalysisRecord.platform,
            AnalysisRecord.needs_engineer,
        ).where(and_(
            AnalysisRecord.created_at >= start,
            AnalysisRecord.created_at <= end,
        ))
        rows = (await session.execute(stmt)).fetchall()
        return [
            {
                "issue_id": r.issue_id,
                "created_at": r.created_at,
                "voc_tags_json": r.voc_tags_json,
                "problem_type": r.problem_type,
                "root_cause": r.root_cause,
                "device_type": r.device_type,
                "platform": r.platform,
                "needs_engineer": bool(r.needs_engineer),
            }
            for r in rows
        ]
```

- [ ] **Step 4: Add the `VocWeeklyDigest` ORM model right after `VocTagRecord`**

Find the end of the `VocTagRecord` class definition (it's the class right before `_voc_tag_to_dict`, per earlier exploration ending with `synced_at = Column(...)` or similar). Immediately after that class, add:

```python
class VocWeeklyDigest(Base):
    """Cached weekly VOC insight digest (app.services.voc_digest). One row
    per ISO week (week_start = that week's Monday, "YYYY-MM-DD"). Generation
    involves an LLM call that can take tens of seconds, so this is a cache
    to read from on every page load, not a view recomputed per-request —
    regenerate via generate_weekly_digest(force=True), not by deleting rows."""
    __tablename__ = "voc_weekly_digests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    week_start = Column(String(10), unique=True, index=True, nullable=False)
    stats_json = Column(Text, default="{}")        # compute_weekly_stats() output
    narrative_json = Column(Text, default="null")  # LLM output dict, or JSON null if generation failed/disabled
    markdown = Column(Text, default="")
    model = Column(String(128), default="")
    total_tokens = Column(Integer, default=0)
    total_cost_usd = Column(Float, default=0.0)
    generated_at = Column(DateTime, default=datetime.utcnow)
```

- [ ] **Step 5: Add the CRUD functions right after `get_voc_analysis_rows()`**

```python
def _voc_weekly_digest_to_dict(row: "VocWeeklyDigest") -> Dict[str, Any]:
    return {
        "week_start": row.week_start,
        "stats": json.loads(row.stats_json or "{}"),
        "narrative": json.loads(row.narrative_json or "null"),
        "markdown": row.markdown or "",
        "model": row.model or "",
        "total_tokens": row.total_tokens or 0,
        "total_cost_usd": row.total_cost_usd or 0.0,
        "generated_at": row.generated_at.isoformat() if row.generated_at else None,
    }


async def get_voc_weekly_digest(week_start: str) -> Optional[Dict[str, Any]]:
    async with get_session() as session:
        from sqlalchemy import select
        row = (await session.execute(
            select(VocWeeklyDigest).where(VocWeeklyDigest.week_start == week_start)
        )).scalar_one_or_none()
        return _voc_weekly_digest_to_dict(row) if row else None


async def list_voc_weekly_digests(limit: int = 12) -> List[Dict[str, Any]]:
    async with get_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(VocWeeklyDigest).order_by(VocWeeklyDigest.week_start.desc()).limit(limit)
        )).scalars().all()
        return [_voc_weekly_digest_to_dict(r) for r in rows]


async def upsert_voc_weekly_digest(
    week_start: str, stats: Dict[str, Any], narrative: Optional[Dict[str, Any]],
    markdown: str, model: str = "", total_tokens: int = 0, total_cost_usd: float = 0.0,
) -> Dict[str, Any]:
    async with get_session() as session:
        from sqlalchemy import select
        row = (await session.execute(
            select(VocWeeklyDigest).where(VocWeeklyDigest.week_start == week_start)
        )).scalar_one_or_none()
        if row is None:
            row = VocWeeklyDigest(week_start=week_start)
            session.add(row)
        row.stats_json = json.dumps(stats, ensure_ascii=False)
        row.narrative_json = json.dumps(narrative, ensure_ascii=False) if narrative is not None else "null"
        row.markdown = markdown
        row.model = model
        row.total_tokens = total_tokens
        row.total_cost_usd = total_cost_usd
        row.generated_at = datetime.utcnow()
        await session.commit()
        return _voc_weekly_digest_to_dict(row)
```

Check that `Optional` is already imported from `typing` at the top of `database.py` (it's used elsewhere in the file per the existing `get_voc_tags`/backfill functions' style) — add it to the existing `from typing import ...` line if missing.

- [ ] **Step 6: Run the tests, confirm they pass**

Run: `cd backend && pytest tests/test_voc_digest_db.py -v`
Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add backend/app/db/database.py backend/tests/test_voc_digest_db.py
git commit -m "feat(voc): add get_voc_analysis_rows reader and voc_weekly_digests cache table"
```

---

## Task 5: `sync_seed_to_db(force=...)` — return a diff, support forced re-upsert

**Files:**
- Modify: `backend/app/services/voc_taxonomy.py:49-72` (`sync_seed_to_db`)
- Modify: `backend/app/main.py` (the one caller, in lifespan startup)
- Modify: `backend/tests/test_voc_taxonomy.py` (3 existing tests assert the old `int` return shape)

**Interfaces:**
- Produces: `async def sync_seed_to_db(force: bool = False) -> Dict[str, Any]` — now returns `{"added": [...], "changed": [...], "retired": [...], "skipped": bool}` instead of an `int`. `skipped=True` when the table already had data and `force=False` (old no-op behavior), or when no seed file/tags exist.

This is a **breaking return-type change** to an existing function — both callers (`main.py` startup, and the tests) must be updated in this same task or the build breaks.

- [ ] **Step 1: Update the 3 existing tests that assert the old `int` shape**

In `backend/tests/test_voc_taxonomy.py`:

```python
async def test_sync_seed_to_db_skips_when_db_already_has_tags(db_engine, db_session):
    import app.db.database as db_mod
    from app.services import voc_taxonomy
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        await db_mod.upsert_voc_tags([_tag("ai-01")])
        with patch.object(voc_taxonomy, "load_seed", return_value={"tags": [_tag("ai-99")]}):
            result = await voc_taxonomy.sync_seed_to_db()
        assert result["skipped"] is True
        assert result["added"] == []
        tags = await db_mod.get_voc_tags()
        assert {t["id"] for t in tags} == {"ai-01"}  # seed's ai-99 was NOT inserted
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_sync_seed_to_db_bootstraps_empty_db(db_engine, db_session):
    import app.db.database as db_mod
    from app.services import voc_taxonomy
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        with patch.object(voc_taxonomy, "load_seed", return_value={"tags": [_tag("ai-01"), _tag("ai-02")]}):
            result = await voc_taxonomy.sync_seed_to_db()
        assert result["skipped"] is False
        assert set(result["added"]) == {"ai-01", "ai-02"}
        tags = await db_mod.get_voc_tags()
        assert {t["id"] for t in tags} == {"ai-01", "ai-02"}
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_sync_seed_to_db_no_seed_file_is_a_noop(db_engine, db_session):
    import app.db.database as db_mod
    from app.services import voc_taxonomy
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        with patch.object(voc_taxonomy, "load_seed", return_value={}):
            result = await voc_taxonomy.sync_seed_to_db()
        assert result["skipped"] is True
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory


async def test_sync_seed_to_db_force_true_upserts_over_existing_data(db_engine, db_session):
    """force=True is the manual-reseed path (/api/voc/taxonomy/reseed, Task 7)
    used to push a freshly re-pulled MCP snapshot into a DB that already has
    older taxonomy data — the whole point is that it must NOT skip."""
    import app.db.database as db_mod
    from app.services import voc_taxonomy
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        await db_mod.upsert_voc_tags([_tag("ai-01", definition="旧定义")])
        with patch.object(voc_taxonomy, "load_seed",
                           return_value={"tags": [_tag("ai-01", definition="新定义"), _tag("ai-02")]}):
            result = await voc_taxonomy.sync_seed_to_db(force=True)
        assert result["skipped"] is False
        assert result["changed"] == ["ai-01"]
        assert result["added"] == ["ai-02"]
        tags = await db_mod.get_voc_tags()
        assert {t["id"] for t in tags} == {"ai-01", "ai-02"}
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory
```

- [ ] **Step 2: Run the tests, confirm they fail against the current `int`-returning implementation**

Run: `cd backend && pytest tests/test_voc_taxonomy.py -v -k sync_seed_to_db`
Expected: FAIL — `TypeError: 'int' object is not subscriptable` (or similar) on `result["skipped"]`

- [ ] **Step 3: Rewrite `sync_seed_to_db()`**

Replace the function body in `backend/app/services/voc_taxonomy.py`:

```python
async def sync_seed_to_db(force: bool = False) -> Dict[str, Any]:
    """Bootstrap (or, with force=True, forcibly re-upsert) voc_tags from the
    checked-in seed file.

    Default (force=False): ONLY runs if the table is currently empty — a
    one-time bootstrap, not a recurring sync, so a stale seed file can't
    clobber newer VOC-side edits once real data exists.

    force=True: re-upserts the seed regardless of existing data (added/
    changed/retired diff, same semantics as sync_from_voc()) — the manual
    substitute for the VOC service-account sync loop
    (voc.sync_enabled=False until Keycloak credentials are provisioned):
    pull a fresh snapshot via the voc-portal MCP tool, overwrite
    backend/seeds/voc_taxonomy_seed.json, redeploy, then call this with
    force=True (exposed as POST /api/voc/taxonomy/reseed).

    Returns {"added": [...], "changed": [...], "retired": [...], "skipped": bool}.
    skipped=True means nothing was written (already-bootstrapped DB with
    force=False, or no seed file/tags available).
    """
    from app.db.database import get_voc_tags, upsert_voc_tags

    existing = await get_voc_tags(include_retired=True)
    if existing and not force:
        return {"added": [], "changed": [], "retired": [], "skipped": True}

    seed = load_seed()
    tags = seed.get("tags", [])
    if not tags:
        return {"added": [], "changed": [], "retired": [], "skipped": True}

    diff = await upsert_voc_tags(tags)
    await reload_from_db()
    logger.info(
        "VOC taxonomy %s from %s: %d added, %d changed, %d retired",
        "reseeded" if (existing and force) else "seeded", SEED_PATH,
        len(diff["added"]), len(diff["changed"]), len(diff["retired"]),
    )
    return {**diff, "skipped": False}
```

- [ ] **Step 4: Update the one caller in `backend/app/main.py`**

Find the lifespan block:

```python
    try:
        from app.services import voc_taxonomy
        seeded = await voc_taxonomy.sync_seed_to_db()
        await voc_taxonomy.reload_from_db()
        if seeded:
            logger.info("VOC taxonomy bootstrapped from seed: %d tags", seeded)
    except Exception as e:
        logger.warning("VOC taxonomy bootstrap failed (non-fatal): %s", e)
```

Replace with:

```python
    try:
        from app.services import voc_taxonomy
        seed_result = await voc_taxonomy.sync_seed_to_db()
        await voc_taxonomy.reload_from_db()
        if not seed_result["skipped"]:
            logger.info("VOC taxonomy bootstrapped from seed: %d tags", len(seed_result["added"]))
    except Exception as e:
        logger.warning("VOC taxonomy bootstrap failed (non-fatal): %s", e)
```

- [ ] **Step 5: Run the tests, confirm they pass**

Run: `cd backend && pytest tests/test_voc_taxonomy.py -v`
Expected: all tests pass (including the new `force=True` test)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/voc_taxonomy.py backend/app/main.py backend/tests/test_voc_taxonomy.py
git commit -m "feat(voc): sync_seed_to_db supports force re-upsert, returns a diff"
```

---

## Task 6: `GET /api/voc/trend` and `GET /api/voc/movers`

**Files:**
- Modify: `backend/app/api/voc.py`
- Test: `backend/tests/test_voc_api.py` (append)

**Interfaces:**
- Consumes: `db.get_voc_analysis_rows` (Task 4), `voc_digest.aggregate_trend` / `voc_digest.aggregate_movers` (Task 3).
- Produces: `GET /api/voc/trend?days=&level=group|label` → `{date_from, date_to, level, trend}`; `GET /api/voc/movers?days=&level=&min_base=` → `{cur_from, cur_to, prev_from, prev_to, level, movers}`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_voc_api.py`:

```python
async def _seed_voc_row(db_session, issue_id, created_at, group="蓝牙连接", label="配对失败"):
    from app.db.database import IssueRecord, AnalysisRecord
    import json as _json
    async with db_session() as s:
        s.add(IssueRecord(id=issue_id, description="desc", category="hardware"))
        s.add(AnalysisRecord(
            task_id=f"t-{issue_id}", issue_id=issue_id, created_at=created_at,
            voc_tags_json=_json.dumps([{
                "tag_id": "ai-01", "level_1_category": group, "level_2_label": label,
                "level_3_diagnosis": "", "role": "primary", "confidence": "high", "reason": "x",
            }]),
        ))
        await s.commit()


async def test_get_trend_groups_by_date_and_level(client, db_session):
    from datetime import datetime
    await _seed_voc_row(db_session, "i1", datetime.utcnow())
    resp = await client.get("/api/voc/trend", params={"days": 7, "level": "group"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["level"] == "group"
    assert any("蓝牙连接" in day for day in body["trend"].values())


async def test_get_trend_rejects_invalid_level(client):
    resp = await client.get("/api/voc/trend", params={"level": "diagnosis"})
    assert resp.status_code == 422


async def test_get_movers_min_base_filters_noise(client, db_session):
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    for i in range(3):
        await _seed_voc_row(db_session, f"cur{i}", now, group="A")
    await _seed_voc_row(db_session, "prev0", now - timedelta(days=8), group="A")

    resp = await client.get("/api/voc/movers", params={"days": 7, "level": "group", "min_base": 5})
    assert resp.status_code == 200
    assert resp.json()["movers"] == []  # 1 -> 3 filtered out by min_base=5

    resp2 = await client.get("/api/voc/movers", params={"days": 7, "level": "group", "min_base": 1})
    movers = resp2.json()["movers"]
    assert movers[0]["key"] == "A"
    assert movers[0]["cur"] == 3
    assert movers[0]["prev"] == 1
```

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `cd backend && pytest tests/test_voc_api.py -v -k "trend or movers"`
Expected: FAIL — 404 (routes don't exist yet)

- [ ] **Step 3: Add the endpoints to `backend/app/api/voc.py`**

Add `from app.services import voc_digest` to the imports at the top. Then append after `get_classification_stats`:

```python
@router.get("/trend")
async def get_trend(
    days: int = Query(30, ge=1, le=3650),
    level: str = Query("group", pattern="^(group|label)$"),
):
    """Multi-line trend data for the VOC analytics tab — date -> {key: count}."""
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = await db.get_voc_analysis_rows(date_from, date_to)
    trend = voc_digest.aggregate_trend(rows, level=level)
    return {"date_from": date_from, "date_to": date_to, "level": level, "trend": trend}


@router.get("/movers")
async def get_movers(
    days: int = Query(7, ge=1, le=90),
    level: str = Query("label", pattern="^(group|label)$"),
    min_base: int = Query(3, ge=1, le=100),
):
    """Week-over-week (or `days`-over-`days`) movers for the diverging bar chart."""
    cur_to = datetime.utcnow().date()
    cur_from = cur_to - timedelta(days=days - 1)
    prev_to = cur_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=days - 1)

    cur_rows = await db.get_voc_analysis_rows(cur_from.isoformat(), cur_to.isoformat())
    prev_rows = await db.get_voc_analysis_rows(prev_from.isoformat(), prev_to.isoformat())
    movers = voc_digest.aggregate_movers(cur_rows, prev_rows, level=level, min_base=min_base)

    return {
        "cur_from": cur_from.isoformat(), "cur_to": cur_to.isoformat(),
        "prev_from": prev_from.isoformat(), "prev_to": prev_to.isoformat(),
        "level": level, "movers": movers,
    }
```

- [ ] **Step 4: Run the tests, confirm they pass**

Run: `cd backend && pytest tests/test_voc_api.py -v`
Expected: all tests pass (existing + new)

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/voc.py backend/tests/test_voc_api.py
git commit -m "feat(voc): add /api/voc/trend and /api/voc/movers endpoints"
```

---

## Task 7: Taxonomy reseed endpoint + snapshot visibility

**Files:**
- Modify: `backend/app/api/voc.py` (`get_taxonomy`, new `reseed_taxonomy`)
- Test: `backend/tests/test_voc_api.py` (append)

**Interfaces:**
- Produces: `GET /api/voc/taxonomy` response gains `seed_fetched_at: str` and `seed_tag_count: int` (read from `voc_taxonomy.load_seed()`, not the DB — this reports "which checked-in snapshot is deployed", independent of DB reseed state). `POST /api/voc/taxonomy/reseed` → `{"status": "ok", "added": [...], "changed": [...], "retired": [...], "skipped": bool}`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_voc_api.py`:

```python
async def test_get_taxonomy_includes_seed_metadata(client, monkeypatch):
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: ACTIVE_TAGS)
    monkeypatch.setattr(voc_taxonomy, "load_seed", lambda: {"fetched_at": "2026-08-07", "tag_count": 158})
    resp = await client.get("/api/voc/taxonomy")
    body = resp.json()
    assert body["seed_fetched_at"] == "2026-08-07"
    assert body["seed_tag_count"] == 158


async def test_get_taxonomy_seed_metadata_missing_seed_file_is_empty(client, monkeypatch):
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: [])
    monkeypatch.setattr(voc_taxonomy, "load_seed", lambda: {})
    resp = await client.get("/api/voc/taxonomy")
    body = resp.json()
    assert body["seed_fetched_at"] == ""
    assert body["seed_tag_count"] == 0


async def test_reseed_taxonomy_forces_upsert(client):
    from unittest.mock import AsyncMock, patch as _patch
    with _patch("app.services.voc_taxonomy.sync_seed_to_db", new_callable=AsyncMock,
                return_value={"added": ["ai-01"], "changed": [], "retired": [], "skipped": False}) as mock_sync:
        resp = await client.post("/api/voc/taxonomy/reseed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["added"] == ["ai-01"]
    mock_sync.assert_called_once_with(force=True)
```

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `cd backend && pytest tests/test_voc_api.py -v -k "seed_metadata or reseed_taxonomy"`
Expected: FAIL — `KeyError: 'seed_fetched_at'` and 404 on `/taxonomy/reseed`

- [ ] **Step 3: Update `get_taxonomy` and add `reseed_taxonomy` in `backend/app/api/voc.py`**

```python
@router.get("/taxonomy")
async def get_taxonomy():
    """Full active-tag tree (group → label → diagnosis) for the analytics
    drill-down UI, plus metadata about which checked-in seed snapshot is
    currently deployed (seed_fetched_at/seed_tag_count) — surfaced so a
    stale taxonomy (VOC changed something upstream, nobody re-pulled) is
    visible in the UI rather than a silent gap. Reads the in-memory active-
    tags cache (refreshed at startup and after every sync) for the tree, and
    the seed file directly for the metadata (the seed file, not the DB, is
    "what snapshot are we running" — see voc_taxonomy.sync_seed_to_db)."""
    tags = voc_taxonomy.active_tags()
    seed = voc_taxonomy.load_seed()
    return {
        "total_active_tags": len(tags),
        "tree": _build_tree(tags),
        "seed_fetched_at": seed.get("fetched_at", ""),
        "seed_tag_count": seed.get("tag_count", 0),
    }


@router.post("/taxonomy/reseed")
async def reseed_taxonomy():
    """Force re-upsert voc_tags from the checked-in seed file (backend/seeds/
    voc_taxonomy_seed.json), bypassing sync_seed_to_db()'s bootstrap-only
    skip. Use this after pulling a fresh MCP snapshot and redeploying — VOC
    has no service account provisioned yet so the daily sync_from_voc() loop
    (POST /taxonomy/sync) can't run; this is the manual substitute."""
    diff = await voc_taxonomy.sync_seed_to_db(force=True)
    return {"status": "ok", **diff}
```

Note this changes `get_taxonomy`'s existing test assertions (`test_get_taxonomy_builds_tree`, `test_get_taxonomy_empty` in the current file) implicitly — they don't assert an exact dict equality against the full body in a way that would break (checking `test_get_taxonomy_empty` from the earlier read: `assert resp.json() == {"total_active_tags": 0, "tree": []}` — **this WILL break**, it's an exact-equality check). Fix it:

```python
async def test_get_taxonomy_empty(client, monkeypatch):
    monkeypatch.setattr(voc_taxonomy, "active_tags", lambda: [])
    monkeypatch.setattr(voc_taxonomy, "load_seed", lambda: {})
    resp = await client.get("/api/voc/taxonomy")
    assert resp.status_code == 200
    assert resp.json() == {
        "total_active_tags": 0, "tree": [], "seed_fetched_at": "", "seed_tag_count": 0,
    }
```

- [ ] **Step 4: Run the full VOC API test suite**

Run: `cd backend && pytest tests/test_voc_api.py -v`
Expected: all tests pass

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/voc.py backend/tests/test_voc_api.py
git commit -m "feat(voc): expose seed snapshot metadata + add manual reseed endpoint"
```

---

## Task 8: Weekly digest generation (LLM narrative)

**Files:**
- Modify: `backend/app/services/voc_digest.py` (append the LLM-orchestration half)
- Modify: `backend/app/config.py` (`VOCSettings` additions — needed here since `generate_weekly_digest` reads them)
- Modify: `backend/config.yaml` (non-secret defaults)
- Test: `backend/tests/test_voc_digest_generate.py`

**Interfaces:**
- Consumes: `claude_headless.run_json` (Task 1), `db.get_voc_analysis_rows`/`get_voc_weekly_digest`/`upsert_voc_weekly_digest` (Task 4), `compute_weekly_stats`/`sample_root_causes`/`default_week_start` (Task 3).
- Produces: `async def generate_weekly_digest(week_start: str, force: bool = False) -> Dict[str, Any]` — the same dict shape `upsert_voc_weekly_digest` returns.

- [ ] **Step 1: Add the new `VOCSettings` fields**

In `backend/app/config.py`, inside the `VOCSettings` class (after the existing `classifier_timeout_seconds` field), add:

```python
    # Weekly insight digest (app.services.voc_digest.generate_weekly_digest).
    # digest_enabled gates the LLM narrative call only — the deterministic
    # stats half always computes regardless, so the digest never has zero
    # content even with this off. digest_push_enabled is OFF by default:
    # this ships the generation pipeline first, Feishu push is a separate
    # explicit opt-in once someone has reviewed a few weeks of output.
    digest_enabled: bool = True
    digest_cron: str = "0 10 * * 1"   # Monday 10:00 UTC — "M H * * D" shape only, see voc_digest_loop
    digest_push_enabled: bool = False
    digest_chat_id: str = ""
    digest_model: str = "claude-sonnet-5"
    digest_timeout_seconds: int = 300
```

In `backend/app/config.py`'s `_merge_yaml_into_settings()`, update the VOC whitelist tuple:

```python
    for k in ("base_url", "token_url", "sync_enabled", "sync_interval_hours", "classifier_model",
              "classifier_timeout_seconds", "digest_enabled", "digest_cron", "digest_push_enabled",
              "digest_chat_id", "digest_model", "digest_timeout_seconds"):
        if k in voc_cfg and not os.getenv(f"VOC_{k.upper()}"):
            setattr(settings.voc, k, voc_cfg[k])
```

In `backend/config.yaml`, extend the `voc:` block:

```yaml
voc:
  base_url: https://voc-portal-apse1.nicebuild.click
  token_url: https://voc-portal-apse1.nicebuild.click/oauth/token
  sync_enabled: false
  sync_interval_hours: 24
  classifier_model: claude-sonnet-5
  digest_enabled: true
  digest_cron: "0 10 * * 1"
  digest_push_enabled: false
  digest_chat_id: ""
  digest_model: claude-sonnet-5
  digest_timeout_seconds: 300
```

- [ ] **Step 2: Write a config sanity test**

Create `backend/tests/test_voc_digest_config.py`:

```python
"""Confirms config.yaml's new voc.digest_* keys actually reach Settings
through _merge_yaml_into_settings — catches the classic failure mode where a
new yaml key is added but the merge whitelist tuple isn't updated, so the
value is silently ignored and the code-level default wins instead."""
from __future__ import annotations


def test_voc_digest_settings_load_from_yaml():
    from app.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.voc.digest_enabled is True
    assert settings.voc.digest_cron == "0 10 * * 1"
    assert settings.voc.digest_push_enabled is False
    assert settings.voc.digest_model == "claude-sonnet-5"
    assert settings.voc.digest_timeout_seconds == 300
    get_settings.cache_clear()
```

- [ ] **Step 3: Run it, confirm it fails**

Run: `cd backend && pytest tests/test_voc_digest_config.py -v`
Expected: FAIL — `AttributeError: 'VOCSettings' object has no attribute 'digest_enabled'`

- [ ] **Step 4: Apply the config.py and config.yaml edits from Step 1, then confirm the test passes**

Run: `cd backend && pytest tests/test_voc_digest_config.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing tests for `generate_weekly_digest`**

Create `backend/tests/test_voc_digest_generate.py`:

```python
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


async def _patch_db(monkeypatch, cur_rows, prev_rows, existing=None):
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
```

- [ ] **Step 6: Run the tests, confirm they fail**

Run: `cd backend && pytest tests/test_voc_digest_generate.py -v`
Expected: FAIL — `AttributeError: module 'app.services.voc_digest' has no attribute 'generate_weekly_digest'`

- [ ] **Step 7: Append the LLM-orchestration half to `voc_digest.py`**

Add these imports at the top of `backend/app/services/voc_digest.py` (alongside the existing ones):

```python
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.services import claude_headless
```

(`date` was already imported for `default_week_start`; add `datetime` alongside it since it wasn't needed by Task 3's functions but isn't needed here either — rows already carry `datetime` objects, so no new use of the bare `datetime` class is actually required. Drop it from this import if unused after Step 7's code below — verify with a grep for `datetime(` before finalizing.)

Append at the end of the file:

```python
_DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "key_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string"},
                    "finding": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["scope", "finding"],
            },
        },
        "product_opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "area": {"type": "string"},
                    "problem": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["area", "problem", "suggestion"],
            },
        },
        "movers_commentary": {"type": "string"},
    },
    "required": ["headline", "key_findings", "product_opportunities"],
}

_DIGEST_REQUIRED_KEYS = ("headline", "key_findings", "product_opportunities")


def _build_digest_system_prompt() -> str:
    return (
        "You are writing a weekly customer-support insight digest for Plaud's "
        "product team, based on support tickets already classified against a "
        "fixed VOC taxonomy.\n\n"
        "You will receive a JSON `stats` object (already-computed group counts, "
        "week-over-week deltas, and top movers) and a `root_cause_samples` "
        "object (real root-cause text sampled from this week's tickets, "
        "grouped by VOC category).\n\n"
        "Rules:\n"
        "- Every number you mention (counts, percentages, deltas) MUST come "
        "verbatim from `stats`. Never compute, round, or restate a number "
        "that isn't already there.\n"
        "- `key_findings` should call out the categories with the highest "
        "volume or the sharpest movers from `stats`.\n"
        "- `product_opportunities` is the most important output. For each "
        "category in `root_cause_samples`, ask: do these root causes describe "
        "something the PRODUCT could prevent or mitigate (a missing hint, a "
        "confusing flow, a hardware limitation users hit repeatedly) rather "
        "than something the user did wrong? Only include an entry if the "
        "sampled root causes actually support that conclusion — it is fine "
        "to return an empty list if nothing this week supports a product "
        "change. Do not invent a plausible-sounding opportunity.\n"
        "- Write in English. Keep `finding`/`problem`/`suggestion` to 1-2 "
        "sentences each."
    )


def _build_digest_user_input(
    stats: Dict[str, Any], root_cause_samples: Dict[str, List[str]],
    week_start: str, week_end: str,
) -> str:
    payload = {
        "week_start": week_start, "week_end": week_end,
        "stats": stats, "root_cause_samples": root_cause_samples,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _render_markdown(
    week_start: str, week_end: str, stats: Dict[str, Any], narrative: Optional[Dict[str, Any]],
) -> str:
    lines = [f"# VOC 周度洞察 · {week_start} ~ {week_end}", ""]
    if narrative and narrative.get("headline"):
        lines += [f"**{narrative['headline']}**", ""]

    total_cur = stats.get("total_cur", 0)
    total_delta_pct = stats.get("total_delta_pct")
    delta_str = f"{total_delta_pct:+.1f}%" if total_delta_pct is not None else "N/A（上周无基线）"
    lines += [f"本周共 {total_cur} 单，环比 {delta_str}。", ""]

    if narrative and narrative.get("key_findings"):
        lines.append("## 关键发现")
        for f in narrative["key_findings"]:
            line = f"- **{f.get('scope', '')}**：{f.get('finding', '')}"
            if f.get("evidence"):
                line += f"（{f['evidence']}）"
            lines.append(line)
        lines.append("")

    if narrative and narrative.get("product_opportunities"):
        lines.append("## 产品优化建议")
        for o in narrative["product_opportunities"]:
            lines.append(f"- **{o.get('area', '')}**：{o.get('problem', '')} → {o.get('suggestion', '')}")
            if o.get("rationale"):
                lines.append(f"  - 依据：{o['rationale']}")
        lines.append("")
    elif narrative is not None:
        lines += ["## 产品优化建议", "", "（本周没有明显可归因为产品问题的重复根因）", ""]

    lines.append("## 分类占比 Top 10")
    for g in stats.get("groups", [])[:10]:
        lines.append(f"- {g['group']}：{g['count']}")
    lines.append("")

    top_movers = stats.get("top_movers", [])
    if top_movers:
        lines.append("## 环比变动 Top 5")
        for m in top_movers[:5]:
            pct = f"{m['delta_pct']:+.0f}%" if m.get("delta_pct") is not None else "new"
            lines.append(f"- {m['key']}：{m['prev']} → {m['cur']}（{pct}）")
        lines.append("")

    if narrative is None:
        lines.append("_洞察生成失败，以上仅为确定性统计。可点击「重新生成」重试。_")

    return "\n".join(lines)


async def generate_weekly_digest(week_start: str, force: bool = False) -> Dict[str, Any]:
    """Generate (or return the cached) weekly digest for the week starting
    `week_start` (Monday, "YYYY-MM-DD"). The deterministic stats half always
    computes; the LLM narrative half degrades to None on any failure
    (disabled, CLI unavailable, timeout, malformed output) — the record is
    ALWAYS written either way, since the numbers alone are useful even
    without a narrative (see _render_markdown's fallback line).
    """
    from app.db import database as db

    if not force:
        existing = await db.get_voc_weekly_digest(week_start)
        if existing:
            return existing

    ws = date.fromisoformat(week_start)
    we = ws + timedelta(days=6)
    prev_ws = ws - timedelta(days=7)
    prev_we = ws - timedelta(days=1)

    cur_rows = await db.get_voc_analysis_rows(ws.isoformat(), we.isoformat())
    prev_rows = await db.get_voc_analysis_rows(prev_ws.isoformat(), prev_we.isoformat())

    stats = compute_weekly_stats(cur_rows, prev_rows)
    root_cause_samples = sample_root_causes(cur_rows)

    settings = get_settings().voc
    narrative: Optional[Dict[str, Any]] = None
    if settings.digest_enabled:
        narrative = await claude_headless.run_json(
            system_prompt=_build_digest_system_prompt(),
            user_input=_build_digest_user_input(stats, root_cause_samples, week_start, we.isoformat()),
            schema=_DIGEST_SCHEMA,
            model=settings.digest_model,
            timeout=float(settings.digest_timeout_seconds),
            log_prefix="voc_digest",
        )
        if narrative is not None:
            missing = [k for k in _DIGEST_REQUIRED_KEYS if k not in narrative]
            if missing:
                logger.warning("VOC digest narrative missing required keys %s — discarding", missing)
                narrative = None

    markdown = _render_markdown(week_start, we.isoformat(), stats, narrative)

    return await db.upsert_voc_weekly_digest(
        week_start=week_start,
        stats=stats,
        narrative=narrative,
        markdown=markdown,
        model=settings.digest_model if narrative else "",
    )
```

- [ ] **Step 8: Run the tests, confirm they pass**

Run: `cd backend && pytest tests/test_voc_digest_generate.py tests/test_voc_digest.py tests/test_voc_digest_config.py -v`
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/voc_digest.py backend/app/config.py backend/config.yaml \
        backend/tests/test_voc_digest_generate.py backend/tests/test_voc_digest_config.py
git commit -m "feat(voc): add weekly digest LLM narrative generation"
```

---

## Task 9: Weekly digest API endpoints + startup loop

**Files:**
- Modify: `backend/app/api/voc.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_voc_api.py` (append)

**Interfaces:**
- Produces: `GET /api/voc/weekly-digest?week_start=` (defaults to `voc_digest.default_week_start()`, returns `null` if nothing cached), `POST /api/voc/weekly-digest/generate?week_start=&force=`, `GET /api/voc/weekly-digests?limit=`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_voc_api.py`:

```python
async def test_get_weekly_digest_defaults_to_most_recent_complete_week(client):
    from unittest.mock import AsyncMock, patch as _patch
    with _patch("app.db.database.get_voc_weekly_digest", new_callable=AsyncMock, return_value=None) as mock_get:
        resp = await client.get("/api/voc/weekly-digest")
    assert resp.status_code == 200
    assert resp.json() is None
    assert mock_get.call_count == 1


async def test_generate_weekly_digest_endpoint(client):
    from unittest.mock import AsyncMock, patch as _patch
    fake_record = {"week_start": "2026-08-03", "stats": {}, "narrative": None, "markdown": "x"}
    with _patch("app.services.voc_digest.generate_weekly_digest", new_callable=AsyncMock,
                return_value=fake_record) as mock_gen:
        resp = await client.post("/api/voc/weekly-digest/generate", params={"week_start": "2026-08-03", "force": True})
    assert resp.status_code == 200
    assert resp.json()["week_start"] == "2026-08-03"
    mock_gen.assert_called_once_with("2026-08-03", force=True)


async def test_list_weekly_digests_endpoint(client):
    from unittest.mock import AsyncMock, patch as _patch
    with _patch("app.db.database.list_voc_weekly_digests", new_callable=AsyncMock,
                return_value=[{"week_start": "2026-08-03"}]):
        resp = await client.get("/api/voc/weekly-digests", params={"limit": 5})
    assert resp.status_code == 200
    assert resp.json()["digests"] == [{"week_start": "2026-08-03"}]
```

- [ ] **Step 2: Run, confirm failure (404s)**

Run: `cd backend && pytest tests/test_voc_api.py -v -k weekly_digest`

- [ ] **Step 3: Add the endpoints to `backend/app/api/voc.py`**

```python
@router.get("/weekly-digest")
async def get_weekly_digest(
    week_start: str = Query("", description="YYYY-MM-DD Monday; default = most recent complete week"),
):
    ws = week_start or voc_digest.default_week_start()
    return await db.get_voc_weekly_digest(ws)


@router.post("/weekly-digest/generate")
async def generate_weekly_digest_endpoint(
    week_start: str = Query("", description="YYYY-MM-DD Monday; default = most recent complete week"),
    force: bool = Query(False),
):
    ws = week_start or voc_digest.default_week_start()
    return await voc_digest.generate_weekly_digest(ws, force=force)


@router.get("/weekly-digests")
async def list_weekly_digests(limit: int = Query(12, ge=1, le=52)):
    return {"digests": await db.list_voc_weekly_digests(limit=limit)}
```

- [ ] **Step 4: Wire `voc_digest_loop()` into `backend/app/main.py`**

Append to `backend/app/services/voc_digest.py` (still in this task, since it's part of the startup-loop feature):

```python
def _parse_weekly_cron(expr: str) -> Optional[tuple]:
    """Parse the 'M H * * D' shape only (fixed minute/hour/day-of-week, DOM
    and MON must be '*') — returns (minute, hour, dow) or None. Deliberately
    NOT a general cron parser and NOT shared with crashguard.workers.
    scheduler._cron_matches — that module is inside the crashguard isolation
    contract (backend/app/crashguard/CLAUDE.md); importing it here would
    violate lint-imports. DOW follows Unix cron convention: Sun=0..Sat=6,
    i.e. (datetime.weekday() + 1) % 7.
    """
    parts = expr.split()
    if len(parts) != 5:
        return None
    minute_f, hour_f, dom_f, month_f, dow_f = parts
    if dom_f != "*" or month_f != "*":
        return None
    try:
        return int(minute_f), int(hour_f), int(dow_f)
    except ValueError:
        return None


async def voc_digest_loop() -> None:
    """Hourly-tick loop: once the current UTC time is at/after this week's
    scheduled cron slot (voc.digest_cron) and no digest is cached yet for
    the target week, generate one (and push to Feishu if digest_push_enabled).
    Checking "does a cached digest already exist" instead of matching the
    exact minute makes this robust to a missed tick — it just fires on the
    next hourly check instead of waiting a full week."""
    from app.db import database as db

    settings = get_settings().voc
    if not settings.digest_enabled:
        logger.info("VOC weekly digest loop disabled (voc.digest_enabled=false)")
        return
    parsed = _parse_weekly_cron(settings.digest_cron)
    if parsed is None:
        logger.warning("voc.digest_cron=%r is not a supported 'M H * * D' expression — loop disabled",
                        settings.digest_cron)
        return
    minute, hour, dow = parsed

    while True:
        try:
            now = datetime.utcnow()
            cron_dow = (now.weekday() + 1) % 7
            scheduled_today = cron_dow == dow and (now.hour, now.minute) >= (hour, minute)
            if scheduled_today:
                ws = default_week_start(now.date())
                existing = await db.get_voc_weekly_digest(ws)
                if existing is None:
                    logger.info("VOC weekly digest cron window reached — generating week_start=%s", ws)
                    record = await generate_weekly_digest(ws, force=False)
                    if settings.digest_push_enabled and settings.digest_chat_id:
                        from app.services import feishu_cli
                        await feishu_cli.send_message(chat_id=settings.digest_chat_id, markdown=record["markdown"])
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("VOC weekly digest loop tick failed (will retry next tick): %s", e)
        await asyncio.sleep(3600)
```

Add `import asyncio` to the top of `voc_digest.py` (needed for `asyncio.CancelledError`/`asyncio.sleep`).

In `backend/app/main.py`, right after the existing `voc_sync_task` block:

```python
    voc_digest_task = asyncio.create_task(voc_digest_loop())
```

with the import added alongside the existing `from app.services.voc_taxonomy import voc_sync_loop` line:

```python
    from app.services.voc_digest import voc_digest_loop
```

And in the shutdown section (where `voc_sync_task.cancel()` is), add:

```python
    if voc_digest_task is not None:
        voc_digest_task.cancel()
```

(`voc_digest_task` is always created — unlike `voc_sync_task` it's not gated by an `if settings.voc.sync_enabled` at the call site, since `voc_digest_loop()` itself checks `digest_enabled` and returns immediately if off, so the task always exists but may be a no-op.)

- [ ] **Step 5: Run the tests, confirm they pass**

Run: `cd backend && pytest tests/test_voc_api.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/voc.py backend/app/main.py backend/app/services/voc_digest.py backend/tests/test_voc_api.py
git commit -m "feat(voc): weekly digest API endpoints + startup cron loop"
```

---

## Task 10: Retire legacy classification writes (agent output + save_analysis + backfill guard)

**Files:**
- Modify: `backend/app/agents/base.py` (output schema, instructions, `AnalysisResult` construction)
- Modify: `backend/app/services/agent_orchestrator.py` (stop writing `context/classification_taxonomy.json`)
- Modify: `backend/app/db/database.py` (`save_analysis` stops auto-classifying; `get_analyses_for_backfill` gains a guard)
- Modify: `backend/tests/test_agent_prompt.py`, `backend/tests/test_voc_tags_wiring.py`
- Test: `backend/tests/test_voc_tags_wiring.py` (append a new backfill-guard test)

This is the "新工单必须用新分类标准，旧的可以放弃" decision. There are TWO separate write paths to stop, not one:

1. The agent's own `problem_categories` output field (base.py schema/prompt).
2. **`save_analysis()`'s backend-side auto-classify fallback** (`app/db/database.py`, "Auto-classify if AI didn't provide categories") — this runs `classify_problem()` keyword matching **unconditionally whenever the AI doesn't supply `problem_categories`**, which after step 1 is now every single new analysis. Without also disabling this, "stop writing" would be false — `problem_categories_json` would keep getting populated by the backend fallback even though the agent stopped sending it. This was found by re-reading `save_analysis()` during planning, not from the original brainstorming doc.

There's a third knock-on effect: the existing `/api/analytics/backfill-classifications` admin button (legacy tab, kept per the design) selects rows with empty `problem_categories_json` ordered by `created_at DESC` — once new rows also have empty `problem_categories_json` (from stopping #2), clicking that button would immediately re-classify brand-new VOC-tagged tickets with the OLD keyword system, undoing the freeze. Guard `get_analyses_for_backfill()` to also require empty `voc_tags_json` — a row that already has VOC tags is definitionally post-cutover and must never be touched by the legacy backfill.

**Interfaces:** no new functions; behavior-only changes to existing ones (`AnalysisResult` output shape, `save_analysis()`, `get_analyses_for_backfill()`).

- [ ] **Step 1: Remove `problem_categories` from the agent's output contract in `base.py`**

In `backend/app/agents/base.py`, in the JSON schema block inside the big prompt-template string, delete these 3 lines:

```json
    "problem_categories": [
        {{"category": "Level-1 category", "subcategory": "Level-2 subcategory"}}
    ],
```

Delete this paragraph (immediately below the JSON block, right before the `voc_tags taxonomy:` paragraph):

```
problem_categories and device_type taxonomy: see `context/classification_taxonomy.json` — **read it before analysis**. One issue can belong to multiple categories.
```

In the `AnalysisResult(...)` construction (around line 660), delete the line:

```python
            problem_categories=_safe_problem_categories(data.get("problem_categories", [])),
```

(`problem_categories` on `AnalysisResult` has `default_factory=list`, so omitting it here just means it's always `[]` — no schema change needed. Leave `_safe_problem_categories()` defined but now uncalled; it's a few harmless lines documenting the old contract, not worth a separate removal PR.)

- [ ] **Step 2: Stop writing `context/classification_taxonomy.json` in `agent_orchestrator.py`**

In `backend/app/services/agent_orchestrator.py`, delete this block:

```python
    # Classification taxonomy — AI reads this file to fill problem_categories + device_type
    from app.classification_taxonomy import CLASSIFICATION_TAXONOMY
    context_files["classification"] = _write_json_file(
        context_dir / "classification_taxonomy.json",
        CLASSIFICATION_TAXONOMY,
    )
```

And update the comment on the block right after it (the one starting `# VOC Portal taxonomy — new classification system, written alongside...`) since "alongside classification_taxonomy.json above" is no longer accurate:

```python
    # VOC Portal taxonomy is now the only classification system new analyses
    # write to — classification_taxonomy.json / problem_categories retired
    # (see docs/superpowers/plans/2026-08-07-voc-analytics-weekly-digest.md
    # Task 10). Empty tag list degrades gracefully — the agent just won't
    # have anything to pick from and voc_tags stays empty (caught by the
    # backfill script's only_empty scan, not retried here).
    from app.services import voc_taxonomy
    context_files["voc_taxonomy"] = _write_json_file(
        context_dir / "voc_taxonomy.json",
        voc_taxonomy.to_prompt_payload(),
    )
```

- [ ] **Step 3: Fix `tests/test_agent_prompt.py`'s stale `context_files` key assertion**

```python
    assert sorted(prompt_meta["context_files"].keys()) == [
        "extraction",
        "few_shot",
        "followup_question",
        "issue",
        "previous_analysis",
        "voc_taxonomy",
    ]
```

(removes `"classification"` from the expected list — was added when `voc_taxonomy` was first wired in a prior session and never updated when `classification` should have been removed alongside it).

- [ ] **Step 4: Run the agent prompt test, confirm it passes**

Run: `cd backend && pytest tests/test_agent_prompt.py -v`
Expected: pass

- [ ] **Step 5: Stop `save_analysis()`'s auto-classify fallback in `database.py`**

In `backend/app/db/database.py`, in `save_analysis()`, replace:

```python
    # Auto-classify if AI didn't provide categories (backend-side, zero AI cost)
    categories = data.get("problem_categories", [])
    if not categories:
        from app.classification_taxonomy import classify_problem
        categories = classify_problem(
            data.get("problem_type", ""),
            data.get("root_cause", ""),
        )
```

with:

```python
    # problem_categories / classify_problem() keyword classification retired
    # 2026-08 in favor of the VOC Portal taxonomy (voc_tags below) — the
    # agent no longer outputs problem_categories (see app.agents.base), and
    # this backend-side fallback is intentionally NOT re-enabled for new
    # rows: doing so would keep repopulating a field this feature explicitly
    # freezes. classify_problem()/classification_taxonomy.py stay in the
    # repo (used only by the historical /api/analytics/backfill-classifications
    # endpoint, now also guarded — see get_analyses_for_backfill below) so
    # old pre-cutover data stays comparable.
    categories = data.get("problem_categories", [])
```

- [ ] **Step 6: Guard `get_analyses_for_backfill()` against re-classifying VOC-tagged rows**

In `backend/app/db/database.py`, in `get_analyses_for_backfill()`, change the `where(...)` clause from:

```python
        ).where(
            AnalysisRecord.problem_type.notin_(_INVALID_TYPES),
            or_(
                AnalysisRecord.problem_categories_json == "[]",
                AnalysisRecord.problem_categories_json == "",
                AnalysisRecord.problem_categories_json.is_(None),
            ),
        ).order_by(AnalysisRecord.created_at.desc()).limit(limit)
```

to:

```python
        ).where(
            AnalysisRecord.problem_type.notin_(_INVALID_TYPES),
            or_(
                AnalysisRecord.problem_categories_json == "[]",
                AnalysisRecord.problem_categories_json == "",
                AnalysisRecord.problem_categories_json.is_(None),
            ),
            # Guard added 2026-08: since save_analysis() stopped auto-
            # classifying, ALL new (VOC-era) rows also have empty
            # problem_categories_json. Without this second condition the
            # legacy /api/analytics/backfill-classifications button would
            # immediately re-classify brand-new VOC-tagged tickets with the
            # retired keyword system, un-freezing the field this feature
            # deliberately stopped touching. A row with any voc_tags_json is
            # definitionally post-cutover and must never match here.
            or_(
                AnalysisRecord.voc_tags_json == "[]",
                AnalysisRecord.voc_tags_json == "",
                AnalysisRecord.voc_tags_json.is_(None),
            ),
        ).order_by(AnalysisRecord.created_at.desc()).limit(limit)
```

Also update the docstring one line above from `"""Get analyses that need classification backfill (empty problem_categories_json)."""` to `"""Get PRE-VOC-CUTOVER analyses that need legacy classification backfill (empty problem_categories_json AND empty voc_tags_json — see the guard comment below)."""`.

- [ ] **Step 7: Update `tests/test_voc_tags_wiring.py`'s now-inverted assertion, and add the backfill-guard test**

Replace `test_save_analysis_leaves_voc_tags_empty_when_ai_omits_it`:

```python
async def test_save_analysis_leaves_voc_tags_empty_when_ai_omits_it(db_engine, db_session):
    """No backend LLM fallback in the hot save_analysis path — an empty/missing
    voc_tags from the AI must stay empty, picked up later by the backfill script.
    Also: the OLD classify_problem() keyword fallback was retired 2026-08 (VOC
    taxonomy replaced it) — problem_categories_json must now stay '[]' for new
    rows too, not silently repopulate via the backend-side fallback that used
    to run unconditionally here."""
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        record = await db_mod.save_analysis({
            "task_id": "t2", "issue_id": "i2",
            "problem_type": "录音丢失", "root_cause": "unknown",
        })
        assert json.loads(record.voc_tags_json) == []
        assert record.problem_categories_json == "[]"
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory
```

Append a new test to the same file:

```python
async def test_get_analyses_for_backfill_excludes_rows_with_voc_tags(db_engine, db_session):
    """A row that already has voc_tags_json populated (a new, VOC-classified
    ticket) must never be picked up by the legacy backfill scan, even though
    its problem_categories_json is empty (new analyses stopped writing it)."""
    import app.db.database as db_mod
    original_engine, original_factory = db_mod._engine, db_mod._session_factory
    db_mod._engine, db_mod._session_factory = db_engine, db_session
    try:
        await db_mod.save_analysis({
            "task_id": "t1", "issue_id": "i1", "problem_type": "蓝牙连接失败",
            "root_cause": "token mismatch",
            "voc_tags": [{"tag_id": "ai-01", "level_1_category": "蓝牙连接",
                          "role": "primary", "confidence": "high", "reason": "x"}],
        })
        await db_mod.save_analysis({
            "task_id": "t2", "issue_id": "i2", "problem_type": "录音丢失",
            "root_cause": "unknown",
        })
        rows = await db_mod.get_analyses_for_backfill(limit=10)
        assert [r["issue_id"] for r in rows] == ["i2"]  # t1 excluded: it has voc_tags
    finally:
        db_mod._engine, db_mod._session_factory = original_engine, original_factory
```

(`get_analyses_for_backfill` currently returns `{"id", "problem_type", "root_cause", "device_type"}` per its existing code — not `issue_id`. Check its `return [...]` line before writing this assertion: if `issue_id` isn't in the returned dict, add it there too, since the test needs a stable way to identify which row survived — `id` works equally well if you'd rather not touch the return shape: `assert len(rows) == 1` combined with `assert rows[0]["problem_type"] == "录音丢失"` is an equivalent, non-invasive assertion.)

- [ ] **Step 8: Run the affected test files**

Run: `cd backend && pytest tests/test_voc_tags_wiring.py tests/test_agent_prompt.py -v`
Expected: all pass

- [ ] **Step 9: Run the FULL backend suite — this task touches shared hot-path code (`save_analysis`, agent prompt) that other tests may depend on**

Run: `cd backend && pytest tests/ --ignore=tests/crashguard -q`
Expected: no new failures vs. the baseline (compare against `git stash` if anything unexpected shows up — see prior session's note that ~8 pre-existing unrelated failures exist in `test_local.py`/`test_oncall.py`/`test_repo_routing_config.py`/`test_sso_settings.py`/`test_users.py`; this task must not add to that count)

- [ ] **Step 10: Commit**

```bash
git add backend/app/agents/base.py backend/app/services/agent_orchestrator.py backend/app/db/database.py \
        backend/tests/test_agent_prompt.py backend/tests/test_voc_tags_wiring.py
git commit -m "feat(voc): retire legacy problem_categories writes for new analyses"
```

---

## Task 11: Frontend — `api.ts` types and wrappers

**Files:**
- Modify: `frontend/src/lib/api.ts`

**Interfaces:**
- Produces: `VocTrend`, `VocMover`, `VocMoversResponse`, `VocDigestFinding`, `VocDigestOpportunity`, `VocDigestNarrative`, `VocWeeklyDigest` types; `fetchVocTrend`, `fetchVocMovers`, `fetchVocWeeklyDigest`, `generateVocWeeklyDigest`, `fetchVocWeeklyDigests`, `reseedVocTaxonomy` functions. `VocTaxonomyTree` gains `seed_fetched_at`/`seed_tag_count`.

No test file — this is a typed API client with no logic branches; correctness is verified by Task 12/13's components compiling against it and by `npm run build` type-checking. This project's frontend has no unit test runner configured (verify with `cat frontend/package.json | grep -i '"test"'` — if a test script exists, skip this note and add a type-only smoke test instead).

- [ ] **Step 1: Add `seed_fetched_at`/`seed_tag_count` to the existing `VocTaxonomyTree` interface**

In `frontend/src/lib/api.ts`, change:

```ts
export interface VocTaxonomyTree {
  total_active_tags: number;
  tree: VocTaxonomyGroup[];
}
```

to:

```ts
export interface VocTaxonomyTree {
  total_active_tags: number;
  tree: VocTaxonomyGroup[];
  seed_fetched_at: string;
  seed_tag_count: number;
}
```

- [ ] **Step 2: Append the new types and wrappers after `fetchVocClassificationStats`**

```ts
export interface VocTrend {
  date_from: string;
  date_to: string;
  level: "group" | "label";
  trend: Record<string, Record<string, number>>; // date -> key -> count
}

export const fetchVocTrend = (days: number = 30, level: "group" | "label" = "group") =>
  request<VocTrend>(`/voc/trend?days=${days}&level=${level}`);

export interface VocMover {
  key: string;
  cur: number;
  prev: number;
  delta: number;
  delta_pct: number | null;
}

export interface VocMoversResponse {
  cur_from: string; cur_to: string;
  prev_from: string; prev_to: string;
  level: "group" | "label";
  movers: VocMover[];
}

export const fetchVocMovers = (days: number = 7, level: "group" | "label" = "label", minBase: number = 3) =>
  request<VocMoversResponse>(`/voc/movers?days=${days}&level=${level}&min_base=${minBase}`);

export const reseedVocTaxonomy = () =>
  request<{ status: string; added: string[]; changed: string[]; retired: string[]; skipped: boolean }>(
    `/voc/taxonomy/reseed`,
    { method: "POST" },
  );

export interface VocDigestFinding {
  scope: string;
  finding: string;
  evidence?: string;
}

export interface VocDigestOpportunity {
  area: string;
  problem: string;
  suggestion: string;
  rationale?: string;
}

export interface VocDigestNarrative {
  headline: string;
  key_findings: VocDigestFinding[];
  product_opportunities: VocDigestOpportunity[];
  movers_commentary?: string;
}

export interface VocWeeklyDigestStats {
  total_cur: number;
  total_prev: number;
  total_delta: number;
  total_delta_pct: number | null;
  groups: { group: string; count: number }[];
  top_movers: VocMover[];
  needs_engineer_rate: number;
  devices: { device_type: string; count: number }[];
}

export interface VocWeeklyDigest {
  week_start: string;
  stats: VocWeeklyDigestStats;
  narrative: VocDigestNarrative | null;
  markdown: string;
  model: string;
  total_tokens: number;
  total_cost_usd: number;
  generated_at: string | null;
}

export const fetchVocWeeklyDigest = (weekStart: string = "") =>
  request<VocWeeklyDigest | null>(`/voc/weekly-digest${weekStart ? `?week_start=${weekStart}` : ""}`);

export const generateVocWeeklyDigest = (weekStart: string = "", force: boolean = false) =>
  request<VocWeeklyDigest>(
    `/voc/weekly-digest/generate?${weekStart ? `week_start=${weekStart}&` : ""}force=${force}`,
    { method: "POST" },
  );

export const fetchVocWeeklyDigests = (limit: number = 12) =>
  request<{ digests: VocWeeklyDigest[] }>(`/voc/weekly-digests?limit=${limit}`);
```

- [ ] **Step 2: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors introduced by this file (pre-existing errors elsewhere, if any, are out of scope)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/api.ts
git commit -m "feat(voc): add trend/movers/weekly-digest API client types and wrappers"
```

---

## Task 12: Frontend — page-level taxonomy switch + VOC Top10/donut/trend charts

**Files:**
- Modify: `frontend/src/app/analytics/page.tsx`
- Modify: `frontend/src/lib/i18n.ts`

**Interfaces:**
- Consumes: `fetchVocTrend`, `VocTrend` from Task 11; existing `fetchVocClassificationStats`, `VocClassificationStats`.
- Produces: page-level `taxonomyMode` state (renamed/promoted from the existing `classificationTab`), replacing its narrow scope (`page.tsx:79`, `:546-564`) with a header-level switch controlling ALL classification cards.

- [ ] **Step 1: Promote the tab state and move the switch to the header**

In `frontend/src/app/analytics/page.tsx`, rename the state (search-and-replace `classificationTab`/`setClassificationTab` → `taxonomyMode`/`setTaxonomyMode` throughout the file — used at lines 79, 548-562, 567, 624, 633):

```tsx
const [taxonomyMode, setTaxonomyMode] = useState<"voc" | "legacy">("voc");
```

Add fetch state for the new VOC trend data, alongside the existing `vocStats` state declaration:

```tsx
const [vocTrend, setVocTrend] = useState<VocTrend | null>(null);
```

Add `fetchVocTrend` and `type VocTrend` to the existing import line from `@/lib/api`.

In the `load` function, add a parallel fetch:

```tsx
const [res, ra, pt, cls, voc, vocTrendRes] = await Promise.all([
  fetch(`/api/analytics/dashboard?days=${d}`),
  fetchRuleAccuracy(d).catch(() => []),
  fetchProblemTypeStats(d).catch(() => null),
  fetchClassificationStats(d).catch(() => null),
  fetchVocClassificationStats(d).catch(() => null),
  fetchVocTrend(d, "group").catch(() => null),
]);
if (res.ok) setData(await res.json());
setRuleAccuracy(ra);
setPtStats(pt);
setClsStats(cls);
setVocStats(voc);
setVocTrend(vocTrendRes);
```

Move the switch UI (currently at `page.tsx:544-564`, gated on `{(vocStats || (clsStats && ...))}`) into the header's button row, right after the days-selector `<div>` (inside the `<div className="flex items-center gap-2">` in the `<header>`):

```tsx
<div className="flex items-center gap-1 rounded-lg p-1" style={{ background: S.overlay }}>
  <button
    onClick={() => setTaxonomyMode("voc")}
    className="rounded-md px-3 py-1.5 text-sm font-medium transition-all"
    style={taxonomyMode === "voc"
      ? { background: S.surface, color: S.text1, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }
      : { color: S.text3 }}>
    {t("VOC 分类")}
  </button>
  <button
    onClick={() => setTaxonomyMode("legacy")}
    className="rounded-md px-3 py-1.5 text-sm font-medium transition-all"
    style={taxonomyMode === "legacy"
      ? { background: S.surface, color: S.text1, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }
      : { color: S.text3 }}>
    {t("旧分类（冻结）")}
  </button>
</div>
```

Delete the old switch block (lines 544-564 in the original file) entirely — it's now redundant with the header one.

- [ ] **Step 2: Gate the existing "问题分类 Top 10" + "问题分类趋势" section (currently `problem_type`-based, `page.tsx:385-542`) behind `taxonomyMode === "legacy"`**

Wrap the existing `{ptStats && ptStats.top10.length > 0 && (() => { ... })()}` block (lines 385-542) in an additional condition:

```tsx
{taxonomyMode === "legacy" && ptStats && ptStats.top10.length > 0 && (() => {
  /* ...unchanged body... */
})()}
```

- [ ] **Step 3: Add the VOC-mode equivalent — Top 10 (group › label) + multi-line trend (top 6 groups)**

Insert this new block immediately before the (now-legacy-gated) block from Step 2, so it occupies the same visual position when `taxonomyMode === "voc"`:

```tsx
{taxonomyMode === "voc" && vocStats && vocStats.groups.length > 0 && (() => {
  // Flatten group>label into a single ranked list for Top 10 — L1 alone is
  // too coarse (11 groups), L3 too sparse at ~400 tickets/month.
  const flat: { key: string; count: number }[] = [];
  for (const g of vocStats.groups) {
    for (const l of g.labels) {
      flat.push({ key: l.label ? `${g.group} › ${l.label}` : g.group, count: l.count });
    }
  }
  flat.sort((a, b) => b.count - a.count);
  const top10 = flat.slice(0, 10);
  const maxCount = top10[0]?.count || 1;
  const COLORS = ["#0E7C86","#2563EB","#16A34A","#DC2626","#7C3AED","#EA580C","#0891B2","#DB2777","#4F46E5","#65A30D"];

  const trendDates = vocTrend ? Object.keys(vocTrend.trend).sort() : [];
  const topGroups = vocStats.groups.slice(0, 6).map((g) => g.group);

  return (
    <div className="grid grid-cols-2 gap-4 j-rise" style={{ ["--d" as string]: "0.12s" }}>
      <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("VOC 分类 Top 10")}</h2>
          <span className="text-[11px] font-mono" style={{ color: S.text3 }}>
            {t("共")} {flat.length} {t("类")} / {vocStats.total_tagged} {t("单已打标")}
          </span>
        </div>
        <div className="space-y-2">
          {top10.map((item, i) => {
            const pct = Math.max(4, (item.count / maxCount) * 100);
            return (
              <div key={item.key} className="flex items-center gap-2">
                <span className="flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold flex-shrink-0"
                  style={{ background: `${COLORS[i]}15`, color: COLORS[i] }}>
                  {i + 1}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between mb-0.5">
                    <span className="text-xs truncate" style={{ color: S.text2 }} title={item.key}>{item.key}</span>
                    <span className="text-xs tabular-nums font-mono flex-shrink-0 ml-2" style={{ color: S.text1 }}>
                      {item.count}
                    </span>
                  </div>
                  <div className="h-3 w-full overflow-hidden rounded-full" style={{ background: S.hover }}>
                    <div className="h-full rounded-full transition-all duration-700"
                      style={{ width: `${pct}%`, background: COLORS[i], opacity: 0.75 }} />
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </section>

      <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
        <h2 className="mb-4 text-sm font-semibold" style={{ color: S.text1 }}>{t("VOC 分类趋势")}</h2>
        {trendDates.length < 2 ? (
          <p className="py-8 text-center text-sm" style={{ color: S.text3 }}>{t("暂无数据")}</p>
        ) : (() => {
          let maxY = 1;
          for (const d of trendDates) {
            for (const g of topGroups) {
              const v = vocTrend!.trend[d]?.[g] || 0;
              if (v > maxY) maxY = v;
            }
          }
          const gridStep = maxY <= 5 ? 1 : maxY <= 20 ? 5 : Math.ceil(maxY / 4 / 5) * 5;
          maxY = Math.ceil(maxY / gridStep) * gridStep;

          const W = 400, H = 200;
          const pad = { top: 8, right: 12, bottom: 22, left: 28 };
          const cw = W - pad.left - pad.right;
          const ch = H - pad.top - pad.bottom;
          const xStep = trendDates.length > 1 ? cw / (trendDates.length - 1) : 0;
          const toX = (i: number) => pad.left + i * xStep;
          const toY = (v: number) => pad.top + ch - (v / maxY) * ch;

          const buildPath = (pts: [number, number][]) => {
            if (pts.length < 2) return "";
            let d = `M${pts[0][0]},${pts[0][1]}`;
            for (let i = 0; i < pts.length - 1; i++) {
              const p0 = pts[Math.max(0, i - 1)];
              const p1 = pts[i];
              const p2 = pts[i + 1];
              const p3 = pts[Math.min(pts.length - 1, i + 2)];
              const cp1x = p1[0] + (p2[0] - p0[0]) / 6;
              const cp1y = p1[1] + (p2[1] - p0[1]) / 6;
              const cp2x = p2[0] - (p3[0] - p1[0]) / 6;
              const cp2y = p2[1] - (p3[1] - p1[1]) / 6;
              d += ` C${cp1x},${cp1y} ${cp2x},${cp2y} ${p2[0]},${p2[1]}`;
            }
            return d;
          };
          const labelInterval = Math.max(1, Math.floor(trendDates.length / 6));

          return (
            <div>
              <div className="flex flex-wrap gap-x-3 gap-y-1 mb-2">
                {topGroups.map((g, i) => (
                  <div key={g} className="flex items-center gap-1">
                    <span className="inline-block h-2 w-2 rounded-full" style={{ background: COLORS[i] }} />
                    <span className="text-[10px]" style={{ color: S.text3 }}>{g}</span>
                  </div>
                ))}
              </div>
              <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ overflow: "visible" }}>
                {Array.from({ length: Math.floor(maxY / gridStep) + 1 }, (_, i) => {
                  const v = i * gridStep;
                  const y = toY(v);
                  return (
                    <g key={v}>
                      <line x1={pad.left} x2={W - pad.right} y1={y} y2={y} stroke={S.border} strokeWidth={0.5} />
                      <text x={pad.left - 4} y={y + 3} textAnchor="end"
                        style={{ fontSize: 8, fill: S.text3, fontFamily: "monospace" }}>{v}</text>
                    </g>
                  );
                })}
                {topGroups.map((g, gi) => {
                  const pts: [number, number][] = trendDates.map((d, di) => [toX(di), toY(vocTrend!.trend[d]?.[g] || 0)]);
                  return (
                    <path key={g} d={buildPath(pts)} fill="none" stroke={COLORS[gi]} strokeWidth={1.8}
                      strokeLinecap="round" strokeLinejoin="round" opacity={0.85} />
                  );
                })}
                {topGroups.map((g, gi) =>
                  trendDates.map((d, di) => {
                    const v = vocTrend!.trend[d]?.[g] || 0;
                    if (v === 0) return null;
                    return (
                      <circle key={`${gi}-${di}`} cx={toX(di)} cy={toY(v)} r={2.5} fill="#fff" stroke={COLORS[gi]} strokeWidth={1.5}>
                        <title>{`${d} ${g}: ${v}`}</title>
                      </circle>
                    );
                  })
                )}
                {trendDates.map((d, i) => {
                  if (i % labelInterval !== 0 && i !== trendDates.length - 1) return null;
                  return (
                    <text key={d} x={toX(i)} y={H - 2} textAnchor="middle"
                      style={{ fontSize: 8, fill: S.text3, fontFamily: "monospace" }}>{d.slice(5)}</text>
                  );
                })}
              </svg>
            </div>
          );
        })()}
      </section>
    </div>
  );
})()}
```

- [ ] **Step 4: Gate the legacy pie chart section behind `taxonomyMode === "legacy"`, add a VOC-mode donut**

The existing legacy pie chart block (`page.tsx:632-820`, currently gated on `classificationTab === "legacy"`) already uses the correct guard after Step 1's rename — no further change needed there beyond the rename.

Add a VOC-mode donut chart in the same visual slot, gated on `taxonomyMode === "voc"`, reusing the donut-slice math from the legacy block:

```tsx
{taxonomyMode === "voc" && vocStats && vocStats.groups.length > 0 && (() => {
  const PIE_COLORS = ["#0E7C86","#2563EB","#16A34A","#DC2626","#7C3AED","#EA580C","#0891B2","#DB2777","#4F46E5","#65A30D","#D97706"];
  const total = vocStats.total_tagged;
  const R = 100, cx = 120, cy = 120;
  let angle = 0;
  const slices = vocStats.groups.map((g, i) => {
    const pct = total > 0 ? g.count / total : 0;
    const startAngle = angle;
    angle += pct * 360;
    const endAngle = angle;
    const large = pct > 0.5 ? 1 : 0;
    const rad1 = (startAngle - 90) * Math.PI / 180;
    const rad2 = (endAngle - 90) * Math.PI / 180;
    const x1 = cx + R * Math.cos(rad1), y1 = cy + R * Math.sin(rad1);
    const x2 = cx + R * Math.cos(rad2), y2 = cy + R * Math.sin(rad2);
    const d = pct >= 1
      ? `M${cx},${cy - R} A${R},${R} 0 1,1 ${cx},${cy + R} A${R},${R} 0 1,1 ${cx},${cy - R}Z`
      : `M${cx},${cy} L${x1},${y1} A${R},${R} 0 ${large},1 ${x2},${y2} Z`;
    return { d, color: PIE_COLORS[i % PIE_COLORS.length], group: g.group, count: g.count, pct };
  });

  return (
    <section className="rounded-xl p-5 j-rise" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
      <h2 className="mb-4 text-sm font-semibold" style={{ color: S.text1 }}>{t("VOC 分类占比")}</h2>
      <div className="grid grid-cols-2 gap-6">
        <div className="flex items-center justify-center">
          <svg viewBox="0 0 240 240" className="w-full max-w-[240px]">
            {slices.map((s, i) => (
              <path key={i} d={s.d} fill={s.color} opacity={0.85}
                className="transition-opacity hover:opacity-100 cursor-pointer"
                onClick={() => setExpandedVocGroup(expandedVocGroup === s.group ? null : s.group)}>
                <title>{`${s.group}: ${s.count} (${(s.pct * 100).toFixed(1)}%)`}</title>
              </path>
            ))}
            <circle cx={cx} cy={cy} r={50} fill="var(--j-surface)" />
            <text x={cx} y={cy - 6} textAnchor="middle" style={{ fontSize: 18, fontWeight: 700, fill: S.text1 }}>{total}</text>
            <text x={cx} y={cy + 12} textAnchor="middle" style={{ fontSize: 9, fill: S.text3 }}>{t("已打标工单")}</text>
          </svg>
        </div>
        <div className="space-y-1 max-h-[320px] overflow-y-auto pr-1">
          {vocStats.groups.map((g, i) => {
            const pct = total > 0 ? (g.count / total * 100).toFixed(1) : "0";
            return (
              <div key={g.group} className="flex items-center gap-2 rounded-lg px-2 py-1.5">
                <span className="inline-block h-2.5 w-2.5 rounded-sm flex-shrink-0" style={{ background: PIE_COLORS[i % PIE_COLORS.length] }} />
                <span className="text-xs flex-1 truncate" style={{ color: S.text2 }}>{g.group}</span>
                <span className="text-[11px] font-mono tabular-nums flex-shrink-0" style={{ color: S.text1 }}>{g.count}</span>
                <span className="text-[10px] font-mono flex-shrink-0 w-12 text-right" style={{ color: S.text3 }}>{pct}%</span>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
})()}
```

Place this block right before the existing three-level drill-down table section (`{classificationTab === "voc" && vocStats...}`, now `taxonomyMode === "voc"` after the rename) — the table stays exactly as-is per the "保留" requirement.

- [ ] **Step 5: Add the new i18n keys**

In `frontend/src/lib/i18n.ts`'s `EN` map, add:

```ts
  "VOC 分类 Top 10": "VOC Categories Top 10",
  "VOC 分类趋势": "VOC Category Trend",
  "VOC 分类占比": "VOC Category Share",
  "已打标工单": "Tagged Tickets",
  "单已打标": "tagged",
```

(Check each key isn't already present elsewhere in the file before adding — `grep -n '"VOC 分类"' frontend/src/lib/i18n.ts` to confirm no duplicate-key collision, since object literals silently let the last duplicate win.)

- [ ] **Step 6: Manual verification (no automated test harness for this file)**

Run: `cd frontend && npm run build`
Expected: build succeeds with no new TypeScript errors

Run: `cd frontend && npm run dev`, open `/analytics`, confirm:
- Header switch toggles between VOC and legacy classification cards
- VOC mode: Top 10 list, trend lines, donut, and the existing drill-down table all render with real data
- Legacy mode: unchanged from before this task

- [ ] **Step 7: Commit**

```bash
git add frontend/src/app/analytics/page.tsx frontend/src/lib/i18n.ts
git commit -m "feat(voc): page-level classification switch + VOC Top10/trend/donut charts"
```

---

## Task 13: Frontend — movers chart + weekly digest summary card

**Files:**
- Modify: `frontend/src/app/analytics/page.tsx`
- Modify: `frontend/src/lib/i18n.ts`

**Interfaces:**
- Consumes: `fetchVocMovers`, `fetchVocWeeklyDigest`, `generateVocWeeklyDigest`, `VocMoversResponse`, `VocWeeklyDigest` from Task 11.

- [ ] **Step 1: Add state + fetches**

Add state declarations near the other VOC state:

```tsx
const [vocMovers, setVocMovers] = useState<VocMoversResponse | null>(null);
const [digest, setDigest] = useState<VocWeeklyDigest | null>(null);
const [digestLoading, setDigestLoading] = useState(false);
const [digestRegenerating, setDigestRegenerating] = useState(false);
```

Add imports: `fetchVocMovers`, `fetchVocWeeklyDigest`, `generateVocWeeklyDigest`, `type VocMoversResponse`, `type VocWeeklyDigest`.

Add a fetch to the `load()` function's `Promise.all` (movers is a fixed 7-day window, independent of the page's `days` selector):

```tsx
fetchVocMovers(7, "label", 3).catch(() => null),
```

and destructure/set it: `setVocMovers(vocMoversRes)`.

Add a separate `useEffect` for the digest (independent lifecycle — it doesn't depend on `days`):

```tsx
useEffect(() => {
  setDigestLoading(true);
  fetchVocWeeklyDigest().then(setDigest).catch(() => setDigest(null)).finally(() => setDigestLoading(false));
}, []);

const regenerateDigest = async () => {
  setDigestRegenerating(true);
  try {
    const result = await generateVocWeeklyDigest("", true);
    setDigest(result);
  } catch {} finally { setDigestRegenerating(false); }
};
```

- [ ] **Step 2: Add the weekly digest card** — insert right after the value-metrics hero section (`page.tsx:177-215`), before the key-metrics grid

```tsx
<section className="rounded-2xl p-6 j-rise" style={{ background: S.surface, border: `1px solid ${S.border}`, ["--d" as string]: "0.02s" }}>
  <div className="flex items-center justify-between mb-4">
    <div className="flex items-center gap-2">
      <span className="rounded-lg px-2 py-0.5 text-[11px] font-semibold"
        style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(14,124,134,0.25)" }}>
        {t("上周焦点")}
      </span>
      {digest && <span className="text-xs" style={{ color: S.text3 }}>{digest.week_start}</span>}
    </div>
    <button
      onClick={regenerateDigest}
      disabled={digestRegenerating}
      className="rounded-lg px-3 py-1.5 text-[11px] font-medium transition-all"
      style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(14,124,134,0.3)", opacity: digestRegenerating ? 0.5 : 1 }}>
      {digestRegenerating ? t("生成中...") : t("重新生成")}
    </button>
  </div>

  {digestLoading ? (
    <p className="py-6 text-center text-sm" style={{ color: S.text3 }}>{t("加载中")}...</p>
  ) : !digest ? (
    <p className="py-6 text-center text-sm" style={{ color: S.text3 }}>{t("本周暂无汇总，点击「重新生成」创建。")}</p>
  ) : (
    <div className="space-y-4">
      {digest.narrative ? (
        <p className="text-lg font-semibold" style={{ color: S.text1 }}>{digest.narrative.headline}</p>
      ) : (
        <p className="text-sm" style={{ color: "#DC2626" }}>{t("洞察生成失败，以下为确定性统计，可点击「重新生成」重试。")}</p>
      )}

      <p className="text-xs" style={{ color: S.text3 }}>
        {t("本期共")} {digest.stats.total_cur} {t("单")}
        {digest.stats.total_delta_pct !== null && (
          <> · {t("环比")} {digest.stats.total_delta_pct > 0 ? "+" : ""}{digest.stats.total_delta_pct}%</>
        )}
      </p>

      {digest.narrative && digest.narrative.key_findings.length > 0 && (
        <div>
          <h3 className="text-xs font-semibold mb-2" style={{ color: S.text2 }}>{t("关键发现")}</h3>
          <ul className="space-y-1">
            {digest.narrative.key_findings.map((f, i) => (
              <li key={i} className="text-xs" style={{ color: S.text2 }}>
                <span className="font-medium" style={{ color: S.text1 }}>{f.scope}</span>：{f.finding}
              </li>
            ))}
          </ul>
        </div>
      )}

      {digest.narrative && digest.narrative.product_opportunities.length > 0 && (
        <div className="rounded-xl p-4" style={{ background: S.accentBg, border: "1px solid rgba(14,124,134,0.25)" }}>
          <h3 className="text-xs font-semibold mb-2" style={{ color: S.accent }}>{t("产品优化建议")}</h3>
          <ul className="space-y-2">
            {digest.narrative.product_opportunities.map((o, i) => (
              <li key={i} className="text-xs" style={{ color: S.text1 }}>
                <span className="font-semibold">{o.area}</span>：{o.problem} → <span className="font-medium">{o.suggestion}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {digest.stats.top_movers.length > 0 && (
        <div>
          <h3 className="text-xs font-semibold mb-2" style={{ color: S.text2 }}>{t("环比变动")}</h3>
          <div className="space-y-1">
            {digest.stats.top_movers.slice(0, 5).map((m) => (
              <div key={m.key} className="flex items-center justify-between text-xs">
                <span className="truncate" style={{ color: S.text2 }} title={m.key}>{m.key}</span>
                <span className="font-mono tabular-nums flex-shrink-0 ml-2"
                  style={{ color: m.delta > 0 ? "#DC2626" : m.delta < 0 ? "#16A34A" : S.text3 }}>
                  {m.prev} → {m.cur} ({m.delta_pct !== null ? `${m.delta_pct > 0 ? "+" : ""}${m.delta_pct}%` : t("新增")})
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )}
</section>
```

- [ ] **Step 3: Add the movers diverging bar chart** — insert right after the VOC donut chart from Task 12 Step 4

```tsx
{taxonomyMode === "voc" && vocMovers && vocMovers.movers.length > 0 && (() => {
  const maxAbs = Math.max(1, ...vocMovers.movers.map((m) => Math.abs(m.delta)));
  const top = vocMovers.movers.slice(0, 10);
  return (
    <section className="rounded-xl p-5 j-rise" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("周环比变动")}</h2>
        <span className="text-[11px] font-mono" style={{ color: S.text3 }}>
          {vocMovers.prev_from} ~ {vocMovers.prev_to} → {vocMovers.cur_from} ~ {vocMovers.cur_to}
        </span>
      </div>
      <div className="space-y-2">
        {top.map((m) => {
          const widthPct = (Math.abs(m.delta) / maxAbs) * 50; // half-width max, diverges from center
          const isUp = m.delta > 0;
          return (
            <div key={m.key} className="flex items-center gap-2">
              <span className="text-xs w-1/3 truncate text-right" style={{ color: S.text2 }} title={m.key}>{m.key}</span>
              <div className="flex-1 flex items-center h-4" style={{ position: "relative" }}>
                <div className="absolute left-1/2 top-0 bottom-0 w-px" style={{ background: S.border }} />
                <div className="h-full rounded"
                  style={{
                    position: "absolute",
                    left: isUp ? "50%" : `${50 - widthPct}%`,
                    width: `${widthPct}%`,
                    background: isUp ? "#DC2626" : "#16A34A",
                    opacity: 0.75,
                  }} />
              </div>
              <span className="text-[11px] font-mono tabular-nums w-20 flex-shrink-0"
                style={{ color: isUp ? "#DC2626" : "#16A34A" }}>
                {m.prev}→{m.cur} ({m.delta_pct !== null ? `${m.delta_pct > 0 ? "+" : ""}${m.delta_pct}%` : t("新增")})
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
})()}
```

- [ ] **Step 4: Add the new i18n keys**

```ts
  "上周焦点": "Last Week's Focus",
  "重新生成": "Regenerate",
  "生成中...": "Generating...",
  "本周暂无汇总，点击「重新生成」创建。": "No digest for this week yet — click Regenerate to create one.",
  "洞察生成失败，以下为确定性统计，可点击「重新生成」重试。": "Insight generation failed — showing deterministic stats only; click Regenerate to retry.",
  "本期共": "This period",
  "环比": "vs. prior period",
  "关键发现": "Key Findings",
  "产品优化建议": "Product Opportunities",
  "环比变动": "Week-over-Week Change",
  "新增": "new",
  "周环比变动": "Week-over-Week Movers",
```

- [ ] **Step 5: Manual verification**

Run: `cd frontend && npm run build`
Expected: no new TypeScript errors

Run: `cd frontend && npm run dev`, open `/analytics`, confirm:
- Weekly digest card renders (either real content or the "no digest yet" empty state)
- Clicking "重新生成" shows a loading state and updates the card
- Movers diverging bar chart renders under the VOC donut

- [ ] **Step 6: Commit**

```bash
git add frontend/src/app/analytics/page.tsx frontend/src/lib/i18n.ts
git commit -m "feat(voc): add weekly digest summary card + movers diverging bar chart"
```

---

## Task 14: Frontend — `/reports` weekly digest history tab

**Files:**
- Modify: `frontend/src/app/reports/page.tsx`
- Modify: `frontend/src/lib/i18n.ts`

**Interfaces:**
- Consumes: `fetchVocWeeklyDigests` from Task 11; existing `MarkdownText` component (`frontend/src/components/MarkdownText.tsx`, already used in this file for the daily report markdown).

- [ ] **Step 1: Add a page-level tab switch**

In `frontend/src/app/reports/page.tsx`, add state:

```tsx
const [reportTab, setReportTab] = useState<"daily" | "weekly">("daily");
const [digests, setDigests] = useState<VocWeeklyDigest[]>([]);
const [selectedWeek, setSelectedWeek] = useState("");
const [weeklyLoading, setWeeklyLoading] = useState(false);
```

Add the import: `fetchVocWeeklyDigests, type VocWeeklyDigest` from `@/lib/api`.

Add a fetch effect:

```tsx
useEffect(() => {
  if (reportTab !== "weekly") return;
  setWeeklyLoading(true);
  fetchVocWeeklyDigests(12)
    .then((r) => {
      setDigests(r.digests);
      if (r.digests.length > 0 && !selectedWeek) setSelectedWeek(r.digests[0].week_start);
    })
    .catch(() => setDigests([]))
    .finally(() => setWeeklyLoading(false));
}, [reportTab]);
```

Add the tab switcher UI right below the page's existing header (before the daily-report date picker):

```tsx
<div className="flex items-center gap-1 rounded-lg p-1 mb-4" style={{ background: S.overlay, width: "fit-content" }}>
  <button onClick={() => setReportTab("daily")}
    className="rounded-md px-3 py-1.5 text-xs font-medium transition-all"
    style={reportTab === "daily" ? { background: S.surface, color: S.text1 } : { color: S.text3 }}>
    {t("值班日报")}
  </button>
  <button onClick={() => setReportTab("weekly")}
    className="rounded-md px-3 py-1.5 text-xs font-medium transition-all"
    style={reportTab === "weekly" ? { background: S.surface, color: S.text1 } : { color: S.text3 }}>
    {t("VOC 周报")}
  </button>
</div>
```

- [ ] **Step 2: Wrap the existing daily-report JSX in `{reportTab === "daily" && (...)}`, add the weekly tab body**

```tsx
{reportTab === "weekly" && (
  <div className="grid grid-cols-[200px_1fr] gap-4">
    <div className="space-y-1">
      {weeklyLoading ? (
        <p className="text-xs" style={{ color: S.text3 }}>{t("加载中")}...</p>
      ) : digests.length === 0 ? (
        <p className="text-xs" style={{ color: S.text3 }}>{t("暂无周报")}</p>
      ) : (
        digests.map((d) => (
          <button key={d.week_start} onClick={() => setSelectedWeek(d.week_start)}
            className="block w-full text-left rounded-lg px-3 py-2 text-xs transition-colors"
            style={selectedWeek === d.week_start
              ? { background: S.accentBg, color: S.accent, border: "1px solid rgba(14,124,134,0.3)" }
              : { color: S.text2, border: `1px solid ${S.border}` }}>
            {d.week_start}
          </button>
        ))
      )}
    </div>
    <div>
      {(() => {
        const selected = digests.find((d) => d.week_start === selectedWeek);
        if (!selected) return <p className="text-sm" style={{ color: S.text3 }}>{t("选择左侧周次查看")}</p>;
        return (
          <div className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <MarkdownText content={selected.markdown} />
          </div>
        );
      })()}
    </div>
  </div>
)}
```

- [ ] **Step 3: Add i18n keys**

```ts
  "值班日报": "Daily Report",
  "VOC 周报": "VOC Weekly Report",
  "暂无周报": "No weekly reports yet",
  "选择左侧周次查看": "Select a week on the left",
```

- [ ] **Step 4: Manual verification**

Run: `cd frontend && npm run build`

Run: `cd frontend && npm run dev`, open `/reports`, confirm both tabs render and the weekly tab lists/selects digests correctly (empty state if none generated yet).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app/reports/page.tsx frontend/src/lib/i18n.ts
git commit -m "feat(voc): add weekly digest history tab to /reports"
```

---

## Final Verification

```bash
cd backend && source .venv/bin/activate

# Full backend regression — not just the VOC-specific files (Task 10's
# database.py/base.py changes touch shared hot-path code)
pytest tests/ --ignore=tests/crashguard -q
lint-imports

cd ../frontend
npm run build

# Manual, local (data/appllo.db is a stale test DB — real content quality
# needs a check against 102's real data before this ships, see below)
cd ../backend && python -m uvicorn app.main:app --port 8000 --reload &
curl -s 'localhost:8000/api/voc/trend?days=30&level=group' | python3 -m json.tool | head -20
curl -s 'localhost:8000/api/voc/movers?days=7' | python3 -m json.tool
curl -s -X POST 'localhost:8000/api/voc/weekly-digest/generate' | python3 -m json.tool
curl -s 'localhost:8000/api/voc/taxonomy' | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['seed_fetched_at'], d['seed_tag_count'])"
```

Then in the browser: `/analytics` (both taxonomy modes, digest card, movers chart) and `/reports` (weekly tab).

**Explicitly out of scope for this plan** (per user's standing rules — surface, don't act):
- Deploying to 102 (`./deploy-all.sh`) — separate, requires fresh in-the-moment confirmation, and avoid peak hours (9:00–22:00).
- Judging the LLM narrative's actual quality against real production ticket data — the design doc calls this out as something that must happen on 102's real data, not the stale local DB.
- Turning on `voc.digest_push_enabled` (Feishu push) — stays False until the generated content has been reviewed for a couple of weeks.
- Re-pulling the VOC taxonomy MCP snapshot and calling `/api/voc/taxonomy/reseed` — that's a follow-up action using the mechanism this plan builds, not part of building it.
