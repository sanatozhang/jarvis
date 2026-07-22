# 打包机上传符号表 + 符号化优先级改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打包机(Jenkins, plaud-native-app2)在打包完成后把 iOS dSYM / Android ProGuard mapping / Android native `.so` 直接上传到 jarvis（同机内网，不经 GitHub/VPN），jarvis 符号化查找路径优先使用这些已上传包，查不到精确版本才回退现有 GitHub 下载逻辑；同时新增 jank issue 占位符堆栈回填机制。

**Architecture:** jarvis 后端 `services/github_symbols.py` 的 4 个 asset getter（`get_ios_dsyms_dir` / `get_android_mapping` / `get_dart_symbols_dir` / `get_android_native_symbols_dir`）各自在函数开头插入一次"查本地已上传包，精确 `(platform, symbol_type, app_version)` 匹配"，命中则直接返回，不命中则原样走后面已有的 GitHub 下载逻辑（调用方 `symbolication.py` 完全无需改动）。已有的 `POST /api/crash/symbols/upload` 补上压缩包完整性校验 + `native_symbols` 类型。新增一个独立 cron job 定期回填占位符堆栈的 jank issue。`plaud-native-app2` 仓库新增 `upload_jarvis_symbols()`，用 curl 把打包产物 POST 到同机 jarvis。

**Tech Stack:** Python 3 (FastAPI + SQLAlchemy async, asyncio.Lock 并发控制, zipfile/tarfile), Bash（Jenkins packaging script）, pytest + pytest-asyncio。

## Global Constraints

- 版本号格式必须对齐：上传/查找 key 用 `app_version`，格式与 Datadog `@application.version` / `CrashIssue.last_seen_version` 一致（`4.0.201-941`，dash 分隔），**不是** GitHub tag 格式（`v4.0.201+941-...`，plus 分隔）。打包脚本侧要把 `staging_version_tag`（`v4.0.201+941`）转换：去掉前缀 `v`，`+` 替换成 `-`。
- 只做 Global flavor；`plaud-native-app-publish-cn.sh` 不改，CN 崩溃符号化现状不变。
- 精确匹配、不做模糊/最近版本回退——查不到就走原有 GitHub 逻辑（GitHub 那条路径自己已有 fallback，不重复造）。
- 所有新代码遵循 crashguard「容错优先」风格：任何异常都不能影响主符号化流程或主构建流程，捕获后原样降级。
- 上传接口不加鉴权，沿用 jarvis 现有"内网工具、无逐接口鉴权"整体风格。
- crashguard 隔离合约（`backend/app/crashguard/CLAUDE.md`）：本计划所有 jarvis 后端改动都在 `app/crashguard/` 内部，不新增任何跨模块耦合点，不需要碰 `.importlinter`。

---

### Task 1: 上传 API — 新增 native_symbols 类型 + 压缩包完整性校验

**Files:**
- Modify: `backend/app/crashguard/api/crash.py:3379-3482`（`upload_symbol_package`）
- Test: `backend/tests/crashguard/test_symbol_upload_api.py`（新建）

**Interfaces:**
- Consumes: 无新依赖，标准库 `zipfile.is_zipfile` / `tarfile.is_tarfile`
- Produces: `POST /api/crash/symbols/upload` 现在接受 `symbol_type=native_symbols`；`symbol_type` 为 `{dsym, dart_symbols, native_symbols}` 时非法压缩包返回 `400` 且不留残留文件/DB 记录。这一行为被 Task 2-6 的"上传优先"逻辑依赖（保证 getter 侧读到的上传文件一定是能被 zipfile/tarfile 正常打开的）。

- [ ] **Step 1: 写失败测试**

```python
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
            platform="ios", app_version="4.0.201-941", symbol_type="dsym", file=upload,
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
        platform="android", app_version="4.0.201-941", symbol_type="native_symbols", file=upload,
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
            platform="android", app_version="4.0.201-941", symbol_type="not_a_real_type", file=upload,
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_proguard_mapping_skips_archive_check(patched_session, monkeypatch, tmp_path):
    """proguard_mapping 是纯文本，即使内容"不是压缩包"也要正常入库（不做格式校验）。"""
    from app.crashguard.api.crash import upload_symbol_package

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    upload = UploadFile(io.BytesIO(b"com.original.Class -> a.b.C:\n"), filename="mapping.txt")

    result = await upload_symbol_package(
        platform="android", app_version="4.0.201-941", symbol_type="proguard_mapping", file=upload,
    )
    assert result["symbol_type"] == "proguard_mapping"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && source .venv/bin/activate && pytest tests/crashguard/test_symbol_upload_api.py -v`
Expected: `test_upload_rejects_corrupt_zip_for_dsym_type` 和 `test_upload_accepts_native_symbols_type_with_valid_targz` 失败——当前 `VALID_TYPES` 不含 `native_symbols`，且没有任何压缩包校验逻辑（corrupt zip 会被当成合法内容原样入库，不会抛 400）。

- [ ] **Step 3: 实现**

在 `backend/app/crashguard/api/crash.py` 的 `upload_symbol_package` 函数内（约第 3405-3423 行），修改 `VALID_TYPES` 并插入完整性校验：

```python
    VALID_PLATFORMS = {"ios", "android", "flutter"}
    VALID_TYPES = {"dsym", "dart_symbols", "proguard_mapping", "native_symbols"}
    _ARCHIVE_TYPES = {"dsym", "dart_symbols", "native_symbols"}  # 这三种上传的是压缩包，需要完整性校验

    if platform not in VALID_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"platform must be one of {VALID_PLATFORMS}")
    if symbol_type not in VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"symbol_type must be one of {VALID_TYPES}")

    data_dir = _os.environ.get("DATA_DIR", "/data")
    dest_dir = _os.path.join(data_dir, "symbols", platform, symbol_type, app_version)
    _os.makedirs(dest_dir, exist_ok=True)

    original_name = file.filename or "upload.zip"
    dest_path = _os.path.join(dest_dir, original_name)

    content = await file.read()
    with open(dest_path, "wb") as f:
        f.write(content)

    if symbol_type in _ARCHIVE_TYPES:
        import zipfile as _zipfile
        import tarfile as _tarfile

        if not (_zipfile.is_zipfile(dest_path) or _tarfile.is_tarfile(dest_path)):
            _os.unlink(dest_path)
            if _os.path.isdir(dest_dir) and not _os.listdir(dest_dir):
                _os.rmdir(dest_dir)
            raise HTTPException(
                status_code=400,
                detail=f"uploaded file is not a valid zip/tar.gz archive (symbol_type={symbol_type})",
            )

    size_bytes = len(content)
```

