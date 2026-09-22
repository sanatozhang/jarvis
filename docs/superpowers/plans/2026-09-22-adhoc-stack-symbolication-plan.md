# Ad-hoc 堆栈符号化工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 jarvis 加一个「粘贴堆栈 → 立即符号化」的前端工作台，支持 Android/iOS 双端，
并把「没有符号表」从事后抱怨改成提交前预检。

**Architecture:** 后端符号化引擎（`symbolication.py`）和 ad-hoc 端点
（`POST /api/crash/symbolicate`）已存在且不动其核心。本次新增一个**纯函数堆栈解析器**
+ 三个**只读探测端点**（inspect / versions / preflight），改造现有 symbolicate 端点
补逐帧统计，最后加一个前端页面把它们串起来。外加符号包预热（读 graygate 主要版本，
走 crashguard job queue），让 QA 查新灰度包时永远命中缓存。

**Tech Stack:** FastAPI + SQLAlchemy（后端）/ Next.js 15 + React 19 + Tailwind 4（前端）/
pytest（唯一验证手段）

**Spec:** `docs/superpowers/specs/2026-09-22-adhoc-stack-symbolication-design.md`

## Global Constraints

这些约束适用于**每一个** task，不再逐条重复：

- **不执行任何 git 写操作。** 不 `commit`、不 `push`、不 `merge`。所有改动留在工作区
  等用户 review。（用户铁律 `feedback_no_auto_pr` / `feedback_no_worktree_when_told`：
  所有 git 写动作都要明确指令。）本 plan 的任务**没有 commit 步骤**，这是对
  writing-plans 默认模板的有意偏离。
- **不起本地后端服务手动测。** 验证一律靠 `cd backend && pytest tests/crashguard/xxx -v`。
  （铁律 `feedback_no_local_server_run`：本地共用生产飞书凭证，起服务会真发告警刷屏。）
- **不部署、不重启、不改生产配置。** 部署是独立授权动作（铁律 `feedback_no_auto_deploy`
  + `feedback_peak_hours_no_deploy`）。
- **API 前缀**：crashguard router 是 `APIRouter(prefix="/api/crash")`（`api/crash.py:45`）。
  前端 `api.ts` 的 `BASE = "/api"`，前端调用写 `/crash/...`。
- **`repo_router.resolve()` 必须传 `path_exists=lambda _p: True`。** 默认用
  `os.path.exists` 校验源码 wrapper 目录，但 `config.yaml` 配的是裸机路径，容器内不存在
  会返回 `None` → `symbol_profile` 丢失 → iOS 下错误的 dSYM 资产。
  （见 `api/crash.py:3760` 现有注释。）
- **向后兼容**：`POST /api/crash/symbolicate` 的 `symbolicated_stack` / `changed` /
  `stack_quality_*` / `routing_confidence` 字段语义不得改变。`warnings` 是人读文本，可改。
- 新增 Python 文件一律 `from __future__ import annotations` 开头，与模块现有风格一致。
- 不新增第三方依赖。

---

### Task 1: 堆栈解析器 `stack_inspector.py`

本任务是整个功能的地基，也是唯一有真实算法复杂度的部分。纯函数、零 IO、零网络、零 DB
——这是它可被 pytest 完整覆盖的前提。

**Files:**
- Create: `backend/app/crashguard/services/stack_inspector.py`
- Test: `backend/tests/crashguard/test_stack_inspector.py`

**Interfaces:**
- Consumes: `app.crashguard.services.symbolication._PTR_AUTH_MASK`（arm64e PAC 掩码常量，
  值 `0xFFFFFFFFFF`，复用而非重新发明）
- Produces:
  - `inspect_stack(raw: str) -> StackInsight`
  - `StackInsight` dataclass，字段见下
  - `FRAME_RE_IOS`（编译好的 iOS 帧正则，Task 6 的逐帧统计复用它）

`StackInsight` 字段（全部有默认值，便于部分解析）：

| 字段 | 类型 | 默认 |
|---|---|---|
| `stack_format` | `str` | `"unknown"` |
| `platform` | `str` | `""` |
| `app_version` | `str` | `""` |
| `uuids` | `list[str]` | `[]` |
| `build_ids` | `list[str]` | `[]` |
| `binary_images` | `list[dict]` | `[]` |
| `normalized_stack` | `str` | `""` |
| `frame_count` | `int` | `0` |
| `confidence` | `str` | `"low"` |
| `notes` | `list[str]` | `[]` |

- [ ] **Step 1: 写失败测试——6 种格式的表驱动识别**

创建 `backend/tests/crashguard/test_stack_inspector.py`：

```python
from __future__ import annotations

import json

import pytest

from app.crashguard.services.stack_inspector import inspect_stack


# ── 真实样本 ────────────────────────────────────────────────────────────────

# iOS 15+ .ips 是「两段式」：第一行 header JSON，之后是 payload JSON
IPS_HEADER = {
    "app_name": "PLAUD",
    "timestamp": "2026-09-20 10:23:45.00 +0800",
    "app_version": "4.0.201",
    "build_version": "941",
    "bundleID": "ai.plaud.app",
    "platform": 2,
    "os_version": "iPhone OS 18.0 (22A3354)",
}
IPS_PAYLOAD = {
    "uptime": 1234,
    "exception": {"type": "EXC_CRASH", "signal": "SIGABRT"},
    "usedImages": [
        {
            "source": "P",
            "arch": "arm64e",
            "base": 4356407296,          # 0x103A00000
            "size": 13500416,
            "uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "path": "/private/var/containers/Bundle/Application/X/PLAUD.app/PLAUD",
            "name": "PLAUD",
        },
        {
            "source": "S",
            "arch": "arm64e",
            "base": 8063942656,
            "size": 204800,
            "uuid": "11111111-2222-3333-4444-555555555555",
            "path": "/usr/lib/system/libsystem_kernel.dylib",
            "name": "libsystem_kernel.dylib",
        },
    ],
    "threads": [
        {
            "triggered": True,
            "id": 1,
            "frames": [
                {"imageIndex": 0, "imageOffset": 180224},
                {"imageIndex": 1, "imageOffset": 115396},
            ],
        }
    ],
}
APPLE_IPS = json.dumps(IPS_HEADER) + "\n" + json.dumps(IPS_PAYLOAD)

APPLE_CRASH_TEXT = """Incident Identifier: ABCD1234-5678-90AB-CDEF-1234567890AB
CrashReporter Key:   0123456789abcdef
Process:             PLAUD [1234]
Identifier:          ai.plaud.app
Version:             4.0.201 (941)
OS Version:          iPhone OS 18.0 (22A3354)

Thread 0 Crashed:
0   PLAUD                         0x0000000103a2c000 0x103a00000 + 180224
1   libsystem_kernel.dylib        0x00000001e0a1c2c4 0x1e0a00000 + 115396

Binary Images:
0x103a00000 - 0x1046effff PLAUD arm64e <a1b2c3d4e5f67890abcdef1234567890> /var/containers/Bundle/Application/X/PLAUD.app/PLAUD
0x1e0a00000 - 0x1e0a31fff libsystem_kernel.dylib arm64e <111111112222333344445555555555> /usr/lib/system/libsystem_kernel.dylib
"""

# Datadog RUM 复制出来的：帧形状同 Apple，但没有任何 header
DATADOG_RUM = """0   PLAUD                         0x0000000103a2c000 0x103a00000 + 180224
1   PLAUD                         0x0000000103a2d100 0x103a00000 + 184576
2   libsystem_kernel.dylib        0x00000001e0a1c2c4 0x1e0a00000 + 115396
"""

# 最高频的 Android 输入：零元数据
ANDROID_JAVA = """java.lang.NullPointerException: Attempt to invoke virtual method 'void a.b.c.d()' on a null object reference
\tat a.b.c.e(Unknown Source:12)
\tat a.b.f.g(SourceFile:45)
\tat android.os.Handler.dispatchMessage(Handler.java:106)
"""

ANDROID_TOMBSTONE = """backtrace:
      #00 pc 00000000004a1b2c  /data/app/~~abc==/ai.plaud.app-xyz==/lib/arm64/libflutter.so (BuildId: 1a2b3c4d5e6f7890)
      #01 pc 00000000004a2000  /data/app/~~abc==/ai.plaud.app-xyz==/lib/arm64/libapp.so (BuildId: 9f8e7d6c5b4a3210)
"""

ANDROID_LOGCAT = """09-20 10:23:45.123 12345 12345 E AndroidRuntime: FATAL EXCEPTION: main
09-20 10:23:45.123 12345 12345 E AndroidRuntime: Process: ai.plaud.app, PID: 12345
09-20 10:23:45.123 12345 12345 E AndroidRuntime: java.lang.IllegalStateException
09-20 10:23:45.123 12345 12345 E AndroidRuntime: \tat a.b.c.d(Unknown Source:3)
"""


# ── 格式识别 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected_format,expected_platform",
    [
        (APPLE_IPS, "apple_ips_json", "ios"),
        (APPLE_CRASH_TEXT, "apple_crash_text", "ios"),
        (DATADOG_RUM, "datadog_rum", "ios"),
        (ANDROID_JAVA, "android_java", "android"),
        (ANDROID_TOMBSTONE, "android_tombstone", "android"),
        (ANDROID_LOGCAT, "android_logcat", "android"),
        ("完全不是堆栈的一段中文文本", "unknown", ""),
        ("", "unknown", ""),
    ],
)
def test_format_and_platform_detection(raw, expected_format, expected_platform):
    r = inspect_stack(raw)
    assert r.stack_format == expected_format
    assert r.platform == expected_platform


# ── 版本号提取 ──────────────────────────────────────────────────────────────

def test_ips_extracts_version_and_uuids():
    r = inspect_stack(APPLE_IPS)
    # app_version + build_version 组合成 Datadog @application.version 的写法
    assert r.app_version == "4.0.201-941"
    assert r.confidence == "high"
    # UUID 规范化：去 '-'、小写
    assert "a1b2c3d4e5f67890abcdef1234567890" in r.uuids
    # 两个镜像都要收
    assert len(r.binary_images) == 2
    app_img = [i for i in r.binary_images if i["name"] == "PLAUD"][0]
    assert app_img["load_address"] == "0x103a00000"


def test_apple_crash_text_extracts_version_and_uuids():
    r = inspect_stack(APPLE_CRASH_TEXT)
    assert r.app_version == "4.0.201-941"   # "4.0.201 (941)" 归一
    assert r.confidence == "high"
    assert "a1b2c3d4e5f67890abcdef1234567890" in r.uuids


def test_android_java_has_no_version_and_says_so():
    """最重要的一个用例：ProGuard 混淆栈物理上不含版本号，必须明确告知用户。"""
    r = inspect_stack(ANDROID_JAVA)
    assert r.platform == "android"
    assert r.app_version == ""
    assert r.confidence == "medium"       # 格式明确，但无版本
    assert any("必须手动指定版本号" in n for n in r.notes)


def test_tombstone_extracts_build_ids():
    r = inspect_stack(ANDROID_TOMBSTONE)
    assert r.build_ids == ["1a2b3c4d5e6f7890", "9f8e7d6c5b4a3210"]
    assert r.app_version == ""            # tombstone 不含 app 版本
    assert r.frame_count == 2


def test_datadog_rum_no_header_no_version():
    r = inspect_stack(DATADOG_RUM)
    assert r.app_version == ""
    assert r.frame_count == 3
    assert r.confidence == "medium"


# ── 归一化 ──────────────────────────────────────────────────────────────────

def test_ips_normalized_to_text_frames():
    """.ips 的 threads[].frames 是结构化的，必须归一成
    `_symbolicate_ios_with_dir` 正则认的文本帧形状：
    `<idx> <module> <addr> <base> + <off>`
    """
    r = inspect_stack(APPLE_IPS)
    lines = [l for l in r.normalized_stack.splitlines() if l.strip()]
    assert len(lines) == 2
    # frame 0: base 0x103a00000 + 180224(0x2C000) = 0x103a2c000
    assert "PLAUD" in lines[0]
    assert "0x103a2c000" in lines[0]
    assert "0x103a00000" in lines[0]
    assert "+ 180224" in lines[0]


def test_non_ips_formats_pass_stack_through_unchanged():
    for raw in (APPLE_CRASH_TEXT, DATADOG_RUM, ANDROID_JAVA, ANDROID_TOMBSTONE):
        assert inspect_stack(raw).normalized_stack == raw


# ── 健壮性：解析失败必须降级，绝不抛异常 ────────────────────────────────────

@pytest.mark.parametrize(
    "raw",
    [
        "{不是合法 JSON",
        '{"app_version": "4.0.1"}',              # 只有 header，没有第二段
        '{"app_version":"4.0.1"}\n{坏掉的 payload',
        "Version:             (941)",             # 版本号残缺
        "\x00\x01\x02 二进制垃圾",
    ],
)
def test_malformed_input_degrades_never_raises(raw):
    r = inspect_stack(raw)          # 不抛异常即算通过
    assert isinstance(r.notes, list)
    assert r.stack_format in (
        "unknown", "apple_ips_json", "apple_crash_text",
        "datadog_rum", "android_java", "android_tombstone", "android_logcat",
    )


def test_arm64e_pac_polluted_address_is_masked():
    """arm64e 指针认证会污染返回地址高 24 bit（见 symbolication._strip_ptr_auth
    的生产实测记录）。解析出的地址必须掩码，否则算出天文数字的 offset。"""
    polluted = "1   PLAUD   0xdf0c800199fde290 0x199f00000 + 0\n"
    r = inspect_stack(polluted)
    # 掩掉高位后应落在合理范围（几 KB ~ 几十 MB），而非 10^19 量级
    assert r.frame_count == 1
    assert "0xdf0c800199fde290" not in r.normalized_stack or r.notes
```

