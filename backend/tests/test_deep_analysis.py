"""深度分析：deep_analysis 标志贯穿 + 跳窗 + 结果 tag。"""
from app.models.schemas import TaskCreate


def test_taskcreate_has_deep_analysis_default_false():
    tc = TaskCreate(issue_id="fb_x")
    assert tc.deep_analysis is False
    tc2 = TaskCreate(issue_id="fb_x", deep_analysis=True)
    assert tc2.deep_analysis is True
