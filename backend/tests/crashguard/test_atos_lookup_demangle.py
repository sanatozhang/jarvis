"""_atos_lookup() 的 Linux llvm-symbolizer fallback 必须带 --demangle（2026-07-24）。

背景：Android 侧 addr2line 调用一直带 -C demangle（symbolication.py 里 GNU
addr2line 命令），但 iOS 侧 Linux 环境下用的 llvm-symbolizer fallback 漏了这个
参数，导致 Swift/C++ 符号原样吐出 mangled 名字（如
`$sSSSHsSH13_rawHashValue4seedS2i_tFTW`），不可读也难以判断是不是编译器生成代码。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import app.crashguard.services.symbolication as sym


def test_llvm_symbolizer_ios_fallback_includes_demangle_flag(monkeypatch):
    monkeypatch.setattr(sym, "_ATOS", None)
    monkeypatch.setattr(sym, "_IS_LLVM_SYMBOLIZER", True)
    monkeypatch.setattr(sym, "_ADDR2LINE", "/usr/bin/llvm-symbolizer")

    fake_run = MagicMock(return_value=MagicMock(stdout="SomeClass.someMethod\n/src/SomeClass.swift:10:5\n"))
    monkeypatch.setattr(sym.subprocess, "run", fake_run)

    result = sym._atos_lookup("/tmp/fake.dSYM/DWARF/App", "0x100000000", "0x100001000")

    assert result == "SomeClass.someMethod /src/SomeClass.swift:10:5"
    called_cmd = fake_run.call_args[0][0]
    assert "--demangle" in called_cmd
