"""POST /api/crash/symbolicate/inspect 单测（2026-09-22）。

仿 test_symbolicate_api.py 的直调 handler 风格（不起 TestClient），保持与
既有 symbolicate 测试一致。

核心护栏：
1. Android Java 混淆栈必须报「无版本号」且 routing=None，但**不报错**。
2. 沿用 test_symbolicate_api.py 那条回归护栏：resolve 必须被传入 path_exists
   覆盖参数 —— 防止有人日后"修回"默认的 os.path.exists 校验（那会导致
   iOS native 版本套错 dSYM 资产名，详见 docs/crashguard/symbolication.md）。
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException


ANDROID_JAVA = (
    "java.lang.NullPointerException\n"
    "\tat a.b.c.e(Unknown Source:12)\n"
)

APPLE_CRASH_TEXT = """Incident Identifier: ABCD1234-5678-90AB-CDEF-1234567890AB
Process:             PLAUD [1234]
Version:             4.0.201 (941)

Thread 0 Crashed:
0   PLAUD                         0x0000000103a2c000 0x103a00000 + 180224

Binary Images:
0x103a00000 - 0x1046effff PLAUD arm64e <a1b2c3d4e5f67890abcdef1234567890> /var/PLAUD
"""


def _req(stack: str):
    from app.crashguard.api.crash import StackInspectRequest

    return StackInspectRequest(stack=stack)


async def _call(stack: str):
    from app.crashguard.api.crash import inspect_stack_endpoint

    return await inspect_stack_endpoint(_req(stack))


@pytest.mark.asyncio
async def test_inspect_rejects_empty_stack():
    with pytest.raises(HTTPException) as ei:
        await _call("")
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_inspect_rejects_whitespace_only_stack():
    with pytest.raises(HTTPException) as ei:
        await _call("   \n\t  ")
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_inspect_rejects_oversized_stack():
    with pytest.raises(HTTPException) as ei:
        await _call("x" * 200_001)
    assert ei.value.status_code == 413


@pytest.mark.asyncio
async def test_inspect_android_java_reports_no_version():
    d = await _call(ANDROID_JAVA)
    assert d["platform"] == "android"
    assert d["app_version"] == ""
    assert d["stack_format"] == "android_java"
    # 无版本号 → 不做路由预览，但不报错
    assert d["routing"] is None
    assert any("必须手动指定版本号" in n for n in d["notes"])


@pytest.mark.asyncio
async def test_inspect_apple_crash_returns_routing_preview():
    d = await _call(APPLE_CRASH_TEXT)
    assert d["platform"] == "ios"
    assert d["app_version"] == "4.0.201-941"
    # 4.0.201 >= 4.0.0 → native band（见 config.yaml repo_routing）
    assert d["routing"]["symbol_profile"] == "native_ios"
    assert d["routing"]["github_repo"] == "Plaud-AI/plaud-native-app"
    assert d["routing"]["family"] == "native"


@pytest.mark.asyncio
async def test_inspect_passes_path_exists_override(monkeypatch):
    """回归护栏（与 test_symbolicate_api.py 同款）：resolve 必须收到
    path_exists 覆盖，且对不存在的路径仍能解析出 native_ios。

    resolve() 默认用 os.path.exists 校验源码 wrapper 目录，但 config.yaml 里配的
    是裸机路径，容器内不存在会返回 None → symbol_profile 丢失 → iOS 下错误的
    dSYM 资产。
    """
    from app.services import repo_router

    captured: dict = {}
    real_resolve = repo_router.resolve

    def spy_resolve(platform, version, routing, **kw):
        captured["path_exists"] = kw.get("path_exists")
        return real_resolve(platform, version, routing, **kw)

    monkeypatch.setattr(repo_router, "resolve", spy_resolve)
    d = await _call(APPLE_CRASH_TEXT)

    assert captured["path_exists"] is not None
    # 对一个真实不存在的路径调用也必须返回 True
    assert captured["path_exists"]("/definitely/not/a/real/path/xyz") is True
    assert d["routing"]["symbol_profile"] == "native_ios"


@pytest.mark.asyncio
async def test_inspect_returns_all_insight_fields():
    """响应必须带齐 StackInsight 的全部字段，前端依赖它们做预填。"""
    d = await _call(APPLE_CRASH_TEXT)
    for k in (
        "stack_format", "platform", "app_version", "uuids", "build_ids",
        "binary_images", "normalized_stack", "frame_count", "confidence",
        "notes", "routing",
    ):
        assert k in d, f"缺字段 {k}"


@pytest.mark.asyncio
async def test_inspect_unknown_format_does_not_raise():
    d = await _call("完全不是堆栈的一段中文文本")
    assert d["stack_format"] == "unknown"
    assert d["routing"] is None