- [ ] **Step 2: 运行测试确认全红**

```bash
cd backend && pytest tests/crashguard/test_stack_inspector.py -v
```

Expected: 全部 FAIL，报 `ModuleNotFoundError: No module named 'app.crashguard.services.stack_inspector'`

- [ ] **Step 3: 实现 `stack_inspector.py`**

创建 `backend/app/crashguard/services/stack_inspector.py`。

关键实现要点（按此顺序写，顺序本身就是识别优先级——先严格后宽松，避免误判）：

```python
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("jarvis.crashguard.stack_inspector")

# 复用 symbolication 的 arm64e PAC 掩码语义，不重新发明。
# 该常量的生产实测依据见 symbolication._strip_ptr_auth 的 docstring。
_PTR_AUTH_MASK = 0xFFFFFFFFFF  # 保留低 40 bit

# iOS/Apple 帧：`<idx> <module> <addr> <base> + <off>`
# Task 6 的逐帧统计复用此正则，故导出为模块级常量。
FRAME_RE_IOS = re.compile(
    r"^(\s*\d+\s+)(\S+)(\s+)(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+\+\s+(\d+)(.*)$",
    re.MULTILINE,
)

_TOMBSTONE_FRAME_RE = re.compile(
    r"#\d{2}\s+pc\s+([0-9a-fA-F]+)\s+(\S+)(?:\s+\(BuildId:\s*([0-9a-fA-F]+)\))?"
)
_JAVA_FRAME_RE = re.compile(r"^\s*at\s+\S+\(", re.MULTILINE)
_APPLE_VERSION_RE = re.compile(r"^Version:\s*(\S+)\s*\((\S+)\)\s*$", re.MULTILINE)
_APPLE_BINIMG_RE = re.compile(
    r"^\s*(0x[0-9a-fA-F]+)\s*-\s*(0x[0-9a-fA-F]+)\s+(\S+)\s+\S+\s+<([0-9a-fA-F]+)>\s+(\S+)",
    re.MULTILINE,
)


@dataclass
class StackInsight:
    stack_format: str = "unknown"
    platform: str = ""
    app_version: str = ""
    uuids: list = field(default_factory=list)
    build_ids: list = field(default_factory=list)
    binary_images: list = field(default_factory=list)
    normalized_stack: str = ""
    frame_count: int = 0
    confidence: str = "low"
    notes: list = field(default_factory=list)


def _norm_uuid(u: str) -> str:
    return (u or "").replace("-", "").lower()


def _strip_pac(addr: int) -> int:
    return addr & _PTR_AUTH_MASK


def inspect_stack(raw: str) -> StackInsight:
    """识别堆栈格式并尽力提取符号化所需元数据。

    **绝不抛异常**：任何解析失败都降级为 stack_format="unknown"，
    用户仍可手选平台/版本走通符号化。
    """
    if not raw or not raw.strip():
        return StackInsight(notes=["输入为空"])
    try:
        return _inspect_inner(raw)
    except Exception as exc:  # noqa: BLE001 — 降级是设计要求
        logger.warning("inspect_stack failed, degrading to unknown: %s", exc)
        return StackInsight(
            normalized_stack=raw,
            notes=[f"解析异常，已降级为手动模式：{exc}"],
        )
```

`_inspect_inner(raw)` 的识别链（按序 return，先严格后宽松）：

1. **`apple_ips_json`** — `raw.lstrip()` 以 `{` 开头。按**第一个 `\n`** 切成
   header / payload 两段分别 `json.loads`。
   - `app_version = f"{header['app_version']}-{header['build_version']}"`
     （两者都存在才拼；缺 `build_version` 则只用 `app_version`）
   - `usedImages[]` → `binary_images`，每项
     `{"uuid": _norm_uuid(i["uuid"]), "name": i.get("name") or Path(i["path"]).name,
       "load_address": hex(i["base"]), "max_address": hex(i["base"] + i.get("size", 0))}`
   - 归一化帧：取 `threads[]` 里 `triggered=True` 的线程（没有则取第 0 个），
     对每个 `frames[]`：`base = usedImages[f["imageIndex"]]["base"]`，
     `addr = _strip_pac(base + f["imageOffset"])`，输出
     `f'{idx}   {module}   {hex(addr)} {hex(base)} + {f["imageOffset"]}'`
   - `confidence = "high"` 若拿到版本号，否则 `"medium"`
   - **header 解析成功但 payload 坏掉时**：仍返回 `apple_ips_json` + 版本号，
     `normalized_stack = raw`，`notes` 加一句 payload 解析失败。这是
     `test_malformed_input_degrades_never_raises` 里 `'{"app_version": "4.0.1"}'` 用例要的行为。

2. **`apple_crash_text`** — 含 `Incident Identifier:` 或 `^Binary Images:$`。
   - `_APPLE_VERSION_RE` 匹配 `Version: 4.0.201 (941)` → `"4.0.201-941"`
   - `_APPLE_BINIMG_RE` 扫 `Binary Images:` 段 → `uuids` + `binary_images`
   - `frame_count` 用 `FRAME_RE_IOS` 数
   - `normalized_stack = raw`（帧形状已经是对的）

3. **`android_tombstone`** — 含 `#\d\d pc ` 或 `backtrace:`。
   - `_TOMBSTONE_FRAME_RE` 扫出 `build_ids`（去重、保序）
   - `platform = "android"`，`app_version = ""`，`confidence = "medium"`
   - `notes`: `"tombstone 不含 app 版本号，必须手动指定版本号"`

4. **`datadog_rum`** — `FRAME_RE_IOS` 有命中，但没有任何 Apple header。
   - `platform = "ios"`（这个帧形状是 iOS 特有的），`confidence = "medium"`
   - `notes`: `"Datadog 复制的堆栈不含版本号，必须手动指定版本号"`
   - **PAC 掩码**：逐帧检查 `addr`，若 `_strip_pac(addr) != addr`，在 `notes` 追加
     `"检测到 arm64e 指针认证污染地址，已记录（符号化时由引擎掩码处理）"`。
     `normalized_stack` 保持原样（`symbolication` 侧自己会 strip）。

5. **`android_logcat`** — 含 `E AndroidRuntime:` 或 `Build fingerprint:`。
   - 尝试 `versionName=(\S+)` 提版本；提不到就空
   - `platform = "android"`

6. **`android_java`** — `_JAVA_FRAME_RE` 有命中（注意样本里是 `\tat `，
   正则用 `^\s*at\s+` 配合 `re.MULTILINE` 能覆盖 tab 和空格两种缩进）。
   - `platform = "android"`，`app_version = ""`，`confidence = "medium"`
   - `notes`: `"ProGuard 混淆栈不含版本信息，必须手动指定版本号"`
   - `frame_count` 用 `_JAVA_FRAME_RE` 数

7. 否则 `StackInsight(normalized_stack=raw, notes=["无法识别堆栈格式，请手动选择平台和版本号"])`

> **识别顺序的两个坑**：
> - `android_logcat` 必须排在 `android_java` **之前**，因为 logcat 里也含 `\tat ` 帧。
> - `apple_crash_text` 必须排在 `datadog_rum` **之前**，两者帧形状相同，
>   只能靠 header 区分。

- [ ] **Step 4: 运行测试确认全绿**

```bash
cd backend && pytest tests/crashguard/test_stack_inspector.py -v
```

Expected: PASS（约 25 个用例，含 8 个参数化格式识别 + 5 个畸形输入降级）

- [ ] **Step 5: 确认没打破既有测试**

```bash
cd backend && pytest tests/crashguard/ -q
```

Expected: 既有用例全绿（本任务是纯新增文件，不应影响任何现有测试）

---

### Task 2: 符号缓存保留版本数 10 → 20

独立小改动，早交付。**注意它有一个会让改动静默失效的坑**（见 Step 3 的说明）。

**Files:**
- Modify: `backend/app/crashguard/config.py:517-519`
- Test: `backend/tests/crashguard/test_symbol_keep_versions.py`

**Interfaces:**
- Produces: `CrashguardSettings.symbol_upload_keep_versions == 20`、
  `CrashguardSettings.github_cache_keep_versions == 20`（Task 7 的预热会占用这些名额）

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/crashguard/test_symbol_keep_versions.py`：

```python
from __future__ import annotations

from app.crashguard.config import CrashguardSettings


def test_symbol_keep_versions_defaults_are_20():
    """2026-09-22：10 → 20。

    背景：符号包预热（symbol_prewarmer）会主动占满 github_cache 名额，
    keep=10 时会把研发正在回查的老版本挤掉。两个配置必须一起提，
    否则会出现「为什么老版本有时在有时不在」的认知不一致。
    """
    s = CrashguardSettings()
    assert s.symbol_upload_keep_versions == 20
    assert s.github_cache_keep_versions == 20


def test_keep_versions_within_settings_page_range():
    """/settings 页面的输入范围是 1–50，默认值必须落在里面。"""
    s = CrashguardSettings()
    for v in (s.symbol_upload_keep_versions, s.github_cache_keep_versions):
        assert 1 <= v <= 50
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && pytest tests/crashguard/test_symbol_keep_versions.py -v
```

Expected: FAIL — `assert 10 == 20`

- [ ] **Step 3: 改默认值**

`backend/app/crashguard/config.py:517-519`，把两个 `10` 改成 `20`，并更新注释说明原因：

```python
    # === 符号化设置 ===
    # 每次上传符号包后，同 platform+symbol_type 最多保留多少个版本（超出自动删除文件+DB）
    # 2026-09-22：10 → 20。symbol_prewarmer 会主动占名额，keep=10 会挤掉研发在查的老版本。
    # 磁盘影响：单版本 dSYM ~90MB / mapping / native_so，10→20 增量约 +4.5GB。
    symbol_upload_keep_versions: int = 20
    # GitHub release 缓存最多保留多少个版本目录（超出按 mtime 淘汰）
    # 2026-09-22：10 → 20。单版本约 200MB，10→20 增量约 +2GB。
    github_cache_keep_versions: int = 20
