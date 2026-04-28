"""
三维新增分类器：
- is_new_in_version  : 该 issue 的首发版本就是当前最新发布版（"全新"）
- is_regression      : fingerprint 在最近 N 个版本静默后又出现（"回归"）
- is_surge           : 当日事件数环比飙升（"飙升"）
"""
from __future__ import annotations

from typing import List


def is_new_in_version(first_seen_version: str, latest_release: str) -> bool:
    """全新崩溃: 首次出现的版本 == 当前线上最新版"""
    if not first_seen_version or not latest_release:
        return False
    return first_seen_version.strip() == latest_release.strip()


def is_regression(
    fingerprint_seen_versions: List[str],
    recent_versions: List[str],
    current_version: str,
    silent_threshold: int = 3,
) -> bool:
    """
    回归崩溃判定:
    - fingerprint 历史上出现过（fingerprint_seen_versions 非空）
    - 但在最近 silent_threshold 个版本里**完全静默**（recent_versions 与 seen 不相交）
    - 当前版本（current_version）又出现了（这里调用方保证 current_version 命中）
    """
    if not fingerprint_seen_versions:
        return False  # 全新 fingerprint，不算 regression

    if len(recent_versions) < silent_threshold:
        return False  # 历史窗口不足，无法判定

    seen_set = set(fingerprint_seen_versions)
    recent_set = set(recent_versions)

    # 历史出现过 + 最近窗口完全静默 = regression
    if seen_set & recent_set:
        return False  # 最近还出现过，不算静默
    return True


def is_surge(
    today_events: int,
    prev_avg_events: float,
    multiplier: float = 1.5,
    min_events: int = 10,
) -> bool:
    """
    飙升判定:
    - today_events > prev_avg_events * multiplier
    - 且 today_events >= min_events（防小基数刷量）
    - prev_avg_events == 0 时，只要 today_events >= min_events 就算
    """
    if today_events < min_events:
        return False
    if prev_avg_events <= 0:
        return True
    return today_events > prev_avg_events * multiplier
