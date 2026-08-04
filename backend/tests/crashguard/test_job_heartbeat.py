"""单测：HeartbeatCtx 三态映射（set_status_from_partial）

抓手：pr_sync 12 个 PR 里 1 个失败不应整 job 标 failed → 误告警。
"""
from app.crashguard.services.job_heartbeat import _HeartbeatCtx


def test_partial_all_success():
    ctx = _HeartbeatCtx("test")
    ctx.set_status_from_partial(success_count=10, total_count=10)
    assert ctx.status == "success"
    assert ctx.error == ""


def test_partial_zero_total():
    """空 tick（无可做之事）不视为异常"""
    ctx = _HeartbeatCtx("test")
    ctx.set_status_from_partial(success_count=0, total_count=0)
    assert ctx.status == "success"


def test_partial_some_failed_is_degraded():
    """1/12 失败 → degraded，不立刻告警"""
    ctx = _HeartbeatCtx("test")
    ctx.set_status_from_partial(success_count=11, total_count=12)
    assert ctx.status == "degraded"
    assert "1/12" in ctx.error


def test_partial_majority_failed_is_degraded():
    """9/12 失败仍是 degraded（系统部分能用），不是 failed"""
    ctx = _HeartbeatCtx("test")
    ctx.set_status_from_partial(success_count=3, total_count=12)
    assert ctx.status == "degraded"
    assert "9/12" in ctx.error


def test_partial_all_failed_is_failed():
    """0/12 成功 = systemic 故障"""
    ctx = _HeartbeatCtx("test")
    ctx.set_status_from_partial(success_count=0, total_count=12)
    assert ctx.status == "failed"
    assert "all 12 items failed" in ctx.error


def test_partial_error_hint_used():
    """显式 error_hint 优先于自动生成"""
    ctx = _HeartbeatCtx("test")
    ctx.set_status_from_partial(
        success_count=5, total_count=12, error_hint="GraphQL FORBIDDEN x7"
    )
    assert ctx.status == "degraded"
    assert ctx.error == "GraphQL FORBIDDEN x7"


def test_partial_error_hint_on_all_failed():
    ctx = _HeartbeatCtx("test")
    ctx.set_status_from_partial(
        success_count=0, total_count=12, error_hint="all timeouts"
    )
    assert ctx.status == "failed"
    assert ctx.error == "all timeouts"


def test_set_summary_keeps_nested_dict():
    """job_health_alert 的 res["fatal_backlog"] 是个嵌套 dict——此前 set_summary
    只保留 (str, int, float, bool, list, None)，dict 被静默过滤掉，导致检查
    本身跑得正常但心跳 JSON 里完全看不到这个字段。"""
    ctx = _HeartbeatCtx("test")
    ctx.set_summary({
        "alerted": False,
        "fatal_backlog": {"count": 3, "threshold": 10, "alerted": False},
    })
    assert ctx.summary["alerted"] is False
    assert ctx.summary["fatal_backlog"] == {"count": 3, "threshold": 10, "alerted": False}


def test_set_summary_truncates_oversized_nested_dict():
    """嵌套 dict 过大时截断成字符串标记，不整体丢弃，也不让 row 被撑爆。"""
    ctx = _HeartbeatCtx("test")
    huge = {"sample_issues": ["x" * 100 for _ in range(50)]}
    ctx.set_summary({"fatal_backlog": huge})
    result = ctx.summary["fatal_backlog"]
    assert result["_truncated"] is True
    assert len(result["raw"]) <= 2000


def test_set_summary_drops_unsupported_nested_type():
    """既不是 JSON 基础类型也不是 dict 的字段（如自定义对象）不能让整个
    set_summary 抛异常，退化成类型说明字符串。"""
    class Weird:
        pass

    ctx = _HeartbeatCtx("test")
    ctx.set_summary({"ok": True, "weird": Weird()})
    assert ctx.summary["ok"] is True
    assert "Weird" in ctx.summary["weird"]
