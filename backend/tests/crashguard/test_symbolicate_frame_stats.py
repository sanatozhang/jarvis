"""逐帧符号化统计 + 修掉误导性 warning（2026-09-22）。

## 为什么需要逐帧统计

现有 POST /symbolicate 只返回 changed: bool，太粗。而 _symbolicate_ios_with_dir
是**故意**跳过系统库帧的（该函数 docstring 记录了生产实测：不做 module gating
会给每一帧都凑出一个看似合理实则完全无关的 Plaud 符号，比原样保留地址更具
误导性）。所以一次正常成功的符号化里也有一半帧是裸地址，用户会误判为失败。

## 诚实的边界

端点层**拿不到** dSYM 里到底有哪些 module（那在 symbolication 内部），所以无法
真正判断"某帧的 module 是否属于符号包"。app_module 靠 _infer_app_module 推断；
推断不到时 app_* 字段返回 None，不编造区分。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker


BEFORE = """0   PLAUD                         0x0000000103a2c000 0x103a00000 + 180224
1   PLAUD                         0x0000000103a2d100 0x103a00000 + 184576
2   libsystem_kernel.dylib        0x00000001e0a1c2c4 0x1e0a00000 + 115396
3   BoardServices                 0x00000001f0a1c2c4 0x1f0a00000 + 4660
"""

# App 帧解出了符号，系统库帧按设计原样保留
AFTER = """0   PLAUD                         -[PLDRecordVC viewDidLoad] (in PLAUD) (PLDRecordVC.m:42)
1   PLAUD                         -[PLDSyncMgr flush] (in PLAUD) (PLDSyncMgr.m:88)
2   libsystem_kernel.dylib        0x00000001e0a1c2c4 0x1e0a00000 + 115396
3   BoardServices                 0x00000001f0a1c2c4 0x1f0a00000 + 4660
"""


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


# ── 粗粒度统计（总是准确，纯文本比对）──────────────────────────────────────

def test_frame_stats_counts_resolved_vs_unresolved():
    from app.crashguard.api.crash import _compute_frame_stats

    st = _compute_frame_stats(BEFORE, AFTER)
    assert st["total_frames"] == 4
    assert st["symbolicated"] == 2
    assert st["unresolved"] == 2


def test_frame_stats_all_failed():
    from app.crashguard.api.crash import _compute_frame_stats

    st = _compute_frame_stats(BEFORE, BEFORE)   # 一帧都没变
    assert st["total_frames"] == 4
    assert st["symbolicated"] == 0
    assert st["unresolved"] == 4


def test_frame_stats_handles_android_java():
    """Java 栈没有 iOS 帧形状，不能崩，也不该瞎报数。"""
    from app.crashguard.api.crash import _compute_frame_stats

    java = "java.lang.NullPointerException\n\tat a.b.c(Unknown Source:12)\n"
    st = _compute_frame_stats(java, java)
    assert st["total_frames"] == 0        # iOS 帧正则零命中
    assert st["symbolicated"] == 0
    assert st["unparsed_lines"] == 2


def test_frame_stats_empty_input():
    from app.crashguard.api.crash import _compute_frame_stats

    st = _compute_frame_stats("", "")
    assert st["total_frames"] == 0


# ── 细分统计（仅当能推断出 app_module）────────────────────────────────────

def test_frame_stats_app_module_breakdown_when_inferable():
    from app.crashguard.api.crash import _compute_frame_stats

    st = _compute_frame_stats(BEFORE, AFTER, app_module="PLAUD")
    assert st["app_module"] == "PLAUD"
    assert st["app_frames"] == 2
    assert st["app_symbolicated"] == 2
    assert st["non_app_frames"] == 2          # 系统库帧，按设计跳过


def test_frame_stats_app_module_failure_is_visible():
    """App 帧没解出来必须能看见 —— 这才是真失败，不能和系统库帧混在一起。"""
    from app.crashguard.api.crash import _compute_frame_stats

    partial = AFTER.replace(
        "1   PLAUD                         -[PLDSyncMgr flush] (in PLAUD) (PLDSyncMgr.m:88)",
        "1   PLAUD                         0x0000000103a2d100 0x103a00000 + 184576",
    )
    st = _compute_frame_stats(BEFORE, partial, app_module="PLAUD")
    assert st["app_frames"] == 2
    assert st["app_symbolicated"] == 1        # 1 成功 1 失败
    assert st["non_app_frames"] == 2


def test_frame_stats_no_app_module_gives_coarse_only():
    """推断不出 App module 时，只给粗粒度，app_* 字段为 None —— 不假装。"""
    from app.crashguard.api.crash import _compute_frame_stats

    st = _compute_frame_stats(BEFORE, AFTER, app_module="")
    assert st["symbolicated"] == 2
    assert st["app_module"] == ""
    assert st["app_frames"] is None
    assert st["app_symbolicated"] is None
    assert st["non_app_frames"] is None


def test_infer_app_module_from_binary_images():
    """优先用 .ips 的 source=="P"（主可执行），退而用最高频 module。"""
    from app.crashguard.api.crash import _infer_app_module

    imgs = [
        {"name": "libsystem_kernel.dylib", "source": "S"},
        {"name": "PLAUD", "source": "P"},
    ]
    assert _infer_app_module(BEFORE, imgs) == "PLAUD"
    # 没有 binary_images 时靠帧频率
    assert _infer_app_module(BEFORE, []) == "PLAUD"
    assert _infer_app_module("", []) == ""


# ── 端点集成 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_symbolicate_response_includes_frame_stats(patched_session, monkeypatch):
    """端点响应必须带 frame_stats，且不能破坏既有字段（向后兼容）。"""
    from app.crashguard.api.crash import SymbolicateRequest, symbolicate_ad_hoc_stack
    from app.crashguard.services import symbolication

    async def _fake(stack, binary_images, platform, app_version="", *,
                    symbol_profile="", github_repo=""):
        return AFTER
    monkeypatch.setattr(symbolication, "symbolicate_stack", _fake)

    d = await symbolicate_ad_hoc_stack(SymbolicateRequest(
        stack=BEFORE, platform="ios", app_version="4.0.201-941",
    ))

    # 新增字段
    assert d["frame_stats"]["symbolicated"] == 2
    assert d["frame_stats"]["unresolved"] == 2
    # PLAUD 是最高频 module → 能推断出 App module，给出细分
    assert d["frame_stats"]["app_module"] == "PLAUD"
    assert d["frame_stats"]["app_symbolicated"] == 2
    # 既有字段语义不变（向后兼容护栏）
    for k in ("symbolicated_stack", "changed", "stack_quality_before",
              "stack_quality_after", "routing_confidence",
              "available_symbol_packages", "duration_ms", "warnings"):
        assert k in d, f"既有字段 {k} 丢了"
    assert d["changed"] is True


@pytest.mark.asyncio
async def test_no_misleading_no_uploaded_package_warning_when_cached(
    patched_session, monkeypatch
):
    """修掉误导性 warning：Plan C 下载成功时，不该再说「无已上传符号包」。

    原逻辑只查 CrashSymbolPackage（本地上传表），完全不看 GitHub Release，
    所以即使符号化完美也会无条件吐这句 —— 是纯噪音，会让人误判成失败。
    """
    from app.crashguard.api.crash import SymbolicateRequest, symbolicate_ad_hoc_stack
    from app.crashguard.services import symbol_catalog, symbolication

    async def _fake(stack, binary_images, platform, app_version="", *,
                    symbol_profile="", github_repo=""):
        return AFTER
    monkeypatch.setattr(symbolication, "symbolicate_stack", _fake)

    async def _pf(platform, app_version, **kw):
        return {"status": "cached", "eta_hint": "符号包已就绪，预计秒级返回",
                "symbol_sources": [], "suggestions": {}, "warnings": []}
    monkeypatch.setattr(symbol_catalog, "preflight_symbols", _pf)

    d = await symbolicate_ad_hoc_stack(SymbolicateRequest(
        stack=BEFORE, platform="ios", app_version="4.0.201-941",
    ))
    assert not any("无已上传符号包" in w for w in d["warnings"])
    assert d["preflight"]["status"] == "cached"


@pytest.mark.asyncio
async def test_missing_preflight_surfaces_warning(patched_session, monkeypatch):
    """反向：preflight 判 missing 时，warning 必须说清楚。"""
    from app.crashguard.api.crash import SymbolicateRequest, symbolicate_ad_hoc_stack
    from app.crashguard.services import symbol_catalog, symbolication

    async def _fake(stack, binary_images, platform, app_version="", *,
                    symbol_profile="", github_repo=""):
        return BEFORE          # 没变，符号化无效
    monkeypatch.setattr(symbolication, "symbolicate_stack", _fake)

    async def _pf(platform, app_version, **kw):
        return {"status": "missing", "eta_hint": "该版本无符号表，符号化不会有任何效果",
                "symbol_sources": [],
                "suggestions": {"reason": "两个仓的 Release 列表里都没有这个版本。",
                                "nearby_versions": ["4.0.201-938"]},
                "warnings": []}
    monkeypatch.setattr(symbol_catalog, "preflight_symbols", _pf)

    d = await symbolicate_ad_hoc_stack(SymbolicateRequest(
        stack=BEFORE, platform="ios", app_version="9.9.9-1",
    ))
    assert any("无符号表" in w for w in d["warnings"])
    assert d["preflight"]["suggestions"]["nearby_versions"] == ["4.0.201-938"]
