"""symbolication.py::_symbolicate_ios_with_dir() module-name gating 单测（2026-07-22）。

背景：生产环境实测发现 _symbolicate_ios_with_dir 对堆栈里的每一帧都无条件套用
下载到的 App dSYM，不管这一帧的 module 是不是真的属于这个 dSYM——libsystem_kernel.dylib
/ BoardServices / ActivityKit 等系统库帧因此被"符号化"成了 App 自己代码里完全无关的
Swift 函数（issue 524e25c6-59a4-11f1-bd6b-da7ad0900002 实测，20+ 帧全部误命中）。

修复：只对 module 名能在下载到的 dSYM 里找到同名 DWARF 二进制的帧发起 atos/llvm-symbolizer
查询；查不到对应 module 的帧原样保留地址。
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _make_dsym(root: Path, binary_name: str) -> None:
    dwarf_dir = root / f"{binary_name}.app.dSYM" / "Contents" / "Resources" / "DWARF"
    dwarf_dir.mkdir(parents=True, exist_ok=True)
    (dwarf_dir / binary_name).write_text("fake dwarf", encoding="utf-8")


def test_only_matching_module_frame_gets_symbolicated(monkeypatch, tmp_path):
    from app.crashguard.services import symbolication as sym

    _make_dsym(tmp_path, "Plaud-Global")

    monkeypatch.setattr(sym, "_ATOS", None)
    monkeypatch.setattr(sym, "_IS_LLVM_SYMBOLIZER", True)
    monkeypatch.setattr(sym, "_ADDR2LINE", "/usr/bin/llvm-symbolizer")

    calls = []

    def fake_atos_lookup(dwarf_path, load_addr, addr):
        calls.append((dwarf_path, load_addr, addr))
        # 就算被（错误地）调用，也返回一个看起来合理的符号，以此证明系统库帧
        # 之所以没被替换是因为根本没发起查询，而不是恰好查询失败。
        return "SomeApp.function(...) File.swift:1"

    monkeypatch.setattr(sym, "_atos_lookup", fake_atos_lookup)

    stack = (
        "0   libsystem_kernel.dylib   0x0000000234b25cd4   0x0000000234b25000 + 3284\n"
        "1   Plaud-Global   0x0000000103e42dd4   0x0000000102f1c000 + 15887828\n"
        "2   BoardServices   0x0000000185d9839c   0x0000000185d35000 + 407452\n"
    )

    result = sym._symbolicate_ios_with_dir(stack, str(tmp_path))
    lines = result.splitlines()

    # 系统库帧原样保留（未发起 atos/llvm-symbolizer 查询）
    assert "libsystem_kernel.dylib   0x0000000234b25cd4   0x0000000234b25000 + 3284" in lines[0]
    assert "BoardServices   0x0000000185d9839c   0x0000000185d35000 + 407452" in lines[2]

    # 只有 module 名匹配 dSYM 的那一帧被替换
    assert lines[1] == "1   Plaud-Global   SomeApp.function(...) File.swift:1"
    assert len(calls) == 1
    assert calls[0][1:] == ("0x0000000102f1c000", "0x0000000103e42dd4")


def test_no_dsym_matches_any_module_returns_stack_unchanged(monkeypatch, tmp_path):
    from app.crashguard.services import symbolication as sym

    _make_dsym(tmp_path, "Plaud-Global")
    monkeypatch.setattr(sym, "_ATOS", None)
    monkeypatch.setattr(sym, "_IS_LLVM_SYMBOLIZER", True)
    monkeypatch.setattr(sym, "_ADDR2LINE", "/usr/bin/llvm-symbolizer")

    def boom(*args, **kwargs):
        raise AssertionError("should not query atos when no module matches")

    monkeypatch.setattr(sym, "_atos_lookup", boom)

    stack = "0   libsystem_kernel.dylib   0x0000000234b25cd4   0x0000000234b25000 + 3284\n"
    result = sym._symbolicate_ios_with_dir(stack, str(tmp_path))
    assert result == stack
