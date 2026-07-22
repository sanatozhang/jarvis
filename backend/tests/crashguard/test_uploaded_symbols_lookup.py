"""github_symbols.py 已上传包查找基础设施单测（2026-07-22）。"""
from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path


def test_uploaded_package_dir_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    assert G._uploaded_package_dir("ios", "dsym", "4.0.201-941") is None


def test_uploaded_package_dir_returns_none_when_empty_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    empty_dir = tmp_path / "symbols" / "ios" / "dsym" / "4.0.201-941"
    empty_dir.mkdir(parents=True)
    assert G._uploaded_package_dir("ios", "dsym", "4.0.201-941") is None


def test_uploaded_package_dir_returns_path_when_nonempty(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    d = tmp_path / "symbols" / "ios" / "dsym" / "4.0.201-941"
    d.mkdir(parents=True)
    (d / "PLAUD.dSYMs.zip").write_bytes(b"fake")

    result = G._uploaded_package_dir("ios", "dsym", "4.0.201-941")
    assert result == d


async def test_get_extract_lock_returns_same_instance_for_same_key():
    from app.crashguard.services import github_symbols as G

    lock1 = await G._get_extract_lock("ios", "dsym", "4.0.201-941")
    lock2 = await G._get_extract_lock("ios", "dsym", "4.0.201-941")
    assert lock1 is lock2


async def test_get_extract_lock_returns_different_instance_for_different_key():
    from app.crashguard.services import github_symbols as G

    lock1 = await G._get_extract_lock("ios", "dsym", "4.0.201-941")
    lock2 = await G._get_extract_lock("android", "native_symbols", "4.0.201-941")
    assert lock1 is not lock2


def _write_fake_dsym_zip(zip_path: Path, binary_name: str) -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{binary_name}.app.dSYM/Contents/Resources/DWARF/{binary_name}", "fake dwarf")


async def test_get_ios_dsyms_dir_prefers_uploaded_package_over_github(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    upload_dir = tmp_path / "symbols" / "ios" / "dsym" / "4.0.201-941"
    upload_dir.mkdir(parents=True)
    _write_fake_dsym_zip(upload_dir / "PLAUD.dSYMs.zip", "Plaud-Global")

    async def boom(*args, **kwargs):
        raise AssertionError("should not hit GitHub when an uploaded package matches exactly")

    monkeypatch.setattr(G, "find_release_tag", boom)

    result = await G.get_ios_dsyms_dir("4.0.201-941")
    assert result is not None
    assert (Path(result) / "Plaud-Global.app.dSYM" / "Contents" / "Resources" / "DWARF" / "Plaud-Global").exists()


async def test_get_ios_dsyms_dir_reuses_extracted_cache_on_second_call(monkeypatch, tmp_path):
    """第二次调用命中 .extracted marker，不重新解压 zip（幂等，避免重复 I/O）。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    upload_dir = tmp_path / "symbols" / "ios" / "dsym" / "4.0.201-941"
    upload_dir.mkdir(parents=True)
    _write_fake_dsym_zip(upload_dir / "PLAUD.dSYMs.zip", "Plaud-Global")

    async def boom(*args, **kwargs):
        raise AssertionError("should not hit GitHub when an uploaded package matches exactly")

    monkeypatch.setattr(G, "find_release_tag", boom)

    first = await G.get_ios_dsyms_dir("4.0.201-941")

    import zipfile as _zf
    original_zipfile = _zf.ZipFile

    def boom_zipfile(*args, **kwargs):
        raise AssertionError("should not re-extract on second call")

    monkeypatch.setattr(_zf, "ZipFile", boom_zipfile)
    second = await G.get_ios_dsyms_dir("4.0.201-941")
    monkeypatch.setattr(_zf, "ZipFile", original_zipfile)

    assert first == second


async def test_get_ios_dsyms_dir_falls_back_to_github_when_no_uploaded_package(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    called = {}

    async def fake_find_release_tag(app_version, allow_fallback=True, repo=G._DEFAULT_REPO):
        called["hit"] = True
        return None

    monkeypatch.setattr(G, "find_release_tag", fake_find_release_tag)

    result = await G.get_ios_dsyms_dir("4.0.999-1")
    assert result is None
    assert called.get("hit") is True


async def test_get_android_mapping_prefers_uploaded_over_github(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    upload_dir = tmp_path / "symbols" / "android" / "proguard_mapping" / "4.0.201-941"
    upload_dir.mkdir(parents=True)
    (upload_dir / "mapping_globalRelease.txt").write_text("com.a.b -> a.b.C:\n")

    async def boom(*args, **kwargs):
        raise AssertionError("should not hit GitHub when uploaded mapping matches exactly")

    monkeypatch.setattr(G, "find_release_tag", boom)

    result = await G.get_android_mapping("4.0.201-941")
    assert result == str(upload_dir / "mapping_globalRelease.txt")


async def test_get_android_mapping_falls_back_to_github_when_no_uploaded_package(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    called = {}

    async def fake_find_release_tag(app_version, allow_fallback=True, repo=G._DEFAULT_REPO):
        called["hit"] = True
        return None

    monkeypatch.setattr(G, "find_release_tag", fake_find_release_tag)

    result = await G.get_android_mapping("4.0.999-1")
    assert result is None
    assert called.get("hit") is True


def _write_fake_dart_symbols_tar(tar_path: Path) -> None:
    content = b"fake dart symbols"
    info = tarfile.TarInfo(name="app.android-arm64.symbols")
    info.size = len(content)
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.addfile(info, io.BytesIO(content))


async def test_get_dart_symbols_dir_prefers_uploaded_over_github(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    upload_dir = tmp_path / "symbols" / "flutter" / "dart_symbols" / "4.0.201-941"
    upload_dir.mkdir(parents=True)
    _write_fake_dart_symbols_tar(upload_dir / "flutter_symbols.tar.gz")

    async def boom(*args, **kwargs):
        raise AssertionError("should not hit GitHub when uploaded dart_symbols matches exactly")

    monkeypatch.setattr(G, "find_release_tag", boom)

    result = await G.get_dart_symbols_dir("4.0.201-941")
    assert result is not None
    assert (Path(result) / "app.android-arm64.symbols").exists()


async def test_get_dart_symbols_dir_falls_back_to_github_when_no_uploaded_package(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    called = {}

    async def fake_find_release_tag(app_version, allow_fallback=True, repo=G._DEFAULT_REPO):
        called["hit"] = True
        return None

    monkeypatch.setattr(G, "find_release_tag", fake_find_release_tag)

    result = await G.get_dart_symbols_dir("4.0.999-1")
    assert result is None
    assert called.get("hit") is True


def _write_fake_native_symbols_tar(tar_path: Path) -> None:
    names_and_content = {
        "merged_native_libs/globalRelease/out/lib/arm64-v8a/libflutter.so": b"flutter-debug",
        "merged_native_libs/globalRelease/out/lib/arm64-v8a/libapp.so": b"app-debug",
        "merged_native_libs/globalRelease/out/lib/armeabi-v7a/libflutter.so": b"skip-arch",
        "stripped_native_libs/globalRelease/out/lib/arm64-v8a/libapp.so": b"skip-stripped",
    }
    with tarfile.open(tar_path, "w:gz") as tf:
        for name, content in names_and_content.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))


async def test_get_android_native_symbols_dir_prefers_uploaded_over_github(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    upload_dir = tmp_path / "symbols" / "android" / "native_symbols" / "4.0.201-941"
    upload_dir.mkdir(parents=True)
    _write_fake_native_symbols_tar(upload_dir / "native_symbols.tar.gz")

    async def boom(*args, **kwargs):
        raise AssertionError("should not hit GitHub when uploaded native_symbols matches exactly")

    monkeypatch.setattr(G, "find_release_tag", boom)

    result = await G.get_android_native_symbols_dir("4.0.201-941")
    assert result is not None
    # 检查文件总数：fixture 写了 4 个 tar member，但过滤后只应有 2 个
    # (merged_native_libs/arm64-v8a/libflutter.so 和 merged_native_libs/arm64-v8a/libapp.so)
    # 另外 2 个被过滤掉（armeabi-v7a 和 stripped_native_libs）
    extracted = list(Path(result).rglob("*.so"))
    assert len(extracted) == 2, f"Expected 2 .so files after filtering, got {len(extracted)}: {extracted}"
    names = {p.name for p in extracted}
    # 只保留 arm64-v8a + merged_native_libs 下的（与现有 _is_native_lib_tar_member 一致）
    assert names == {"libflutter.so", "libapp.so"}


async def test_get_android_native_symbols_dir_falls_back_to_github_when_no_uploaded_package(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.crashguard.services import github_symbols as G

    called = {}

    async def fake_find_release_tag(app_version, allow_fallback=True, repo=G._DEFAULT_REPO):
        called["hit"] = True
        return None

    monkeypatch.setattr(G, "find_release_tag", fake_find_release_tag)

    result = await G.get_android_native_symbols_dir("4.0.999-1")
    assert result is None
    assert called.get("hit") is True