（`size_bytes = len(content)` 原本紧跟在写文件之后，现在挪到校验之后；后续 `pkg_id = str(_uuid.uuid4())` 及 DB 写入逻辑不变。）

同时更新函数 docstring 里的"上传符号包（dSYM / dart_symbols / proguard_mapping）"为"上传符号包（dSYM / dart_symbols / proguard_mapping / native_symbols）"。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/crashguard/test_symbol_upload_api.py -v`
Expected: 4 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/api/crash.py backend/tests/crashguard/test_symbol_upload_api.py
git commit -m "feat(crashguard): 符号包上传接口新增 native_symbols 类型 + 压缩包完整性校验"
```

---

### Task 2: github_symbols.py — 已上传包查找的共享基础设施

**Files:**
- Modify: `backend/app/crashguard/services/github_symbols.py`（新增函数，插入位置：`_tag_cache_dir` 函数之后、`get_ios_dsyms_dir` 之前，约第 308 行）
- Test: `backend/tests/crashguard/test_uploaded_symbols_lookup.py`（新建）

**Interfaces:**
- Produces:
  - `_uploaded_symbols_root() -> Path`
  - `_uploaded_package_dir(platform: str, symbol_type: str, app_version: str) -> Optional[Path]`
  - `async _get_extract_lock(platform: str, symbol_type: str, app_version: str) -> asyncio.Lock`
  这三个函数是 Task 3-6 的共同依赖，命名和签名后续任务直接引用，不再重复定义。

- [ ] **Step 1: 写失败测试**

```python
"""github_symbols.py 已上传包查找基础设施单测（2026-07-22）。"""
from __future__ import annotations


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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/crashguard/test_uploaded_symbols_lookup.py -v`
Expected: `AttributeError: module 'app.crashguard.services.github_symbols' has no attribute '_uploaded_package_dir'`（等函数尚不存在）

- [ ] **Step 3: 实现**

在 `backend/app/crashguard/services/github_symbols.py` 的 `_tag_cache_dir` 函数（约第 298-307 行）之后插入：

```python
# ── 已上传符号包查找（打包机上传优先，GitHub 兜底）───────────────────────────
# 与 api/crash.py::upload_symbol_package 的落盘路径保持完全一致：
# <DATA_DIR>/symbols/<platform>/<symbol_type>/<app_version>/<原始文件名>

_EXTRACT_LOCKS: "dict[tuple[str, str, str], asyncio.Lock]" = {}
_EXTRACT_LOCK_GUARD = asyncio.Lock()


def _uploaded_symbols_root() -> Path:
    """与 api/crash.py::upload_symbol_package 的 dest_dir 解析方式保持一致
    （同样直接用 DATA_DIR 环境变量，默认 /data，不做额外可写性探测）。"""
    return Path(os.environ.get("DATA_DIR", "/data")) / "symbols"


def _uploaded_package_dir(platform: str, symbol_type: str, app_version: str) -> Optional[Path]:
    """精确匹配 (platform, symbol_type, app_version) 的已上传包目录。

    只做精确字符串匹配，不做模糊/最近版本回退——查不到就让调用方原样走 GitHub 逻辑
    （GitHub 那条路径自己已有 fallback，这里重蹈"错误 dSYM 硬套"覆辙的代价太高）。
    """
    d = _uploaded_symbols_root() / platform / symbol_type / app_version
    if not d.exists() or not d.is_dir():
        return None
    if not any(d.iterdir()):
        return None
    return d


async def _get_extract_lock(platform: str, symbol_type: str, app_version: str) -> asyncio.Lock:
    """按 (platform, symbol_type, app_version) 复用同一把锁，防止并发解压同一个上传包
    互相踩踏（同款模式见 _get_download_lock，用于 GitHub 下载侧）。"""
    async with _EXTRACT_LOCK_GUARD:
        key = (platform, symbol_type, app_version)
        lock = _EXTRACT_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _EXTRACT_LOCKS[key] = lock
        return lock
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/crashguard/test_uploaded_symbols_lookup.py -v`
Expected: 5 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/github_symbols.py backend/tests/crashguard/test_uploaded_symbols_lookup.py
git commit -m "feat(crashguard): 新增已上传符号包查找基础设施（Task 3-6 共用）"
```

---

### Task 3: iOS dSYM 上传优先接入

**Files:**
- Modify: `backend/app/crashguard/services/github_symbols.py:310-345`（`get_ios_dsyms_dir`，之前插入新函数 `_find_uploaded_ios_dsyms_dir`）
- Test: `backend/tests/crashguard/test_uploaded_symbols_lookup.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `_uploaded_package_dir` / `_get_extract_lock`
- Produces: `async _find_uploaded_ios_dsyms_dir(app_version: str) -> Optional[str]`；`get_ios_dsyms_dir()` 行为变化：命中已上传包时不再调用 `find_release_tag`/`_download_asset`

- [ ] **Step 1: 写失败测试**（追加到 `test_uploaded_symbols_lookup.py`）

```python
import zipfile
from pathlib import Path


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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/crashguard/test_uploaded_symbols_lookup.py -v -k ios_dsyms`
Expected: 失败——`get_ios_dsyms_dir` 目前无条件调用 `find_release_tag`，`boom`/`fake_find_release_tag` 会被触发或返回值不匹配。

- [ ] **Step 3: 实现**

