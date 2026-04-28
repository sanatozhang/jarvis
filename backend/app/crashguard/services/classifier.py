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