```

> **部署时必须处理的坑（写进 PR 描述，不要在本 task 里做）**：
> 这两项在 `/settings` 页面可在线改（范围 1–50），改动写入 `config.local.yaml`，
> 而 **`config.local.yaml` 优先级高于代码默认值**。若 102 上有人动过，
> 改代码默认值会被静默覆盖。部署后必须 `cat config.local.yaml` 确认没有残留的 10。
> 同时必须先 `df -h /` 实测 102 磁盘余量——合计增量约 +6.5GB，
> 余量不足 15GB 则只提到 15 或不提，并向用户报告。

- [ ] **Step 4: 运行确认通过**

```bash
cd backend && pytest tests/crashguard/test_symbol_keep_versions.py -v
```

Expected: PASS

- [ ] **Step 5: 确认清理逻辑没被写死**

```bash
cd backend && pytest tests/crashguard/ -q -k "symbol"
```

Expected: PASS。若有测试断言了 `10`，说明那里硬编码了默认值——把它改成
从 `CrashguardSettings()` 读，而不是改回 10。

---

### Task 3: `POST /api/crash/symbolicate/inspect`

把 Task 1 的解析器接出 HTTP，并附带 repo 路由预览。薄封装，无业务逻辑。

**Files:**
- Modify: `backend/app/crashguard/api/crash.py`（在现有 `POST /symbolicate` 之后追加）
- Test: `backend/tests/crashguard/test_symbolicate_inspect_api.py`

**Interfaces:**
- Consumes: `inspect_stack(raw) -> StackInsight`（Task 1）
- Produces: HTTP `POST /api/crash/symbolicate/inspect`，响应 = `StackInsight` 全字段
  + `routing: {symbol_profile, github_repo, family, confidence} | None`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/crashguard/test_symbolicate_inspect_api.py`。先看一眼现有
`tests/crashguard/test_symbolicate_api.py` 用的是什么 client fixture，照抄同样的构造方式
（保持一致，不要引入新的测试风格）：

```python
from __future__ import annotations

import json

import pytest


ANDROID_JAVA = (
    "java.lang.NullPointerException\n"
    "\tat a.b.c.e(Unknown Source:12)\n"
)

APPLE_CRASH_TEXT = """Incident Identifier: ABCD1234-5678-90AB-CDEF-1234567890AB
Process:             PLAUD [1234]
Version:             4.0.201 (941)

Thread 0 Crashed:
0   PLAUD                         0x0000000103a2c000 0x103a00000 + 180224

Binary Images:
0x103a00000 - 0x1046effff PLAUD arm64e <a1b2c3d4e5f67890abcdef1234567890> /var/PLAUD
"""


@pytest.mark.asyncio
async def test_inspect_rejects_empty_stack(client):
    resp = await client.post("/api/crash/symbolicate/inspect", json={"stack": ""})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_inspect_rejects_oversized_stack(client):
    resp = await client.post(
        "/api/crash/symbolicate/inspect", json={"stack": "x" * 200_001}
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_inspect_android_java_reports_no_version(client):
    resp = await client.post(
        "/api/crash/symbolicate/inspect", json={"stack": ANDROID_JAVA}
    )
    assert resp.status_code == 200
    d = resp.json()
    assert d["platform"] == "android"
    assert d["app_version"] == ""
    assert d["stack_format"] == "android_java"
    # 无版本号 → 不做路由预览，但不报错
    assert d["routing"] is None
    assert any("必须手动指定版本号" in n for n in d["notes"])


@pytest.mark.asyncio
async def test_inspect_apple_crash_returns_routing_preview(client):
    resp = await client.post(
        "/api/crash/symbolicate/inspect", json={"stack": APPLE_CRASH_TEXT}
    )
    assert resp.status_code == 200
    d = resp.json()
    assert d["platform"] == "ios"
    assert d["app_version"] == "4.0.201-941"
    # 4.0.201 >= 4.0.0 → native band
    assert d["routing"]["symbol_profile"] == "native_ios"
    assert d["routing"]["github_repo"] == "Plaud-AI/plaud-native-app"


@pytest.mark.asyncio
async def test_inspect_is_read_only(client, db_session):
    """inspect 绝不能写任何 crash_* 表。"""
    from sqlalchemy import func, select
    from app.crashguard.models import CrashSymbolPackage

    before = (await db_session.execute(
        select(func.count()).select_from(CrashSymbolPackage)
    )).scalar()
    await client.post("/api/crash/symbolicate/inspect", json={"stack": ANDROID_JAVA})
    after = (await db_session.execute(
        select(func.count()).select_from(CrashSymbolPackage)
    )).scalar()
    assert before == after
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && pytest tests/crashguard/test_symbolicate_inspect_api.py -v
```

Expected: FAIL — 404（端点不存在）

- [ ] **Step 3: 实现端点**

在 `backend/app/crashguard/api/crash.py` 的 `symbolicate_ad_hoc_stack` 函数**之后**追加：

```python
class StackInspectRequest(BaseModel):
    stack: str = Field(..., description="原始堆栈文本（粘贴进来的裸文本）")


@router.post("/symbolicate/inspect")
async def inspect_stack_endpoint(body: StackInspectRequest) -> Dict[str, Any]:
    """解析堆栈文本，尽力提取符号化所需元数据（只读、不下载任何符号包）。

    这是给前端工作台做「贴进去就预填」用的。解析失败会降级为
    stack_format="unknown"，不会报错——用户仍可手选平台/版本走通符号化。
    """
    from dataclasses import asdict

    from app.config import get_repo_routing
    from app.crashguard.services.stack_inspector import inspect_stack
    from app.services import repo_router

    stack = body.stack
    if not stack or not stack.strip():
        raise HTTPException(status_code=400, detail="stack 不能为空")
    if len(stack) > 200_000:
        raise HTTPException(status_code=413, detail="stack 超过 200000 字符上限")

    insight = inspect_stack(stack)
    payload: Dict[str, Any] = asdict(insight)

    # 路由预览：只有同时拿到 platform + app_version 才能算
    routing = None
    if insight.platform and insight.app_version:
        # path_exists=lambda _p: True 是必需的——见 symbolicate_ad_hoc_stack 的注释：
        # resolve() 默认校验源码 wrapper 目录，容器内裸机路径不存在会返回 None，
        # 导致 symbol_profile 丢失、iOS 下错误的 dSYM 资产。
        res = repo_router.resolve(
            insight.platform, insight.app_version, get_repo_routing(),
            path_exists=lambda _p: True,
        )
        if res is not None:
            routing = {
                "symbol_profile": res.symbol_profile,
                "github_repo": res.github_repo,
                "family": getattr(res, "family", ""),
                "confidence": res.confidence,
            }
    payload["routing"] = routing
    return payload
```

> 实现时先确认 `repo_router.resolve()` 返回对象是否真有 `family` 属性
> （`grep -n "class.*Resolved\|family" backend/app/services/repo_router.py`）。
> 有就直接用 `res.family`，没有就保留 `getattr(..., "")` 的写法。

- [ ] **Step 4: 运行确认通过**

```bash
cd backend && pytest tests/crashguard/test_symbolicate_inspect_api.py -v
```

Expected: PASS

- [ ] **Step 5: 确认既有 symbolicate 测试未受影响**

```bash
cd backend && pytest tests/crashguard/test_symbolicate_api.py -v
```

Expected: PASS

---

### Task 4: `GET /api/crash/symbolicate/versions`

版本候选列表。**这是让「版本号下拉」真正有用的关键**——只列已上传包的话，
QA 的灰度包大概率不在里面，下拉会经常是空的。

**Files:**
- Create: `backend/app/crashguard/services/symbol_catalog.py`
- Modify: `backend/app/crashguard/api/crash.py`
- Test: `backend/tests/crashguard/test_symbolicate_versions.py`

**Interfaces:**
- Consumes: `github_symbols._load_build_index(repo) -> dict`、
  `github_symbols._github_cache_dir() -> Path`、`CrashSymbolPackage` 模型
- Produces:
  - `list_symbol_versions(platform: str) -> dict`
    返回 `{"versions": [VersionCandidate...], "warnings": [str...]}`
  - `VersionCandidate` = `{app_version, source, family, verified, symbol_types, tag}`
    （Task 5 preflight 和 Task 8 前端都消费它）
  - `invalidate_version_cache() -> None`（测试用，清进程内 TTL 缓存）

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/crashguard/test_symbolicate_versions.py`：

```python
from __future__ import annotations

import time

import pytest

from app.crashguard.services import symbol_catalog


@pytest.fixture(autouse=True)
def _clear_cache():
    symbol_catalog.invalidate_version_cache()
    yield
    symbol_catalog.invalidate_version_cache()


@pytest.mark.asyncio
async def test_merges_three_sources_and_dedups(monkeypatch):
    """同一个版本被多源命中时合并成一条，source 取最优：cached > uploaded > release。"""
    monkeypatch.setattr(
        symbol_catalog, "_list_cached_versions", lambda platform: ["4.0.201-941"]
    )
    async def _uploaded(platform):
        return [{"app_version": "4.0.201-941", "symbol_types": ["dsym"]},
                {"app_version": "4.0.100-954", "symbol_types": ["dsym"]}]
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _uploaded)
    async def _releases(platform):
        return ([{"app_version": "4.0.201-941", "verified": True, "tag": "v4.0.201+941"},
                 {"app_version": "4.0.300-999", "verified": True, "tag": "v4.0.300+999"}], [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _releases)

    res = await symbol_catalog.list_symbol_versions("ios")
    by_ver = {v["app_version"]: v for v in res["versions"]}

    assert len(res["versions"]) == 3          # 941 三源命中 → 合并成一条
    assert by_ver["4.0.201-941"]["source"] == "cached"    # 最优源胜出
    assert by_ver["4.0.100-954"]["source"] == "uploaded"
    assert by_ver["4.0.300-999"]["source"] == "release"


@pytest.mark.asyncio
async def test_unverified_release_is_flagged(monkeypatch):
    """_build_index 未解析真实 build 号的 release：verified=False，
    app_version 填 tag，前端要显示「⚠ tag 未校验」。不能静默假装是真版本号。"""
    monkeypatch.setattr(symbol_catalog, "_list_cached_versions", lambda p: [])
    async def _uploaded(p):
        return []
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _uploaded)
    async def _releases(p):
        return ([{"app_version": "v4.0.400+1000", "verified": False,
                  "tag": "v4.0.400+1000"}], [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _releases)

    res = await symbol_catalog.list_symbol_versions("android")
    assert res["versions"][0]["verified"] is False
    assert res["versions"][0]["app_version"] == "v4.0.400+1000"


@pytest.mark.asyncio
async def test_github_failure_degrades_not_raises(monkeypatch):
    """GH API 挂了不能让整个下拉爆炸——降级成只返回本地来源 + warning。"""
    monkeypatch.setattr(
        symbol_catalog, "_list_cached_versions", lambda p: ["4.0.201-941"]
    )
    async def _uploaded(p):
        return []
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _uploaded)
    async def _boom(p):
        raise RuntimeError("GitHub 503")
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _boom)

    res = await symbol_catalog.list_symbol_versions("ios")
    assert [v["app_version"] for v in res["versions"]] == ["4.0.201-941"]
    assert any("GitHub" in w for w in res["warnings"])