在 `get_ios_dsyms_dir` 函数（约第 310 行）之前插入：

```python
async def _find_uploaded_ios_dsyms_dir(app_version: str) -> Optional[str]:
    """查已上传的 iOS dSYM 包（platform=ios, symbol_type=dsym），精确 app_version 匹配。

    上传目录里通常是原始 zip（首次使用时解压到 .extracted/ 子目录，marker 文件标记，
    之后直接返回缓存目录，不重复解压）；也兼容"目录里已经是解压后的 .dSYM bundle"的
    情况（万一未来上传接口改成直接存目录）。按 (platform, symbol_type, app_version)
    加锁防止并发解压互相踩踏。
    """
    src_dir = _uploaded_package_dir("ios", "dsym", app_version)
    if not src_dir:
        return None

    extracted_dir = src_dir / ".extracted"
    marker = extracted_dir / ".done"
    if marker.exists():
        return str(extracted_dir)

    zips = list(src_dir.glob("*.zip"))
    if not zips:
        if any(src_dir.rglob("*.dSYM")):
            return str(src_dir)
        return None

    lock = await _get_extract_lock("ios", "dsym", app_version)
    async with lock:
        if marker.exists():  # 锁内复检：等锁期间可能已被前一个 task 解压完
            return str(extracted_dir)
        try:
            extracted_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zips[0]) as zf:
                zf.extractall(extracted_dir)
            marker.touch()
            logger.info(
                "uploaded iOS dSYMs extracted to %s (app_version=%s)", extracted_dir, app_version,
            )
            return str(extracted_dir)
        except Exception as exc:
            logger.warning("failed to extract uploaded iOS dSYMs for %s: %s", app_version, exc)
            return None
```

然后修改 `get_ios_dsyms_dir` 函数体第一行（紧跟 docstring 之后）：

```python
async def get_ios_dsyms_dir(
    app_version: str, repo: str = _DEFAULT_REPO, asset_name: str = _ASSET_IOS_DSYM,
) -> Optional[str]:
    """
    返回 iOS dSYMs 目录路径（含 .dSYM bundles）。
    优先查打包机已上传的包（精确 app_version 匹配），查不到再走 GitHub release 下载
    （按 tag 共享 cache：多个 app_version 命中同一 release 时不重复下载/解压）。

    asset_name：flutter 用 PLAUD.dSYMs.zip，native 用 Plaud-Global.dSYMs.zip
    （见 _ASSET_IOS_DSYM_NATIVE），由调用方按 symbol_profile 选择。
    """
    uploaded = await _find_uploaded_ios_dsyms_dir(app_version)
    if uploaded:
        return uploaded

    tag = await find_release_tag(app_version, repo=repo)
    # ...（后续代码原样保留，不改）
```

（即：只在函数最开头插入 3 行 `uploaded = ...` 检查，函数其余部分不变。）

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/crashguard/test_uploaded_symbols_lookup.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/github_symbols.py backend/tests/crashguard/test_uploaded_symbols_lookup.py
git commit -m "feat(crashguard): iOS dSYM 符号化优先使用已上传包，查不到再回退 GitHub"
```

---

### Task 4: Android ProGuard mapping 上传优先接入

**Files:**
- Modify: `backend/app/crashguard/services/github_symbols.py:348-362`（`get_android_mapping`）
- Test: `backend/tests/crashguard/test_uploaded_symbols_lookup.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `_uploaded_package_dir`
- Produces: `_find_uploaded_android_mapping(app_version: str) -> Optional[str]`（同步函数，无需加锁——直接返回原始 .txt 路径，不解压）

- [ ] **Step 1: 写失败测试**（追加）

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/crashguard/test_uploaded_symbols_lookup.py -v -k android_mapping`
Expected: 失败——当前无条件调用 GitHub 路径。

- [ ] **Step 3: 实现**

在 `get_android_mapping` 函数（约第 348 行）之前插入：

```python
def _find_uploaded_android_mapping(app_version: str) -> Optional[str]:
    """查已上传的 Android ProGuard mapping（platform=android, symbol_type=proguard_mapping）。

    上传的就是原始 .txt，找到该目录下第一个 .txt 文件直接返回路径，不需要解压/加锁。
    """
    src_dir = _uploaded_package_dir("android", "proguard_mapping", app_version)
    if not src_dir:
        return None
    txts = sorted(src_dir.glob("*.txt"))
    return str(txts[0]) if txts else None
```

修改 `get_android_mapping` 函数体：

```python
async def get_android_mapping(app_version: str, repo: str = _DEFAULT_REPO) -> Optional[str]:
    """
    返回 Android ProGuard mapping 文件路径。
    优先查打包机已上传的包（精确 app_version 匹配），查不到再走 GitHub（按 tag 共享 cache）。
    """
    uploaded = _find_uploaded_android_mapping(app_version)
    if uploaded:
        return uploaded

    tag = await find_release_tag(app_version, repo=repo)
    # ...（后续代码原样保留，不改）
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/crashguard/test_uploaded_symbols_lookup.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/github_symbols.py backend/tests/crashguard/test_uploaded_symbols_lookup.py
git commit -m "feat(crashguard): Android ProGuard mapping 符号化优先使用已上传包"
```

---

### Task 5: Dart symbols 上传优先接入

**Files:**
- Modify: `backend/app/crashguard/services/github_symbols.py:443-470`（`get_dart_symbols_dir`）
- Test: `backend/tests/crashguard/test_uploaded_symbols_lookup.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `_uploaded_package_dir` / `_get_extract_lock`
- Produces: `async _find_uploaded_dart_symbols_dir(app_version: str) -> Optional[str]`

- [ ] **Step 1: 写失败测试**（追加）

```python
import tarfile
import io


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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/crashguard/test_uploaded_symbols_lookup.py -v -k dart_symbols`
Expected: 失败——当前无条件调用 GitHub 路径。

- [ ] **Step 3: 实现**

在 `get_dart_symbols_dir` 函数（约第 443 行）之前插入：

