"""native(4.0) 符号包接入单测（2026-07-14）。

背景：之前以为 native 符号化全靠 Datadog 服务端完成，github_symbols.py 的本地重符号
化路径对 native 是 no-op 占位——今天实测证伪（Android r8-map-id 占位符 / iOS 原始地址
栈均未解析）。symbol 包实际发布在独立仓 Plaud-AI/plaud-native-app 的 Release assets
里：Android 资产名与 flutter 相同但 tar 内部目录结构不同（没有 global_apk 前缀），
iOS 资产名不同（Plaud-Global.dSYMs.zip）。
"""
from __future__ import annotations

from app.crashguard.services import github_symbols as G
from app.crashguard.services import symbolication as S


def test_native_lib_tar_member_matches_flutter_layout():
    # flutter: global_apk/merged_native_libs/.../arm64-v8a/libflutter.so
    name = "global_apk/merged_native_libs/globalRelease/out/lib/arm64-v8a/libflutter.so"
    assert G._is_native_lib_tar_member(name, ["libflutter.so", "libapp.so"])


def test_native_lib_tar_member_matches_native_layout_without_global_apk_prefix():
    # native: merged_native_libs/globalRelease/mergeGlobalReleaseNativeLibs/out/lib/arm64-v8a/libapp.so
    name = (
        "native_symbols/merged_native_libs/globalRelease/mergeGlobalReleaseNativeLibs/"
        "out/lib/arm64-v8a/libapp.so"
    )
    assert G._is_native_lib_tar_member(name, ["libflutter.so", "libapp.so"])


def test_native_lib_tar_member_rejects_stripped_variant():
    # native tar 同时打包了 stripped_native_libs（release 产物，已去 debug_info）——
    # 子串不含 "merged_native_libs"，不应被选中（会被 stripped 版本的 addr2line 解不出符号）
    name = (
        "native_symbols/stripped_native_libs/globalRelease/stripGlobalReleaseDebugSymbols/"
        "out/lib/arm64-v8a/libapp.so"
    )
    assert not G._is_native_lib_tar_member(name, ["libflutter.so", "libapp.so"])


def test_native_lib_tar_member_rejects_non_arm64():
    name = "global_apk/merged_native_libs/globalRelease/out/lib/armeabi-v7a/libflutter.so"
    assert not G._is_native_lib_tar_member(name, ["libflutter.so", "libapp.so"])


def test_native_lib_tar_member_rejects_off_allowlist():
    name = "native_symbols/merged_native_libs/globalRelease/out/lib/arm64-v8a/libonnxruntime.so"
    assert not G._is_native_lib_tar_member(name, ["libflutter.so", "libapp.so"])


async def test_symbolicate_with_github_ios_picks_native_dsym_asset(monkeypatch):
    captured = {}

    async def fake_get_ios_dsyms_dir(app_version, repo=G._DEFAULT_REPO, asset_name=G._ASSET_IOS_DSYM):
        captured["repo"] = repo
        captured["asset_name"] = asset_name
        return None

    monkeypatch.setattr(G, "get_ios_dsyms_dir", fake_get_ios_dsyms_dir)
    await S._symbolicate_with_github(
        "some stack", "ios", "4.0.100-905",
        symbol_profile="native_ios", github_repo="Plaud-AI/plaud-native-app",
    )
    assert captured["asset_name"] == G._ASSET_IOS_DSYM_NATIVE
    assert captured["repo"] == "Plaud-AI/plaud-native-app"


async def test_symbolicate_with_github_ios_picks_flutter_dsym_asset(monkeypatch):
    captured = {}

    async def fake_get_ios_dsyms_dir(app_version, repo=G._DEFAULT_REPO, asset_name=G._ASSET_IOS_DSYM):
        captured["asset_name"] = asset_name
        return None

    monkeypatch.setattr(G, "get_ios_dsyms_dir", fake_get_ios_dsyms_dir)
    await S._symbolicate_with_github(
        "some stack", "ios", "3.18.0-708",
        symbol_profile="flutter_ios", github_repo="Plaud-AI/Plaud-App",
    )
    assert captured["asset_name"] == G._ASSET_IOS_DSYM
