"""Unit tests for stack_path_resolver — 验证栈帧 token 抽取 + Glob 命中过滤。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.crashguard.services.stack_path_resolver import (
    _extract_tokens,
    format_stack_paths_block,
    resolve_stack_paths,
)


def test_extract_dart_package_token():
    stack = "package:plaud_flutter_common/app/data/foo.dart:123:7\nat _foo"
    tokens = _extract_tokens(stack, "flutter")
    assert len(tokens) == 1
    assert tokens[0]["kind"] == "dart_full"
    assert tokens[0]["token"] == "app/data/foo.dart"
    assert tokens[0]["package"] == "plaud_flutter_common"
    assert tokens[0]["line"] == 123


def test_extract_basename_token_ios():
    stack = "0   PlaudIOS    0x000  -[LoginVC viewDidLoad] LoginVC.m:42"
    tokens = _extract_tokens(stack, "ios")
    # PlaudIOS 包含 P 大写后接小写但没扩展，不会匹配；LoginVC.m 命中
    names = [t["token"] for t in tokens]
    assert "LoginVC.m" in names


def test_extract_dedup_across_frames():
    stack = (
        "package:foo/bar.dart:1\n"
        "package:foo/bar.dart:2\n"
        "Baz.kt:3\n"
        "Baz.kt:4\n"
    )
    tokens = _extract_tokens(stack, "flutter")
    # Dart 路径只抓一次；Baz.kt 基础名只抓一次
    tokenset = {t["token"] for t in tokens}
    assert "bar.dart" in tokenset
    assert "Baz.kt" in tokenset
    assert len(tokenset) == 2


def test_resolve_against_fake_workspace(tmp_path: Path):
    """构造一个 fake workspace/code/<repo>/lib/ 结构，验证 rglob 命中 + 噪声过滤。"""
    ws = tmp_path / "ws"
    repo = ws / "code" / "plaud-flutter-common" / "lib" / "app" / "data"
    repo.mkdir(parents=True)
    (repo / "auth_interceptor.dart").write_text("// fake")
    # 噪声：build/ 目录里同名文件不能被算上
    noise = ws / "code" / "plaud-flutter-common" / "build" / "generated"
    noise.mkdir(parents=True)
    (noise / "auth_interceptor.dart").write_text("// noise")

    stack = "package:plaud_flutter_common/app/data/auth_interceptor.dart:42"
    resolved = resolve_stack_paths(stack, "flutter", ws)
    assert len(resolved) == 1
    r = resolved[0]
    assert r["candidates"] == ["plaud-flutter-common/lib/app/data/auth_interceptor.dart"]
    assert r["hits"] == 1
    assert r["line"] == 42


def test_resolve_empty_when_no_code_root(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # code/ 不存在 → 直接返回空
    resolved = resolve_stack_paths("package:x/y.dart:1", "flutter", ws)
    assert resolved == []


def test_format_block_warns_when_all_missing(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "code").mkdir(parents=True)  # empty code root
    stack = "Foo.dart\nBar.kt"
    resolved = resolve_stack_paths(stack, "flutter", ws)
    text = format_stack_paths_block(resolved)
    # 没有命中 → 块里要提示 agent 不要造路径
    assert "未在仓库中找到任何实存文件" in text or "未命中" in text


def test_format_block_empty_when_no_tokens():
    assert format_stack_paths_block([]) == ""