```python
async def _find_uploaded_dart_symbols_dir(app_version: str) -> Optional[str]:
    """查已上传的 Dart debug symbols 包（platform=flutter, symbol_type=dart_symbols）。

    比照 iOS dSYM 的 tar.gz 解压模式：首次使用时解压到 .extracted/，marker 标记后
    复用；按 (platform, symbol_type, app_version) 加锁防并发解压互相踩踏。
    """
    src_dir = _uploaded_package_dir("flutter", "dart_symbols", app_version)
    if not src_dir:
        return None

    extracted_dir = src_dir / ".extracted"
    marker = extracted_dir / ".done"
    if marker.exists():
        return str(extracted_dir)

    tars = list(src_dir.glob("*.tar.gz")) or list(src_dir.glob("*.tgz"))
    if not tars:
        if any(src_dir.rglob("*.symbols")):
            return str(src_dir)
        return None

    lock = await _get_extract_lock("flutter", "dart_symbols", app_version)
    async with lock:
        if marker.exists():
            return str(extracted_dir)
        try:
            extracted_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tars[0]) as tf:
                tf.extractall(extracted_dir)
            marker.touch()
            logger.info(
                "uploaded Dart symbols extracted to %s (app_version=%s)", extracted_dir, app_version,
            )
            return str(extracted_dir)
        except Exception as exc:
            logger.warning("failed to extract uploaded Dart symbols for %s: %s", app_version, exc)
            return None
```

修改 `get_dart_symbols_dir` 函数体：

```python
async def get_dart_symbols_dir(app_version: str, repo: str = _DEFAULT_REPO) -> Optional[str]:
    """
    返回 Dart debug symbols 目录路径（flutter_symbols.tar.gz 解压后）。
    优先查打包机已上传的包（精确 app_version 匹配），查不到再走 GitHub（按 tag 共享）。
    """
    uploaded = await _find_uploaded_dart_symbols_dir(app_version)
    if uploaded:
        return uploaded

    tag = await find_release_tag(app_version, repo=repo)
    # ...（后续代码原样保留，不改）
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/crashguard/test_uploaded_symbols_lookup.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/github_symbols.py backend/tests/crashguard/test_uploaded_symbols_lookup.py
git commit -m "feat(crashguard): Dart symbols 符号化优先使用已上传包"
```

---

### Task 6: Android native `.so` 符号包上传优先接入

**Files:**
- Modify: `backend/app/crashguard/services/github_symbols.py:381-440`（`get_android_native_symbols_dir`）
- Test: `backend/tests/crashguard/test_uploaded_symbols_lookup.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `_uploaded_package_dir` / `_get_extract_lock`；已有的 `_is_native_lib_tar_member()`（复用不改）
- Produces: `async _find_uploaded_android_native_symbols_dir(app_version: str) -> Optional[str]`

- [ ] **Step 1: 写失败测试**（追加）

```python
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
    names = {p.name for p in Path(result).rglob("*.so")}
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/crashguard/test_uploaded_symbols_lookup.py -v -k native_symbols_dir`
Expected: 失败——当前无条件调用 GitHub 路径。

- [ ] **Step 3: 实现**

在 `get_android_native_symbols_dir` 函数（约第 381 行）之前插入：

```python
async def _find_uploaded_android_native_symbols_dir(app_version: str) -> Optional[str]:
    """查已上传的 Android native_symbols.tar.gz（platform=android, symbol_type=native_symbols）。

    解压逻辑与现有 GitHub 那份一致：只保留 arm64-v8a + merged_native_libs 下的
    libflutter.so / libapp.so（复用 _is_native_lib_tar_member，不重复实现体积决策）。
    """
    src_dir = _uploaded_package_dir("android", "native_symbols", app_version)
    if not src_dir:
        return None

    extracted_dir = src_dir / ".extracted"
    marker = extracted_dir / ".done"
    if marker.exists():
        return str(extracted_dir)

    tars = list(src_dir.glob("*.tar.gz")) or list(src_dir.glob("*.tgz"))
    if not tars:
        return None

    lock = await _get_extract_lock("android", "native_symbols", app_version)
    async with lock:
        if marker.exists():
            return str(extracted_dir)
        try:
            from app.crashguard.config import get_crashguard_settings as _gs
            allowlist = getattr(_gs(), "android_extract_so_allowlist", None) \
                or ["libflutter.so", "libapp.so"]
        except Exception:
            allowlist = ["libflutter.so", "libapp.so"]

        try:
            extracted_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tars[0]) as tf:
                members = [m for m in tf.getmembers() if _is_native_lib_tar_member(m.name, allowlist)]
                tf.extractall(extracted_dir, members=members)
            marker.touch()
            logger.info(
                "uploaded Android native symbols extracted to %s (app_version=%s, kept=%d)",
                extracted_dir, app_version, len(members),
            )
            return str(extracted_dir)
        except Exception as exc:
            logger.warning("failed to extract uploaded Android native symbols for %s: %s", app_version, exc)
            return None
```

修改 `get_android_native_symbols_dir` 函数体：

```python
async def get_android_native_symbols_dir(app_version: str, repo: str = _DEFAULT_REPO) -> Optional[str]:
    """
    返回 Android native_symbols 目录路径（带 debug 符号的 libflutter.so / libapp.so 等）。
    优先查打包机已上传的包（精确 app_version 匹配），查不到再走 GitHub（按 tag 共享 cache）。
    """
    uploaded = await _find_uploaded_android_native_symbols_dir(app_version)
    if uploaded:
        return uploaded

    tag = await find_release_tag(app_version, repo=repo)
    # ...（后续代码原样保留，不改）
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/crashguard/test_uploaded_symbols_lookup.py -v`
Expected: 全部 PASS（累计 ~13 个测试）

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/services/github_symbols.py backend/tests/crashguard/test_uploaded_symbols_lookup.py
git commit -m "feat(crashguard): Android native .so 符号化优先使用已上传包（iOS/Android 完整对等）"
```