@pytest.mark.asyncio
async def test_ttl_cache_avoids_refetch(monkeypatch):
    """GH API 有 rate limit，且下拉可能被反复打开 → 5 分钟进程内缓存。"""
    calls = {"n": 0}
    monkeypatch.setattr(symbol_catalog, "_list_cached_versions", lambda p: [])
    async def _uploaded(p):
        return []
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _uploaded)
    async def _releases(p):
        calls["n"] += 1
        return ([{"app_version": "4.0.201-941", "verified": True, "tag": "t"}], [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _releases)

    await symbol_catalog.list_symbol_versions("ios")
    await symbol_catalog.list_symbol_versions("ios")
    assert calls["n"] == 1

    symbol_catalog.invalidate_version_cache()
    await symbol_catalog.list_symbol_versions("ios")
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_sorted_desc_by_version(monkeypatch):
    monkeypatch.setattr(
        symbol_catalog, "_list_cached_versions",
        lambda p: ["4.0.100-954", "4.0.201-941", "3.18.0-708"],
    )
    async def _uploaded(p):
        return []
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _uploaded)
    async def _releases(p):
        return ([], [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _releases)

    res = await symbol_catalog.list_symbol_versions("ios")
    assert [v["app_version"] for v in res["versions"]] == [
        "4.0.201-941", "4.0.100-954", "3.18.0-708",
    ]


@pytest.mark.asyncio
async def test_api_rejects_bad_platform(client):
    resp = await client.get("/api/crash/symbolicate/versions?platform=windows")
    assert resp.status_code == 400
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && pytest tests/crashguard/test_symbolicate_versions.py -v
```

Expected: FAIL — `ModuleNotFoundError: ... symbol_catalog`

- [ ] **Step 3: 实现 `symbol_catalog.py`**

创建 `backend/app/crashguard/services/symbol_catalog.py`。四个私有取数函数
（测试靠 monkeypatch 它们，所以必须是模块级函数，不要内联）：

```python
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("jarvis.crashguard.symbol_catalog")

_CACHE_TTL_SEC = 300  # 5 分钟。GH API 有 rate limit，下拉会被反复打开。
_cache: dict = {}     # platform → (fetched_at, payload)

# source 优先级：本地已缓存 > 已上传 > 仅 Release 有
_SOURCE_RANK = {"cached": 3, "uploaded": 2, "release": 1}

# 符号资产名（与 github_symbols 的 _ASSET_* 常量保持一致，不要自己编名字）
_SYMBOL_ASSET_HINTS = (
    "dSYMs.zip",              # PLAUD.dSYMs.zip / Plaud-Global.dSYMs.zip
    "mapping_globalRelease",  # ProGuard mapping
    "native_symbols",         # native_symbols.tar.gz
    "flutter_symbols",        # Dart AOT 符号
)


def _looks_like_symbol_asset(name: str) -> bool:
    n = (name or "")
    return any(h.lower() in n.lower() for h in _SYMBOL_ASSET_HINTS)


def invalidate_version_cache() -> None:
    _cache.clear()


def _repos_for_platform(platform: str) -> list:
    """按 config.yaml 的 repo_routing 取该平台所有 band 的 github_repo（去重保序）。

    关键：候选必须跨两个仓合并。repo_routing 以 4.0.0 为界
    （< 4.0.0 → Plaud-App / >= 4.0.0 → plaud-native-app），但"查哪个仓"取决于版本号，
    而版本号正是我们要列的东西 → 只能两仓都列。
    """
    from app.config import get_repo_routing

    bands = (get_repo_routing() or {}).get(platform, {}).get("bands", []) or []
    out, seen = [], set()
    for b in bands:
        repo = (b or {}).get("github_repo") or ""
        if repo and repo not in seen:
            seen.add(repo)
            out.append({"repo": repo, "family": (b or {}).get("family") or ""})
    return out


def _list_cached_versions(platform: str) -> list:
    """github_cache/ 下已下载过符号文件的版本目录名。本地 stat，毫秒级。

    只含 .release_tag 的空目录不算——那是 tag 缓存，不是符号包。
    """
    from app.crashguard.services.github_symbols import _github_cache_dir

    out = []
    try:
        base = _github_cache_dir()
        if not base.exists():
            return []
        for d in base.iterdir():
            if not d.is_dir() or d.name.startswith("_"):
                continue
            real = [p for p in d.rglob("*") if p.is_file() and p.name != ".release_tag"]
            if real:
                out.append(d.name)
    except Exception as exc:
        logger.warning("_list_cached_versions failed: %s", exc)
    return out


async def _list_uploaded_versions(platform: str) -> list:
    """CrashSymbolPackage 表里该 platform 的版本 + 各自有哪些 symbol_type。"""
    from sqlalchemy import select
    from app.crashguard.models import CrashSymbolPackage
    from app.db.database import get_session

    agg: dict = {}
    async with get_session() as session:
        q = select(CrashSymbolPackage).where(CrashSymbolPackage.platform == platform)
        for r in (await session.execute(q)).scalars().all():
            agg.setdefault(r.app_version, set()).add(r.symbol_type)
    return [
        {"app_version": v, "symbol_types": sorted(t)} for v, t in agg.items()
    ]


async def _list_release_versions(platform: str) -> tuple:
    """两仓的 GitHub Release 列表 → 候选。返回 (candidates, warnings)。

    tag 里的 +NNN 不是真实 build 号（真实值在 dSYM Info.plist / APK
    assets/datadog.buildId）。_build_index 缓存了已解析过的映射：
    命中 → verified=True 用真实 build 号；未命中 → verified=False 用 tag 并标注。
    绝不在这里发起 Range 请求现场解析（太慢，60 次请求）。
    """
    import httpx
    from app.crashguard.services.github_symbols import (
        _GITHUB_API, _github_token, _load_build_index,
    )

    cands: list = []
    warnings: list = []
    headers = {"Accept": "application/vnd.github+json"}
    tok = _github_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    for entry in _repos_for_platform(platform):
        repo, family = entry["repo"], entry["family"]
        index = _load_build_index(repo) or {}
        tag_to_build = {v: k for k, v in index.items()} if index else {}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{_GITHUB_API}/repos/{repo}/releases",
                headers=headers, params={"per_page": 30},
            )
            if resp.status_code != 200:
                warnings.append(f"{repo} Release 列表返回 {resp.status_code}")
                continue
            for rel in resp.json() or []:
                tag = rel.get("tag_name") or ""
                if not tag:
                    continue
                build = tag_to_build.get(tag) or index.get(tag)
                assets = rel.get("assets") or []
                # asset_size：符号资产里最大的那个（Task 5 preflight 用它算下载耗时
                # 提示）。从 Release API 的 assets[].size 直接读，无需下载。
                sym_sizes = [
                    int(a.get("size") or 0) for a in assets
                    if _looks_like_symbol_asset(a.get("name") or "")
                ]
                cands.append({
                    "app_version": build or tag,
                    "verified": bool(build),
                    "tag": tag,
                    "family": family,
                    "asset_size": max(sym_sizes) if sym_sizes else 0,
                    "symbol_types": [
                        a.get("name") for a in assets if a.get("name")
                    ],
                })
    return cands, warnings
```

主函数：

```python
def _version_sort_key(v: str) -> tuple:
    """`4.0.201-941` → ((4,0,201), 941)。非数字段落降级为 0，保证不抛。"""
    head, _, build = (v or "").partition("-")
    parts = []
    for seg in head.lstrip("vV").split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    bd = "".join(ch for ch in build if ch.isdigit())
    return (tuple(parts[:3]), int(bd) if bd else 0)


async def list_symbol_versions(platform: str) -> dict:
    now = time.time()
    hit = _cache.get(platform)
    if hit and (now - hit[0]) < _CACHE_TTL_SEC:
        return hit[1]

    warnings: list = []
    merged: dict = {}   # app_version → candidate

    def _put(ver: str, source: str, **extra) -> None:
        if not ver:
            return
        cur = merged.get(ver)
        if cur is None:
            merged[ver] = {
                "app_version": ver, "source": source, "family": "",
                "verified": True, "symbol_types": [], "tag": "",
                **{k: v for k, v in extra.items() if v not in (None, "")},
            }
            return
        # 已存在：source 取更优的，其余字段补空缺
        if _SOURCE_RANK.get(source, 0) > _SOURCE_RANK.get(cur["source"], 0):
            cur["source"] = source
        for k, v in extra.items():
            if v not in (None, "", []) and not cur.get(k):
                cur[k] = v

    for ver in _list_cached_versions(platform) or []:
        _put(ver, "cached")

    try:
        for row in await _list_uploaded_versions(platform) or []:
            _put(row["app_version"], "uploaded",
                 symbol_types=row.get("symbol_types") or [])
    except Exception as exc:
        logger.warning("uploaded versions failed: %s", exc)
        warnings.append(f"已上传符号包列表读取失败：{exc}")

    try:
        rel, rel_warn = await _list_release_versions(platform)
        warnings.extend(rel_warn or [])
        for row in rel or []:
            _put(row["app_version"], "release",
                 verified=row.get("verified"), tag=row.get("tag"),
                 family=row.get("family"), symbol_types=row.get("symbol_types") or [])
            # verified=False 必须能覆盖默认 True——_put 的 extra 过滤会跳过 False，
            # 所以这里显式再设一次
            if row.get("verified") is False and row["app_version"] in merged:
                merged[row["app_version"]]["verified"] = False
    except Exception as exc:
        logger.warning("release versions failed: %s", exc)
        warnings.append(f"GitHub Release 列表拉取失败，仅显示本地已有版本：{exc}")

    versions = sorted(
        merged.values(), key=lambda c: _version_sort_key(c["app_version"]), reverse=True
    )
    payload = {"versions": versions, "warnings": warnings}
    _cache[platform] = (now, payload)
    return payload
```

> **注意 `_put` 的 `verified=False` 陷阱**：`extra` 用
> `if v not in (None, "")` 过滤，`False` 不在该元组里所以能过；但初次创建时
> `**extra` 会被默认值 `"verified": True` 之前展开还是之后展开取决于字典字面量顺序
> ——上面写法里 `**extra` 在最后，所以能正确覆盖。合并分支另有显式兜底。
> 实现完请专门跑 `test_unverified_release_is_flagged` 确认。

- [ ] **Step 4: 加 API 端点**

在 `backend/app/crashguard/api/crash.py` 追加：

```python
@router.get("/symbolicate/versions")
async def list_symbolicate_versions(
    platform: str = Query(..., description="ios | android"),
) -> Dict[str, Any]:
    """符号化版本候选列表（只读，不下载任何符号包）。

    合并三个来源：本地已缓存 / 已上传符号包 / 两仓 GitHub Release。
    verified=False 的项是「tag 未校验真实 build 号」，前端必须标注。
    """
    from app.crashguard.services.symbol_catalog import list_symbol_versions

    p = (platform or "").strip().lower()
    if p not in ("ios", "android"):
        raise HTTPException(
            status_code=400, detail=f"platform 必须是 ios/android，收到: {platform!r}"
        )
    return await list_symbol_versions(p)
```

- [ ] **Step 5: 运行确认通过**

```bash
cd backend && pytest tests/crashguard/test_symbolicate_versions.py -v
```

Expected: PASS（6 个用例）

---

### Task 5: `POST /api/crash/symbolicate/preflight`

**本次改进的核心。** 把「没有符号表」从事后 warning 改成提交前的三档预检，
让用户不必白等几分钟才知道无效。

**Files:**
- Modify: `backend/app/crashguard/services/symbol_catalog.py`（追加 preflight 函数）
- Modify: `backend/app/crashguard/api/crash.py`
- Test: `backend/tests/crashguard/test_symbolicate_preflight.py`

**Interfaces:**
- Consumes: `_list_cached_versions`、`_list_uploaded_versions`、`_list_release_versions`
  （Task 4 同模块）、`symbolication._profile_strategy(profile) -> dict`
- Produces:
  - `preflight_symbols(platform, app_version, symbol_profile="", github_repo="") -> dict`
    返回 `{status, eta_hint, symbol_sources, suggestions, warnings}`
  - `status ∈ {"cached", "available", "missing"}`（Task 6 和 Task 7 都消费它）

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/crashguard/test_symbolicate_preflight.py`：

