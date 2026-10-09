"""2026-10-09：灰度日报网页端完整版 + Slack 精简版 + markdown 缓存。

- 完整版 markdown：指标 × 平台口径 一张 GFM 表，核心指标在前
- Slack：传了 report_url 只放核心指标 + 「查看完整日报 →」，不发 thread
- 发送路径：取数只跑一次（provider=both 两条腿共用），发送前落库缓存
- 详情读取：有缓存直接读；没有就现算回填；超出保留期不现算
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.graygate.services import notify as gnotify
from app.graygate.services import report_store
from app.graygate.services.card_builder import GraygateReportData, TierSummary
from app.graygate.services.report_markdown import assemble_report_markdown
from app.graygate.services.slack_report import assemble_slack_message

URL = "https://jarvis.example/graygate/reports?date=2026-08-18"


def _tiers(platform: str):
    na = "—（不适用）"
    hang = "🟩 0.10%" if platform == "ios" else na
    anr = na if platform == "ios" else "🟥 0.40%"
    cells = {"crash_free": "🟩 99.90%", "hang_rate": hang, "android_anr": anr, "fps": "58.00"}
    return [
        TierSummary("大盘", "4.0.3*", 12345, False, dict(cells)),
        TierSummary("主要版本", "4.0.301-1038", 900, platform == "ios", dict(cells)),
    ]


def _data(target=date(2026, 8, 18), *, new_crashes=0) -> GraygateReportData:
    return GraygateReportData(
        target_date=target,
        d1_day=target - timedelta(days=1),
        version_pattern="4.0.3*",
        worsen_lines=[],
        columns_md=["**🍎 iOS**\n\n· 全量指标", "**🤖 Android**\n\n· 全量指标"],
        new_crash_md=("**🆕 新增崩溃堆栈**\n\n- IOS · `x` · [Foo →](https://dd/x)" if new_crashes else None),
        top_crash_md="**🔥 Top 5 崩溃（按 events，不限是否新增）**\n\n1. IOS · **10** events · [Bar →](https://dd/y)",
        top_jank_md=None,
        metric_rows=[("crash_free", "Crash-free sessions", True), ("hang_rate", "Hang Rate", True),
                     ("android_anr", "Android ANR", True), ("fps", "APP单次运行平均FPS", False)],
        tiers={"ios": _tiers("ios"), "android": _tiers("android")},
        new_crash_count=new_crashes,
    )


# ---------------------------------------------------------------------------
# 完整版 markdown
# ---------------------------------------------------------------------------
def test_full_markdown_is_one_gfm_table_with_core_metrics_first():
    md = assemble_report_markdown(_data(new_crashes=1))
    assert "| 指标 | 🍎 iOS · 大盘 | 🍎 iOS · 主要版本 | 🤖 Android · 大盘 | 🤖 Android · 主要版本 |" in md
    assert "|---|---|---|---|---|" in md
    assert "`4.0.301-1038`（人工指定）" in md          # iOS 主要版本人工指定
    assert "| Sessions | 12,345 | 900 | 12,345 | 900 |" in md
    core_pos = md.index("**Crash-free sessions**")
    sep_pos = md.index("其他指标（不参与恶化判定）")
    assert core_pos < sep_pos < md.index("APP单次运行平均FPS")
    # 附录段：加粗首行变成 ## 标题
    assert "## 🆕 新增崩溃堆栈" in md and "## 🔥 Top 5 崩溃" in md


def test_full_markdown_falls_back_to_columns_without_structured_tiers():
    d = _data()
    d.tiers = {}
    md = assemble_report_markdown(d)
    assert "· 全量指标" in md and "| 指标 |" not in md


# ---------------------------------------------------------------------------
# Slack 精简版
# ---------------------------------------------------------------------------
def test_slack_compact_has_only_core_metrics_and_link_no_thread():
    msg = assemble_slack_message(_data(new_crashes=2), URL)
    body = json.dumps(msg.payload, ensure_ascii=False)
    assert msg.folds == ()                              # 不再发 thread
    assert "查看完整日报" in body and URL in body
    assert "Crash-free sessions" in body
    assert "FPS" not in body                            # 非核心指标不进主消息
    assert "不适用" not in body                          # 不适用的格子不列
    assert "新增崩溃 2 个" in body


def test_slack_without_url_keeps_old_layout():
    msg = assemble_slack_message(_data(new_crashes=1))
    body = json.dumps(msg.payload, ensure_ascii=False)
    assert "全量指标" in body and "查看完整日报" not in body
    assert [f.title for f in msg.folds] == ["🆕 新增崩溃堆栈", "🔥 Top 崩溃 / 卡顿"]


# ---------------------------------------------------------------------------
# 发送路径：取数一次 + 落库
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_send_collects_once_saves_and_sends_both_legs():
    settings = SimpleNamespace(notify_provider="both", feishu_chat_id="oc_gray",
                               slack_channel="C_gray", alert_email="ops@plaud.ai")
    collect = AsyncMock(return_value=_data())
    save = AsyncMock()
    with patch.object(gnotify, "_settings", return_value=settings), \
         patch("app.graygate.services.card_builder.collect_report_data", collect), \
         patch("app.graygate.services.report_store.save_report", save), \
         patch("app.graygate.services.slack_report.report_url_for", return_value=URL), \
         patch("app.services.feishu_cli.send_interactive_card", AsyncMock(return_value=True)) as feishu, \
         patch("app.services.slack_cli.post_message", AsyncMock(return_value="1790.1")) as post:
        ok = await gnotify.send_daily_report(date(2026, 8, 18))
    assert ok is True
    collect.assert_awaited_once()                       # 两条腿共用一份数据
    save.assert_awaited_once()
    feishu.assert_awaited_once()
    assert post.await_count == 1                        # 精简版没有 thread 回复
    assert URL in json.dumps(post.await_args.kwargs or post.await_args.args, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 缓存读写
# ---------------------------------------------------------------------------
@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod
    import app.graygate.models  # noqa: F401

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)
    original = db_mod._session_factory
    db_mod._session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    yield
    db_mod._session_factory = original


@pytest.mark.asyncio
async def test_save_then_read_from_cache_without_recompute(patched_session):
    d = _data(target=date.today() - timedelta(days=1), new_crashes=1)
    await report_store.save_report(d)
    await report_store.save_report(d)                   # upsert，不重复插行
    items = await report_store.list_reports()
    assert len(items) == 1 and items[0]["is_red"] is True and items[0]["new_crash_count"] == 1

    collect = AsyncMock()
    with patch.object(report_store, "collect_report_data", collect):
        r = await report_store.ensure_report_markdown(d.target_date)
    collect.assert_not_awaited()
    assert r["cached"] is True and "| 指标 |" in r["markdown"]


@pytest.mark.asyncio
async def test_missing_cache_recomputes_and_backfills(patched_session):
    day = date.today() - timedelta(days=2)
    with patch.object(report_store, "collect_report_data", AsyncMock(return_value=_data(target=day))):
        r = await report_store.ensure_report_markdown(day)
    assert r["cached"] is False and "| 指标 |" in r["markdown"]
    assert (await report_store.get_report(day))["markdown"] == r["markdown"]


@pytest.mark.asyncio
async def test_expired_date_is_not_recomputed_and_old_cache_is_pruned(patched_session):
    old = date.today() - timedelta(days=report_store.REPORT_MARKDOWN_RETENTION_DAYS + 5)
    await report_store.save_report(_data(target=old))
    await report_store.save_report(_data(target=date.today() - timedelta(days=1)))  # 触发清理
    assert (await report_store.get_report(old))["markdown"] == ""      # 行保留、markdown 清空

    collect = AsyncMock()
    with patch.object(report_store, "collect_report_data", collect):
        r = await report_store.ensure_report_markdown(old)
    collect.assert_not_awaited()
    assert "已过期" in r["markdown"]


@pytest.mark.asyncio
async def test_save_failure_does_not_raise():
    with patch("app.db.database.get_session", side_effect=RuntimeError("db down")):
        await report_store.save_report(_data())        # 不抛