- [ ] **Step 6: 全量回归**

Run: `pytest tests/crashguard/ -v && lint-imports`
Expected: 全部 PASS（含 Task 1-6 新增测试 + 既有测试无回归），`lint-imports` 输出「crashguard 模块隔离合约 KEPT」

---

### Task 7: jank 占位符堆栈回填

**Files:**
- Modify: `backend/app/crashguard/config.py`（新增 2 个字段，紧跟第 475 行 `github_cache_keep_versions` 之后）
- Modify: `backend/app/crashguard/services/jank_ingester.py`（新增函数，追加到文件末尾）
- Test: `backend/tests/crashguard/test_jank_backfill.py`（新建）

**Interfaces:**
- Consumes: 已有 `_parse_jank_event`、`_jank_frame_looks_symbolized`、`_symbolicate_new_jank_issue`、`_JANK_LOG_QUERY`、`_MAX_PAGES_PER_TICK`（jank_ingester.py 内已定义，直接复用不改）
- Produces: `async backfill_stuck_jank_issues(now: Optional[datetime] = None) -> Dict[str, Any]`，返回 `{"scanned_events": int, "candidates": int, "resymbolized": int}`。Task 8 直接调用这个函数。

- [ ] **Step 1: 写失败测试**

```python
"""jank 占位符堆栈回填单测（2026-07-22）。

覆盖 backfill_stuck_jank_issues()：仍是占位符标题的 fixable jank issue 用最近一次
匹配的 Datadog 原始事件重新符号化；已成功符号化的 issue 不被打扰；datadog_api_key
缺失时整体 skip。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.crashguard.models  # noqa: F401


@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)
    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


def _patch_settings(monkeypatch):
    s = MagicMock()
    s.datadog_api_key = "fake-key"
    s.datadog_app_key = "fake-app-key"
    s.datadog_site = "datadoghq.com"
    s.datadog_service_filter = ""
    s.jank_backfill_lookback_hours = 24
    monkeypatch.setattr(
        "app.crashguard.services.jank_ingester.get_crashguard_settings", lambda: s,
    )
    return s


def _raw_event(attrs: dict) -> dict:
    return {"attributes": {"attributes": attrs}}


@pytest.mark.asyncio
async def test_backfill_resymbolizes_stuck_issue_using_fresh_event(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import backfill_stuck_jank_issues, compute_jank_aggregation_key
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session

    _patch_settings(monkeypatch)

    agg_key = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset="0x0000000000e31758",
    )
    issue_id = f"jank:{agg_key}"

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id, title="Jank @ Plaud-Global",  # 占位符：等于原始 module 名
            platform="ios", kind="jank", fatality="jank", fixable=True,
            representative_stack="0   Plaud-Global 0x... + 1",
        ))
        await session.commit()

    event = _raw_event({
        "os": {"name": "iOS"}, "has_app_frame": True,
        "app_stack_module": "Plaud-Global", "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_offset": "0x0000000000e31758",
        "app_stack_module_base": "0x0000000102f1c000",
        "stack_trace": "0   Plaud-Global 0x... + 1",
        "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_frame",
        AsyncMock(return_value="SomeClass.someMethod"),
    )
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_stack",
        AsyncMock(return_value="0   Plaud-Global   SomeClass.someMethod\n"),
    )
    monkeypatch.setattr("app.config.get_repo_routing", lambda: {})
    monkeypatch.setattr("app.services.repo_router.resolve", lambda platform, version, routing: None)

    result = await backfill_stuck_jank_issues(now=datetime(2026, 7, 22, 12, 0, 0))

    assert result["scanned_events"] == 1
    assert result["candidates"] == 1
    assert result["resymbolized"] == 1

    async with get_session() as session:
        from sqlalchemy import select
        row = (await session.execute(select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id))).scalar_one()
    assert row.title == "Jank @ SomeClass.someMethod"


@pytest.mark.asyncio
async def test_backfill_skips_issue_already_symbolized(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import backfill_stuck_jank_issues, compute_jank_aggregation_key
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session

    _patch_settings(monkeypatch)

    agg_key = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset="0x0000000000e31758",
    )
    issue_id = f"jank:{agg_key}"

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id, title="Jank @ AlreadyResolved.method",  # 不等于原始 module 名 → 已符号化
            platform="ios", kind="jank", fatality="jank", fixable=True,
            representative_stack="already resolved stack",
        ))
        await session.commit()

    event = _raw_event({
        "os": {"name": "iOS"}, "has_app_frame": True,
        "app_stack_module": "Plaud-Global", "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_offset": "0x0000000000e31758",
        "app_stack_module_base": "0x0000000102f1c000",
        "stack_trace": "0   Plaud-Global 0x... + 1",
        "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )
    resymbolize_mock = AsyncMock()
    monkeypatch.setattr(
        "app.crashguard.services.jank_ingester._symbolicate_new_jank_issue", resymbolize_mock,
    )

    result = await backfill_stuck_jank_issues(now=datetime(2026, 7, 22, 12, 0, 0))

    assert result["candidates"] == 0
    assert result["resymbolized"] == 0
    resymbolize_mock.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_skips_when_datadog_key_missing(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import backfill_stuck_jank_issues

    s = MagicMock()
    s.datadog_api_key = ""
    monkeypatch.setattr("app.crashguard.services.jank_ingester.get_crashguard_settings", lambda: s)
    search_mock = AsyncMock()
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    result = await backfill_stuck_jank_issues()
    assert result == {"scanned_events": 0, "candidates": 0, "resymbolized": 0}
    search_mock.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_ignores_events_with_no_matching_db_issue(patched_session, monkeypatch):
    """Datadog 返回的事件命中的 issue_id 在 DB 里不存在（例如还没被 ingest_jank_logs
    摄入过）——不应该报错，只是不处理。"""
    from app.crashguard.services.jank_ingester import backfill_stuck_jank_issues

    _patch_settings(monkeypatch)

    event = _raw_event({
        "os": {"name": "iOS"}, "has_app_frame": True,
        "app_stack_module": "Plaud-Global", "app_stack_pc": "0x1",
        "app_stack_module_offset": "0x0000000000e31758",
        "app_stack_module_base": "0x0", "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )

    result = await backfill_stuck_jank_issues(now=datetime(2026, 7, 22, 12, 0, 0))
    assert result["scanned_events"] == 1
    assert result["candidates"] == 0
    assert result["resymbolized"] == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/crashguard/test_jank_backfill.py -v`
