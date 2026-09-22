"""POST /api/crash/symbolicate/preflight 单测（2026-09-22）。

这是本次改进的核心：把「没有符号表」从事后 warning 改成**提交前**的三档预检。

现有 POST /symbolicate 是"先干活再抱怨"——花几十秒到几分钟尝试下载 dSYM
（~90MB），跑完符号化后才在 warnings 里说无符号包。用户白等。preflight 在
用户点「开始符号化」之前就给出结论。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.crashguard.services import symbol_catalog


@pytest.fixture(autouse=True)
def _clear():
    symbol_catalog.invalidate_version_cache()
    yield
    symbol_catalog.invalidate_version_cache()


def _stub_sources(monkeypatch, *, cached=(), uploaded=(), release=()):
    monkeypatch.setattr(
        symbol_catalog, "_list_cached_versions", lambda p: list(cached)
    )
    async def _u(p):
        return [{"app_version": v, "symbol_types": ["dsym"]} for v in uploaded]
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _u)
    async def _r(p):
        return (list(release), [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _r)


@pytest.mark.asyncio
async def test_cached_status_promises_seconds(monkeypatch):
    _stub_sources(monkeypatch, cached=["4.0.201-941"])
    r = await symbol_catalog.preflight_symbols("ios", "4.0.201-941")
    assert r["status"] == "cached"
    assert "秒级" in r["eta_hint"]
    assert r["suggestions"] == {}


@pytest.mark.asyncio
async def test_uploaded_package_also_counts_as_cached(monkeypatch):
    """Plan B 的上传包同样是本地就绪，不需要下载。"""
    _stub_sources(monkeypatch, uploaded=["4.0.201-941"])
    r = await symbol_catalog.preflight_symbols("ios", "4.0.201-941")
    assert r["status"] == "cached"


@pytest.mark.asyncio
async def test_available_status_warns_about_download(monkeypatch):
    _stub_sources(monkeypatch, release=[{
        "app_version": "4.0.201-941", "verified": True, "tag": "v4.0.201+941",
        "symbol_types": ["Plaud-Global.dSYMs.zip"], "asset_size": 94_371_840,
    }])
    r = await symbol_catalog.preflight_symbols("ios", "4.0.201-941")
    assert r["status"] == "available"
    assert "90" in r["eta_hint"]          # 94371840 B ≈ 90MB
    assert "分钟" in r["eta_hint"]


@pytest.mark.asyncio
async def test_missing_status_is_explicit_and_actionable(monkeypatch):
    """三处都没有 → 明确说「不会有任何效果」，并给出邻近版本救场。

    QA 记错 build 号是高频事件，nearby_versions 这一条能直接救场。
    """
    _stub_sources(
        monkeypatch,
        cached=["4.0.201-938"],
        uploaded=["4.0.100-954"],
        release=[{"app_version": "4.0.300-999", "verified": True, "tag": "t"}],
    )
    r = await symbol_catalog.preflight_symbols("ios", "4.0.201-941")
    assert r["status"] == "missing"
    assert "不会有任何效果" in r["eta_hint"]
    nearby = r["suggestions"]["nearby_versions"]
    assert "4.0.201-938" in nearby        # 同 minor 且 build 最接近 → 应排第一
    assert nearby[0] == "4.0.201-938"
    assert len(nearby) <= 5


@pytest.mark.asyncio
async def test_missing_with_release_but_no_symbol_asset_explains_why(monkeypatch):
    """该版本有 Release 但没有符号 asset → 大概率不是线上包。"""
    _stub_sources(monkeypatch, release=[{
        "app_version": "4.0.201-941", "verified": True, "tag": "v4.0.201+941",
        "symbol_types": ["app-release.apk"],     # 只有 apk，没有符号包
    }])
    r = await symbol_catalog.preflight_symbols("android", "4.0.201-941")
    assert r["status"] == "missing"
    assert "IS_ONLINE_PACKAGE" in r["suggestions"]["reason"]


@pytest.mark.asyncio
async def test_missing_without_release_says_version_not_found(monkeypatch):
    _stub_sources(monkeypatch)
    r = await symbol_catalog.preflight_symbols("ios", "9.9.9-1")
    assert r["status"] == "missing"
    assert "9.9.9-1" in r["suggestions"]["reason"]


@pytest.mark.asyncio
async def test_unverified_input_still_judged_honestly(monkeypatch):
    """verified=False 的 tag 形态输入，照常跑三档判定，判为 missing 就老实报
    missing —— 不因为"它来自 Release 列表"就假定可用。"""
    _stub_sources(monkeypatch, release=[{
        "app_version": "v4.0.400+1000", "verified": False, "tag": "v4.0.400+1000",
        "symbol_types": ["app-release.apk"],
    }])
    r = await symbol_catalog.preflight_symbols("android", "v4.0.400+1000")
    assert r["status"] == "missing"


@pytest.mark.asyncio
async def test_preflight_downloads_nothing(monkeypatch):
    """预检绝不能下载任何字节 —— 这是它能秒级返回的前提。"""
    called = {"n": 0}
    import app.crashguard.services.github_symbols as gs

    async def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("preflight 不应调用下载 getter")

    monkeypatch.setattr(gs, "get_ios_dsyms_dir", _boom)
    monkeypatch.setattr(gs, "get_android_mapping", _boom)
    monkeypatch.setattr(gs, "get_android_native_symbols_dir", _boom)
    monkeypatch.setattr(gs, "get_dart_symbols_dir", _boom)
    _stub_sources(monkeypatch)

    await symbol_catalog.preflight_symbols("ios", "4.0.201-941")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_source_failure_degrades_not_raises(monkeypatch):
    """任一源炸了也要给出结论，不能让预检本身失败。"""
    def _boom_cached(p):
        raise RuntimeError("磁盘炸了")
    monkeypatch.setattr(symbol_catalog, "_list_cached_versions", _boom_cached)
    async def _u(p):
        raise RuntimeError("DB 炸了")
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _u)
    async def _r(p):
        raise RuntimeError("GH 炸了")
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _r)

    r = await symbol_catalog.preflight_symbols("ios", "4.0.201-941")
    assert r["status"] == "missing"
    assert len(r["warnings"]) == 3


@pytest.mark.asyncio
async def test_api_validates_input():
    from app.crashguard.api.crash import (
        SymbolPreflightRequest, preflight_symbolicate,
    )

    with pytest.raises(HTTPException) as e1:
        await preflight_symbolicate(
            SymbolPreflightRequest(platform="windows", app_version="1.0")
        )
    assert e1.value.status_code == 400

    with pytest.raises(HTTPException) as e2:
        await preflight_symbolicate(
            SymbolPreflightRequest(platform="ios", app_version="   ")
        )
    assert e2.value.status_code == 400


# ── preflight 必须复用 list_symbol_versions 的缓存（2026-09-22 三次实测）──────
#
# 102 实测：preflight 返回 warnings=["GitHub Release 检查失败："]（异常消息为空），
# 而同期 versions 端点能正常拉到 61 个候选。根因是 preflight 直连
# _list_release_versions、绕过 5 分钟缓存，每次真打两个仓的 GH API 并超时
# （httpx ReadTimeout 的 str() 恰好是空的）。这砸掉了 preflight "秒级返回"
# 的立身之本——它的全部价值就在于比真符号化快几个数量级。

@pytest.mark.asyncio
async def test_preflight_reuses_catalog_cache(monkeypatch):
    """连续三次 preflight 只应打一次 GH API（其余走 5 分钟缓存）。"""
    calls = {"n": 0}
    monkeypatch.setattr(symbol_catalog, "_list_cached_versions", lambda p: [])
    async def _u(p):
        return []
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _u)
    async def _r(p):
        calls["n"] += 1
        return ([{"app_version": "4.0.302-1171", "verified": True,
                  "tag": "v4.0.302+1000-x", "family": "native",
                  "symbol_types": ["Plaud-Global.dSYMs.zip"],
                  "asset_size": 94_371_840}], [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _r)

    for _ in range(3):
        r = await symbol_catalog.preflight_symbols("ios", "4.0.302-1171")
        assert r["status"] == "available"
    assert calls["n"] == 1, f"打了 {calls['n']} 次 GH API，应只打 1 次"


@pytest.mark.asyncio
async def test_empty_exception_message_is_not_swallowed(monkeypatch):
    """异常消息为空时（httpx ReadTimeout 就是这样）必须用异常类名兜底，
    否则 warning 是 'xxx失败：' 这种无法诊断的空串。"""
    monkeypatch.setattr(symbol_catalog, "_list_cached_versions", lambda p: [])
    async def _u(p):
        return []
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _u)
    async def _r(p):
        raise TimeoutError("")        # 空消息，模拟 httpx.ReadTimeout
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _r)

    r = await symbol_catalog.preflight_symbols("ios", "4.0.302-1171")
    assert r["status"] == "missing"
    assert any("TimeoutError" in w for w in r["warnings"]), r["warnings"]
