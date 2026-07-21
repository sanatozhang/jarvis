"""symbolication.py::symbolicate_jank_frame() 单测（2026-07-20）。

背景：jank_watchdog_block 卡顿日志每条只给"应用自身模块"单帧地址（不是整段多帧
堆栈），符号化成本很低，复用现有的多帧解析函数（伪造成一行"stack"喂给它们）而不是
重新实现 dSYM 下载/ProGuard 解析/atos 调用。iOS 复用 github_symbols.py 的 GitHub
release 符号包下载（native app 自己的 dSYM，不是 Flutter engine 的）+
_symbolicate_ios_with_dir()；Android 复用 _retrace_proguard()。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


# ── iOS ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ios_symbolicates_when_dsym_resolves(monkeypatch):
    """dSYM 下载成功 + atos 解析成功 → 返回符号化后的函数名文本。"""
    from app.crashguard.services import symbolication as sym
    # symbolicate_jank_frame 内部用局部 import 拿 github_symbols.get_ios_dsyms_dir，
    # 所以要 patch 的是 github_symbols 源模块，而不是 symbolication 模块的属性。
    import app.crashguard.services.github_symbols as gh
    monkeypatch.setattr(gh, "get_ios_dsyms_dir", AsyncMock(return_value="/fake/dsyms"))

    def fake_resolve(stack: str, dsyms_dir: str) -> str:
        assert dsyms_dir == "/fake/dsyms"
        assert stack == "0   Plaud-Global   0x0000000103e42dd4   0x0000000102f1c000 + 15887828\n"
        return "0   Plaud-Global   -[PLRecordManager stopRecording] PLRecordManager.swift:120\n"

    monkeypatch.setattr(sym, "_symbolicate_ios_with_dir", fake_resolve)

    result = await sym.symbolicate_jank_frame(
        platform="ios",
        app_version="4.0.201-941",
        module="Plaud-Global",
        pc="0x0000000103e42dd4",
        module_base="0x0000000102f1c000",
        symbol_profile="native_ios",
        github_repo="Plaud-AI/plaud-native-ios",
    )
    assert result == "-[PLRecordManager stopRecording] PLRecordManager.swift:120"


@pytest.mark.asyncio
async def test_ios_falls_back_to_placeholder_when_dsym_missing(monkeypatch):
    """符号包下载失败（返回 None）→ 原样返回 "{module} + {pc}" 占位。"""
    from app.crashguard.services import symbolication as sym
    import app.crashguard.services.github_symbols as gh

    monkeypatch.setattr(gh, "get_ios_dsyms_dir", AsyncMock(return_value=None))

    result = await sym.symbolicate_jank_frame(
        platform="ios",
        app_version="4.0.201-941",
        module="Plaud-Global",
        pc="0x0000000103e42dd4",
        module_base="0x0000000102f1c000",
        symbol_profile="native_ios",
        github_repo="Plaud-AI/plaud-native-ios",
    )
    assert result == "Plaud-Global + 0x0000000103e42dd4"


@pytest.mark.asyncio
async def test_ios_falls_back_to_placeholder_when_atos_unresolved(monkeypatch):
    """dSYM 目录存在但 atos 没能解析任何帧（_symbolicate_ios_with_dir 原样返回）→ 占位。"""
    from app.crashguard.services import symbolication as sym
    import app.crashguard.services.github_symbols as gh

    monkeypatch.setattr(gh, "get_ios_dsyms_dir", AsyncMock(return_value="/fake/dsyms"))

    def fake_resolve_noop(stack: str, dsyms_dir: str) -> str:
        return stack  # 未命中任何 dSYM，原样返回

    monkeypatch.setattr(sym, "_symbolicate_ios_with_dir", fake_resolve_noop)

    result = await sym.symbolicate_jank_frame(
        platform="ios",
        app_version="4.0.201-941",
        module="Plaud-Global",
        pc="0x0000000103e42dd4",
        module_base="0x0000000102f1c000",
    )
    assert result == "Plaud-Global + 0x0000000103e42dd4"


@pytest.mark.asyncio
async def test_ios_missing_pc_or_base_skips_download(monkeypatch):
    """pc/module_base 缺失时直接占位，不触发下载（避免无意义的网络调用）。"""
    from app.crashguard.services import symbolication as sym
    import app.crashguard.services.github_symbols as gh

    download_mock = AsyncMock(return_value="/fake/dsyms")
    monkeypatch.setattr(gh, "get_ios_dsyms_dir", download_mock)

    result = await sym.symbolicate_jank_frame(
        platform="ios", app_version="4.0.201-941", module="Plaud-Global", pc="", module_base="",
    )
    assert result == "Plaud-Global + "
    download_mock.assert_not_called()


# ── Android ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_android_retraces_obfuscated_frame(monkeypatch, tmp_path):
    """混淆帧（ProGuard mapping 命中）→ 返回反混淆后的原始类名+方法名。"""
    from app.crashguard.services import symbolication as sym
    import app.crashguard.services.github_symbols as gh

    mapping_file = tmp_path / "mapping.txt"
    mapping_file.write_text(
        "ai.plaud.android.payment.PaymentValidator -> ai.plaud.android.payment.k:\n"
        "    void validate() -> a\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gh, "get_android_mapping", AsyncMock(return_value=str(mapping_file)))
    sym._PG_INDEX_CACHE.clear()

    result = await sym.symbolicate_jank_frame(
        platform="android",
        app_version="4.0.201-941",
        frame_text="ai.plaud.android.payment.k.a",
        symbol_profile="native_android",
        github_repo="Plaud-AI/plaud-native-android",
    )
    assert result == "ai.plaud.android.payment.PaymentValidator.validate"


@pytest.mark.asyncio
async def test_android_passthrough_when_class_not_in_mapping(monkeypatch, tmp_path):
    """类名不在映射表里（本来就没混淆）→ 原样返回，不报错。"""
    from app.crashguard.services import symbolication as sym
    import app.crashguard.services.github_symbols as gh

    mapping_file = tmp_path / "mapping.txt"
    mapping_file.write_text(
        "ai.plaud.android.unrelated.Foo -> ai.plaud.android.unrelated.x:\n"
        "    void bar() -> a\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gh, "get_android_mapping", AsyncMock(return_value=str(mapping_file)))
    sym._PG_INDEX_CACHE.clear()

    frame = "ai.plaud.android.plaud.monitoring.DatadogConfig.trackTransaction"
    result = await sym.symbolicate_jank_frame(
        platform="android", app_version="4.0.201-941", frame_text=frame,
    )
    assert result == frame


@pytest.mark.asyncio
async def test_android_passthrough_when_no_mapping_available(monkeypatch):
    """拿不到 mapping 文件（None）→ 原样返回 frame_text。"""
    from app.crashguard.services import symbolication as sym
    import app.crashguard.services.github_symbols as gh

    monkeypatch.setattr(gh, "get_android_mapping", AsyncMock(return_value=None))

    frame = "ai.plaud.android.payment.k.a"
    result = await sym.symbolicate_jank_frame(
        platform="android", app_version="4.0.201-941", frame_text=frame,
    )
    assert result == frame


@pytest.mark.asyncio
async def test_android_passthrough_when_frame_text_empty():
    """frame_text 为空（没有可用文本）→ 原样返回空字符串，不触发任何下载。"""
    from app.crashguard.services import symbolication as sym

    result = await sym.symbolicate_jank_frame(
        platform="android", app_version="4.0.201-941", frame_text="",
    )
    assert result == ""


# ── 通用容错 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_swallows_exceptions_and_falls_back(monkeypatch):
    """任何子步骤抛异常都不应该向上传播——原样返回占位文本。"""
    from app.crashguard.services import symbolication as sym
    import app.crashguard.services.github_symbols as gh

    async def boom(*args, **kwargs):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(gh, "get_ios_dsyms_dir", boom)

    result = await sym.symbolicate_jank_frame(
        platform="ios",
        app_version="4.0.201-941",
        module="Plaud-Global",
        pc="0x0000000103e42dd4",
        module_base="0x0000000102f1c000",
    )
    assert result == "Plaud-Global + 0x0000000103e42dd4"


@pytest.mark.asyncio
async def test_unknown_platform_returns_frame_text_placeholder():
    """既不是 ios 也不是 android 的平台（防御性兜底）→ 原样返回。"""
    from app.crashguard.services import symbolication as sym

    result = await sym.symbolicate_jank_frame(
        platform="web", app_version="1.0.0", frame_text="some.Frame.text",
    )
    assert result == "some.Frame.text"


# ── symbolicate_jank_stack（完整多帧堆栈，2026-07-21） ─────────────────────────

_RAW_STACK_4_FRAMES = (
    "0   libsystem_kernel.dylib 0x0000000234b25cd4 mach_msg2_trap + 8\n"
    "1   Plaud-Global 0x0000000103e42dd4 0x0000000102f1c000 + 15887828\n"
    "2   Foundation 0x0000000182ff006c 0x0000000182fc7000 + 165996\n"
    "3   CoreFoundation 0x0000000185d9839c 0x0000000185d35000 + 407452\n"
)


@pytest.mark.asyncio
async def test_stack_symbolicates_app_frame_and_keeps_system_frames(monkeypatch):
    """4 帧假堆栈：app 自己模块的帧被替换成符号，系统框架帧原样保留 —— 断言结果
    是多行文本（不是单帧覆盖），且能看到符号化后的函数名。"""
    from app.crashguard.services import symbolication as sym
    import app.crashguard.services.github_symbols as gh

    monkeypatch.setattr(gh, "get_ios_dsyms_dir", AsyncMock(return_value="/fake/dsyms"))

    expected_fake_stack = (
        "0   libsystem_kernel.dylib   0x0000000234b25cd4   0x0000000234b25000 + 3284\n"
        "1   Plaud-Global   0x0000000103e42dd4   0x0000000102f1c000 + 15887828\n"
        "2   Foundation   0x0000000182ff006c   0x0000000182fc7000 + 168044\n"
        "3   CoreFoundation   0x0000000185d9839c   0x0000000185d35000 + 406428\n"
    )

    def fake_resolve(stack: str, dsyms_dir: str) -> str:
        assert dsyms_dir == "/fake/dsyms"
        assert stack == expected_fake_stack
        # 只有 app 自己的模块 (idx=1) 命中符号；其余系统框架帧原样保留（模拟
        # _symbolicate_ios_with_dir 逐行降级：查不到的行不变）。
        return (
            "0   libsystem_kernel.dylib   0x0000000234b25cd4   0x0000000234b25000 + 3284\n"
            "1   Plaud-Global   -[PLRecordManager stopRecording] PLRecordManager.swift:120\n"
            "2   Foundation   0x0000000182ff006c   0x0000000182fc7000 + 168044\n"
            "3   CoreFoundation   0x0000000185d9839c   0x0000000185d35000 + 406428\n"
        )

    monkeypatch.setattr(sym, "_symbolicate_ios_with_dir", fake_resolve)

    result = await sym.symbolicate_jank_stack(
        platform="ios",
        app_version="4.0.201-941",
        stack_trace=_RAW_STACK_4_FRAMES,
        stack_modules="libsystem_kernel.dylib|Plaud-Global|Foundation|CoreFoundation",
        stack_pcs="0x0000000234b25cd4|0x0000000103e42dd4|0x0000000182ff006c|0x0000000185d9839c",
        stack_module_bases="0x0000000234b25000|0x0000000102f1c000|0x0000000182fc7000|0x0000000185d35000",
        symbol_profile="native_ios",
        github_repo="Plaud-AI/plaud-native-ios",
    )

    assert result.count("\n") == 4
    assert "-[PLRecordManager stopRecording] PLRecordManager.swift:120" in result
    # 系统框架帧内容保留（原样地址，未被误替换）
    assert "libsystem_kernel.dylib" in result
    assert "CoreFoundation" in result


@pytest.mark.asyncio
async def test_stack_falls_back_to_raw_when_dsyms_dir_missing(monkeypatch):
    """dSYM 目录拿不到 → 原样返回传入的 stack_trace。"""
    from app.crashguard.services import symbolication as sym
    import app.crashguard.services.github_symbols as gh

    monkeypatch.setattr(gh, "get_ios_dsyms_dir", AsyncMock(return_value=None))

    result = await sym.symbolicate_jank_stack(
        platform="ios",
        app_version="4.0.201-941",
        stack_trace=_RAW_STACK_4_FRAMES,
        stack_modules="libsystem_kernel.dylib|Plaud-Global",
        stack_pcs="0x0000000234b25cd4|0x0000000103e42dd4",
        stack_module_bases="0x0000000234b25000|0x0000000102f1c000",
    )
    assert result == _RAW_STACK_4_FRAMES


@pytest.mark.asyncio
async def test_stack_falls_back_to_raw_when_arrays_missing():
    """数组字段缺失（空字符串）→ 原样返回，不尝试下载 dSYM。"""
    from app.crashguard.services import symbolication as sym

    result = await sym.symbolicate_jank_stack(
        platform="ios",
        app_version="4.0.201-941",
        stack_trace=_RAW_STACK_4_FRAMES,
    )
    assert result == _RAW_STACK_4_FRAMES


@pytest.mark.asyncio
async def test_stack_falls_back_to_raw_when_arrays_length_mismatch(monkeypatch):
    """三个数组长度不一致（畸形数据）→ 原样返回，不做逐帧对齐猜测。"""
    from app.crashguard.services import symbolication as sym
    import app.crashguard.services.github_symbols as gh

    download_mock = AsyncMock(return_value="/fake/dsyms")
    monkeypatch.setattr(gh, "get_ios_dsyms_dir", download_mock)

    result = await sym.symbolicate_jank_stack(
        platform="ios",
        app_version="4.0.201-941",
        stack_trace=_RAW_STACK_4_FRAMES,
        stack_modules="libsystem_kernel.dylib|Plaud-Global",
        stack_pcs="0x1|0x2|0x3",
        stack_module_bases="0x1|0x2",
    )
    assert result == _RAW_STACK_4_FRAMES
    download_mock.assert_not_called()


@pytest.mark.asyncio
async def test_stack_falls_back_to_raw_when_a_frame_missing_base(monkeypatch):
    """某一帧缺 base/pc（畸形数据）→ 整体回退到原始 stack_trace，不做部分符号化。"""
    from app.crashguard.services import symbolication as sym
    import app.crashguard.services.github_symbols as gh

    monkeypatch.setattr(gh, "get_ios_dsyms_dir", AsyncMock(return_value="/fake/dsyms"))
    monkeypatch.setattr(
        sym, "_symbolicate_ios_with_dir",
        lambda stack, dsyms_dir: (_ for _ in ()).throw(
            AssertionError("should not reach atos when a frame is missing base")
        ),
    )

    result = await sym.symbolicate_jank_stack(
        platform="ios",
        app_version="4.0.201-941",
        stack_trace=_RAW_STACK_4_FRAMES,
        stack_modules="libsystem_kernel.dylib|Plaud-Global",
        stack_pcs="0x0000000234b25cd4|0x0000000103e42dd4",
        stack_module_bases="0x0000000234b25000|",  # 第二帧缺 base
    )
    assert result == _RAW_STACK_4_FRAMES


@pytest.mark.asyncio
async def test_stack_android_passthrough_no_processing():
    """Android 不做任何多帧地址符号化，直接原样返回 stack_trace（现有摄入逻辑
    存的本来就是完整可读文本，不需要额外处理）。"""
    from app.crashguard.services import symbolication as sym

    raw = "  at ai.plaud.android.payment.k.a(Unknown Source)\n  at foo.bar.Baz.qux()\n"
    result = await sym.symbolicate_jank_stack(
        platform="android",
        app_version="4.0.201-941",
        stack_trace=raw,
        stack_modules="x|y",
        stack_pcs="0x1|0x2",
        stack_module_bases="0x1|0x2",
    )
    assert result == raw


@pytest.mark.asyncio
async def test_stack_swallows_exceptions_and_falls_back(monkeypatch):
    """任何子步骤抛异常都不应该向上传播——原样返回 stack_trace。"""
    from app.crashguard.services import symbolication as sym
    import app.crashguard.services.github_symbols as gh

    async def boom(*args, **kwargs):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(gh, "get_ios_dsyms_dir", boom)

    result = await sym.symbolicate_jank_stack(
        platform="ios",
        app_version="4.0.201-941",
        stack_trace=_RAW_STACK_4_FRAMES,
        stack_modules="libsystem_kernel.dylib|Plaud-Global",
        stack_pcs="0x0000000234b25cd4|0x0000000103e42dd4",
        stack_module_bases="0x0000000234b25000|0x0000000102f1c000",
    )
    assert result == _RAW_STACK_4_FRAMES
