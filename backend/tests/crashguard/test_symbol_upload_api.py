"""upload_symbol_package 校验单测（2026-07-22）：native_symbols 类型 + 压缩包完整性。"""
from __future__ import annotations

import io

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy.ext.asyncio import async_sessionmaker


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


@pytest.mark.asyncio
async def test_upload_rejects_corrupt_zip_for_dsym_type(patched_session, monkeypatch, tmp_path):
    from app.crashguard.api.crash import upload_symbol_package

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    upload = UploadFile(io.BytesIO(b"this is not a real zip"), filename="broken.zip")

    with pytest.raises(HTTPException) as exc_info:
        await upload_symbol_package(
            platform="ios", app_version="4.0.201-941", symbol_type="dsym", file=upload, keep_versions=10,
        )
    assert exc_info.value.status_code == 400
    # 校验失败不能留残留文件
    dest_dir = tmp_path / "symbols" / "ios" / "dsym" / "4.0.201-941"
    assert not dest_dir.exists() or not any(dest_dir.iterdir())


@pytest.mark.asyncio
async def test_upload_accepts_native_symbols_type_with_valid_targz(patched_session, monkeypatch, tmp_path):
    import tarfile

    from app.crashguard.api.crash import upload_symbol_package

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="libapp.so")
        content = b"fake-so-bytes"
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    tar_buf.seek(0)
    upload = UploadFile(tar_buf, filename="native_symbols.tar.gz")

    result = await upload_symbol_package(
        platform="android", app_version="4.0.201-941", symbol_type="native_symbols", file=upload, keep_versions=10,
    )
    assert result["symbol_type"] == "native_symbols"
    dest = tmp_path / "symbols" / "android" / "native_symbols" / "4.0.201-941" / "native_symbols.tar.gz"
    assert dest.exists()


@pytest.mark.asyncio
async def test_upload_rejects_unknown_symbol_type(patched_session, monkeypatch, tmp_path):
    from app.crashguard.api.crash import upload_symbol_package

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    upload = UploadFile(io.BytesIO(b"whatever"), filename="x.txt")

    with pytest.raises(HTTPException) as exc_info:
        await upload_symbol_package(
            platform="android", app_version="4.0.201-941", symbol_type="not_a_real_type", file=upload, keep_versions=10,
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_proguard_mapping_skips_archive_check(patched_session, monkeypatch, tmp_path):
    """proguard_mapping 是纯文本，即使内容"不是压缩包"也要正常入库（不做格式校验）。"""
    from app.crashguard.api.crash import upload_symbol_package

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    upload = UploadFile(io.BytesIO(b"com.original.Class -> a.b.C:\n"), filename="mapping.txt")

    result = await upload_symbol_package(
        platform="android", app_version="4.0.201-941", symbol_type="proguard_mapping", file=upload, keep_versions=10,
    )
    assert result["symbol_type"] == "proguard_mapping"


@pytest.mark.asyncio
async def test_corrupt_reupload_does_not_overwrite_previously_valid_file(patched_session, monkeypatch, tmp_path):
    """同 platform/symbol_type/app_version/filename 先成功上传一个有效 zip，再用同名文件
    上传损坏内容——损坏上传必须被 400 拒绝，且不能破坏掉之前那份已验证有效的文件
    （写入应该先落临时路径校验通过后才原子替换，不能先覆盖 dest_path 再校验）。"""
    import zipfile

    from app.crashguard.api.crash import upload_symbol_package

    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("foo.dSYM/x", b"debug-info")
    valid_bytes = zip_buf.getvalue()
    upload1 = UploadFile(io.BytesIO(valid_bytes), filename="Plaud.dSYMs.zip")

    await upload_symbol_package(
        platform="ios", app_version="4.0.201-941", symbol_type="dsym", file=upload1, keep_versions=10,
    )
    dest_path = tmp_path / "symbols" / "ios" / "dsym" / "4.0.201-941" / "Plaud.dSYMs.zip"
    assert dest_path.exists()
    assert dest_path.read_bytes() == valid_bytes

    upload2 = UploadFile(io.BytesIO(b"not a zip at all"), filename="Plaud.dSYMs.zip")
    with pytest.raises(HTTPException) as exc_info:
        await upload_symbol_package(
            platform="ios", app_version="4.0.201-941", symbol_type="dsym", file=upload2, keep_versions=10,
        )
    assert exc_info.value.status_code == 400

    # 原来有效的文件必须原样保留——没被损坏上传的内容覆盖，也没被删除
    assert dest_path.exists()
    assert dest_path.read_bytes() == valid_bytes
    # 也不能残留 .part 临时文件
    assert not (tmp_path / "symbols" / "ios" / "dsym" / "4.0.201-941" / "Plaud.dSYMs.zip.part").exists()
