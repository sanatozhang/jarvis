"""native(4.0) 符号包接入单测（2026-07-14）。

背景：之前以为 native 符号化全靠 Datadog 服务端完成，github_symbols.py 的本地重符号
化路径对 native 是 no-op 占位——今天实测证伪（Android r8-map-id 占位符 / iOS 原始地址
栈均未解析）。symbol 包实际发布在独立仓 Plaud-AI/plaud-native-app 的 Release assets
里：Android 资产名与 flutter 相同但 tar 内部目录结构不同（没有 global_apk 前缀），
iOS 资产名不同（Plaud-Global.dSYMs.zip）。
"""
from __future__ import annotations

import io
import plistlib
import zipfile

import httpx

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


# ── 2026-07-24：native「按真实 build 号匹配」修复的单测 ──────────────────────────
#
# 背景见 backend/app/crashguard/services/github_symbols.py 里 "native(4.0) 按真实
# build 号精确匹配 GitHub release" 那一节注释：native app 仓库的 tag 后缀 `+NNN`
# 是粗粒度冻结号，不是真实 app build 号；真实 build 号要么在 Android .aab 资产名
# 里，要么在 dSYM 包自带 Info.plist 的 CFBundleVersion 里。


# ---- 纯函数：_parse_semver_build / _aab_build_from_assets ----------------------

def test_parse_semver_build_basic():
    assert G._parse_semver_build("4.0.100-950") == ("4.0.100", "950")


def test_parse_semver_build_rejects_build_id_uuid():
    # Android jank 的 symbol_key 是 build_id UUID，最后一段含十六进制字母 a-f，
    # rsplit("-", 1) 拿到的尾段 .isdigit() 天然为 False。
    assert G._parse_semver_build("196bae40-11fd-3a88-bfb8-e2dc315b3bbb") is None


def test_parse_semver_build_rejects_no_dash():
    assert G._parse_semver_build("nodash") is None


def test_parse_semver_build_rejects_empty_string():
    assert G._parse_semver_build("") is None


def test_aab_build_from_assets_extracts_build():
    assets = [
        {"name": "mapping_globalRelease.txt"},
        {"name": "PLAUD_v4.0.100_950_globalRelease.aab"},
    ]
    assert G._aab_build_from_assets(assets) == "950"


def test_aab_build_from_assets_returns_none_when_no_aab_present():
    # 实测约 3/18 个 release 没有 aab 资产（如 v4.0.100+999-2026_07_23-163004-global）
    assets = [
        {"name": "mapping_globalRelease.txt"},
        {"name": "Plaud-Global.dSYMs.zip"},
    ]
    assert G._aab_build_from_assets(assets) is None


def test_aab_build_from_assets_handles_empty_list():
    assert G._aab_build_from_assets([]) is None


# ---- find_release_tag(match_by_build=True) ------------------------------------

class _FakeReleasesResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeReleasesClient:
    """模拟 GitHub `GET /repos/{repo}/releases` 分页列表接口。"""

    def __init__(self, pages: dict):
        self._pages = pages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        page = (params or {}).get("page", 1)
        return _FakeReleasesResponse(self._pages.get(page, []))


