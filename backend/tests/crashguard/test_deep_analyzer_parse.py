"""Unit tests for deep_analyzer Phase 1 parsing utilities."""
from __future__ import annotations
import json
import pytest


def test_diagnosis_json_schema():
    """diagnosis.json 输出结构必须包含 hypotheses + data_gaps + crash_type."""
    raw = {
        "crash_type": "anr",
        "investigation_log": ["读了 foo.dart"],
        "hypotheses": [
            {
                "id": "h1",
                "title": "主线程 IO 阻塞",
                "evidence": ["堆栈第3帧"],
                "confidence": 0.85,
                "fix_direction": "移到 isolate",
                "code_pointers": ["lib/foo.dart:42"],
                "can_fix_now": True,
                "complexity": "simple",
            }
        ],
        "data_gaps": [],
        "overall_confidence": 0.85,
        "recommended_hypothesis": "h1",
        "auto_proceed_to_fix": False,
    }
    for key in ("crash_type", "hypotheses", "data_gaps", "recommended_hypothesis",
                "auto_proceed_to_fix", "overall_confidence"):
        assert key in raw

    hyp = raw["hypotheses"][0]
    for key in ("id", "title", "evidence", "confidence", "fix_direction",
                "can_fix_now", "complexity"):
        assert key in hyp


def test_auto_proceed_conditions():
    """auto_proceed_to_fix=True 当且仅当单假设 confidence>=0.9 + can_fix_now + no data_gaps."""
    from app.crashguard.services.deep_analyzer import _should_auto_proceed

    assert _should_auto_proceed(
        hypotheses=[{"id": "h1", "confidence": 0.92, "can_fix_now": True}],
        data_gaps=[],
        threshold=0.9,
    ) is True

    assert _should_auto_proceed(
        hypotheses=[
            {"id": "h1", "confidence": 0.95, "can_fix_now": True},
            {"id": "h2", "confidence": 0.80, "can_fix_now": True},
        ],
        data_gaps=[],
        threshold=0.9,
    ) is False

    assert _should_auto_proceed(
        hypotheses=[{"id": "h1", "confidence": 0.85, "can_fix_now": True}],
        data_gaps=[],
        threshold=0.9,
    ) is False

    assert _should_auto_proceed(
        hypotheses=[{"id": "h1", "confidence": 0.95, "can_fix_now": True}],
        data_gaps=[{"description": "缺数据"}],
        threshold=0.9,
    ) is False
