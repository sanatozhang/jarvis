"""coreguard 的 Slack 出口必须**全英文**（2026-10-09），飞书出口保持中文不变。

守卫方式：把 Slack 渲染产物整体 json.dumps 后用 CJK 正则扫一遍——比逐个断言
字面量更能防"以后新加一行中文漏进 Slack"。数据里故意用中文 `title`，确保
`title_en` 这条链路真的被走到（而不是碰巧数据本身就是英文）。
"""
from __future__ import annotations

import json
import re
from datetime import date as _date_t, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from app.coreguard.services import daily_section as ds
from app.coreguard.services.dashboard_loader import MetricConfig, english_title
from app.coreguard.services.feishu_card_demo import build_demo_alert_card
from app.coreguard.services.feishu_summary_card import build_summary_card
from app.coreguard.services.slack_summary import build_summary_message

CJK = re.compile(r"[　-〿一-鿿＀-￯]")

_CUR_START = datetime(2026, 9, 22, 6, 0, 0)
_CUR_END = datetime(2026, 9, 22, 7, 0, 0)
_BASE_START = datetime(2026, 9, 15, 6, 0, 0)
_BASE_END = datetime(2026, 9, 15, 7, 0, 0)


def _dump(msg) -> str:
    return json.dumps(
        [msg.payload, [f.blocks for f in msg.folds], [f.title for f in msg.folds], msg.text,
         [f.text for f in msg.folds]],
        ensure_ascii=False,
    )


def _band(title, title_en, key, tier="P0", change=-7.0):
    return dict(
        key=key, title=title, title_en=title_en, tier=tier, value_type="percent_pp",
        current_value=92.1, baseline_value=97.3, change=change, direction="down_is_bad",
        threshold={"pp": 4.0}, error=None, datadog_widget_id=123,
        baseline_mode="band", band_lower=95.0, band_upper=99.0, baseline_n=14,
    )


def _absolute(title, title_en, key, tier="P1", change=0.35):
    return dict(
        key=key, title=title, title_en=title_en, tier=tier, value_type="latency_pct",
        current_value=1.8, baseline_value=1.3, change=change, direction="up_is_bad",
        threshold={"pct": 0.2}, error=None, datadog_widget_id=None,
        baseline_mode="show", band_lower=None, band_upper=None, baseline_n=None,
    )


def _args(breached=(), errored=(), forced=False):
    return dict(
        cur_start=_CUR_START, cur_end=_CUR_END,
        base_start=_BASE_START, base_end=_BASE_END,
        breached=list(breached),
        healthy=[_band("音频播放成功率", "Audio playback success rate", "audio_playback_success_rate")],
        errored=list(errored), forced=forced,
        dashboard_id="4h8-qff-zra", datadog_site="datadoghq.com",
    )


_BREACHED = [
    _band("云同步上传成功率V2", "Cloud sync upload success rate V2", "cloud_upload_success_rate_v2"),
    _band("音频导入成功率", "Audio import success rate", "audio_import_success_rate",
          tier="P1", change=-4.5),
    _band("设备绑定成功率", "Device bind success rate", "device_bind_success_rate", change=-3.2),
    _absolute("首页文件列表加载P75时长", "Home file list load time P75", "home_file_list_load_p75"),
    _absolute("分享导出成功率", "Share/export success rate", "share_export_success_rate",
              change=None),
]
# 缺数据：没有 title_en（老数据 / yaml 漏配）→ 必须退回 key，不能漏中文
_ERRORED = [
    dict(_band(f"中文指标{i}", "", f"metric_key_{i}"), error="timeout", current_value=None)
    for i in range(7)
]


# ---------------------------------------------------------------------------
# 告警摘要（metric_watcher → notify.send_summary → slack_summary）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("breached,errored,forced", [
    (_BREACHED, _ERRORED, False),
    ([], [], True),
    ([], [], False),
    ([], _ERRORED[:2], False),
])
def test_slack_summary_has_no_cjk(breached, errored, forced):
    msg = build_summary_message(**_args(breached=breached, errored=errored, forced=forced))
    body = _dump(msg)
    assert not CJK.search(body), CJK.findall(body)