Expected: `AttributeError: module 'app.crashguard.services.jank_ingester' has no attribute 'backfill_stuck_jank_issues'`

- [ ] **Step 3: 实现**

在 `backend/app/crashguard/config.py` 第 475 行（`github_cache_keep_versions: int = 10`）之后插入：

```python
    # === jank 回填（占位符堆栈重新符号化，2026-07-22）===
    # 定期扫一遍最近窗口内的卡顿(jank)原始日志，若匹配到的 fixable jank issue 仍是
    # 占位符标题(从未成功符号化)，用这条最新事件的 module/pc/base 重新尝试符号化——
    # 常见触发场景：issue 创建时符号包还没上传/GitHub 下载失败(VPN 链路不稳)，后来
    # 符号包补传了但旧 issue 不会自动重试。仅 jank：crash/ANR 的 representative_stack
    # 一旦被(错误)覆写，原始地址信息已从 DB 消失，这种"重放最近一次原始事件"的回填
    # 方式对它们不适用。
    jank_backfill_cron: str = "*/5 * * * *"
    jank_backfill_lookback_hours: int = 24
```

在 `backend/app/crashguard/services/jank_ingester.py` 文件末尾（`_record_jank_prewarm_result` 函数之后，`_load_cursor_ms` 之前，任意位置均可，选择追加到文件末尾）追加：

```python
def _jank_issue_looks_stuck(row_title: str, original_frame_text: str) -> bool:
    """判断一条已存在的 jank issue 标题是否仍是摄入时的占位符（从未被成功符号化）。

    复用 _jank_frame_looks_symbolized 的启发式：标题去掉 "Jank @ " 前缀后如果等于
    这次事件的原始 module/frame 文本，说明标题从摄入那一刻起就没被替换过。
    """
    prefix = "Jank @ "
    current_frame = row_title[len(prefix):] if row_title.startswith(prefix) else ""
    return not _jank_frame_looks_symbolized(current_frame, original_frame_text)


async def backfill_stuck_jank_issues(now: Optional[datetime] = None) -> Dict[str, Any]:
    """回填仍是占位符堆栈的 fixable jank issue（2026-07-22）。

    实现：拉一遍最近 lookback 窗口内的 jank 原始日志（与 ingest_jank_logs 同一个
    Datadog 查询、同一套分页逻辑），按 issue_id 只保留每个聚合键最近一次出现的原始
    事件（Datadog 按 timestamp 升序返回，后出现的事件覆盖字典里先出现的）。再用这份
    "最新鲜"的 module/pc/base，对仍是占位符标题的 fixable jank issue 重新跑一遍
    _symbolicate_new_jank_issue 同款逻辑。

    范围限定 jank only：crash/ANR 的 representative_stack 一旦被（错误）覆写，原始
    地址信息已从 DB 消失，这种"重放最近一次原始事件"的回填方式对它们不适用（历史
    坏数据留作后续单独处理，见
    docs/superpowers/specs/2026-07-22-symbol-upload-priority-design.md）。
    """
    from app.crashguard.models import CrashIssue

    s = get_crashguard_settings()
    if not s.datadog_api_key:
        logger.info("jank backfill skipped: datadog_api_key 未配置")
        return {"scanned_events": 0, "candidates": 0, "resymbolized": 0}

    now = now or datetime.utcnow()
    lookback_hours = int(getattr(s, "jank_backfill_lookback_hours", 24) or 24)
    to_ms = int(now.replace(tzinfo=timezone.utc).timestamp() * 1000)
    from_ms = to_ms - lookback_hours * 3600 * 1000

    client = DatadogClient(
        api_key=s.datadog_api_key, app_key=s.datadog_app_key,
        site=s.datadog_site, service_filter=s.datadog_service_filter,
    )

    latest_by_issue: Dict[str, Dict[str, Any]] = {}
    scanned_events = 0
    cursor: Optional[str] = None
    for _ in range(_MAX_PAGES_PER_TICK):
        page = await client.search_logs_page(
            query=_JANK_LOG_QUERY, from_ms=from_ms, to_ms=to_ms, cursor=cursor, limit=100,
        )
        events = page.get("data") or []
        for event in events:
            scanned_events += 1
            parsed = _parse_jank_event(event)
            if parsed and parsed["has_app_frame"]:
                latest_by_issue[parsed["issue_id"]] = parsed
        cursor = page.get("next_cursor")
        if not cursor or not events:
            break

    if not latest_by_issue:
        return {"scanned_events": scanned_events, "candidates": 0, "resymbolized": 0}

    async with get_session() as session:
        rows = (await session.execute(
            select(CrashIssue).where(
                CrashIssue.kind == "jank",
                CrashIssue.fixable == True,  # noqa: E712
                CrashIssue.datadog_issue_id.in_(list(latest_by_issue.keys())),
            )
        )).scalars().all()

    candidates = 0
    resymbolized = 0
    for row in rows:
        parsed = latest_by_issue[row.datadog_issue_id]
        original_frame = (
            parsed["app_stack_module"] if "ios" in (parsed["platform"] or "").lower()
            else parsed["app_stack_frame"]
        )
        if not _jank_issue_looks_stuck(row.title or "", original_frame):
            continue
        candidates += 1
        await _symbolicate_new_jank_issue(row.datadog_issue_id, parsed)
        resymbolized += 1

    logger.info(
        "jank backfill done: scanned_events=%d candidates=%d resymbolized=%d",
        scanned_events, candidates, resymbolized,
    )
    return {"scanned_events": scanned_events, "candidates": candidates, "resymbolized": resymbolized}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/crashguard/test_jank_backfill.py -v`
