"""追问重裁日志：深度→窗口参数调度 + 历史追问链查询。

追问 = 上次回复不满意（日志很可能裁错/不全），故不复用上次裁好的日志，按追问深度
递进放宽时间窗重裁，到阈值给全量原始日志。
"""
from datetime import datetime

from app.workers.analysis_worker import (
    FOLLOWUP_FULL_LOGS_AT_DEPTH,
    FOLLOWUP_WIDEN_FACTOR,
    _followup_window_params,
)
from tests.conftest import seed_issue, seed_task


def test_window_params_non_followup():
    # depth 0 = 非追问：不放大、不全量
    assert _followup_window_params(0) == (1.0, False)


def test_window_params_progressive_widen():
    # 首次追问 ×2，第二次 ×4（FOLLOWUP_WIDEN_FACTOR=2）
    assert _followup_window_params(1) == (float(FOLLOWUP_WIDEN_FACTOR), False)
    assert _followup_window_params(2) == (float(FOLLOWUP_WIDEN_FACTOR ** 2), False)


def test_window_params_full_logs_at_threshold():
    # 深度到阈值 → 全量原始日志；阈值前一档仍是放宽窗口
    assert _followup_window_params(FOLLOWUP_FULL_LOGS_AT_DEPTH)[1] is True
    assert _followup_window_params(FOLLOWUP_FULL_LOGS_AT_DEPTH - 1)[1] is False
    assert _followup_window_params(FOLLOWUP_FULL_LOGS_AT_DEPTH + 5)[1] is True


async def test_prior_followup_history_counts_and_orders(db_session):
    from app.db.database import get_prior_followup_history

    await seed_issue(db_session, "iss_fu")
    # 原始分析（无追问）+ 两次历史追问，时间递增
    await seed_task(db_session, "t0", "iss_fu", followup_question="", created_at=datetime(2026, 6, 1, 8, 0))
    await seed_task(db_session, "t1", "iss_fu", followup_question="是蓝牙问题吗", created_at=datetime(2026, 6, 1, 9, 0))
    await seed_task(db_session, "t2", "iss_fu", followup_question="看更早的日志", created_at=datetime(2026, 6, 1, 10, 0))
    # 当前（第三次追问）task 也已入库，应被 exclude，不计入 depth
    await seed_task(db_session, "t3", "iss_fu", followup_question="还是不对", created_at=datetime(2026, 6, 1, 11, 0))

    depth, questions = await get_prior_followup_history("iss_fu", exclude_task_id="t3")
    assert depth == 3                                    # t0, t1, t2（不含当前 t3）
    assert questions == ["是蓝牙问题吗", "看更早的日志"]   # 去掉空追问的 t0，按时间正序


async def test_prior_followup_history_empty_for_first_analysis(db_session):
    from app.db.database import get_prior_followup_history

    await seed_issue(db_session, "iss_first")
    await seed_task(db_session, "only", "iss_first", followup_question="", created_at=datetime(2026, 6, 2, 8, 0))
    # 首次分析（当前 task 即 only）：无历史
    depth, questions = await get_prior_followup_history("iss_first", exclude_task_id="only")
    assert depth == 0
    assert questions == []