async def test_find_release_tag_match_by_build_hits_exact_build(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    pages = {
        1: [
            {
                # tag 后缀 +999 只是粗粒度冻结号，真实 build 在 aab 文件名里 (950)
                "tag_name": "v4.0.100+999-2026_07_22-163327-global",
                "assets": [{"name": "PLAUD_v4.0.100_950_globalRelease.aab"}],
            },
            {
                "tag_name": "v4.0.100+999-2026_07_20-000000-global",
                "assets": [{"name": "PLAUD_v4.0.100_919_globalRelease.aab"}],
            },
        ],
        2: [],
        3: [],
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeReleasesClient(pages))

    tag = await G.find_release_tag(
        "4.0.100-950", repo="Plaud-AI/plaud-native-app", match_by_build=True,
    )
    assert tag == "v4.0.100+999-2026_07_22-163327-global"

    # 命中应写 `.release_tag` 正向缓存
    cache_file = tmp_path / "symbols" / "github_cache" / "4.0.100-950" / ".release_tag"
    assert cache_file.exists()
    assert cache_file.read_text().strip() == "v4.0.100+999-2026_07_22-163327-global"


async def test_find_release_tag_match_by_build_miss_returns_none_and_does_not_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # 4.0.201 系列真实 build 上限只到 917，找不到 951——不该回落到任何一个无关 release
    pages = {
        1: [
            {
                "tag_name": "v4.0.201+813-2026_06_01-000000-global",
                "assets": [{"name": "PLAUD_v4.0.201_917_globalRelease.aab"}],
            },
        ],
        2: [],
        3: [],
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeReleasesClient(pages))

    # allow_fallback=True 也不应生效——match_by_build 永远忽略它
    tag = await G.find_release_tag(
        "4.0.201-951", repo="Plaud-AI/plaud-native-app", allow_fallback=True, match_by_build=True,
    )
    assert tag is None

    # miss 不写 `.release_tag`，避免"还没发布"的 build 以后补发了也被永久钉死
    cache_file = tmp_path / "symbols" / "github_cache" / "4.0.201-951" / ".release_tag"
    assert not cache_file.exists()


# ---- _read_dsym_build_via_range: 206 支持 / 200 不支持 Range 两种分支 ----------

def _build_test_dsym_zip(build: str = "954", short_version: str = "4.0.100") -> bytes:
    """内存构造一个含 `X.app.dSYM/Contents/Info.plist` 的小 zip，模拟真实 dSYM 包结构。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        plist_bytes = plistlib.dumps({
            "CFBundleVersion": build,
            "CFBundleShortVersionString": short_version,
        })
        zf.writestr("Plaud-Global.app.dSYM/Contents/Info.plist", plist_bytes)
        # 塞一个体积较大的无关成员，模拟真实 dSYM 包里还有 DWARF 符号数据
        zf.writestr("Plaud-Global.app.dSYM/Contents/Resources/DWARF/Plaud-Global", b"\x00" * 4096)
    return buf.getvalue()


class _FakeRangeResponse:
    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self._content = content

    async def aread(self):
        return self._content

    async def aclose(self):
        return None


class _FakeRangeStreamCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeRangeClient:
    """模拟 GitHub release asset 302 到（不）支持 HTTP Range 的 blob 存储。"""

    def __init__(self, data: bytes, supports_range: bool):
        self._data = data
        self._supports_range = supports_range

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None):
        headers = headers or {}
        if not self._supports_range:
            # 服务端不支持 Range，退化为普通 200 整包响应
            return _FakeRangeStreamCtx(_FakeRangeResponse(200, self._data))
        range_header = headers.get("Range")
        assert range_header is not None
        start_s, end_s = range_header.split("=", 1)[1].split("-")
        start, end = int(start_s), min(int(end_s), len(self._data) - 1)
        return _FakeRangeStreamCtx(_FakeRangeResponse(206, self._data[start:end + 1]))


async def test_read_dsym_build_via_range_server_supports_range(monkeypatch):
    data = _build_test_dsym_zip(build="954")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeRangeClient(data, supports_range=True))

    build = await G._read_dsym_build_via_range(
        asset_id=123, asset_size=len(data), repo="Plaud-AI/plaud-native-app", headers={},
    )
    assert build == "954"


async def test_read_dsym_build_via_range_server_does_not_support_range(monkeypatch):
    data = _build_test_dsym_zip(build="954")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeRangeClient(data, supports_range=False))

    build = await G._read_dsym_build_via_range(
        asset_id=123, asset_size=len(data), repo="Plaud-AI/plaud-native-app", headers={},
    )
    # 200（不支持 Range）→ 干净返回 None，绝不整包下载兜底
    assert build is None


# ---- get_ios_dsyms_dir 解压后 build 校验闸门 -----------------------------------

async def test_get_ios_dsyms_dir_native_rejects_build_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def fake_find_release_tag(app_version, allow_fallback=True, repo=G._DEFAULT_REPO, match_by_build=False):
        assert match_by_build is True  # native 分支必须传 match_by_build=True
        return "v4.0.100+999-2026_07_23-163004-global"

    monkeypatch.setattr(G, "find_release_tag", fake_find_release_tag)

    # 目标 build 是 954，但 GitHub release 里实际下载到的 dSYM 是 950 的
    # （模拟"iOS/Android 同 release 共用 build"这条假设万一被打破的场景）
    wrong_build_zip = _build_test_dsym_zip(build="950")

    async def fake_download_asset(tag, asset_name, dest, repo=G._DEFAULT_REPO):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(wrong_build_zip)
        return dest

    monkeypatch.setattr(G, "_download_asset", fake_download_asset)

    result = await G.get_ios_dsyms_dir(
        "4.0.100-954", repo="Plaud-AI/plaud-native-app", asset_name=G._ASSET_IOS_DSYM_NATIVE,
    )
    assert result is None


async def test_get_ios_dsyms_dir_native_accepts_build_match(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def fake_find_release_tag(app_version, allow_fallback=True, repo=G._DEFAULT_REPO, match_by_build=False):
        assert match_by_build is True
        return "v4.0.100+999-2026_07_23-163004-global"

    monkeypatch.setattr(G, "find_release_tag", fake_find_release_tag)

    correct_build_zip = _build_test_dsym_zip(build="954")

    async def fake_download_asset(tag, asset_name, dest, repo=G._DEFAULT_REPO):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(correct_build_zip)
        return dest

    monkeypatch.setattr(G, "_download_asset", fake_download_asset)

    result = await G.get_ios_dsyms_dir(
        "4.0.100-954", repo="Plaud-AI/plaud-native-app", asset_name=G._ASSET_IOS_DSYM_NATIVE,
    )
    assert result is not None
    assert result.endswith(G._ASSET_IOS_DSYM_NATIVE)