Expected: 4 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/crashguard/config.py backend/app/crashguard/services/jank_ingester.py backend/tests/crashguard/test_jank_backfill.py
git commit -m "feat(crashguard): jank 占位符堆栈回填——符号包补传后自动重试符号化"
```

---

### Task 8: scheduler.py 挂载 jank_backfill 定时任务

**Files:**
- Modify: `backend/app/crashguard/workers/scheduler.py`（新增全局变量 + `_tick_once` 内新增任务块）

**Interfaces:**
- Consumes: Task 7 的 `backfill_stuck_jank_issues()`
- Produces: 新 cron job `jank_backfill`（心跳记录到 `crash_job_heartbeats`，`/crashguard/jobs` 页面可见），复用现有 `enabled` 总开关（不新增 kill switch，与 `analyze_tick`/`pr_sync` 等一致）

- [ ] **Step 1: 实现**（本任务是纯 glue 代码，跟随现有 `analyze_tick`/`top_crash_auto_pr` 等任务块同款模式，这一层在现有代码库里没有专门的单元测试先例——只测底层函数本身，见 Task 7 —— 因此本任务不新增测试文件，直接实现 + 手动/集成验证）

在 `backend/app/crashguard/workers/scheduler.py` 顶部全局变量区（约第 38 行，`_deep_analyze_auto_last_fired` 之后）新增：

```python
_jank_backfill_last_fired: str = ""  # 卡顿回填 tick 进程级幂等
```

在 `_tick_once()` 函数内，紧跟"AI 分析定时小步分批"任务块（约第 318 行 `_enqueue_job("analyze_tick", _analyze_job)` 之后）插入：

```python
    # 卡顿(jank)占位符堆栈回填：符号包补传后自动重试符号化（独立 cron，默认 */5）
    global _jank_backfill_last_fired
    jank_backfill_cron = getattr(s, "jank_backfill_cron", "") or ""
    if jank_backfill_cron and _jank_backfill_last_fired != tag and _cron_matches(jank_backfill_cron, now):
        _jank_backfill_last_fired = tag
        async def _jank_backfill_job():
            async with record_heartbeat("jank_backfill") as hb:
                from app.crashguard.services.jank_ingester import backfill_stuck_jank_issues
                res = await backfill_stuck_jank_issues()
                hb.set_summary(res)
                if res.get("candidates", 0) == 0:
                    hb.status = "skipped"
                logger.info(
                    "crashguard jank_backfill fired: scanned_events=%d candidates=%d resymbolized=%d",
                    res.get("scanned_events", 0), res.get("candidates", 0), res.get("resymbolized", 0),
                )
        _enqueue_job("jank_backfill", _jank_backfill_job)
```

- [ ] **Step 2: 手动验证 wiring 不报错**

Run: `cd backend && source .venv/bin/activate && python -c "import app.crashguard.workers.scheduler"`
Expected: 无 ImportError/SyntaxError

- [ ] **Step 3: 全量回归**

Run: `pytest tests/crashguard/ -v && lint-imports`
Expected: 全部 PASS，隔离合约 KEPT

- [ ] **Step 4: 提交**

```bash
git add backend/app/crashguard/workers/scheduler.py
git commit -m "feat(crashguard): 挂载 jank_backfill 定时任务（每 5 分钟，复用 enabled 总开关）"
```

---

### Task 9: plaud-native-app2 打包脚本新增符号表上传

**仓库：** `~/Desktop/code/plaud-native-app2`（与 jarvis 是不同的 git 仓库，本任务的 commit/push 在该仓库内独立进行，不影响 jarvis 的 git 历史）

**Files:**
- Modify: `jenkins/plaud-native-app-publish-global.sh:1346-1347`（在 `upload_datadog_symbols` 函数结束、下一段注释块开始之间插入新函数 `upload_jarvis_symbols`）
- Modify: `jenkins/plaud-native-app-publish-global.sh:2518`（`upload_datadog_symbols` 调用之后追加新调用）

**Interfaces:**
- Consumes: 已有的 `dir_has_files()`（第 182-185 行，判断目录非空）、`$staging_version_tag`（第 2070 行赋值，格式 `v4.0.201+941`）、`$global_native_symbols_dir`（第 2378 行赋值，`archive_android_native_symbols` 已产出，可能因 `BUILD_GLOBAL_ANDROID=false` 而未设置）
- Produces: `upload_jarvis_symbols()` — 3 次 curl 上传（iOS dSYM zip / Android mapping.txt / Android native_symbols tar.gz），全部失败只打日志不中断构建

- [ ] **Step 1: 创建分支**

```bash
cd ~/Desktop/code/plaud-native-app2
git status   # 确认工作区干净，无未提交改动会被分支切换影响
git checkout -b feat/jarvis-symbol-upload
```

- [ ] **Step 2: 新增 `upload_jarvis_symbols()` 函数**

在 `jenkins/plaud-native-app-publish-global.sh` 第 1346 行（`upload_datadog_symbols` 函数的结束 `}`）之后、第 1348 行（`# ============== Datadog 包体积上报...`注释块）之前插入：

