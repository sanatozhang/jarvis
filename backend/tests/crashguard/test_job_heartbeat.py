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
