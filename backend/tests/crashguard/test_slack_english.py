"""Slack 一律英文守卫（2026-10-09）。

用户要求：**所有 Slack 消息不得出现任何中文**，飞书保持中文不变。crashguard 的
Slack 出口逐个渲染一遍，断言零 CJK：

| 出口 | 渲染方式 |
|---|---|
| 早晚报 | `slack_daily.build_daily_slack` + 降级纯文本 `_slack_text_fallback` |
| hourly / core_metric / fatal_backlog / job_health / symbol 告警 | `feishu_card.build_*(lang="en")` → `compile_card` |
| PR reviewer / fallback / pending review 卡片 | `build_*(lang="en")` → `compile_card` |
| PR QA / PR review 动态 / 冲突通知（纯文本） | 截获 `notify.send_text` 的 `text_en` |

另外钉两件事：调用点给 Slack 的 lambda 确实走 `lang="en"`；默认 `lang="zh"`
的飞书卡片仍是中文。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.crashguard.services import feishu_card as fc
from app.services.im.feishu_to_slack import compile_card

_CJK = re.compile(r"[　-〿一-鿿＀-￯]")


def _dump(msg) -> str:
    return json.dumps(
        [msg.payload, [f.blocks for f in msg.folds], [f.title for f in msg.folds], msg.text],
        ensure_ascii=False,
    )


def _assert_no_cjk(s: str) -> None:
    m = _CJK.search(s)
    assert m is None, f"Slack 出现中文 {m.group()!r}: …{s[max(0, m.start() - 60):m.end() + 60]}…"


# ── 样例数据（尽量把每个分支都打开） ──────────────────────────────────────

_ITEM = {
    "issue_id": "i1", "title": "SIGABRT in AudioEngine", "platform": "ios",
    "version": "4.0.1", "first_seen_version": "4.0.0", "first_seen_at": "2026-10-08",
    "events_h": 50, "sessions_h": 10, "user_rate_pct": 1.2, "events_24h": 9, "sessions_24h": 3,
    "baseline": 10.0, "growth_pct": 400.0, "baseline_source": "show",
    "rate_now": 1.5, "rate_growth_pct": 20.0,
}
_ITEM_7D = dict(_ITEM, baseline_source="7d", sessions_h=0, rate_now=None)
_CORE_ITEMS = [
    {"platform": "ios", "dimension": "overall", "direction": "down", "delta_pp": -0.5,
     "crash_free_pct": 99.1, "baseline_pct": 99.6, "total_sessions": 100, "crashed_sessions": 5},
    {"platform": "android", "dimension": "main_version", "version_tag": "4.0",
     "direction": "up", "delta_pp": 0.5},
    {"platform": "android", "dimension": "latest_version", "direction": "up", "delta_pp": 0.5},
]
_HOUR = datetime(2026, 10, 8, 3)


def _hourly(lang):
    return fc.build_hourly_alert_card(
        hour_utc=_HOUR, new_items=[_ITEM, _ITEM_7D], surge_items=[_ITEM, _ITEM_7D],
        new_version_items=[_ITEM], new_crash_items=[_ITEM], alert_id=7, lang=lang)


def _core(lang):
    return fc.build_core_metric_alert_card(_HOUR, _CORE_ITEMS, alert_id=7, lang=lang)


def _backlog(lang):
    return fc.build_fatal_backlog_alert_card(
        12, 10, [{"datadog_issue_id": "d", "title": "NPE in Foo", "platform": "android",
                  "first_seen_at": "2026-10-08", "events_count": 3}], lang=lang)


def _job(lang):
    return fc.build_job_health_alert_card(
        [{"job_name": "hourly_alert", "health": "failing", "consecutive_failures": 3,
          "last_error": "TimeoutError", "interval_minutes": 60},
         {"job_name": "daily_report", "health": "stale"}], lang=lang)


def _symbol(lang, stale=None):
    return fc.build_symbol_health_alert_card(
        [{"platform": "ios", "version": "4.0.1", "events": 5}],
        [{"platform": "android", "version": "4.0.2", "raw_rate": 0.5, "raw_count": 1, "total": 2}],
        stale or {"age_days": 20, "stale_days": 7, "last_upload_at": "2026-09-18"}, lang=lang)


def _reviewer(lang):
    from app.crashguard.services.pr_reviewer import build_reviewer_card
    return build_reviewer_card(pr_url="https://github.com/o/r/pull/1", pr_title="[crashguard] r #1",
                               crash_title="issue abc", crash_url="https://dd/x",
                               line_count=3, total_lines=10, lang=lang)


def _fallback(lang, reason="all_unresolved"):
    from app.crashguard.services.pr_reviewer import build_fallback_card
    return build_fallback_card("https://github.com/o/r/pull/1", "[crashguard] r #1", reason,
                               ["a@plaud.ai"], lang=lang)


def _pending(lang, empty=False):
    from app.crashguard.services.pr_pending_review_alert import build_pending_review_card
    pr = {"repo": "plaud-native-app", "pr_number": 12, "pr_url": "https://github.com/o/r/pull/12",
          "generation": "native", "age_days": 3, "pr_status": "draft",
          "reviewer_emails": ["a@plaud.ai", "b@plaud.ai", "c@plaud.ai"]}
    lst = [] if empty else [pr, dict(pr, age_days=0, pr_status="open", reviewer_emails=[])]
    return build_pending_review_card(
        lst, stats={"yesterday_merged": 1, "yesterday_closed": 0, "yesterday_created": 2},
        frontend_base_url="http://h", approved_prs=lst, yesterday_merged_prs=lst,
        yesterday_closed_prs=[], yesterday_created_prs=lst, lang=lang)


_CARD_BUILDERS = {
    "hourly": _hourly, "core_metric": _core, "fatal_backlog": _backlog,
    "job_health": _job, "symbol": _symbol,
    "symbol_never": lambda lang: _symbol(lang, {"age_days": None, "stale_days": 7}),
    "pr_reviewer": _reviewer, "pr_fallback": _fallback,
    "pr_fallback_unknown_reason": lambda lang: _fallback(lang, "diff_empty"),
    "pr_pending": _pending, "pr_pending_empty": lambda lang: _pending(lang, empty=True),
}


@pytest.mark.parametrize("name", sorted(_CARD_BUILDERS))
def test_card_slack_english(name):
    _assert_no_cjk(_dump(compile_card(_CARD_BUILDERS[name]("en"))))


@pytest.mark.parametrize("name", sorted(_CARD_BUILDERS))
def test_card_default_stays_chinese_for_feishu(name):
    """飞书腿不动：默认 lang 产出仍含中文（逐字节一致由各模块原有测试钉着）。"""
    builder = _CARD_BUILDERS[name]
    default = builder("zh")
    assert _CJK.search(json.dumps(default, ensure_ascii=False))
    assert default != builder("en")


def test_hourly_feedback_buttons_english():
    s = json.dumps(_hourly("en"), ensure_ascii=False)
    assert "👍 Accurate" in s and "👎 Inaccurate" in s and "📊 View on web" in s
    assert "Crashguard real-time alert" in s


def test_core_metric_dim_labels_english():
    s = json.dumps(_core("en"), ensure_ascii=False)
    assert "📊 Overall" in s and "👥 Primary version" in s and "🆕 Latest version" in s


# ── 早晚报 ────────────────────────────────────────────────────────────────

def _daily_payload():
    from tests.crashguard.test_slack_daily import _payload
    return _payload()


@pytest.mark.parametrize("report_type", ["morning", "evening"])
@pytest.mark.parametrize("coreguard", [
    None,
    {"available": True, "headline_hint": "⚠️ 转写成功率持续偏低"},  # 没有英文版 → 整段丢弃
    {"available": True, "headline_hint": "⚠️ 转写成功率持续偏低", "headline_hint_en": "⚠️ Low ASR"},
])
def test_daily_slack_english(report_type, coreguard):
    from app.crashguard.services.slack_daily import build_daily_slack
    r = build_daily_slack(report_type=report_type, target_date="2026-10-08",
                          payload=_daily_payload(), frontend_base_url="http://h/",
                          coreguard_section=coreguard)
    _assert_no_cjk(_dump(r))


@pytest.mark.parametrize("tldr", [
    {"severity": "weird"},
    {"severity": "green", "fatal_today_total": 10, "fatal_baseline_total": 0},
    {"severity": "red", "fatal_today_total": 300, "fatal_baseline_total": 100},
])
def test_daily_slack_fallback_headline_english(tldr):
    """user 维度缺失时不再用 compose 的中文 headline。"""
    from app.crashguard.services.slack_daily import build_daily_slack
    p = _daily_payload()
    p["tldr"] = tldr
    r = build_daily_slack(report_type="morning", target_date="2026-10-08", payload=p,
                          frontend_base_url="http://h/")
    _assert_no_cjk(_dump(r))


@pytest.mark.parametrize("report_type", ["morning", "evening"])
def test_daily_slack_text_fallback_english(report_type):
    from app.crashguard.services.daily_report import _slack_text_fallback
    t = _slack_text_fallback(report_type, "2026-10-08", "http://h/")
    _assert_no_cjk(t)
    assert f"http://h/crashguard/reports?type={report_type}&date=2026-10-08" in t


# ── 调用点：Slack lambda 真的走 lang="en" ──────────────────────────────────

_SERVICES = Path(__file__).resolve().parents[2] / "app" / "crashguard" / "services"


@pytest.mark.parametrize("fname,calls", [
    ("hourly_alerter.py", 1), ("core_metric_alerter.py", 1), ("job_health_alerter.py", 2),
    ("symbol_coverage_monitor.py", 1), ("pr_reviewer.py", 2), ("pr_pending_review_alert.py", 1),
])
def test_call_sites_build_english_card_for_slack(fname, calls):
    """每个 send_alert / send_card_to 调用里，Slack lambda 都按 lang="en" 重新构卡。

    防回归：有人把 lambda 改回 `compile_card(card)`（中文卡）时这里会红。
    """
    src = (_SERVICES / fname).read_text(encoding="utf-8")
    sites = [m.start() for m in re.finditer(r"notify\.send_(alert|card_to)\(", src)]
    assert len(sites) == calls
    for start in sites:
        window = src[start:start + 400]
        assert 'lang="en"' in window, f"{fname}: Slack lambda 没走 lang=\"en\"：{window[:200]}"
        assert "lambda: compile_card(card)" not in window


@pytest.mark.asyncio
async def test_notify_reviewers_slack_lambdas_english(monkeypatch):
    from app.crashguard.services import notify
    from app.crashguard.services.pr_reviewer import ReviewerResolution, notify_reviewers

    captured = []

    async def _fake_send_card_to(email, card, slack=None, **kw):
        captured.append((card, slack()))
        return kw.get("what") != "pr_reviewer"  # reviewer 全失败 → 再走 fallback

    monkeypatch.setattr(notify, "send_card_to", _fake_send_card_to)
    pr = SimpleNamespace(pr_url="https://github.com/o/r/pull/1", pr_number=1, repo="r",
                         datadog_issue_id="abc")
    settings = SimpleNamespace(pr_reviewer_fallback_email="fb@plaud.ai")
    await notify_reviewers(pr, ReviewerResolution(emails=["a@plaud.ai"],
                                                  line_counts={"a@plaud.ai": 3}, reason="ok"),
                           settings)
    assert len(captured) == 2  # reviewer + fallback
    for feishu_card, slack_msg in captured:
        assert _CJK.search(json.dumps(feishu_card, ensure_ascii=False))  # 飞书仍中文
        _assert_no_cjk(_dump(slack_msg))


# ── 纯文本出口：截 text_en ─────────────────────────────────────────────────

def _capture_send_text(monkeypatch):
    from app.crashguard.services import notify
    calls = []

    async def _fake(text, *, text_en="", email="", s=None, what="text"):
        calls.append({"text": text, "text_en": text_en, "what": what})
        return True

    monkeypatch.setattr(notify, "send_text", _fake)
    return calls


def _settings_stub():
    return SimpleNamespace(feishu_alert_email="ops@plaud.ai", feishu_target_email="",
                           conflict_resync_fallback_email="ops@plaud.ai",
                           pr_reviewer_fallback_email="")


@pytest.mark.asyncio
@pytest.mark.parametrize("llm_lang", ["zh", "en"])
async def test_pr_qa_text_en(monkeypatch, llm_lang):
    from app.crashguard.services import pr_qa_agent
    calls = _capture_send_text(monkeypatch)
    monkeypatch.setattr("app.crashguard.config.get_crashguard_settings", _settings_stub)
    zh = llm_lang == "zh"
    parsed = {
        "quality_score": 42, "verdict": "needs_revision", "addresses_root_cause": False,
        "reviewer_summary": "修复不完整，未处理空指针" if zh else "Fix is partial",
        "scope_issues": ["改动了无关文件" if zh else "touches unrelated file", "ok scope"],
        "regression_risks": ["可能影响录音" if zh else "may affect recording"],
    }
    await pr_qa_agent._notify_low_quality("https://github.com/o/r/pull/1", "o/r", 1, parsed)
    assert len(calls) == 1
    assert _CJK.search(calls[0]["text"])  # 飞书仍中文
    en = calls[0]["text_en"]
    _assert_no_cjk(en)
    assert "42/100" in en and "https://github.com/o/r/pull/1" in en
    if not zh:
        assert "Fix is partial" in en and "may affect recording" in en


@pytest.mark.asyncio
async def test_pr_sync_review_text_en(monkeypatch):
    from app.crashguard.services import pr_sync
    calls = _capture_send_text(monkeypatch)
    monkeypatch.setattr("app.crashguard.config.get_crashguard_settings", _settings_stub)
    pr_row = SimpleNamespace(pr_url="https://github.com/o/r/pull/1", branch_name="crashguard/fix-1",
                             pr_status="draft")
    acts = [{"type": "review", "state": "CHANGES_REQUESTED", "author": "alice",
             "body": "这里会空指针，请修改", "at": "1"},
            {"type": "comment", "state": "", "author": "bob", "body": "LGTM overall", "at": "2"}]
    acts += [dict(acts[1], at=str(i)) for i in range(3, 9)]
    await pr_sync._notify_review_activity(pr_row, acts, "CHANGES_REQUESTED")
    assert len(calls) == 1
    assert _CJK.search(calls[0]["text"])
    en = calls[0]["text_en"]
    _assert_no_cjk(en)
    assert "LGTM overall" in en and "(non-English comment, see PR)" in en and "more" in en


@pytest.mark.asyncio
async def test_pr_conflict_text_en(monkeypatch):
    from app.crashguard.services import pr_conflict_resync
    calls = _capture_send_text(monkeypatch)
    monkeypatch.setattr(pr_conflict_resync, "get_crashguard_settings", _settings_stub)
    await pr_conflict_resync._notify_conflicts([
        {"pr_url": "https://github.com/o/r/pull/1", "reviewer_emails": '["a@plaud.ai","b@plaud.ai"]'},
        {"pr_url": "https://github.com/o/r/pull/2", "reviewer_emails": None},
    ])
    assert len(calls) == 1
    assert _CJK.search(calls[0]["text"])
    en = calls[0]["text_en"]
    _assert_no_cjk(en)
    assert "a@plaud.ai, b@plaud.ai" in en and "unassigned" in en


def test_has_cjk_helper():
    from app.crashguard.services.notify import has_cjk
    assert has_cjk("修复") and has_cjk("a，b") and not has_cjk("plain — ascii · ok 🚀")