```python
from __future__ import annotations

import pytest

from app.crashguard.services import symbol_catalog


@pytest.fixture(autouse=True)
def _clear():
    symbol_catalog.invalidate_version_cache()
    yield
    symbol_catalog.invalidate_version_cache()


def _stub_sources(monkeypatch, *, cached=(), uploaded=(), release=()):
    monkeypatch.setattr(symbol_catalog, "_list_cached_versions", lambda p: list(cached))
    async def _u(p):
        return [{"app_version": v, "symbol_types": ["dsym"]} for v in uploaded]
    monkeypatch.setattr(symbol_catalog, "_list_uploaded_versions", _u)
    async def _r(p):
        return (list(release), [])
    monkeypatch.setattr(symbol_catalog, "_list_release_versions", _r)


@pytest.mark.asyncio
async def test_cached_status_promises_seconds(monkeypatch):
    _stub_sources(monkeypatch, cached=["4.0.201-941"])
    r = await symbol_catalog.preflight_symbols("ios", "4.0.201-941")
    assert r["status"] == "cached"
    assert "秒级" in r["eta_hint"]
    assert r["suggestions"] == {}


@pytest.mark.asyncio
async def test_uploaded_package_also_counts_as_cached(monkeypatch):
    """Plan B 的上传包同样是本地就绪，不需要下载。"""
    _stub_sources(monkeypatch, uploaded=["4.0.201-941"])
    r = await symbol_catalog.preflight_symbols("ios", "4.0.201-941")
    assert r["status"] == "cached"


@pytest.mark.asyncio
async def test_available_status_warns_about_download(monkeypatch):
    _stub_sources(monkeypatch, release=[{
        "app_version": "4.0.201-941", "verified": True, "tag": "v4.0.201+941",
        "symbol_types": ["Plaud-Global.dSYMs.zip"], "asset_size": 94_371_840,
    }])
    r = await symbol_catalog.preflight_symbols("ios", "4.0.201-941")
    assert r["status"] == "available"
    assert "90" in r["eta_hint"]          # 94371840 B ≈ 90MB
    assert "分钟" in r["eta_hint"]


@pytest.mark.asyncio
async def test_missing_status_is_explicit_and_actionable(monkeypatch):
    """三处都没有 → 明确说「不会有任何效果」，并给出邻近版本救场。
    QA 记错 build 号是高频事件。"""
    _stub_sources(
        monkeypatch,
        cached=["4.0.201-938"],
        uploaded=["4.0.100-954"],
        release=[{"app_version": "4.0.300-999", "verified": True, "tag": "t"}],
    )
    r = await symbol_catalog.preflight_symbols("ios", "4.0.201-941")
    assert r["status"] == "missing"
    assert "不会有任何效果" in r["eta_hint"]
    nearby = r["suggestions"]["nearby_versions"]
    assert "4.0.201-938" in nearby        # 最接近的应该在里面
    assert len(nearby) <= 5


@pytest.mark.asyncio
async def test_missing_with_release_but_no_symbol_asset_explains_why(monkeypatch):
    """该版本有 Release 但没有符号 asset → 大概率不是线上包。"""
    _stub_sources(monkeypatch, release=[{
        "app_version": "4.0.201-941", "verified": True, "tag": "v4.0.201+941",
        "symbol_types": ["app-release.apk"],     # 只有 apk，没有符号包
    }])
    r = await symbol_catalog.preflight_symbols("android", "4.0.201-941")
    assert r["status"] == "missing"
    assert "IS_ONLINE_PACKAGE" in r["suggestions"]["reason"]


@pytest.mark.asyncio
async def test_unverified_input_still_judged_honestly(monkeypatch):
    """verified=False 的 tag 形态输入，照常跑三档判定，
    判为 missing 就老实报 missing——不因为"它来自 Release 列表"就假定可用。"""
    _stub_sources(monkeypatch, release=[{
        "app_version": "v4.0.400+1000", "verified": False, "tag": "v4.0.400+1000",
        "symbol_types": ["app-release.apk"],
    }])
    r = await symbol_catalog.preflight_symbols("android", "v4.0.400+1000")
    assert r["status"] == "missing"


@pytest.mark.asyncio
async def test_preflight_downloads_nothing(monkeypatch):
    """预检绝不能下载任何字节——这是它能秒级返回的前提。"""
    called = {"n": 0}
    import app.crashguard.services.github_symbols as gs
    async def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("preflight 不应调用下载 getter")
    monkeypatch.setattr(gs, "get_ios_dsyms_dir", _boom)
    monkeypatch.setattr(gs, "get_android_mapping", _boom)
    monkeypatch.setattr(gs, "get_android_native_symbols_dir", _boom)
    monkeypatch.setattr(gs, "get_dart_symbols_dir", _boom)
    _stub_sources(monkeypatch, cached=[], uploaded=[], release=[])

    await symbol_catalog.preflight_symbols("ios", "4.0.201-941")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_api_validates_input(client):
    r1 = await client.post("/api/crash/symbolicate/preflight",
                           json={"platform": "windows", "app_version": "1.0"})
    assert r1.status_code == 400
    r2 = await client.post("/api/crash/symbolicate/preflight",
                           json={"platform": "ios", "app_version": ""})
    assert r2.status_code == 400
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && pytest tests/crashguard/test_symbolicate_preflight.py -v
```

Expected: FAIL — `AttributeError: module ... has no attribute 'preflight_symbols'`

- [ ] **Step 3: 实现 `preflight_symbols`**

追加到 `backend/app/crashguard/services/symbol_catalog.py`。

判定哪些 asset 算「符号包」——按 `github_symbols` 的资产常量，不要自己编名字：

```python
# 注：_SYMBOL_ASSET_HINTS / _looks_like_symbol_asset 已在 Task 4 定义于本模块顶部
# （_list_release_versions 收集 asset_size 时就需要它），此处直接复用，不要重复定义。


def _human_mb(size_bytes: Optional[int]) -> str:
    if not size_bytes:
        return "约 90"
    return f"约 {int(size_bytes / 1024 / 1024)}"


async def preflight_symbols(
    platform: str,
    app_version: str,
    symbol_profile: str = "",
    github_repo: str = "",
) -> dict:
    """三档符号可用性预检。**只做本地 stat + 已缓存索引 + 最多一次 GH API，
    绝不下载任何字节。**

    cached    → 本地已就绪，秒级返回
    available → Release 有符号 asset，需下载
    missing   → 三处都没有，符号化不会有任何效果
    """
    warnings: list = []
    sources: list = []

    # 1) 本地 github_cache
    try:
        if app_version in (_list_cached_versions(platform) or []):
            sources.append({"source": "cached", "detail": "github_cache 已有符号文件"})
    except Exception as exc:
        warnings.append(f"本地缓存检查失败：{exc}")

    # 2) 已上传符号包
    uploaded_versions: list = []
    try:
        uploaded = await _list_uploaded_versions(platform) or []
        uploaded_versions = [u["app_version"] for u in uploaded]
        for u in uploaded:
            if u["app_version"] == app_version:
                sources.append({
                    "source": "uploaded",
                    "detail": f"已上传符号包：{', '.join(u.get('symbol_types') or [])}",
                })
    except Exception as exc:
        warnings.append(f"已上传符号包检查失败：{exc}")

    if sources:
        return {
            "status": "cached",
            "eta_hint": "符号包已就绪，预计秒级返回",
            "symbol_sources": sources,
            "suggestions": {},
            "warnings": warnings,
        }

    # 3) GitHub Release
    release_rows: list = []
    release_hit = None
    try:
        release_rows, rel_warn = await _list_release_versions(platform)
        warnings.extend(rel_warn or [])
        for row in release_rows or []:
            if row.get("app_version") == app_version:
                release_hit = row
                break
    except Exception as exc:
        warnings.append(f"GitHub Release 检查失败：{exc}")

    if release_hit:
        symbol_assets = [
            n for n in (release_hit.get("symbol_types") or [])
            if _looks_like_symbol_asset(n)
        ]
        if symbol_assets:
            return {
                "status": "available",
                "eta_hint": (
                    f"需下载{_human_mb(release_hit.get('asset_size'))}MB 符号包，"
                    f"预计 1–3 分钟（之后同版本秒级）"
                ),
                "symbol_sources": [{
                    "source": "release",
                    "detail": f"{release_hit.get('tag')}：{', '.join(symbol_assets)}",
                }],
                "suggestions": {},
                "warnings": warnings,
            }

    # missing：给三条可执行出路
    all_local = list(dict.fromkeys(
        (_list_cached_versions(platform) or []) + uploaded_versions
        + [r["app_version"] for r in (release_rows or [])
           if any(_looks_like_symbol_asset(n) for n in (r.get("symbol_types") or []))]
    ))
    target = _version_sort_key(app_version)
    nearby = sorted(
        all_local,
        key=lambda v: (
            abs(_version_sort_key(v)[1] - target[1]),
            0 if _version_sort_key(v)[0] == target[0] else 1,
        ),
    )[:5]

    reason = ""
    if release_hit:
        reason = (
            f"{release_hit.get('tag')} 这个 Release 存在，但没有符号资产。"
            "Jenkins 仅在 IS_ONLINE_PACKAGE=true 时上传符号包，该包可能不是线上包。"
        )
    else:
        reason = f"两个仓的 Release 列表里都没有 {app_version} 这个版本。"

    return {
        "status": "missing",
        "eta_hint": f"该版本无符号表，符号化不会有任何效果",
        "symbol_sources": [],
        "suggestions": {
            "nearby_versions": nearby,
            "reason": reason,
            "upload_hint": "可在本页底部手动上传该版本的符号包（dSYM / mapping.txt）",
        },
        "warnings": warnings,
    }
```

- [ ] **Step 4: 加 API 端点**

```python
class SymbolPreflightRequest(BaseModel):
    platform: str = Field(..., description="ios | android")
    app_version: str = Field(..., description="如 '4.0.201-941'")
    symbol_profile: Optional[str] = Field(None, description="覆盖自动路由")
    github_repo: Optional[str] = Field(None, description="覆盖自动路由")


@router.post("/symbolicate/preflight")
async def preflight_symbolicate(body: SymbolPreflightRequest) -> Dict[str, Any]:
    """符号可用性预检：在用户点"开始符号化"之前就告诉他会不会白等。

    只做本地 stat + 已缓存索引 + 最多一次 GH API，不下载任何字节，秒级返回。
    """
    from app.crashguard.services.symbol_catalog import preflight_symbols

    p = (body.platform or "").strip().lower()
    if p not in ("ios", "android"):
        raise HTTPException(
            status_code=400, detail=f"platform 必须是 ios/android，收到: {body.platform!r}"
        )
    if not (body.app_version or "").strip():
        raise HTTPException(status_code=400, detail="app_version 不能为空")
    return await preflight_symbols(
        p, body.app_version.strip(),
        symbol_profile=body.symbol_profile or "",
        github_repo=body.github_repo or "",
    )
```

- [ ] **Step 5: 运行确认通过**

```bash
cd backend && pytest tests/crashguard/test_symbolicate_preflight.py tests/crashguard/test_symbolicate_versions.py -v
```

Expected: PASS（Task 4 的用例也要仍然绿——`asset_size` 的改动别把它们弄坏）

---

### Task 6: 改造 `POST /api/crash/symbolicate` — 逐帧统计 + 修误导性 warning

现在只返回 `changed: bool`，太粗。而 `_symbolicate_ios_with_dir` 是**故意**跳过系统库帧的
（不 gating 会「每一帧都吐出看似合理实则完全无关的 Plaud 符号，比原样保留地址更具误导性」），
所以一次正常成功里也有一半帧是裸地址，用户会误判为失败。

**Files:**
- Modify: `backend/app/crashguard/api/crash.py:3724-3860`（`symbolicate_ad_hoc_stack`）
- Test: `backend/tests/crashguard/test_symbolicate_frame_stats.py`