def test_slack_summary_uses_title_en_and_key_fallback():
    body = _dump(build_summary_message(**_args(breached=_BREACHED, errored=_ERRORED)))
    assert "Cloud sync upload success rate V2" in body
    assert "Home file list load time P75" in body
    assert "metric_key_0" in body                  # 缺 title_en → key
    assert "and 2 more" in body                    # 7 项缺数据，展示 5 项


def test_slack_summary_overflow_thread_has_no_cjk():
    many = [_band(f"指标{i}", f"Metric {i}", f"m{i}", change=-float(i + 4)) for i in range(40)]
    msg = build_summary_message(**_args(breached=many))
    assert msg.folds
    body = _dump(msg)
    assert not CJK.search(body), CJK.findall(body)


def test_feishu_summary_card_stays_chinese():
    """飞书出口不受影响：仍用中文 `title`、中文字面量。"""
    card = json.dumps(build_summary_card(**_args(breached=_BREACHED, errored=_ERRORED)),
                      ensure_ascii=False)
    assert "核心指标异常告警" in card
    assert "云同步上传成功率V2" in card
    assert "Cloud sync upload success rate V2" not in card
    assert "偏离预测带" in card and "缺数据" in card


async def test_render_summary_on_slack_is_english():
    from app.coreguard.services import notify

    with patch.object(notify, "_settings", return_value=SimpleNamespace(notify_provider="slack")):
        msg = notify.render_summary(**_args(breached=_BREACHED))
    assert not CJK.search(_dump(msg))


# ---------------------------------------------------------------------------
# title_en：yaml → loader → MetricResult dict
# ---------------------------------------------------------------------------
def _yaml_metrics():
    p = Path(__file__).resolve().parents[2] / "app" / "coreguard" / "metrics.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))["metrics"]


def test_every_metric_has_ascii_title_en():
    metrics = _yaml_metrics()
    assert metrics
    bad = [m["key"] for m in metrics
           if not (m.get("title_en") or "").strip() or not m["title_en"].isascii()]
    assert bad == []


def test_english_title_titles_already_english_are_unchanged():
    for m in _yaml_metrics():
        if m["title"].isascii():
            assert m["title_en"] == m["title"], m["key"]


async def test_loader_reads_title_en():
    from app.coreguard.services import dashboard_loader as dl

    with patch.object(dl, "_fetch_dashboard", AsyncMock(return_value=None)):
        cfg = await dl.load_metrics_config()
    m = cfg.by_key("cloud_upload_success_rate_v2")
    assert m.title == "云同步上传成功率V2"           # 原 title 不动（widget title 校验认它）
    assert m.title_en == "Cloud sync upload success rate V2"


async def test_metric_result_dict_carries_title_en():
    from app.coreguard.services.metric_watcher import evaluate_one, results_to_dict

    cfg = MetricConfig(key="k", title="中文", title_en="English", widget_id=0,
                       widget_type="query_value", tier="P0", value_type="percent_pp",
                       direction="down_is_bad")
    r = await evaluate_one(cfg, _CUR_START, _CUR_END, settings=SimpleNamespace(band_enabled=True))
    assert results_to_dict([r])[0]["title_en"] == "English"


def test_english_title_fallbacks():
    assert english_title("Eng", "中文", "k") == "Eng"
    assert english_title("", "Ascii", "k") == "Ascii"
    assert english_title("", "中文", "k") == "k"
    assert english_title(None, None, None) == "metric"


# ---------------------------------------------------------------------------
# demo（coreguard API 手动触发 → notify.send_simple_card）
# ---------------------------------------------------------------------------
def _demo_kwargs(forced=True):
    return dict(
        current_value=99.1, baseline_value=99.8, change_pp=-0.7, threshold_pp=0.5,
        sessions_count=1234,
        current_window_label="2026-09-22 06:00 ~ 07:00 UTC",
        baseline_window_label="2026-09-15 06:00 ~ 07:00 UTC",
        dashboard_url="https://app.datadoghq.com/dashboard/x", forced=forced,
    )