```bash

# ============== Jarvis 符号表上传（同机内网，符号化优先使用，GitHub 兜底）==============
# 背景：jarvis (crashguard) 的符号化查找路径此前完全依赖 GitHub Release 里的符号包，
# 下载走公网/VPN，链路不稳定（2026-07-22 实测：91MB dSYM zip 两次下载分别卡在 3MB 和
# 90.1MB，随机断点）。Jenkins 和 jarvis backend 部署在同一台服务器（10.0.52.102），
# 直接内网 curl 上传，符号化时优先使用这份，查不到精确版本才回退 GitHub。
# 失败只打日志，不中断构建（与上面 upload_sentry_symbols / upload_datadog_symbols 同风格）。
upload_jarvis_symbols() {
  local dsym_root_dir="$1"
  local mapping_path="$2"
  local native_symbols_dir="$3"

  local base_url="${JARVIS_SYMBOLS_BASE_URL:-http://localhost:8000}"
  local app_version=""
  if [ -n "${staging_version_tag:-}" ]; then
    # v4.0.201+941 → 4.0.201-941（对齐 Datadog @application.version / jarvis CrashIssue.last_seen_version 格式）
    app_version="${staging_version_tag#v}"
    app_version="${app_version//+/-}"
  fi

  if [ -z "$app_version" ]; then
    echo "Jarvis 符号表：无法解析 app_version（staging_version_tag 未设置），跳过全部上传"
    return 0
  fi

  echo "Jarvis 符号表：base_url=$base_url app_version=$app_version"

  # ---- iOS dSYM ----
  if [ -n "$dsym_root_dir" ] && [ -d "$dsym_root_dir" ] && dir_has_files "$dsym_root_dir"; then
    if ! command -v zip >/dev/null 2>&1; then
      echo "Jarvis 符号表：未找到 zip 命令，跳过 iOS dSYM 上传"
    else
      local ios_zip="$STAGING_DIR/jarvis_ios_dsyms.zip"
      rm -f "$ios_zip"
      (cd "$dsym_root_dir" && zip -qr "$ios_zip" .) \
        && curl -sS --max-time 120 -F "file=@${ios_zip}" \
             "${base_url}/api/crash/symbols/upload?platform=ios&symbol_type=dsym&app_version=${app_version}" \
             -o /dev/null -w "Jarvis 符号表：iOS dSYM 上传 http=%{http_code}\n" \
        || echo "Jarvis 符号表：iOS dSYM 上传失败（不中断）"
    fi
  else
    echo "Jarvis 符号表：未找到 iOS dSYMs 目录，跳过"
  fi

  # ---- Android ProGuard mapping ----
  if [ -n "$mapping_path" ] && [ -f "$mapping_path" ]; then
    curl -sS --max-time 60 -F "file=@${mapping_path}" \
      "${base_url}/api/crash/symbols/upload?platform=android&symbol_type=proguard_mapping&app_version=${app_version}" \
      -o /dev/null -w "Jarvis 符号表：Android mapping 上传 http=%{http_code}\n" \
      || echo "Jarvis 符号表：Android mapping 上传失败（不中断）"
  else
    echo "Jarvis 符号表：未找到 Android mapping 文件，跳过: $mapping_path"
  fi

  # ---- Android native .so（带 debug 符号，archive_android_native_symbols 已产出）----
  if [ -n "$native_symbols_dir" ] && dir_has_files "$native_symbols_dir"; then
    local native_tar="$STAGING_DIR/jarvis_android_native_symbols.tar.gz"
    rm -f "$native_tar"
    (cd "$native_symbols_dir" && tar -czf "$native_tar" .) \
      && curl -sS --max-time 300 -F "file=@${native_tar}" \
           "${base_url}/api/crash/symbols/upload?platform=android&symbol_type=native_symbols&app_version=${app_version}" \
           -o /dev/null -w "Jarvis 符号表：Android native symbols 上传 http=%{http_code}\n" \
      || echo "Jarvis 符号表：Android native symbols 上传失败（不中断）"
  else
    echo "Jarvis 符号表：未找到 Android native_symbols 目录，跳过"
  fi
}
```

- [ ] **Step 3: 调用点接入**

在第 2518 行 `upload_datadog_symbols "$DSYM_ROOT_DIR" "$ANDROID_MAPPING_PATH" "${android_path:-}"` 之后追加：

```bash

# Jarvis：iOS dSYM + Android mapping + Android native .so（同机内网，符号化优先用这份，GitHub 兜底）
upload_jarvis_symbols "$DSYM_ROOT_DIR" "$ANDROID_MAPPING_PATH" "${global_native_symbols_dir:-}"
```

- [ ] **Step 4: 语法自检**

Run: `cd ~/Desktop/code/plaud-native-app2 && bash -n jenkins/plaud-native-app-publish-global.sh`
Expected: 无输出（exit 0），说明脚本语法合法（不会真的执行打包流程，只做语法解析）

- [ ] **Step 5: 提交（该仓库独立提交，不影响 jarvis）**

```bash
cd ~/Desktop/code/plaud-native-app2
git add jenkins/plaud-native-app-publish-global.sh
git commit -m "feat(jenkins): 打包完成后上传符号表到 jarvis（同机内网，符号化优先，GitHub 兜底）"
```

- [ ] **Step 6: 真实验证（部署 Task 1-8 到 102 之后，需要一次真实 Jenkins 构建或手动 curl 模拟）**

这一步不在 CI 单元测试范围内（shell 脚本无单测框架，见 spec「测试计划」一节）。验证方式：

1. 确认 jarvis 后端（Task 1-8）已部署到 102 且 `GET http://10.0.52.102:8000/api/crash/health` 正常。
2. 触发一次 Jenkins global 构建（或在 102 上手动模拟）：
   ```bash
   curl -sS -F "file=@/path/to/test.zip" \
     "http://localhost:8000/api/crash/symbols/upload?platform=ios&symbol_type=dsym&app_version=4.0.201-941"
   ```
3. 确认 `GET http://10.0.52.102:8000/api/crash/symbols?platform=ios&app_version=4.0.201-941` 能看到刚上传的记录。
4. 确认设置页 `/settings` → Crashguard 区块的符号包管理列表能看到同一条记录。

---

## 执行顺序与依赖关系

Task 1（上传 API 校验）与 Task 2（共享基础设施）互相独立，可并行；Task 3-6 依次依赖 Task 2；Task 7 独立于 Task 1-6（只涉及 jank_ingester.py + config.py）；Task 8 依赖 Task 7；Task 9 依赖 Task 1（上传接口必须先支持 native_symbols + 校验，打包机才能成功上传该类型）但不依赖 Task 2-8（Task 9 可以先写完，实际生效等 Task 1-8 部署后）。

建议顺序：Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8 → Task 9。
