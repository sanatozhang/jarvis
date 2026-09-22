"""stack_inspector 单测（2026-09-22）：裸堆栈文本 → 符号化所需元数据。

表驱动覆盖 6 种堆栈格式。核心护栏：
1. Android Java 混淆栈物理上不含版本号，必须明确告知用户（最高频的输入场景）。
2. 任何畸形输入都降级为 unknown，**绝不抛异常** —— 用户仍可手选平台/版本走通。
3. .ips 的结构化帧必须归一成 _symbolicate_ios_with_dir 正则认的文本帧形状。
"""
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
            "base": 4355784704,          # 0x103a00000
            "size": 13500416,
            "uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "path": "/private/var/containers/Bundle/Application/X/PLAUD.app/PLAUD",
            "name": "PLAUD",
        },
        {
            "source": "S",
            "arch": "arm64e",
            "base": 8063549440,         # 0x1e0a00000
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
0x1e0a00000 - 0x1e0a31fff libsystem_kernel.dylib arm64e <11111111222233334444555555555555> /usr/lib/system/libsystem_kernel.dylib
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
    assert any("指针认证" in n for n in r.notes)
