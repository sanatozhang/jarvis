"""app.platforms.normalize_platform 边界测试"""
from __future__ import annotations

import pytest

from app.platforms import PLATFORMS, normalize_platform


def test_platforms_whitelist():
    assert PLATFORMS == ["app", "web", "desktop", "mcp"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", "app"),
        (None, "app"),
        ("   ", "app"),
        ("app", "app"),
        ("APP", "app"),
        ("App", "app"),
        ("web", "web"),
        ("Web", "web"),
        ("WEB", "web"),
        ("desktop", "desktop"),
        ("Desktop", "desktop"),
        ("mcp", "mcp"),
        ("MCP", "mcp"),
        ("Mcp", "mcp"),
        ("xyz", "app"),
        ("android", "app"),  # 不在白名单里的历史值，兜底到 app
        ("  web  ", "web"),  # 首尾空白容错
    ],
)
def test_normalize_platform(raw, expected):
    assert normalize_platform(raw) == expected


def test_normalize_platform_always_returns_valid_platform():
    """无论输入什么，返回值必须落在 PLATFORMS 白名单内。"""
    for raw in ["", None, "APP", "unknown", "  ", "Web", "desktop", "MCP", "xyz", "123"]:
        assert normalize_platform(raw) in PLATFORMS