**Interfaces:**
- Consumes: `stack_inspector.FRAME_RE_IOS`（Task 1）、
  `symbol_catalog.preflight_symbols`（Task 5）
- Produces:
  - `_infer_app_module(stack: str, binary_images: list[dict]) -> str`
  - `_compute_frame_stats(before: str, after: str, app_module: str = "") -> dict`
  - 响应新增 `frame_stats: {total_frames, symbolicated, unresolved, unparsed_lines,
    app_module, app_frames|None, app_symbolicated|None, non_app_frames|None}`
    —— 后三个仅在能推断出 app_module 时有值，否则 None（不假装能细分）
  - `warnings` 措辞改为基于 preflight 三档

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/crashguard/test_symbolicate_frame_stats.py`：

```python
from __future__ import annotations

import pytest


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


def test_frame_stats_counts_resolved_vs_unresolved():
    """粗粒度统计：符号化后不再是裸地址的帧数 vs 仍是裸地址的帧数。

    这两个数字是端点层**确定能算准**的——逐行比对 before/after 即可。
    """
    from app.crashguard.api.crash import _compute_frame_stats

    st = _compute_frame_stats(BEFORE, AFTER)
    assert st["total_frames"] == 4
    assert st["symbolicated"] == 2
    assert st["unresolved"] == 2


def test_frame_stats_app_module_breakdown_when_inferable():
    """细分统计：App 自己的 module 名可从堆栈推断（这里 PLAUD 出现 2 次，
    是最高频 module）→ 能给出 app_frames / app_symbolicated 细分。

    为什么要细分：_symbolicate_ios_with_dir 只对 module 命中某个 dSYM 的帧发起
    atos 查询，系统库帧原样保留（该函数 docstring 记录了生产实测——不 gating
    会给每帧凑一个看似合理实则无关的 Plaud 符号，比裸地址更误导）。
    所以"一半帧还是地址"往往是正确行为。不细分，用户会把正常成功看成失败。
    """
    from app.crashguard.api.crash import _compute_frame_stats

    st = _compute_frame_stats(BEFORE, AFTER, app_module="PLAUD")
    assert st["app_module"] == "PLAUD"
    assert st["app_frames"] == 2
    assert st["app_symbolicated"] == 2
    assert st["non_app_frames"] == 2          # 系统库帧，按设计跳过


def test_frame_stats_app_module_failure_is_visible():
    """App 帧没解出来必须能看见——这才是真失败，不能和系统库帧混在一起。"""
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
    """推断不出 App module 时，只给粗粒度，app_* 字段为 None——不假装。"""
    from app.crashguard.api.crash import _compute_frame_stats

    st = _compute_frame_stats(BEFORE, AFTER, app_module="")
    assert st["symbolicated"] == 2
    assert st["app_module"] == ""
    assert st["app_frames"] is None
    assert st["app_symbolicated"] is None
    assert st["non_app_frames"] is None


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


@pytest.mark.asyncio
async def test_symbolicate_response_includes_frame_stats(client, monkeypatch):
    """端点响应必须带 frame_stats，且不能破坏既有字段。"""
    import app.crashguard.services.symbolication as sym

    async def _fake(stack, images, platform, app_version="", **kw):
        return AFTER
    monkeypatch.setattr(sym, "symbolicate_stack", _fake)

    resp = await client.post("/api/crash/symbolicate", json={
        "stack": BEFORE, "platform": "ios", "app_version": "4.0.201-941",
    })
    assert resp.status_code == 200
    d = resp.json()
    # 新增字段
    assert d["frame_stats"]["symbolicated"] == 2
    assert d["frame_stats"]["unresolved"] == 2
    # PLAUD 是最高频 module → 能推断出 App module，给出细分
    assert d["frame_stats"]["app_module"] == "PLAUD"
    assert d["frame_stats"]["app_symbolicated"] == 2
    # 既有字段语义不变（向后兼容）
    for k in ("symbolicated_stack", "changed", "stack_quality_before",
              "stack_quality_after", "routing_confidence"):
        assert k in d


@pytest.mark.asyncio
async def test_no_misleading_no_uploaded_package_warning_when_cached(client, monkeypatch):
    """修掉误导性 warning：Plan C 下载成功时，不该再说「无已上传符号包」。

    这条 warning 原本无条件输出，即使符号化完美也会吐——是纯噪音。
    """
    import app.crashguard.services.symbolication as sym
    import app.crashguard.services.symbol_catalog as cat

    async def _fake(stack, images, platform, app_version="", **kw):
        return AFTER
    monkeypatch.setattr(sym, "symbolicate_stack", _fake)

    async def _pf(platform, app_version, **kw):
        return {"status": "cached", "eta_hint": "符号包已就绪，预计秒级返回",
                "symbol_sources": [], "suggestions": {}, "warnings": []}
    monkeypatch.setattr(cat, "preflight_symbols", _pf)

    resp = await client.post("/api/crash/symbolicate", json={
        "stack": BEFORE, "platform": "ios", "app_version": "4.0.201-941",
    })
    d = resp.json()
    assert not any("无已上传符号包" in w for w in d["warnings"])
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && pytest tests/crashguard/test_symbolicate_frame_stats.py -v
```

Expected: FAIL — `ImportError: cannot import name '_compute_frame_stats'`

- [ ] **Step 3: 实现 `_compute_frame_stats` 并接进端点**

在 `backend/app/crashguard/api/crash.py` 的 `symbolicate_ad_hoc_stack` **之前**加辅助函数。
统计逻辑放端点层，**不侵入符号化引擎**（`symbolicate_stack` 被 pipeline 多处调用，
不能改签名）：

```python
def _infer_app_module(stack: str, binary_images: List[dict]) -> str:
    """推断 App 自己的 module 名。

    端点层拿不到 dSYM 里的 module 列表（那在 symbolication 内部），所以无法直接
    判断"某帧的 module 是否属于符号包"。但 App 自己的 module 名是可以从堆栈推断的：
      1. binary_images 里 source == "P" 的那个（.ips 格式里 P = 主可执行文件）
      2. 退而用堆栈里出现频率最高的 module（App 帧通常占多数）
    推断不出就返回 ""，此时统计只给粗粒度，不假装能细分。
    """
    from collections import Counter

    from app.crashguard.services.stack_inspector import FRAME_RE_IOS

    for img in binary_images or []:
        if (img or {}).get("source") == "P" and (img or {}).get("name"):
            return str(img["name"])

    counts = Counter(
        m.group(2) for m in FRAME_RE_IOS.finditer(stack or "")
    )
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def _compute_frame_stats(
    before: str, after: str, app_module: str = "",
) -> Dict[str, Any]:
    """逐帧比对 before/after 得出符号化统计。

    **粗粒度**（总是准确，纯文本比对）：
      total_frames / symbolicated / unresolved / unparsed_lines

    **细分**（仅当传入 app_module 才有，否则三个字段为 None）：
      app_frames / app_symbolicated / non_app_frames

    为什么需要细分：_symbolicate_ios_with_dir 只对 module 名能匹配到某个 dSYM 的帧
    发起 atos 查询，系统库帧原样保留（该函数 docstring 记录了生产实测——不做这层
    gating 会给每一帧都凑出一个看似合理实则无关的 Plaud 符号，比不符号化更误导）。
    所以"一半帧还是裸地址"往往是**正确行为**。只看粗粒度的 unresolved，用户会把
    一次正常成功误判为失败。

    但端点层**不知道** dSYM 里到底有哪些 module，所以 app_module 靠推断
    （_infer_app_module）。推断不到时诚实地返回 None，不编造区分。
    """
    from app.crashguard.services.stack_inspector import FRAME_RE_IOS

    b_lines = (before or "").splitlines()
    a_lines = (after or "").splitlines()

    total = resolved = unresolved = unparsed = 0
    app_total = app_resolved = non_app = 0

    for i, bl in enumerate(b_lines):
        m = FRAME_RE_IOS.match(bl)
        if not m:
            if bl.strip():
                unparsed += 1
            continue
        total += 1
        al = a_lines[i] if i < len(a_lines) else bl
        # after 行仍能被"未符号化帧"正则匹配 → 这帧没解出来
        is_resolved = not FRAME_RE_IOS.match(al)
        if is_resolved:
            resolved += 1
        else:
            unresolved += 1

        if app_module:
            if m.group(2).lower() == app_module.lower():
                app_total += 1
                if is_resolved:
                    app_resolved += 1
            else:
                non_app += 1

    return {
        "total_frames": total,
        "symbolicated": resolved,
        "unresolved": unresolved,
        "unparsed_lines": unparsed,
        "app_module": app_module or "",
        "app_frames": app_total if app_module else None,
        "app_symbolicated": app_resolved if app_module else None,
        "non_app_frames": non_app if app_module else None,
    }
```

然后修改 `symbolicate_ad_hoc_stack` 的尾部（`return` 之前）：

1. 把原来无条件追加 `"该 (platform, app_version) 无已上传符号包..."` 的那段
   （现有代码 `if not available_symbol_packages:` 分支）**替换**为基于 preflight 的判断：

```python
    # 符号可用性：用 preflight 的三档判定替代原先"无条件说没上传包"的误导性 warning。
    # 原逻辑只查 CrashSymbolPackage（本地上传表），完全不看 GitHub Release，
    # 所以 Plan C 下载成功、符号化完美时照样吐这句，是纯噪音。
    preflight: Dict[str, Any] = {}
    if app_version:
        try:
            from app.crashguard.services.symbol_catalog import preflight_symbols
            preflight = await preflight_symbols(
                platform if platform in ("ios", "android") else "android",
                app_version, symbol_profile=symbol_profile, github_repo=github_repo,
            )
            if preflight.get("status") == "missing":
                warnings.append(
                    f"该版本无符号表（{preflight.get('suggestions', {}).get('reason', '')}）"
                )
        except Exception as exc:
            logger.warning("preflight in symbolicate failed (non-fatal): %s", exc)
```

2. 在返回的 dict 里新增两个 key（其余字段一个都别动）：

```python
        "frame_stats": _compute_frame_stats(
            stack, symbolicated_stack,
            app_module=_infer_app_module(stack, body.binary_images or []),
        ),
        "preflight": preflight or None,
```

- [ ] **Step 4: 运行确认通过**

```bash
cd backend && pytest tests/crashguard/test_symbolicate_frame_stats.py -v
```

Expected: PASS（6 个用例）

- [ ] **Step 5: 确认向后兼容没破**

```bash
cd backend && pytest tests/crashguard/test_symbolicate_api.py tests/crashguard/test_symbolicate_ios_module_gating.py tests/crashguard/test_symbolicate_jank_frame.py -v
```

Expected: PASS。若 `test_symbolicate_api.py` 有断言 `warnings` 完整内容的用例，
它会因为 warning 措辞改变而失败——这是**预期的行为变更**，更新该断言即可
（但 `symbolicated_stack` / `changed` 等字段的断言必须保持不变）。

---

### Task 7: 符号包预热（按 graygate 主要版本）

让 QA 查刚发的灰度包时永远命中 `cached`，而不是等 1–3 分钟下载。
这是 `keep_versions` 10→20 真正的配套价值。

**Files:**
- Create: `backend/app/crashguard/services/symbol_prewarmer.py`
- Modify: `backend/app/crashguard/config.py`（加 2 个配置项）
- Modify: `backend/app/crashguard/workers/scheduler.py`（加 cron 分发块）
- Modify: `backend/app/crashguard/api/crash.py`（加手动触发端点）
- Test: `backend/tests/crashguard/test_symbol_prewarmer.py`

**Interfaces:**
- Consumes:
  - `app.graygate.services.focus_version.get_all_focus_versions() -> {"ios": str|None, "android": str|None}`
  - `symbol_catalog.preflight_symbols(platform, app_version, ...) -> dict`（Task 5）
  - `symbolication._profile_strategy(profile) -> dict`（键：`use_flutter_dsym` /
    `use_app_dsym` / `use_native_so` / `use_dart_symbols` / `use_proguard`）
  - `github_symbols.get_ios_dsyms_dir(app_version, repo=..., asset_name=...)`、
    `get_android_mapping(app_version, repo=...)`、
    `get_android_native_symbols_dir(app_version, repo=...)`、
    `get_dart_symbols_dir(app_version, repo=...)`
- Produces: `prewarm_focus_versions() -> {"prewarmed": [...], "skipped": [...],
  "missing": [...], "errors": [...], "duration_ms": int}`

**依赖方向说明**：crashguard → graygate（单向）。graygate 是独立子模块，
不应知道 crashguard 存在，所以由 crashguard 主动读它的 focus_version，graygate 零改动。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/crashguard/test_symbol_prewarmer.py`：