@pytest.mark.parametrize("forced", [True, False])
async def test_demo_slack_blocks_are_english(forced):
    from app.coreguard.services import demo_runner, notify

    zh = build_demo_alert_card(metric_title="Crash-free sessions", **_demo_kwargs(forced))
    en = build_demo_alert_card(metric_title="Crash-free sessions", lang="en", **_demo_kwargs(forced))
    send = AsyncMock(return_value=True)
    with patch.object(notify, "send_simple_card", send):
        assert await demo_runner._send_feishu(zh, en) is True

    feishu_card = send.await_args.args[0]
    assert feishu_card is zh                                  # 飞书仍发中文卡
    if forced:
        assert "强制演示" in feishu_card["header"]["title"]["content"]
    slack = json.dumps([send.await_args.kwargs["slack_blocks"], send.await_args.kwargs["text"]],
                       ensure_ascii=False)
    assert not CJK.search(slack), CJK.findall(slack)
    assert "Current window" in slack and "Open Datadog dashboard" in slack


# ---------------------------------------------------------------------------
# 早报 coreguard 板块 → crashguard Slack 早报读 headline_hint_en
# ---------------------------------------------------------------------------
def _agg(title, title_en, key, tier, consecutive, breaches=3):
    return ds._AggMetric(
        key=key, title=title, title_en=title_en, tier=tier, value_type="percent_pp",
        direction="down_is_bad", threshold={"pp": 1.0},
        breach_windows=breaches, total_windows=24, longest_consecutive=consecutive,
        worst_change=-2.5,
    )


@pytest.mark.parametrize("persistent,transient", [
    ([_agg("云同步上传成功率V2", "Cloud sync upload success rate V2", "c", "P0", 3)], []),
    ([_agg("音频导入成功率", "", "audio_import_success_rate", "P1", 2)], []),
    ([], [_agg("音频导入成功率", "Audio import success rate", "a", "P1", 1)]),
])
def test_headline_hint_en_has_no_cjk(persistent, transient):
    zh = ds._build_headline_hint(persistent, transient)
    en = ds._build_headline_hint(persistent, transient, lang="en")
    assert zh and CJK.search(zh)                              # 飞书版不变（中文）
    assert en and not CJK.search(en), en


def test_headline_hint_en_shapes():
    p0 = [_agg("云同步上传成功率V2", "Cloud sync upload success rate V2", "c", "P0", 3)]
    assert ds._build_headline_hint(p0, [], lang="en") == (
        "Business core metrics: 1 P0 persistent anomaly: "
        "**Cloud sync upload success rate V2** Δ `-2.50 pp` ≥3h"
    )
    assert ds._build_headline_hint([], [], lang="en") is None


async def test_build_morning_section_exposes_headline_hint_en():
    m = _agg("云同步上传成功率V2", "Cloud sync upload success rate V2", "c", "P0", 3)
    with patch.object(ds, "_aggregate_snapshots", AsyncMock(side_effect=[([m], 24), ([], 0)])), \
         patch.object(ds, "_check_day_level", AsyncMock(return_value=[])), \
         patch.object(ds, "get_coreguard_settings",
                      return_value=SimpleNamespace(dashboard_id="d", datadog_site="datadoghq.com")):
        sec = await ds.build_morning_section(_date_t(2026, 9, 22))
    assert sec["headline_hint"] and CJK.search(sec["headline_hint"])
    assert "Cloud sync upload success rate V2" in sec["headline_hint_en"]
    assert not CJK.search(sec["headline_hint_en"])


async def test_build_morning_section_unavailable_has_headline_hint_en_key():
    with patch.object(ds, "_aggregate_snapshots", AsyncMock(return_value=([], 0))):
        sec = await ds.build_morning_section(_date_t(2026, 9, 22))
    assert sec["available"] is False and sec["headline_hint_en"] is None
