"""GET /api/crash/symbolicate/versions 单测（2026-09-22）：版本候选列表。

为什么必须带 GitHub Release 源：Jenkins 仅在 IS_ONLINE_PACKAGE=true 时上传符号包
（见 api/crash.py 注释），所以 QA 手上的灰度包大概率没上传过。只列已上传包的话
下拉会经常是空的，功能等于没做。

为什么候选要跨两个仓：config.yaml 的 repo_routing 以 4.0.0 为界分流
（< 4.0.0 → Plaud-App / >= 4.0.0 → plaud-native-app），但"查哪个仓"取决于版本号，
而版本号正是我们要列的东西 → 只能两仓都列。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.crashguard.services import symbol_catalog


@pytest.fixture(autouse=True)
def _clear_cache():
    symbol_catalog.invalidate_version_cache()
    yield
    symbol_catalog.invalidate_version_cache()


@pytest.mark.asyncio
async def test_merges_three_sources_and_dedups(monkeypatch):
    """同一个版本被多源命中时合并成一条，source 取最优：cached > uploaded > release。"""
    monkeypatch.setattr(
        symbol_catalog, "_list_cached_versions", lambda platform: ["4.0.201-941"]
    )
    async def _uploaded(platform):
        return [{"app_version": "4.0.201-941", "symbol_types": ["dsym"]},
                {"app_version": "4.0.100-954", "symbol_types": ["dsym"]}]
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _uploaded)
    async def _releases(platform):
        return ([{"app_version": "4.0.201-941", "verified": True, "tag": "v4.0.201+941"},
                 {"app_version": "4.0.300-999", "verified": True, "tag": "v4.0.300+999"}], [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _releases)

    res = await symbol_catalog.list_symbol_versions("ios")
    by_ver = {v["app_version"]: v for v in res["versions"]}

    assert len(res["versions"]) == 3          # 941 三源命中 → 合并成一条
    assert by_ver["4.0.201-941"]["source"] == "cached"    # 最优源胜出
    assert by_ver["4.0.100-954"]["source"] == "uploaded"
    assert by_ver["4.0.300-999"]["source"] == "release"


@pytest.mark.asyncio
async def test_unverified_release_is_flagged(monkeypatch):
    """_build_index 未解析真实 build 号的 release：verified=False，
    app_version 填 tag，前端要显示「⚠ tag 未校验」。

    Release tag 里的 +NNN 不是真实 build 号（真实值在 dSYM Info.plist /
    APK assets/datadog.buildId）。不能静默假装它是真版本号，否则用户选了个
    假 build 号，符号化静默失败 —— 正是本功能要消除的坑。
    """
    monkeypatch.setattr(symbol_catalog, "_list_cached_versions", lambda p: [])
    async def _uploaded(p):
        return []
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _uploaded)
    async def _releases(p):
        return ([{"app_version": "v4.0.400+1000", "verified": False,
                  "tag": "v4.0.400+1000"}], [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _releases)

    res = await symbol_catalog.list_symbol_versions("android")
    assert res["versions"][0]["verified"] is False
    assert res["versions"][0]["app_version"] == "v4.0.400+1000"


@pytest.mark.asyncio
async def test_github_failure_degrades_not_raises(monkeypatch):
    """GH API 挂了不能让整个下拉爆炸 —— 降级成只返回本地来源 + warning。"""
    monkeypatch.setattr(
        symbol_catalog, "_list_cached_versions", lambda p: ["4.0.201-941"]
    )
    async def _uploaded(p):
        return []
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _uploaded)
    async def _boom(p):
        raise RuntimeError("GitHub 503")
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _boom)

    res = await symbol_catalog.list_symbol_versions("ios")
    assert [v["app_version"] for v in res["versions"]] == ["4.0.201-941"]
    assert any("GitHub" in w for w in res["warnings"])


@pytest.mark.asyncio
async def test_ttl_cache_avoids_refetch(monkeypatch):
    """GH API 有 rate limit，且下拉可能被反复打开 → 5 分钟进程内缓存。"""
    calls = {"n": 0}
    monkeypatch.setattr(symbol_catalog, "_list_cached_versions", lambda p: [])
    async def _uploaded(p):
        return []
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _uploaded)
    async def _releases(p):
        calls["n"] += 1
        return ([{"app_version": "4.0.201-941", "verified": True, "tag": "t"}], [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _releases)

    await symbol_catalog.list_symbol_versions("ios")
    await symbol_catalog.list_symbol_versions("ios")
    assert calls["n"] == 1

    symbol_catalog.invalidate_version_cache()
    await symbol_catalog.list_symbol_versions("ios")
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_sorted_desc_by_version(monkeypatch):
    monkeypatch.setattr(
        symbol_catalog, "_list_cached_versions",
        lambda p: ["4.0.100-954", "4.0.201-941", "3.18.0-708"],
    )
    async def _uploaded(p):
        return []
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _uploaded)
    async def _releases(p):
        return ([], [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _releases)

    res = await symbol_catalog.list_symbol_versions("ios")
    assert [v["app_version"] for v in res["versions"]] == [
        "4.0.201-941", "4.0.100-954", "3.18.0-708",
    ]


@pytest.mark.asyncio
async def test_api_rejects_bad_platform():
    from app.crashguard.api.crash import list_symbolicate_versions

    with pytest.raises(HTTPException) as ei:
        await list_symbolicate_versions(platform="windows")
    assert ei.value.status_code == 400


def test_looks_like_symbol_asset_matches_real_asset_names():
    """资产名判定必须对齐 github_symbols 的 _ASSET_* 常量，不能自己编名字。"""
    f = symbol_catalog._looks_like_symbol_asset
    assert f("PLAUD.dSYMs.zip") is True
    assert f("Plaud-Global.dSYMs.zip") is True
    assert f("mapping_globalRelease.txt") is True
    assert f("native_symbols.tar.gz") is True
    assert f("flutter_symbols.tar.gz") is True
    # 非符号资产
    assert f("app-release.apk") is False
    assert f("Plaud.ipa") is False
    assert f("") is False


def test_version_sort_key_never_raises_on_garbage():
    """版本号可能是 tag 形态（v4.0.400+1000）或畸形值，排序不能崩。"""
    k = symbol_catalog._version_sort_key
    for v in ("4.0.201-941", "v4.0.400+1000", "", "abc", "4-", "1.2.3.4.5-x9"):
        assert isinstance(k(v), tuple)


# ── Release tag → app_version 的还原（2026-09-22 生产实测发现的 bug）──────────
#
# 首次上线后在 102 实测发现候选的 app_version 显示成纯 build 号（"1171"/"1143"），
# 丢掉了语义版本，用户根本看不出是哪个版本，且 _version_sort_key 把纯数字当主版本
# 排序、语义版本顺序全乱。根因两处：
#   1. _build_index 的结构是 {tag: build}，代码却多做了一次 {v:k} 反转
#   2. app_version = build or tag —— build 号不是 app_version
#
# 正确形式是 semver + 真实 build 号拼接：v4.0.100+999-... + "1004" → "4.0.100-1004"
#
# 且两个仓的规则不同（见 github_symbols._version_to_tag_prefix 上方注释）：
#   - flutter 仓（Plaud-App）：tag 后缀 +NNN **就是**真实 build 号
#   - native 仓（plaud-native-app）：+NNN 是假的（几十个 build 才冻结换一次），
#     真实值要查 _build_index 或 .aab 资产名

def test_parse_tag_extracts_semver_and_suffix():
    f = symbol_catalog._parse_tag
    assert f("v4.0.100+999-2026_08_11-203603-global") == ("4.0.100", "999")
    assert f("v3.18.0+708-2026_05_01-120000-global") == ("3.18.0", "708")
    assert f("4.0.100+999") == ("4.0.100", "999")
    # 不合规 tag → 空，调用方据此标 verified=False
    assert f("some-random-tag") == ("", "")
    assert f("") == ("", "")


def test_flutter_tag_suffix_is_the_real_build():
    """flutter 仓 tag 后缀就是真 build 号，不需要查 index。"""
    av, verified = symbol_catalog._release_app_version(
        "v3.18.0+708-2026_05_01-120000-global", "flutter", {}, [],
    )
    assert av == "3.18.0-708"
    assert verified is True


def test_native_tag_uses_build_index_not_suffix():
    """native 仓的 +999 是假的，真实 build 在 _build_index 里。"""
    index = {"v4.0.100+999-2026_08_11-203603-global": "1004"}
    av, verified = symbol_catalog._release_app_version(
        "v4.0.100+999-2026_08_11-203603-global", "native", index, [],
    )
    assert av == "4.0.100-1004"      # 不是 4.0.100-999
    assert verified is True


def test_native_falls_back_to_aab_asset_name():
    """index 没命中时退到 .aab 资产名抠 build 号（零额外请求）。"""
    assets = [{"name": "PLAUD_v4.0.100_1171_release.aab", "size": 1}]
    av, verified = symbol_catalog._release_app_version(
        "v4.0.100+999-2026_09_20-100000-global", "native", {}, assets,
    )
    assert av == "4.0.100-1171"
    assert verified is True


def test_native_unresolved_build_falls_back_to_tag_unverified():
    """index 和 aab 都拿不到 → app_version 用 tag 且 verified=False，
    前端显示「⚠ tag 未校验」。绝不拿假 build 号（+999）冒充真版本号。"""
    tag = "v4.0.100+999-2026_09_21-100000-global"
    av, verified = symbol_catalog._release_app_version(tag, "native", {}, [])
    assert av == tag
    assert verified is False


@pytest.mark.asyncio
async def test_app_version_is_never_a_bare_build_number(monkeypatch):
    """回归护栏：候选的 app_version 必须是 semver-build 形式，
    绝不能是裸 build 号（那是 102 上实测到的 bug）。"""
    monkeypatch.setattr(symbol_catalog, "_list_cached_versions", lambda p: [])
    async def _u(p):
        return []
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _u)
    async def _r(p):
        return ([
            {"app_version": "4.0.100-1004", "verified": True,
             "tag": "v4.0.100+999-x", "family": "native", "symbol_types": [],
             "asset_size": 0},
            {"app_version": "4.0.201-941", "verified": True,
             "tag": "v4.0.201+941-x", "family": "native", "symbol_types": [],
             "asset_size": 0},
        ], [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _r)

    res = await symbol_catalog.list_symbol_versions("ios")
    for v in res["versions"]:
        assert not v["app_version"].isdigit(), (
            f"app_version 是裸 build 号 {v['app_version']!r}，丢了语义版本"
        )
    # 排序按语义版本优先：4.0.201-941 应排在 4.0.100-1004 之前
    assert [v["app_version"] for v in res["versions"]] == [
        "4.0.201-941", "4.0.100-1004",
    ]


# ── 排序：verified 优先 + tag 形态不产生畸形大数（2026-09-22 二次实测）─────────
#
# 修掉裸 build 号后在 102 实测：61 个候选里 46 个 verified（用户真正能用的），
# 但它们从位置 15 才开始——前 15 个全是不可用的 tag 形态，下拉基本没法用。
#
# 根因：_version_sort_key("v4.0.301+1000-2026_08_21-193837-global") 按第一个
# "-" 切，head = "v4.0.301+1000"，第三段 "301+1000" 抠数字得 3011000，
# 畸形大数把 unverified 的 tag 顶到最前面。

def test_version_sort_key_does_not_explode_on_tag_form():
    k = symbol_catalog._version_sort_key
    # "301+1000" 必须只取 "+" 之前的 301，不能变成 3011000
    assert k("v4.0.301+1000-2026_08_21-193837-global")[0] == (4, 0, 301)
    assert k("4.0.302-1171")[0] == (4, 0, 302)
    # 正常语义版本应当大于同主版本的 tag 形态
    assert k("4.0.302-1171") > k("v4.0.301+1000-2026_08_21-193837-global")


@pytest.mark.asyncio
async def test_verified_candidates_sort_first(monkeypatch):
    """verified 的排在前面——用户真正能用的不该被 tag 形态挤到 15 位之后。"""
    monkeypatch.setattr(symbol_catalog, "_list_cached_versions", lambda p: [])
    async def _u(p):
        return []
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _u)
    async def _r(p):
        return ([
            # 版本号更高但未校验 —— 不该排第一
            {"app_version": "v4.0.999+1000-2026_09_01-120000-global",
             "verified": False, "tag": "v4.0.999+1000-x", "family": "native",
             "symbol_types": [], "asset_size": 0},
            {"app_version": "4.0.302-1143", "verified": True,
             "tag": "v4.0.302+1000-x", "family": "native",
             "symbol_types": [], "asset_size": 0},
            {"app_version": "4.0.100-1004", "verified": True,
             "tag": "v4.0.100+999-x", "family": "native",
             "symbol_types": [], "asset_size": 0},
        ], [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _r)

    res = await symbol_catalog.list_symbol_versions("ios")
    got = [(v["app_version"], v["verified"]) for v in res["versions"]]
    assert got == [
        ("4.0.302-1143", True),
        ("4.0.100-1004", True),
        ("v4.0.999+1000-2026_09_01-120000-global", False),
    ], got