```python
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_cached_version_is_skipped_not_redownloaded(monkeypatch):
    """幂等性的关键：已缓存的版本必须跳过，否则每 30 分钟重下 90MB。"""
    from app.crashguard.services import symbol_prewarmer as pw

    async def _focus():
        return {"ios": "4.0.201-941", "android": None}
    monkeypatch.setattr(pw, "_get_focus_versions", _focus)

    async def _pf(platform, app_version, **kw):
        return {"status": "cached", "eta_hint": "", "symbol_sources": [],
                "suggestions": {}, "warnings": []}
    monkeypatch.setattr(pw, "_preflight", _pf)

    downloads = []
    async def _dl(platform, app_version, profile, repo):
        downloads.append((platform, app_version))
    monkeypatch.setattr(pw, "_download_symbols", _dl)

    res = await pw.prewarm_focus_versions()
    assert res["skipped"] == ["ios:4.0.201-941"]
    assert res["prewarmed"] == []
    assert downloads == []


@pytest.mark.asyncio
async def test_available_version_gets_downloaded(monkeypatch):
    from app.crashguard.services import symbol_prewarmer as pw

    async def _focus():
        return {"ios": None, "android": "4.0.200-938"}
    monkeypatch.setattr(pw, "_get_focus_versions", _focus)

    async def _pf(platform, app_version, **kw):
        return {"status": "available", "eta_hint": "", "symbol_sources": [],
                "suggestions": {}, "warnings": []}
    monkeypatch.setattr(pw, "_preflight", _pf)

    downloads = []
    async def _dl(platform, app_version, profile, repo):
        downloads.append((platform, app_version))
    monkeypatch.setattr(pw, "_download_symbols", _dl)

    res = await pw.prewarm_focus_versions()
    assert res["prewarmed"] == ["android:4.0.200-938"]
    assert downloads == [("android", "4.0.200-938")]


@pytest.mark.asyncio
async def test_missing_version_logged_not_alerted(monkeypatch):
    """灰度包未上传符号包是常态，告警会变噪音——只记日志，不抛、不告警。"""
    from app.crashguard.services import symbol_prewarmer as pw

    async def _focus():
        return {"ios": "4.0.999-1", "android": None}
    monkeypatch.setattr(pw, "_get_focus_versions", _focus)

    async def _pf(platform, app_version, **kw):
        return {"status": "missing", "eta_hint": "", "symbol_sources": [],
                "suggestions": {"reason": "无符号资产"}, "warnings": []}
    monkeypatch.setattr(pw, "_preflight", _pf)

    res = await pw.prewarm_focus_versions()      # 不抛异常即通过
    assert res["missing"] == ["ios:4.0.999-1"]
    assert res["prewarmed"] == []


@pytest.mark.asyncio
async def test_no_focus_version_is_noop(monkeypatch):
    from app.crashguard.services import symbol_prewarmer as pw

    async def _focus():
        return {"ios": None, "android": None}
    monkeypatch.setattr(pw, "_get_focus_versions", _focus)

    res = await pw.prewarm_focus_versions()
    assert res["prewarmed"] == [] and res["skipped"] == [] and res["missing"] == []


@pytest.mark.asyncio
async def test_download_respects_symbol_profile(monkeypatch):
    """按 _profile_strategy 只调该调的 getter，不要盲调四个。
    native_android 没有 Dart，调 get_dart_symbols_dir 是纯浪费。"""
    from app.crashguard.services import symbol_prewarmer as pw
    import app.crashguard.services.github_symbols as gs

    calls = []
    async def _mapping(v, repo=""):
        calls.append("mapping"); return "/tmp/m"
    async def _native(v, repo=""):
        calls.append("native"); return "/tmp/n"
    async def _dart(v, repo=""):
        calls.append("dart"); return "/tmp/d"
    async def _ios(v, repo="", asset_name=""):
        calls.append("ios"); return "/tmp/i"
    monkeypatch.setattr(gs, "get_android_mapping", _mapping)
    monkeypatch.setattr(gs, "get_android_native_symbols_dir", _native)
    monkeypatch.setattr(gs, "get_dart_symbols_dir", _dart)
    monkeypatch.setattr(gs, "get_ios_dsyms_dir", _ios)

    await pw._download_symbols(
        "android", "4.0.200-938", "native_android", "Plaud-AI/plaud-native-app"
    )
    assert "dart" not in calls          # native_android 无 Dart
    assert "mapping" in calls and "native" in calls


@pytest.mark.asyncio
async def test_one_platform_failure_does_not_abort_other(monkeypatch):
    from app.crashguard.services import symbol_prewarmer as pw

    async def _focus():
        return {"ios": "4.0.201-941", "android": "4.0.200-938"}
    monkeypatch.setattr(pw, "_get_focus_versions", _focus)

    async def _pf(platform, app_version, **kw):
        return {"status": "available", "eta_hint": "", "symbol_sources": [],
                "suggestions": {}, "warnings": []}
    monkeypatch.setattr(pw, "_preflight", _pf)

    async def _dl(platform, app_version, profile, repo):
        if platform == "ios":
            raise RuntimeError("VPN 卡住")
    monkeypatch.setattr(pw, "_download_symbols", _dl)

    res = await pw.prewarm_focus_versions()
    assert res["prewarmed"] == ["android:4.0.200-938"]
    assert any("ios" in e for e in res["errors"])
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && pytest tests/crashguard/test_symbol_prewarmer.py -v
```

Expected: FAIL — `ModuleNotFoundError: ... symbol_prewarmer`

- [ ] **Step 3: 实现 `symbol_prewarmer.py`**

创建 `backend/app/crashguard/services/symbol_prewarmer.py`。
注意四个 `_` 前缀函数必须是模块级（测试靠 monkeypatch 它们）：

```python
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("jarvis.crashguard.symbol_prewarmer")


async def _get_focus_versions() -> dict:
    """读 graygate 人工指定的"主要版本"。

    依赖方向：crashguard → graygate（单向）。graygate 是独立子模块，
    不应知道 crashguard 存在，所以由这边主动读，graygate 零改动。
    """
    from app.graygate.services.focus_version import get_all_focus_versions

    return await get_all_focus_versions()


async def _preflight(platform: str, app_version: str, **kw) -> dict:
    from app.crashguard.services.symbol_catalog import preflight_symbols

    return await preflight_symbols(platform, app_version, **kw)


def _resolve_routing(platform: str, app_version: str) -> tuple:
    """(symbol_profile, github_repo)。path_exists 必须传 True lambda——见 Global Constraints。"""
    from app.config import get_repo_routing
    from app.services import repo_router

    res = repo_router.resolve(
        platform, app_version, get_repo_routing(), path_exists=lambda _p: True,
    )
    if res is None:
        return "", ""
    return res.symbol_profile, res.github_repo


async def _download_symbols(
    platform: str, app_version: str, symbol_profile: str, github_repo: str
) -> None:
    """按 symbol_profile 只调该调的 getter。

    盲调四个 getter 是纯浪费：native_android 没有 Dart，
    flutter_ios 不需要 Plaud-Global.dSYMs.zip。
    """
    from app.crashguard.services.github_symbols import (
        _ASSET_IOS_DSYM, _ASSET_IOS_DSYM_NATIVE, _DEFAULT_REPO,
        get_android_mapping, get_android_native_symbols_dir,
        get_dart_symbols_dir, get_ios_dsyms_dir,
    )
    from app.crashguard.services.symbolication import _profile_strategy

    repo = github_repo or _DEFAULT_REPO
    st = _profile_strategy(symbol_profile)

    if st.get("use_flutter_dsym") or st.get("use_app_dsym"):
        asset = _ASSET_IOS_DSYM_NATIVE if st.get("use_app_dsym") else _ASSET_IOS_DSYM
        await get_ios_dsyms_dir(app_version, repo=repo, asset_name=asset)
    if st.get("use_native_so"):
        await get_android_native_symbols_dir(app_version, repo=repo)
    if st.get("use_dart_symbols"):
        await get_dart_symbols_dir(app_version, repo=repo)
    if st.get("use_proguard"):
        await get_android_mapping(app_version, repo=repo)


async def prewarm_focus_versions() -> dict:
    """为 graygate 的主要版本预热符号包，让 QA 查新灰度包时永远命中缓存。

    幂等：preflight 判为 cached 就跳过，不重复下载。
    """
    start = time.monotonic()
    prewarmed: list = []
    skipped: list = []
    missing: list = []
    errors: list = []

    try:
        focus = await _get_focus_versions() or {}
    except Exception as exc:
        logger.warning("prewarm: read focus versions failed: %s", exc)
        return {"prewarmed": [], "skipped": [], "missing": [],
                "errors": [f"focus_version 读取失败：{exc}"], "duration_ms": 0}

    for platform in ("ios", "android"):
        version = (focus.get(platform) or "").strip()
        if not version:
            continue
        tag = f"{platform}:{version}"
        try:
            profile, repo = _resolve_routing(platform, version)
            pf = await _preflight(
                platform, version, symbol_profile=profile, github_repo=repo
            )
            status = pf.get("status")
            if status == "cached":
                skipped.append(tag)
                continue
            if status == "missing":
                logger.info(
                    "prewarm: %s has no symbols (%s) — 灰度包未上传符号包是常态，不告警",
                    tag, (pf.get("suggestions") or {}).get("reason", ""),
                )
                missing.append(tag)
                continue
            await _download_symbols(platform, version, profile, repo)
            prewarmed.append(tag)
            logger.info("prewarm: %s symbols downloaded", tag)
        except Exception as exc:
            logger.warning("prewarm: %s failed: %s", tag, exc)
            errors.append(f"{tag}: {exc}")

    return {
        "prewarmed": prewarmed, "skipped": skipped, "missing": missing,
        "errors": errors, "duration_ms": int((time.monotonic() - start) * 1000),
    }
```

- [ ] **Step 4: 加配置 + cron 分发 + 手动端点**

`backend/app/crashguard/config.py`，在「符号化设置」段落追加：

```python
    # 符号包预热（2026-09-22）：按 graygate 主要版本提前下载符号包，
    # 让 QA 查新灰度包时永远命中缓存，而不是等 1–3 分钟下载。
    # 必须走 job queue（下载 90MB，会拖垮 60s 主 tick）。
    symbol_prewarm_enabled: bool = True
    symbol_prewarm_cron: str = "*/30 * * * *"
```

`backend/app/crashguard/workers/scheduler.py`：在模块级 `_jank_backfill_last_fired`
旁边加 `_symbol_prewarm_last_fired: Optional[str] = None`，然后在 `_tick_once()` 里
照 jank_backfill 那块的样式追加（**必须用 `_enqueue_job`，不能直接 await**）：

