"""POST /api/crash/symbolicate 单测（2026-08-03）：手工符号化任意堆栈文本。

仿 test_symbol_upload_api.py 的直调 handler 风格（不起 TestClient）。

回归护栏（最重要）：断言 repo_router.resolve 调用时传入了 path_exists 覆盖参数，
且对"不存在的路径"仍能解析出 native_ios —— 防止有人日后把这行"修回"默认的
os.path.exists 校验（那样会导致 iOS native 版本套错 dSYM 资产名，详见
docs/crashguard/symbolication.md）。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)
    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


def _make_fake_resolve(captured: dict, *, return_none: bool = False):
    """构造一个假 repo_router.resolve：捕获调用参数，验证 path_exists 覆盖生效。"""
    from app.services import repo_router

    def fake_resolve(platform, version, routing, *, sub_hint="", stack_text="", os_name="", path_exists=None):
        captured["platform"] = platform
        captured["version"] = version
        captured["path_exists"] = path_exists
        if return_none:
            return None
        # 核心断言：调用方必须传入了 path_exists 覆盖（非默认 os.path.exists），
        # 且对着一个真实不存在的路径调用也要返回 True —— 这正是任务简报里点名的
        # "path_exists=lambda _p: True" 效果。
        assert path_exists is not None
        assert path_exists("/definitely/does/not/exist/on/this/machine") is True
        return repo_router.RepoResolution(
            family="native",
            platform="ios",
            wrapper_path="/definitely/does/not/exist/on/this/machine",
            sub_repo_path="/definitely/does/not/exist/on/this/machine/plaud-native-ios",
            logical_name="plaud-native-ios",
            github_repo="Plaud-AI/plaud-native-app",
            symbol_profile="native_ios",
            confidence="high",
        )

    return fake_resolve


@pytest.mark.asyncio
async def test_rejects_invalid_platform(patched_session, monkeypatch):
    from app.crashguard.api.crash import SymbolicateRequest, symbolicate_ad_hoc_stack

    body = SymbolicateRequest(stack="some stack", platform="windows")
    with pytest.raises(HTTPException) as exc_info:
        await symbolicate_ad_hoc_stack(body)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_rejects_empty_stack(patched_session, monkeypatch):
    from app.crashguard.api.crash import SymbolicateRequest, symbolicate_ad_hoc_stack

    body = SymbolicateRequest(stack="", platform="ios")
    with pytest.raises(HTTPException) as exc_info:
        await symbolicate_ad_hoc_stack(body)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_rejects_oversized_stack(patched_session, monkeypatch):
    from app.crashguard.api.crash import SymbolicateRequest, symbolicate_ad_hoc_stack

    body = SymbolicateRequest(stack="x" * 200_001, platform="ios")
    with pytest.raises(HTTPException) as exc_info:
        await symbolicate_ad_hoc_stack(body)
    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_changed_true_when_stack_symbolicated(patched_session, monkeypatch):
    from app.crashguard.api.crash import SymbolicateRequest, symbolicate_ad_hoc_stack
    from app.crashguard.services import symbolication
    from app.services import repo_router

    captured: dict = {}
    monkeypatch.setattr(repo_router, "resolve", _make_fake_resolve(captured))

    symbolicated_fixed = "0  MyApp  MyApp.swift:42  MyFunction()"

    async def fake_symbolicate_stack(stack, binary_images, platform, app_version, *, symbol_profile="", github_repo=""):
        return symbolicated_fixed

    monkeypatch.setattr(symbolication, "symbolicate_stack", fake_symbolicate_stack)

    body = SymbolicateRequest(
        stack="0  MyApp  0x0000000100123456 0x100000000 + 123456",
        platform="ios",
        app_version="4.0.201-941",
    )
    result = await symbolicate_ad_hoc_stack(body)

    assert result["changed"] is True
    assert result["symbolicated_stack"] == symbolicated_fixed
    assert result["stack_quality_before"] == "raw"
    assert result["stack_quality_after"] == "symbolicated_native"
    assert result["symbol_profile"] == "native_ios"
    assert result["github_repo"] == "Plaud-AI/plaud-native-app"
    assert result["routing_confidence"] == "high"
    assert isinstance(result["duration_ms"], int)


@pytest.mark.asyncio
async def test_changed_false_and_warns_when_stack_unchanged(patched_session, monkeypatch):
    from app.crashguard.api.crash import SymbolicateRequest, symbolicate_ad_hoc_stack
    from app.crashguard.services import symbolication
    from app.services import repo_router

    captured: dict = {}
    monkeypatch.setattr(repo_router, "resolve", _make_fake_resolve(captured))

    async def fake_symbolicate_stack_noop(stack, binary_images, platform, app_version, *, symbol_profile="", github_repo=""):
        return stack  # 原样返回，模拟符号化失败/无匹配符号包

    monkeypatch.setattr(symbolication, "symbolicate_stack", fake_symbolicate_stack_noop)

    raw_stack = "0  MyApp  0x0000000100123456 0x100000000 + 123456"
    body = SymbolicateRequest(stack=raw_stack, platform="ios", app_version="4.0.201-941")
    result = await symbolicate_ad_hoc_stack(body)

    assert result["changed"] is False
    assert result["symbolicated_stack"] == raw_stack
    assert result["warnings"], "changed=False 时 warnings 必须非空"


@pytest.mark.asyncio
async def test_missing_app_version_produces_warning(patched_session, monkeypatch):
    from app.crashguard.api.crash import SymbolicateRequest, symbolicate_ad_hoc_stack
    from app.crashguard.services import symbolication
    from app.services import repo_router

    captured: dict = {}
    monkeypatch.setattr(repo_router, "resolve", _make_fake_resolve(captured))

    async def fake_symbolicate_stack_noop(stack, binary_images, platform, app_version, *, symbol_profile="", github_repo=""):
        return stack

    monkeypatch.setattr(symbolication, "symbolicate_stack", fake_symbolicate_stack_noop)

    body = SymbolicateRequest(stack="raw stack line", platform="android")
    result = await symbolicate_ad_hoc_stack(body)

    assert any("app_version" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_unresolved_routing_sets_confidence_and_warning(patched_session, monkeypatch):
    from app.crashguard.api.crash import SymbolicateRequest, symbolicate_ad_hoc_stack
    from app.crashguard.services import symbolication
    from app.services import repo_router

    captured: dict = {}
    monkeypatch.setattr(repo_router, "resolve", _make_fake_resolve(captured, return_none=True))

    async def fake_symbolicate_stack_noop(stack, binary_images, platform, app_version, *, symbol_profile="", github_repo=""):
        return stack

    monkeypatch.setattr(symbolication, "symbolicate_stack", fake_symbolicate_stack_noop)

    body = SymbolicateRequest(stack="raw stack line", platform="android", app_version="4.0.201-941")
    result = await symbolicate_ad_hoc_stack(body)

    assert result["routing_confidence"] == "unresolved"
    assert any("resolve" in w or "回退" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_regression_guard_resolve_called_with_path_exists_override(patched_session, monkeypatch):
    """回归护栏：确保端点调用 repo_router.resolve 时传入了 path_exists 覆盖参数，
    且即使覆盖的路径校验函数面对不存在的路径也返回 True 时，最终仍能解析出
    native_ios —— 防止有人把这行"修回"默认的 os.path.exists 校验。"""
    from app.crashguard.api.crash import SymbolicateRequest, symbolicate_ad_hoc_stack
    from app.crashguard.services import symbolication
    from app.services import repo_router

    captured: dict = {}
    monkeypatch.setattr(repo_router, "resolve", _make_fake_resolve(captured))

    async def fake_symbolicate_stack_noop(stack, binary_images, platform, app_version, *, symbol_profile="", github_repo=""):
        return stack

    monkeypatch.setattr(symbolication, "symbolicate_stack", fake_symbolicate_stack_noop)

    body = SymbolicateRequest(stack="raw stack line", platform="ios", app_version="4.0.201-941")
    result = await symbolicate_ad_hoc_stack(body)

    assert "path_exists" in captured
    assert captured["path_exists"] is not None
    # fake_resolve 内部已经断言过 path_exists(不存在的路径) is True 才会返回结果；
    # 这里再次从响应确认最终确实解析出了 native_ios（说明没有被默认校验拦下来）。
    assert result["symbol_profile"] == "native_ios"
    assert result["routing_confidence"] == "high"


@pytest.mark.asyncio
async def test_does_not_write_any_symbol_package_row(patched_session, monkeypatch):
    from app.crashguard.api.crash import SymbolicateRequest, symbolicate_ad_hoc_stack
    from app.crashguard.models import CrashSymbolPackage
    from app.crashguard.services import symbolication
    from app.services import repo_router

    captured: dict = {}
    monkeypatch.setattr(repo_router, "resolve", _make_fake_resolve(captured))

    async def fake_symbolicate_stack_noop(stack, binary_images, platform, app_version, *, symbol_profile="", github_repo=""):
        return stack

    monkeypatch.setattr(symbolication, "symbolicate_stack", fake_symbolicate_stack_noop)

    async with patched_session() as session:
        before = (await session.execute(select(CrashSymbolPackage))).scalars().all()
    assert before == []

    body = SymbolicateRequest(stack="raw stack line", platform="ios", app_version="4.0.201-941")
    result = await symbolicate_ad_hoc_stack(body)
    assert result["available_symbol_packages"] == []
    assert any("无已上传符号包" in w for w in result["warnings"])

    async with patched_session() as session:
        after = (await session.execute(select(CrashSymbolPackage))).scalars().all()
    assert after == [], "端点是只读操作，不应写入任何 crash_symbol_packages 行"
