"""
版本分类器：把 issue 的 version 字段映射到三个桶之一。

底层逻辑：复用 version_util.parse_semver 做 semver 比较，忽略 -build 号。
"""
from __future__ import annotations
from typing import Literal

from app.crashguard.services.version_util import parse_semver


VersionBucket = Literal["new", "main", "legacy"]


def classify_version(
    issue_version: str,
    platform: str,
    top_versions: dict,
) -> VersionBucket:
    """把 issue 按版本归到三桶之一。

    Args:
        issue_version: issue 的 last_seen_version / app_version
        platform: 平台 key（必须在 top_versions 里有同名 key）
        top_versions: {platform: {"version": str, "users": int}}

    Returns:
        "new" — issue_version semver > top_version
        "main" — issue_version semver == top_version（忽略 build 号）
        "legacy" — issue_version < top_version，或解析失败/数据缺失
    """
    if not issue_version or not platform:
        return "legacy"

    platform_data = top_versions.get(platform)
    if not platform_data:
        return "legacy"

    top_ver = platform_data.get("version") or ""
    if not top_ver:
        return "legacy"

    issue_parsed = parse_semver(issue_version)
    top_parsed = parse_semver(top_ver)
    if issue_parsed is None or top_parsed is None:
        return "legacy"

    # 只比较 (major, minor, patch)，忽略 suffix（build 号）
    issue_key = issue_parsed[:3]
    top_key = top_parsed[:3]

    if issue_key > top_key:
        return "new"
    elif issue_key == top_key:
        return "main"
    return "legacy"