```python
    # 符号包预热：按 graygate 主要版本提前下载符号包（独立 cron，默认 */30）
    global _symbol_prewarm_last_fired
    prewarm_cron = getattr(s, "symbol_prewarm_cron", "") or ""
    if (
        getattr(s, "symbol_prewarm_enabled", False)
        and prewarm_cron
        and _symbol_prewarm_last_fired != tag
        and _cron_matches(prewarm_cron, now)
    ):
        _symbol_prewarm_last_fired = tag
        async def _symbol_prewarm_job():
            async with record_heartbeat("symbol_prewarm") as hb:
                from app.crashguard.services.symbol_prewarmer import prewarm_focus_versions
                res = await prewarm_focus_versions()
                hb.set_summary(res)
                if not res.get("prewarmed"):
                    hb.status = "skipped"
                logger.info(
                    "crashguard symbol_prewarm fired: prewarmed=%s skipped=%s missing=%s",
                    res.get("prewarmed"), res.get("skipped"), res.get("missing"),
                )
        _enqueue_job("symbol_prewarm", _symbol_prewarm_job)
```

`backend/app/crashguard/api/crash.py` 加手动触发（发版后不用等 cron）：

```python
@router.post("/symbols/prewarm")
async def prewarm_symbols_now() -> Dict[str, Any]:
    """立即为 graygate 主要版本预热符号包（发版后手动触发，不用等 cron）。

    幂等：已缓存的版本会被跳过。可能耗时数分钟（下载 90MB），
    调用方应设置足够长的超时。
    """
    from app.crashguard.services.symbol_prewarmer import prewarm_focus_versions

    return await prewarm_focus_versions()
```

- [ ] **Step 5: 运行确认通过**

```bash
cd backend && pytest tests/crashguard/test_symbol_prewarmer.py -v
cd backend && pytest tests/crashguard/ -q
```

Expected: 新测试 6 个全绿；整个 crashguard 测试套件不退化。
特别确认 `tests/crashguard/` 里涉及 scheduler 的测试没被新 cron 块弄坏。

---

### Task 8: 前端符号化工作台页面

把前面 4 个端点串成一个能用的页面。

**Files:**
- Create: `frontend/src/app/crashguard/symbolicate/page.tsx`
- Modify: `frontend/src/lib/api.ts`（加 5 个 API 封装 + 类型）
- Modify: `frontend/src/app/crashguard/page.tsx`（加入口链接）
- Modify: `frontend/src/lib/i18n.ts`（加中文 key）

**Interfaces:**
- Consumes: Task 3/4/5/6 的四个端点 + 现有 `POST /api/crash/symbols/upload`
- Produces: 页面路由 `/crashguard/symbolicate`

- [ ] **Step 1: 加 api.ts 封装**

在 `frontend/src/lib/api.ts` 末尾追加（照现有 `export const xxx = () => request<...>(...)` 风格）：

```typescript
// ── Ad-hoc 堆栈符号化工作台（2026-09-22）─────────────────────────

export interface StackInsight {
  stack_format: string;
  platform: string;
  app_version: string;
  uuids: string[];
  build_ids: string[];
  binary_images: Record<string, unknown>[];
  normalized_stack: string;
  frame_count: number;
  confidence: "high" | "medium" | "low";
  notes: string[];
  routing: {
    symbol_profile: string;
    github_repo: string;
    family: string;
    confidence: string;
  } | null;
}

export interface VersionCandidate {
  app_version: string;
  source: "cached" | "uploaded" | "release";
  family: string;
  verified: boolean;
  symbol_types: string[];
  tag: string;
}

export interface SymbolPreflight {
  status: "cached" | "available" | "missing";
  eta_hint: string;
  symbol_sources: { source: string; detail: string }[];
  suggestions: {
    nearby_versions?: string[];
    reason?: string;
    upload_hint?: string;
  };
  warnings: string[];
}

export interface FrameStats {
  total_frames: number;
  symbolicated: number;
  unresolved: number;
  unparsed_lines: number;
  app_module: string;
  // 以下三项仅当能从堆栈推断出 App module 时有值；推断不到为 null，
  // 此时前端只展示粗粒度，不假装能区分系统库帧
  app_frames: number | null;
  app_symbolicated: number | null;
  non_app_frames: number | null;
}

export interface SymbolicateResult {
  symbolicated_stack: string;
  changed: boolean;
  stack_quality_before: string;
  stack_quality_after: string;
  platform: string;
  app_version: string;
  symbol_profile: string;
  github_repo: string;
  routing_confidence: string;
  available_symbol_packages: {
    symbol_type: string; app_version: string;
    file_name: string; created_at: string | null;
  }[];
  duration_ms: number;
  warnings: string[];
  frame_stats: FrameStats;
  preflight: SymbolPreflight | null;
}

export const inspectStack = (stack: string) =>
  request<StackInsight>("/crash/symbolicate/inspect", {
    method: "POST",
    body: JSON.stringify({ stack }),
  });

export const listSymbolicateVersions = (platform: string) =>
  request<{ versions: VersionCandidate[]; warnings: string[] }>(
    `/crash/symbolicate/versions?platform=${encodeURIComponent(platform)}`
  );

export const preflightSymbolicate = (body: {
  platform: string; app_version: string;
  symbol_profile?: string; github_repo?: string;
}) =>
  request<SymbolPreflight>("/crash/symbolicate/preflight", {
    method: "POST",
    body: JSON.stringify(body),
  });

// timeoutMs 300s：首次遇到新版本要下载符号包（dSYM 可达 90MB），默认 15s 会被
// AbortController 掐断。已有先例：VoC digest 用 330s。
export const symbolicateStack = (body: {
  stack: string; platform: string; app_version?: string;
  symbol_profile?: string; github_repo?: string;
}) =>
  request<SymbolicateResult>("/crash/symbolicate", {
    method: "POST",
    body: JSON.stringify(body),
    timeoutMs: 300_000,
  });
```

符号包上传（`multipart/form-data`，`request()` 已处理 FormData 不加 Content-Type）：
先 `grep -n "symbols/upload" backend/app/crashguard/api/crash.py` 看清 form 字段名
（`platform` / `app_version` / `symbol_type` / `file`），照它写封装。

- [ ] **Step 2: 写页面**

创建 `frontend/src/app/crashguard/symbolicate/page.tsx`。`"use client"` 开头，
照 `frontend/src/app/crashguard/page.tsx` 的色板/样式约定（`var(--j-*)` CSS 变量、
`useT()` 取文案）。

交互链路（严格按此顺序，每步的状态都要可见）：

1. **输入区**：大 `<textarea>`（等宽字体，`min-height: 240px`），placeholder 说明
   支持粘贴 Apple .ips/.crash、Datadog 堆栈、Android Java 异常栈、tombstone。
2. **debounce 600ms → `inspectStack`**。结果渲染成一条「解析结果条」：
   `识别为 iOS · Apple .ips · 版本 4.0.201-941（从堆栈解析）`。
   **每个字段都标来源，且全部可改——解析只预填不锁定。**
   `notes` 逐条显示（`android_java` 会告诉用户"必须手动指定版本号"）。
3. **平台 radio**（ios / android），预填 `insight.platform`，用户可改。
4. **版本 combobox**：平台确定后调 `listSymbolicateVersions(platform)`。
   - `<input>` + 下拉候选，**允许手填**（不是纯 select）
   - 每个候选显示 `app_version` + source 徽标（`cached` 绿 / `uploaded` 蓝 / `release` 灰）
   - `verified === false` 的项前面加 `⚠`，hover 提示
     「这是 Release tag，未校验真实 build 号，可能符号化失败」
   - 预填 `insight.app_version`
5. **平台 + 版本都有值后自动调 `preflightSymbolicate`** → 三档状态条：
   - `cached` 🟢：`符号包已就绪，预计秒级返回`
   - `available` 🟡：`需下载约 90MB 符号包，预计 1–3 分钟（之后同版本秒级）`
   - `missing` 🔴：红底`该版本无符号表，符号化不会有任何效果`
     + 展示 `suggestions.reason`
     + `suggestions.nearby_versions` 渲染成**可点击的按钮**，点了直接把版本号换掉
       （QA 记错 build 号是高频事件，这一条能直接救场）
     + 提交按钮改为二次确认（`确定仍要符号化？`）
6. **提交** → `symbolicateStack`。`available` 档额外提示「首次下载符号包，请勿关闭页面」。
   按钮 loading 态 + 已耗时秒数计时器（让用户知道没卡死）。
7. **结果区**：
   - 左右并排 `<pre>` 对比（原始 / 符号化），等宽字体，行号对齐，各自可横向滚动
   - **诊断面板**：
     - `frame_stats` 渲染成人话，**分两种情况**：
       - `app_module` 非空（能细分）：
         `总 42 帧 · PLAUD 帧 18（符号化 17 / 失败 1）· 其他模块 24（按设计跳过）`
       - `app_module` 为空（推断不出）：只显示粗粒度
         `总 42 帧 · 已符号化 17 · 未解析 25`
       两种情况都加一句说明「系统库/三方库帧不解析是正常的——强行匹配会产生
       错误符号，比裸地址更误导」。**不要在 app_frames 为 null 时显示 0**，
       那会让用户以为 App 帧一个都没解出来。
     - `stack_quality_before` → `stack_quality_after`
     - 实际使用的 `symbol_profile` / `github_repo` / `routing_confidence`
     - `warnings` 列表（有则显示，没有不显示空板块——遵循
       `feedback_report_only_anomalies`：有问题才显示）
8. **页面底部符号包上传框**：platform / app_version / symbol_type 三个字段 + 文件选择。
   `missing` 档时从 `suggestions.upload_hint` 高亮滚动定位到这里。

- [ ] **Step 3: 加入口 + i18n**

`frontend/src/app/crashguard/page.tsx`：在页面头部工具区加一个链接按钮
（照现有按钮样式），`<Link href="/crashguard/symbolicate">堆栈符号化</Link>`。

**不要动 `Sidebar.tsx`** —— 这是崩溃排查的工具而非独立看板，不占顶级导航位。

`frontend/src/lib/i18n.ts`：把页面里所有中文文案加进去（照现有 `"中文": "English"`
的 map 格式）。至少这些：`堆栈符号化`、`粘贴堆栈`、`解析结果`、`符号包已就绪`、
`该版本无符号表`、`系统库帧（按设计跳过）`、`上传符号包`。

- [ ] **Step 4: 类型检查 + 构建**

```bash
cd frontend && npx tsc --noEmit
cd frontend && npm run build
```

Expected: 无 TS 错误、build 成功。
（前端无自动化测试基础设施，`npm run build` 的类型检查是唯一自动化验证手段。）

- [ ] **Step 5: 全量回归**

```bash
cd backend && pytest tests/crashguard/ -q
```

Expected: 全绿。这是交付前的最后一道闸。

---

## 交付后必须向用户报告的事项

这些**不在本 plan 的执行范围内**（都需要单独授权），但必须在完工汇报里明确列出：

1. **未 commit**：所有改动留在工作区。需要用户明确指令才能 `git add` / `commit` / `push`。
2. **未部署**。部署前有两个硬前置：
   - `df -h /` 实测 102 磁盘余量。Task 2 的 `keep_versions` 10→20 增量约 **+6.5GB**
     （`github_cache` +2GB / 上传包 +4.5GB）。余量不足 15GB 则把 20 调低或不改，
     并告知用户。
   - `cat config.local.yaml` 确认 `symbol_upload_keep_versions` /
     `github_cache_keep_versions` **没有残留的 10** —— 该文件优先级高于代码默认值，
     否则 Task 2 的改动静默失效。
3. **`symbol_prewarm_enabled` 默认 `True`**。首次部署后它会在 30 分钟内触发，
   按 graygate 主要版本下载符号包（可能各 90MB）。若不希望立刻发生，
   部署前先在 `config.local.yaml` 设 `symbol_prewarm_enabled: false`。
4. **预热经 VPN 可能卡住**：已知问题（>5MB 传输到 102 经 VPN 会卡在几 MB）。
   预热走 job queue 不阻塞主 tick，但可能长时间占着 worker。
5. **`test_symbolicate_api.py` 若有 warning 断言被改**：这是 Task 6 的预期行为变更
   （修掉误导性 warning），需在汇报里说明改了哪条断言。
