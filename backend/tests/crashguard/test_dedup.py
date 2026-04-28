"""stack_fingerprint 算法测试"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def stacks() -> dict:
    return json.loads((FIXTURES / "stack_traces.json").read_text())


def test_normalize_strips_line_numbers(stacks):
    """归一化剥离行号"""
    from app.crashguard.services.dedup import normalize_stack_frames

    frames = normalize_stack_frames(stacks["flutter_v1"], top_n=5)
    # 不应包含 :42, :18 这类行号
    for f in frames:
        assert ":" not in f or f.endswith(".dart")  # 行号被剥离
        assert not any(c.isdigit() and i > 0 and f[i - 1] == ":" for i, c in enumerate(f))


def test_normalize_strips_anonymous_closures(stacks):
    """归一化剥离 <anonymous> / _$xxxxx_closure"""
    from app.crashguard.services.dedup import normalize_stack_frames

    frames = normalize_stack_frames(stacks["flutter_v1"], top_n=5)
    for f in frames:
        assert "_$xxxxx" not in f
        assert "<anonymous>" not in f
        assert "closure" not in f.lower() or "_$" not in f


def test_same_bug_same_fingerprint_across_versions(stacks):
    """同一 bug 不同版本（行号变了）→ 同一 fingerprint"""
    from app.crashguard.services.dedup import compute_fingerprint

    fp1 = compute_fingerprint(stacks["flutter_v1"])
    fp2 = compute_fingerprint(stacks["flutter_v2_same_bug"])
    assert fp1 == fp2


def test_different_bugs_different_fingerprint(stacks):
    """不同 bug → 不同 fingerprint"""
    from app.crashguard.services.dedup import compute_fingerprint

    fp1 = compute_fingerprint(stacks["flutter_v1"])
    fp_other = compute_fingerprint(stacks["different_bug"])
    assert fp1 != fp_other


def test_empty_stack_returns_stable_fingerprint():
    """空字符串/异常输入不应崩溃"""
    from app.crashguard.services.dedup import compute_fingerprint

    fp = compute_fingerprint("")
    assert isinstance(fp, str)
    assert len(fp) == 40  # SHA1


def test_ios_stack_strips_libsystem(stacks):
    """iOS 栈归一化剥离 libsystem 噪音"""
    from app.crashguard.services.dedup import normalize_stack_frames

    frames = normalize_stack_frames(stacks["ios_native"], top_n=5)
    assert all("libsystem" not in f.lower() for f in frames)
