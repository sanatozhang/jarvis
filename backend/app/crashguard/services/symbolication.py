"""
Flutter Engine + 用户自上传符号包 符号化服务。

支持：
  - Android: addr2line / llvm-symbolizer + libflutter.so
  - iOS:     atos + Flutter.dSYM
  - 用户上传的 dart_symbols / proguard_mapping / dsym 包（Plan B fallback）

容错优先：任何子步骤失败都不影响主调用方，原始地址原样保留。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("crashguard.symbolication")

# ── 符号化 profile 策略表 ─────────────────────────────────────────────────────
# 每个 symbol_profile 控制哪些 asset getter 会被调用（True = 调用，False = 跳过）。
# native_android: R8/ProGuard mapping + native .so，没有 Dart 符号
# native_ios: app dSYM 只，没有 Flutter.dSYM
# flutter_*: 当前完整行为（向后兼容）
# none: 全部跳过
_SYMBOL_PROFILES: dict = {
    "flutter_android": {
        "use_dart_symbols": True,
        "use_proguard": True,
        "use_native_so": True,
        "use_flutter_dsym": True,
        "use_app_dsym": False,
    },
    "flutter_ios": {
        "use_dart_symbols": True,
        "use_proguard": False,
        "use_native_so": False,
        "use_flutter_dsym": True,
        "use_app_dsym": False,
    },
    "native_android": {
        "use_dart_symbols": False,
        "use_proguard": True,
        "use_native_so": True,
        "use_flutter_dsym": False,
        "use_app_dsym": False,
    },
    "native_ios": {
        "use_dart_symbols": False,
        "use_proguard": False,
        "use_native_so": False,
        "use_flutter_dsym": False,
        "use_app_dsym": True,
    },
    "none": {
        "use_dart_symbols": False,
        "use_proguard": False,
        "use_native_so": False,
        "use_flutter_dsym": False,
        "use_app_dsym": False,
    },
}


def _profile_strategy(symbol_profile: str) -> dict:
    """返回 symbol_profile 对应的策略 dict。未知/空 profile → 'none'（全 False）。

    向后兼容注：symbol_profile="" 返回 none 策略（全 False），但 symbolicate_stack
    在 symbol_profile 为空时会用 flutter 默认策略回退（platform-based 路由），
    不走此函数控制的 gating，保持现有行为不变。
    """
    key = (symbol_profile or "none").strip().lower()
    return _SYMBOL_PROFILES.get(key, _SYMBOL_PROFILES["none"])


# ── 工具可用性缓存（进程级，启动时探测一次）──────────────────────────────────
_ADDR2LINE: Optional[str] = None   # addr2line 或 llvm-symbolizer 路径
_ATOS: Optional[str] = None        # atos 路径（仅 macOS）
_IS_LLVM_SYMBOLIZER: bool = False  # True 时 _ADDR2LINE 是 llvm-symbolizer（可读 Mach-O）
_TOOLS_PROBED = False

def _probe_tools() -> None:
    global _ADDR2LINE, _ATOS, _TOOLS_PROBED, _IS_LLVM_SYMBOLIZER
    if _TOOLS_PROBED:
        return
    _ADDR2LINE = shutil.which("llvm-symbolizer") or shutil.which("addr2line")
    _IS_LLVM_SYMBOLIZER = bool(_ADDR2LINE and "llvm-symbolizer" in _ADDR2LINE)
    _ATOS = shutil.which("atos")
    _TOOLS_PROBED = True
    logger.info(
        "symbolication tools: addr2line/llvm-symbolizer=%s  atos=%s  is_llvm=%s",
        _ADDR2LINE, _ATOS, _IS_LLVM_SYMBOLIZER,
    )


# ── 缓存目录 ──────────────────────────────────────────────────────────────────
def _data_root() -> Path:
    env = os.environ.get("DATA_DIR")
    if env:
        return Path(env)
    if os.access("/data", os.W_OK):
        return Path("/data")
    # repo_root/data : symbolication.py → services/ → crashguard/ → app/ → backend/ → repo
    return Path(__file__).resolve().parents[4] / "data"


def _flutter_engine_cache_dir() -> Path:
    p = _data_root() / "symbols" / "flutter_engine_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _user_symbols_dir() -> Path:
    p = _data_root() / "symbols"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── 公开入口 ──────────────────────────────────────────────────────────────────

async def symbolicate_stack(
    stack: str,
    binary_images: list,
    platform: str,
    app_version: str = "",
    *,
    symbol_profile: str = "",
    github_repo: str = "",
) -> str:
    """
    尝试符号化 stack 中的帧，返回增强后的 stack 字符串。

    优先级：
      1. Flutter engine 帧（Plan A：从公开存储自动下载）
      2. 用户上传的符号包（Plan B）
      3. GitHub release 符号包（Plan C：自动按版本下载）

    Args:
        stack:          原始堆栈字符串
        binary_images:  Datadog RUM 事件里的 binary_images 列表（可为空 list）
        platform:       "ios" | "android" | "flutter" 等
        app_version:    Datadog @application.version，如 "3.18.0-708"（可为空）
        symbol_profile: 符号化策略（"flutter_android" / "flutter_ios" / "native_android" /
                        "native_ios" / "none" / ""）。空字符串 → 向后兼容（platform-based）。
        github_repo:    源码/符号仓（如 "Plaud-AI/Plaud-App"）。空 → 默认 Flutter 仓。

    Returns:
        符号化后的堆栈字符串（失败时原样返回 stack）
    """
    _probe_tools()
    if not stack:
        return stack
    try:
        # Plan A + Plan B（同步，在线程里跑）
        result = await asyncio.to_thread(_symbolicate_stack_sync, stack, binary_images, platform)
        # Plan C：GitHub release 符号（异步，按需下载）
        if app_version:
            result = await _symbolicate_with_github(
                result, platform, app_version,
                symbol_profile=symbol_profile,
                github_repo=github_repo,
            )
        return result
    except Exception as exc:
        logger.warning("symbolicate_stack failed (non-fatal): %s", exc)
        return stack


# arm64e 指针认证(PAC) 签名位掩码——见 _strip_ptr_auth() 说明。
_PTR_AUTH_MASK = 0xFFFFFFFFFF  # 保留低 40 bit


def _strip_ptr_auth(addr_hex: str) -> str:
    """去掉 arm64e 指针认证(PAC)签名位对栈回溯地址高位的污染（2026-07-23 生产实测）。

    现场证据：同一条 jank 日志里，frame 0（watchdog 直接采样的 PC 寄存器值）地址干净
    （如 `0x000000019e364860`），但从 frame 1 起（栈回溯得到的返回地址，如
    `0xdf0c800199fde290`）高 24 bit 变成随机值——高 24 bit 掩掉之后剩下的低 40 bit
    （`0x0199fde290`）和该帧的 `stack_module_offsets`/`app_stack_module_offset`
    字段（同一日志里另外单独给的、Datadog 客户端自己算好的 offset）完全对得上，
    算出的 offset 落在几 KB～几十 MB 的合理范围；不掩码则会算出 10^19 量级的
    天文数字（`int(pc,16) - int(base,16)` 双方都是 Python 任意精度整数，不会自动
    截断/环绕，所以看到的是巨大正数而非报错）。

    这是 arm64e 设备上自研栈回溯 + 符号化的已知通病（Apple 官方 atos/
    symbolicatecrash 处理系统崩溃报告时也会做同样的 strip），根因在客户端/SDK
    序列化返回地址时没有先 strip PAC 签名位，不是本模块符号化逻辑本身的 bug；
    在服务端掩码是通用、无副作用的规避方式（对本来就干净的地址是 no-op）。
    """
    try:
        return hex(int(addr_hex, 16) & _PTR_AUTH_MASK)
    except (ValueError, TypeError):
        return addr_hex


async def symbolicate_jank_frame(
    *,
    platform: str,
    app_version: str,
    module: str = "",
    frame_text: str = "",
    pc: str = "",
    module_base: str = "",
    symbol_profile: str = "",
    github_repo: str = "",
) -> str:
    """
    jank_watchdog_block 卡顿事件的单帧符号化（2026-07-20）。

    每条卡顿日志只给"应用自身模块"单帧地址（不是整段多帧堆栈），符号化成本很低，
    复用现有多帧解析函数（伪造成一行"stack"喂给它们），不重复造 dSYM 下载 / ProGuard
    解析 / atos 调用这些底层机制：

    - iOS：用 `github_symbols.py::get_ios_dsyms_dir()`（同一套 GitHub release 符号包
      下载，本次会话前半段修的 GH_TOKEN 403 bug 在这里同样生效）+ 复用
      `_symbolicate_ios_with_dir()` 做单帧 atos 查询。
    - Android：`app_stack_frame` 大多数情况下已是可读文本（class.method），但实测
      确有混淆样本（如 "ai.plaud.android.payment.k.a"），统一走现有 ProGuard
      retrace（`_retrace_proguard` 对映射表里查不到的类名是 no-op，原样返回，
      不需要先判断"是否混淆"）。

    失败时返回 `frame_text`（Android）或 `"{module} + {pc}"`（iOS）占位，调用方用
    `DatadogClient._stack_quality_label()` 判断质量是否足够进入 AI 分析。
    """
    _probe_tools()
    plat = (platform or "").lower()
    try:
        if "ios" in plat:
            return await _symbolicate_jank_frame_ios(
                app_version=app_version, module=module, pc=pc, module_base=module_base,
                symbol_profile=symbol_profile, github_repo=github_repo,
            )
        if "android" in plat:
            return await _symbolicate_jank_frame_android(
                app_version=app_version, frame_text=frame_text,
                symbol_profile=symbol_profile, github_repo=github_repo,
            )
    except Exception as exc:
        logger.warning("symbolicate_jank_frame failed (non-fatal): %s", exc)
    return frame_text or (f"{module} + {pc}" if module else pc)


async def _symbolicate_jank_frame_ios(
    *,
    app_version: str,
    module: str,
    pc: str,
    module_base: str,
    symbol_profile: str,
    github_repo: str,
) -> str:
    from app.crashguard.services.github_symbols import (
        get_ios_dsyms_dir, _ASSET_IOS_DSYM, _ASSET_IOS_DSYM_NATIVE,
    )

    placeholder = f"{module} + {pc}" if module else pc
    if not pc or not module_base:
        return placeholder

    strategy = _profile_strategy(symbol_profile)
    ios_asset = _ASSET_IOS_DSYM_NATIVE if strategy.get("use_app_dsym") else _ASSET_IOS_DSYM
    dsyms_dir = await get_ios_dsyms_dir(app_version, repo=github_repo, asset_name=ios_asset)
    if not dsyms_dir:
        return placeholder

    pc = _strip_ptr_auth(pc)
    module_base = _strip_ptr_auth(module_base)
    try:
        offset_decimal = int(pc, 16) - int(module_base, 16)
    except ValueError:
        return placeholder

    # 伪造成 _symbolicate_ios_with_dir 认识的单行格式，直接复用它的 dSYM 遍历 + atos 查询，
    # 不重复实现 "找 dSYM bundle → 调 atos" 这套逻辑。
    fake_stack = f"0   {module}   {pc}   {module_base} + {offset_decimal}\n"
    resolved = await asyncio.to_thread(_symbolicate_ios_with_dir, fake_stack, dsyms_dir)
    if resolved == fake_stack:
        return placeholder  # 未命中任何 dSYM，原样返回（_symbolicate_ios_with_dir 的失败态）

    prefix = f"0   {module}   "
    if resolved.startswith(prefix):
        return resolved[len(prefix):].strip() or placeholder
    return resolved.strip() or placeholder


async def _symbolicate_jank_frame_android(
    *,
    app_version: str,
    frame_text: str,
    symbol_profile: str,
    github_repo: str,
) -> str:
    from app.crashguard.services.github_symbols import get_android_mapping

    if not frame_text or "." not in frame_text:
        return frame_text

    strategy = _profile_strategy(symbol_profile)
    if strategy and not strategy.get("use_proguard"):
        return frame_text

    mapping_path = await get_android_mapping(app_version, repo=github_repo)
    if not mapping_path:
        return frame_text

    # 伪造成 _retrace_proguard 认识的 "at Class.method(...)" 单行格式，复用它的
    # mapping.txt 解析 + 查表逻辑；映射表里查不到该类名时 _retrace_proguard 原样
    # 返回，不需要预先判断"这一帧是否混淆"。
    class_part, _, method_part = frame_text.rpartition(".")
    fake_line = f"  at {class_part}.{method_part}(Unknown Source)"
    resolved = await asyncio.to_thread(_retrace_proguard, fake_line, mapping_path)
    m = _ANDROID_FRAME_RE.search(resolved)
    return f"{m.group(2)}.{m.group(3)}" if m else frame_text


async def symbolicate_jank_stack(
    *,
    platform: str,
    app_version: str,
    stack_trace: str,
    stack_modules: str = "",
    stack_pcs: str = "",
    stack_module_bases: str = "",
    symbol_profile: str = "",
    github_repo: str = "",
) -> str:
    """
    jank_watchdog_block 卡顿事件的**完整多帧堆栈**符号化（2026-07-21）。

    背景：`symbolicate_jank_frame()` 只符号化"应用自身模块"单帧，返回一个函数名
    字符串——过去 `_symbolicate_new_jank_issue` 把这个单帧结果整个覆盖到
    `representative_stack`，导致详情页丢失摄入时原本存的完整多行堆栈（用户反馈的
    "堆栈只显示一行" bug）。这个函数改用 Datadog 提供的 pipe 分隔等长数组字段
    （`stack_modules`/`stack_pcs`/`stack_module_bases`，102 生产环境实测三者等长，
    约 20 帧，系统框架帧也有非空 base）逐帧拼出 `_symbolicate_ios_with_dir` 认识
    的多行格式，一次性符号化整段堆栈——app 自己模块的帧能查到符号就替换，系统框架
    帧查不到会原样保留地址（`_symbolicate_ios_with_dir` 本身就是逐行降级、不会报错）。

    Android 目前没有多帧地址符号化需求：`_upsert_jank_event` 摄入时存的
    `representative_stack` 本来就是完整的原始 `stack_trace`（人类可读文本），不需要
    额外处理，直接原样返回。

    容错策略（刻意简单，不做部分符号化的复杂合并）：任何字段缺失 / 数组长度不一致 /
    dSYM 拿不到 / 计算 offset 出错 / 任何异常 → 整体原样返回 `stack_trace`。原始未
    符号化文本总比报错或空白好——这是本次修复的最低要求：至少不能比现在更差（现在
    好歹是完整的多行原始文本）。
    """
    _probe_tools()
    plat = (platform or "").lower()
    if "ios" not in plat:
        return stack_trace
    try:
        return await _symbolicate_jank_stack_ios(
            app_version=app_version,
            stack_trace=stack_trace,
            stack_modules=stack_modules,
            stack_pcs=stack_pcs,
            stack_module_bases=stack_module_bases,
            symbol_profile=symbol_profile,
            github_repo=github_repo,
        )
    except Exception as exc:
        logger.warning("symbolicate_jank_stack failed (non-fatal): %s", exc)
        return stack_trace


async def _symbolicate_jank_stack_ios(
    *,
    app_version: str,
    stack_trace: str,
    stack_modules: str,
    stack_pcs: str,
    stack_module_bases: str,
    symbol_profile: str,
    github_repo: str,
) -> str:
    from app.crashguard.services.github_symbols import (
        get_ios_dsyms_dir, _ASSET_IOS_DSYM, _ASSET_IOS_DSYM_NATIVE,
    )

    if not stack_modules or not stack_pcs or not stack_module_bases:
        return stack_trace

    modules = stack_modules.split("|")
    pcs = stack_pcs.split("|")
    bases = stack_module_bases.split("|")
    if not modules or not (len(modules) == len(pcs) == len(bases)):
        return stack_trace

    strategy = _profile_strategy(symbol_profile)
    ios_asset = _ASSET_IOS_DSYM_NATIVE if strategy.get("use_app_dsym") else _ASSET_IOS_DSYM
    # 复用 symbolicate_jank_frame 同一份 (tag, asset_name) 磁盘缓存：get_ios_dsyms_dir
    # 内部先查本地 ".extracted" marker，命中即返回，不重复下载——这里和调用方后续
    # 可能再调一次 symbolicate_jank_frame 拿标题不会产生二次网络 I/O。
    dsyms_dir = await get_ios_dsyms_dir(app_version, repo=github_repo, asset_name=ios_asset)
    if not dsyms_dir:
        return stack_trace

    lines: list = []
    for i, (module, pc, base) in enumerate(zip(modules, pcs, bases)):
        module = module.strip()
        pc = _strip_ptr_auth(pc.strip())
        base = _strip_ptr_auth(base.strip())
        if not module or not pc or not base:
            # 逐帧回退会导致行对不齐、复杂度不值得——直接整体回退到原始文本
            return stack_trace
        try:
            offset_decimal = int(pc, 16) - int(base, 16)
        except ValueError:
            return stack_trace
        lines.append(f"{i}   {module}   {pc}   {base} + {offset_decimal}\n")

    fake_multiline_stack = "".join(lines)
    resolved = await asyncio.to_thread(_symbolicate_ios_with_dir, fake_multiline_stack, dsyms_dir)
    return resolved or stack_trace


def _symbolicate_stack_sync(stack: str, binary_images: list, platform: str) -> str:
    plat = (platform or "").lower()
    if "ios" in plat or "iphone" in plat or "ipados" in plat:
        return _symbolicate_ios(stack, binary_images)
    if "android" in plat:
        return _symbolicate_android(stack, binary_images)
    # flutter 在 Android/iOS 底层跑，尝试两者
    out = _symbolicate_android(stack, binary_images)
    if out != stack:
        return out
    return _symbolicate_ios(out, binary_images)


async def _symbolicate_with_github(
    stack: str,
    platform: str,
    app_version: str,
    *,
    symbol_profile: str = "",
    github_repo: str = "",
) -> str:
    """Plan C：利用 Plaud GitHub release 里的符号文件对 stack 做进一步增强。

    Android：
      1. 优先用 native_symbols.tar.gz 里的 libflutter.so / libapp.so（带 debug 符号）解 native 帧
      2. 用 mapping_globalRelease.txt 做 ProGuard 反混淆 Java 帧
    iOS：用 PLAUD.dSYMs.zip 里的 dSYM bundle 用 atos 解析

    symbol_profile 控制哪些 getter 被调用（见 _SYMBOL_PROFILES 表）。
    空 profile → platform-based 向后兼容路径（与既有行为一致）。
    github_repo 为空 → 使用 _DEFAULT_REPO（向后兼容）。
    """
    from app.crashguard.services.github_symbols import (
        get_ios_dsyms_dir, get_android_mapping, get_android_native_symbols_dir,
        get_dart_symbols_dir, _DEFAULT_REPO, _ASSET_IOS_DSYM, _ASSET_IOS_DSYM_NATIVE,
    )
    repo = github_repo or _DEFAULT_REPO
    plat = (platform or "").lower()

    # 如果有明确的 symbol_profile，走 strategy 表控制哪些 getter 调用
    # 如果没有（空/none），回退到原来的 platform-based 逻辑（向后兼容）
    profile_key = (symbol_profile or "").strip().lower()
    use_strategy = bool(profile_key and profile_key != "none")
    strategy = _profile_strategy(symbol_profile) if use_strategy else None

    try:
        is_ios = "ios" in plat or "iphone" in plat or "ipados" in plat
        is_android = "android" in plat or "flutter" in plat

        if is_ios:
            # native_ios (use_app_dsym) 和 flutter_ios (use_flutter_dsym) 都走
            # get_ios_dsyms_dir，但资产名不同：native release 发的是
            # Plaud-Global.dSYMs.zip，不是 flutter 的 PLAUD.dSYMs.zip（2026-07-14 实测确认，
            # repo 也不同：native 在独立仓 plaud-native-app，由 github_repo 参数传入）。
            if strategy is None or strategy.get("use_flutter_dsym") or strategy.get("use_app_dsym"):
                ios_asset = _ASSET_IOS_DSYM_NATIVE if (strategy and strategy.get("use_app_dsym")) else _ASSET_IOS_DSYM
                dsyms_dir = await get_ios_dsyms_dir(app_version, repo=repo, asset_name=ios_asset)
                if dsyms_dir:
                    stack = await asyncio.to_thread(_symbolicate_ios_with_dir, stack, dsyms_dir)
        elif is_android:
            # 1. native 符号（关键：libflutter.so / libapp.so 带 debug 符号）+
            #    Dart AOT 符号包 flutter_symbols.tar.gz 里的 app.android-arm64.symbols
            #    （libapp.so stripped 后真正的 DWARF 在这里）
            native_dir = None
            dart_dir = None

            # native_so: always run unless strategy explicitly disables it
            if strategy is None or strategy.get("use_native_so"):
                native_dir = await get_android_native_symbols_dir(app_version, repo=repo)

            # dart_symbols: skip for native_android (no Dart in native builds)
            if strategy is None or strategy.get("use_dart_symbols"):
                dart_dir = await get_dart_symbols_dir(app_version, repo=repo)

            if native_dir or dart_dir:
                stack = await asyncio.to_thread(
                    _symbolicate_android_with_dir, stack, native_dir, dart_dir,
                )
            # 2. ProGuard mapping（Java 帧反混淆）
            if strategy is None or strategy.get("use_proguard"):
                mapping_path = await get_android_mapping(app_version, repo=repo)
                if mapping_path:
                    stack = await asyncio.to_thread(_retrace_proguard, stack, mapping_path)
    except Exception as exc:
        logger.debug("github symbolication failed (non-fatal): %s", exc)
    return stack


def _symbolicate_android_with_dir(
    stack: str,
    native_dir: Optional[str],
    dart_dir: Optional[str] = None,
) -> str:
    """
    用 Plaud release 解压出的符号文件对 Android native crash 帧做 addr2line 符号化。

    扫描来源：
      - native_dir: native_symbols.tar.gz 里的 *.so（libflutter.so / libapp.so，
        merged_native_libs 下是 unstripped）
      - dart_dir:   flutter_symbols.tar.gz 里的 *.symbols（libapp.so 真正的 DWARF
        debug ELF，stripped libapp.so 替不了它）

    匹配策略：
      1. 优先按 BuildId 精确匹配（同 BuildId 下优先 with debug_info > merged > size）
      2. 精确不命中时按 lib 名 fuzzy fallback（用户场景：一个 crash 跨多版本，
        GitHub 上传的 release 与设备实际跑的 build 不同 commit → BuildId 不同
        但函数表大致同源；fallback 后输出标记 [fuzzy] 提示）
    """
    if not _ADDR2LINE:
        return stack

    extra_re = re.compile(
        r"(#\d+\s+pc\s+)([0-9a-fA-F]+)\s+.*?(\S+\.so).*?(?:BuildId:\s*([0-9a-fA-F]+))",
        re.MULTILINE,
    )

    # 从 stack 收 (lib_basename, arch, BuildId) 集合
    # arch 用于 fuzzy fallback 时区分 app.android-arm64.symbols / arm.symbols / x64.symbols
    stack_libs: set = set()
    for m in extra_re.finditer(stack):
        path = m.group(3) or ""
        lib = path.rsplit("/", 1)[-1].lower()
        bid = (m.group(4) or "").lower()
        if "arm64" in path or "arm64-v8a" in path:
            arch = "arm64"
        elif "armeabi" in path or "/arm/" in path:
            arch = "arm"
        elif "x86_64" in path or "/x64/" in path:
            arch = "x64"
        elif "x86" in path:
            arch = "x86"
        else:
            arch = "arm64"  # Android 主流，默认猜 arm64
        if lib and bid:
            stack_libs.add((lib, arch, bid))
    if not stack_libs:
        return stack

    # 扫描所有候选符号文件（.so + .symbols）
    candidates: list = []  # list[(file_path, lib_hint, bid, size, score)]
    for root in (native_dir, dart_dir):
        if not root:
            continue
        for fp in list(Path(root).rglob("*.so")) + list(Path(root).rglob("*.symbols")):
            try:
                r = subprocess.run(
                    ["file", str(fp)], capture_output=True, text=True, timeout=5,
                )
                out_lower = r.stdout.lower()
                # 抽 BuildId（file 命令格式：BuildID[md5/uuid]=<hex>）
                bid_match = re.search(r"buildid\[[^\]]+\]=([0-9a-f]+)", out_lower)
                bid = bid_match.group(1) if bid_match else ""
                has_debug = "with debug_info" in out_lower or "not stripped" in out_lower
                size = fp.stat().st_size
                # lib_hint：app.android-arm64.symbols ⇔ libapp.so；libflutter.so ⇔ libflutter.so
                # arch：从文件名/路径推断（用于 fuzzy fallback 按架构匹配）
                name = fp.name.lower()
                full = str(fp).lower()
                if "libflutter" in name:
                    lib_hint = "libflutter.so"
                elif "libapp" in name or name.startswith("app.android"):
                    lib_hint = "libapp.so"
                else:
                    lib_hint = name
                if "arm64" in full or "arm64-v8a" in full:
                    arch_hint = "arm64"
                elif "armeabi" in full or "android-arm.symbols" in full:
                    arch_hint = "arm"
                elif "x86_64" in full or "android-x64.symbols" in full:
                    arch_hint = "x64"
                elif "x86" in full:
                    arch_hint = "x86"
                else:
                    arch_hint = "arm64"
                # 评分：debug > merged path > size
                score = (1 if has_debug else 0, 1 if "merged_native_libs" in str(fp) else 0, size)
                candidates.append((str(fp), lib_hint, arch_hint, bid, size, score))
            except Exception:
                continue

    if not candidates:
        return stack

    # 建索引：bid → best file（精确匹配）；(lib_hint, arch_hint) → best file（fuzzy fallback）
    by_bid: dict = {}
    by_lib_arch: dict = {}
    for path, lib_hint, arch_hint, bid, size, score in candidates:
        if bid:
            cur = by_bid.get(bid)
            if cur is None or score > cur[1]:
                by_bid[bid] = (path, score)
        key = (lib_hint, arch_hint)
        cur = by_lib_arch.get(key)
        if cur is None or score > cur[1]:
            by_lib_arch[key] = (path, score)

    # 构造 stack 帧的 BuildId → 符号文件映射（精确优先，fuzzy fallback 按 (lib, arch) 严格匹配）
    resolved: dict = {}  # bid → (path, is_fuzzy)
    for lib, arch, bid in stack_libs:
        if bid in by_bid:
            resolved[bid] = (by_bid[bid][0], False)
            continue
        key = (lib, arch)
        if key in by_lib_arch:
            resolved[bid] = (by_lib_arch[key][0], True)
            logger.info(
                "fuzzy symbolicate: stack BuildId %s not found, falling back to %s for %s/%s",
                bid, by_lib_arch[key][0], lib, arch,
            )

    if not resolved:
        return stack

    def replace_frame(m: re.Match) -> str:
        bid = (m.group(4) or "").lower()
        if bid not in resolved:
            return m.group(0)
        offset = m.group(2)
        path, is_fuzzy = resolved[bid]
        sym = _addr2line_lookup(path, offset)
        if not sym:
            return m.group(0)
        suffix = " [fuzzy]" if is_fuzzy else ""
        return m.group(0).replace("(???)", f"[{sym}{suffix}]")

    return extra_re.sub(replace_frame, stack)


# ── iOS 符号化 ─────────────────────────────────────────────────────────────────

# Flutter iOS 帧格式：
#   1   Flutter  0x00000001076c6100 0x1071dc000 + 5153024
_IOS_FLUTTER_FRAME_RE = re.compile(
    r"^(\s*\d+\s+Flutter\s+)(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+\+\s+(\d+)(.*)",
    re.MULTILINE,
)

def _symbolicate_ios(stack: str, binary_images: list) -> str:
    # 找 Flutter binary_image entry
    flutter_entry = _find_ios_flutter_image(binary_images)
    if not flutter_entry:
        return stack

    uuid = (flutter_entry.get("uuid") or "").replace("-", "").lower()
    load_addr = flutter_entry.get("load_address") or flutter_entry.get("load") or ""

    if not uuid:
        return stack

    dsym_path = _get_or_download_ios_dsym(uuid)
    if not dsym_path:
        # 尝试用户上传的符号包
        dsym_path = _find_user_dsym(uuid, "ios")
    if not dsym_path or (not _ATOS and not _IS_LLVM_SYMBOLIZER):
        return stack

    dwarf_path = _find_dwarf_in_dsym(dsym_path)
    if not dwarf_path:
        return stack

    def replace_frame(m: re.Match) -> str:
        prefix = m.group(1)
        addr = m.group(2)
        base = m.group(3) if not load_addr else load_addr
        suffix = m.group(5)
        sym = _atos_lookup(dwarf_path, base, addr)
        if sym:
            return f"{prefix}{sym}{suffix}"
        return m.group(0)

    return _IOS_FLUTTER_FRAME_RE.sub(replace_frame, stack)


def _find_ios_flutter_image(binary_images: list) -> Optional[dict]:
    for img in (binary_images or []):
        if not isinstance(img, dict):
            continue
        name = (img.get("name") or img.get("image") or "").lower()
        if "flutter" in name:
            return img
    return None


def _atos_lookup(dwarf_path: str, load_addr: str, addr: str) -> Optional[str]:
    # Primary: atos（macOS-only，精确处理 ASLR slide）
    if _ATOS:
        try:
            result = subprocess.run(
                [_ATOS, "-arch", "arm64", "-o", dwarf_path, "-l", str(load_addr), str(addr)],
                capture_output=True, text=True, timeout=10,
            )
            out = result.stdout.strip()
            if out and out != addr and "???" not in out:
                return out
        except Exception as exc:
            logger.debug("atos failed for %s: %s", addr, exc)

    # Fallback: llvm-symbolizer（Linux，可读 Mach-O DWARF）
    # iOS arm64 text segment 在 DWARF 中的 VA 起点固定为 0x100000000；
    # ASLR slide = load_addr - 0x100000000，文件偏移 = addr - load_addr。
    # 因此 DWARF 地址 = 0x100000000 + (addr - load_addr)
    if _IS_LLVM_SYMBOLIZER and _ADDR2LINE:
        try:
            iaddr = int(addr, 16)
            ibase = int(load_addr, 16) if load_addr else 0
            dwarf_addr = 0x100000000 + (iaddr - ibase)
            result = subprocess.run(
                [_ADDR2LINE, "--obj", dwarf_path, hex(dwarf_addr)],
                capture_output=True, text=True, timeout=10,
            )
            out = result.stdout.strip()
            lines = [l.strip() for l in out.splitlines() if l.strip() and "??" not in l]
            if lines:
                return " ".join(lines[:2])
        except Exception as exc:
            logger.debug("llvm-symbolizer (iOS) failed for %s: %s", addr, exc)
    return None


def _find_dwarf_in_dsym(dsym_path: str) -> Optional[str]:
    p = Path(dsym_path)
    # Flutter.dSYM/Contents/Resources/DWARF/Flutter
    dwarf_dir = p / "Contents" / "Resources" / "DWARF"
    if dwarf_dir.exists():
        candidates = list(dwarf_dir.iterdir())
        if candidates:
            return str(candidates[0])
    return None


# ── Android 符号化 ─────────────────────────────────────────────────────────────

# Android flutter 帧格式：
#   #00 pc 00897954  /data/app/.../libflutter.so (???) (BuildId: 0a7fde9baaf490ad50a8480ebc422ea4ee862a2e)
_ANDROID_FLUTTER_FRAME_RE = re.compile(
    r"(#\d+\s+pc\s+)([0-9a-fA-F]+)(\s+.*?libflutter\.so.*?(?:BuildId:\s*([0-9a-fA-F]+)).*?)$",
    re.MULTILINE,
)

def _symbolicate_android(stack: str, binary_images: list) -> str:
    # 提取所有 BuildId
    build_ids_in_stack = set(m.group(4) for m in _ANDROID_FLUTTER_FRAME_RE.finditer(stack) if m.group(4))

    if not build_ids_in_stack:
        return stack

    # 对每个 BuildId 找符号文件
    so_map: dict[str, Optional[str]] = {}
    for bid in build_ids_in_stack:
        so_map[bid] = _get_or_download_android_so(bid) or _find_user_so(bid, "android")

    def replace_frame(m: re.Match) -> str:
        build_id = m.group(4)
        if not build_id:
            return m.group(0)
        so_path = so_map.get(build_id)
        if not so_path:
            return m.group(0)
        offset = m.group(2)
        sym = _addr2line_lookup(so_path, offset)
        if not sym:
            return m.group(0)
        # 统一输出格式：把 .so (???) 替换成 .so [symname]，与 Plan C 一致
        # 若原帧没有 (???)（已被符号化或本来就有），追加 [sym] 到行尾
        original = m.group(0)
        if "(???)" in original:
            return original.replace("(???)", f"[{sym}]")
        return f"{original}  [{sym}]"

    return _ANDROID_FLUTTER_FRAME_RE.sub(replace_frame, stack)


def _addr2line_lookup(so_path: str, offset: str) -> Optional[str]:
    """对 ELF .so 文件按 offset 查符号名 + 文件:行；支持 inlined 帧。

    优先 llvm-symbolizer（输出更结构化），回落 addr2line。
    返回格式示例：
      "InternalFlutterGpu_Texture_AsImage → Gpu::TextureCreate impeller/gpu.cc:142"
      （innermost → outermost 链式 inline 展示，innermost 最具体）
    """
    if not _ADDR2LINE:
        return None
    try:
        if _IS_LLVM_SYMBOLIZER:
            # --inlines 默认开启；显式 --pretty-print 让一帧一行 "func at file:line"
            cmd = [_ADDR2LINE, "--obj", so_path, "--inlines", "--pretty-print",
                   "--demangle", f"0x{offset}"]
        else:
            # GNU addr2line: -i 输出 inlined frames，-f 函数名，-C demangle
            cmd = [_ADDR2LINE, "-f", "-i", "-C", "-e", so_path, f"0x{offset}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = result.stdout.strip()
        if not out:
            return None

        if _IS_LLVM_SYMBOLIZER:
            # llvm-symbolizer --pretty-print 输出每行：
            #   "funcName at /path/file.cpp:50:0"
            # inlined 时多行，innermost 在前
            frames = []
            for line in out.splitlines():
                line = line.strip()
                if not line or "??" in line:
                    continue
                # 简化路径：只保留 basename
                if " at " in line:
                    func, _, loc = line.partition(" at ")
                    loc_basename = loc.rsplit("/", 1)[-1] if "/" in loc else loc
                    frames.append(f"{func} {loc_basename}".strip())
                else:
                    frames.append(line)
            if frames:
                # innermost → outermost；最多展示 2 层避免过长
                return " → ".join(frames[:2])
        else:
            # addr2line -f -i 输出交替的：func / file:line
            lines = [l.strip() for l in out.splitlines() if l.strip() and "??" not in l]
            frames = []
            i = 0
            while i < len(lines):
                func = lines[i]
                loc = lines[i + 1] if i + 1 < len(lines) else ""
                loc_basename = loc.rsplit("/", 1)[-1] if "/" in loc else loc
                frames.append(f"{func} {loc_basename}".strip())
                i += 2
            if frames:
                return " → ".join(frames[:2])
    except Exception as exc:
        logger.debug("addr2line failed for offset %s: %s", offset, exc)
    return None


# ── Flutter Engine 符号下载 ──────────────────────────────────────────────────

def _normalize_uuid(uuid: str) -> str:
    return uuid.replace("-", "").lower()


def _get_or_download_ios_dsym(uuid: str) -> Optional[str]:
    """按 UUID 查找或下载 Flutter.dSYM。"""
    uid = _normalize_uuid(uuid)
    cache = _flutter_engine_cache_dir() / f"ios_{uid}"
    dsym_marker = cache / "Flutter.dSYM"
    if dsym_marker.exists():
        return str(dsym_marker)

    engine_hash = _find_flutter_engine_hash(uid, "ios")
    if not engine_hash:
        return None

    url = (
        f"https://storage.googleapis.com/flutter_infra_release/flutter/"
        f"{engine_hash}/ios-release/Flutter.dSYM.zip"
    )
    return _download_and_extract(url, cache, "Flutter.dSYM")


def _get_or_download_android_so(build_id: str) -> Optional[str]:
    """按 BuildId 查找或下载 libflutter.so（带符号）。"""
    bid = build_id.lower()
    cache = _flutter_engine_cache_dir() / f"android_{bid}"
    so_path = cache / "libflutter.so"
    if so_path.exists():
        return str(so_path)

    engine_hash = _find_flutter_engine_hash(bid, "android")
    if not engine_hash:
        return None

    url = (
        f"https://storage.googleapis.com/flutter_infra_release/flutter/"
        f"{engine_hash}/android-arm64/symbols.zip"
    )
    return _download_and_extract(url, cache, "libflutter.so")


def _find_flutter_engine_hash(uuid_or_build_id: str, platform: str) -> Optional[str]:
    """
    从 UUID / BuildId 推导 Flutter engine commit hash。

    底层逻辑（2026-05 修订）：Plaud 使用自定义 fork Flutter engine，BuildId 永远不
    在 stock 公网符号库（storage.googleapis.com/flutter_infra_release）出现。旧版
    "遍历 40+ stable/beta 下载 symbols.zip 验 BuildId" 的策略对 fork engine 永远 0
    命中，但每次留下 ~12MB 残骸，14GB 起步。Plan C（GitHub release 的
    native_symbols.tar.gz）已经覆盖 fork engine 场景，此函数仅保留本地 index 兜底。

    策略：
    1. 本地 engine_hash_index.json 索引（命中即用 — 历史已命中过的 stock engine
       或用户手动配置的映射）
    2. 索引未命中：返回 None，不再触发下载，避免污染磁盘。
       fork engine 场景走 Plan C；stock engine 场景需用户手动维护 index 或显式开启
       enable_stock_engine_lookup（默认关闭）。
    """
    key = uuid_or_build_id.lower()
    index_path = _flutter_engine_cache_dir() / "engine_hash_index.json"
    if index_path.exists():
        try:
            import json as _json
            index = _json.loads(index_path.read_text(encoding="utf-8")) or {}
            if key in index:
                return index[key]
        except Exception:
            pass

    # 默认不再触发 stock engine 遍历下载（对 fork engine 用户是纯污染）
    try:
        from app.crashguard.config import get_crashguard_settings as _gs
        if not getattr(_gs(), "enable_stock_engine_lookup", False):
            logger.debug(
                "skip stock Flutter engine lookup for %s on %s "
                "(set crashguard.enable_stock_engine_lookup=true to re-enable)",
                key, platform,
            )
            return None
    except Exception:
        # config 读不到时按 fork engine 默认（关闭）
        return None

    # 显式开启时才走遍历下载（保留给上游 Flutter 项目用，非 fork engine 场景）
    try:
        hashes = _fetch_recent_flutter_engine_hashes(max_versions=40)
    except Exception as exc:
        logger.debug("fetch_recent_flutter_engine_hashes failed: %s", exc)
        return None

    index = {}
    if index_path.exists():
        try:
            import json as _json
            index = _json.loads(index_path.read_text(encoding="utf-8")) or {}
        except Exception:
            index = {}

    for engine_hash in hashes:
        if not engine_hash:
            continue
        verified = _verify_engine_hash_against_build_id(engine_hash, key, platform)
        if verified:
            index[key] = engine_hash
            try:
                import json as _json
                index_path.write_text(_json.dumps(index, indent=2), encoding="utf-8")
                logger.info("cached engine_hash mapping: %s → %s", key, engine_hash)
            except Exception:
                pass
            return engine_hash
    return None


# Module-level cache for Flutter releases meta（防止每次重复拉）
_FLUTTER_RELEASES_CACHE: Optional[list] = None
_FLUTTER_RELEASES_CACHE_AT: float = 0.0


def _fetch_recent_flutter_engine_hashes(max_versions: int = 8) -> List[str]:
    """从 Flutter 官方 releases.json 拉最近 N 个 stable SDK 版本，按 SDK hash → engine.version 派生 engine commit hash。

    Flutter releases JSON 字段：每个 release 有 `hash`(Flutter SDK commit) + `channel`。
    engine commit 单独存在 GitHub `flutter/flutter@{sdk_hash}:bin/internal/engine.version` 文件里。
    """
    import time as _time
    import urllib.request as _ureq

    global _FLUTTER_RELEASES_CACHE, _FLUTTER_RELEASES_CACHE_AT
    now = _time.time()
    # 6h 缓存防 Flutter API 速率限制
    if _FLUTTER_RELEASES_CACHE is not None and (now - _FLUTTER_RELEASES_CACHE_AT) < 6 * 3600:
        stable_hashes = _FLUTTER_RELEASES_CACHE
    else:
        url = "https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json"
        try:
            with _ureq.urlopen(url, timeout=15) as resp:  # noqa: S310
                import json as _json
                data = _json.loads(resp.read().decode("utf-8"))
            # 包含 stable + beta（Plaud 灰度可能用 beta channel）；按 releases 顺序遍历
            allowed_channels = {"stable", "beta"}
            stable_hashes = []
            seen = set()
            for r in (data.get("releases") or []):
                if r.get("channel") not in allowed_channels:
                    continue
                h = (r.get("hash") or "").strip()
                if h and h not in seen:
                    seen.add(h)
                    stable_hashes.append(h)
                if len(stable_hashes) >= max_versions * 2:  # 多取一些防部分 engine 查询失败
                    break
            _FLUTTER_RELEASES_CACHE = stable_hashes
            _FLUTTER_RELEASES_CACHE_AT = now
        except Exception as exc:
            logger.warning("fetch_flutter_releases failed: %s", exc)
            return []

    # SDK hash → engine commit
    engine_hashes: List[str] = []
    seen_engines = set()
    for sdk_hash in stable_hashes:
        engine_hash = _sdk_hash_to_engine_hash(sdk_hash)
        if engine_hash and engine_hash not in seen_engines:
            seen_engines.add(engine_hash)
            engine_hashes.append(engine_hash)
        if len(engine_hashes) >= max_versions:
            break
    return engine_hashes


def _sdk_hash_to_engine_hash(sdk_hash: str) -> Optional[str]:
    """通过 GitHub raw 拉 flutter/flutter@{sdk_hash}:bin/internal/engine.version，返回 engine commit。"""
    import urllib.request as _ureq

    url = f"https://raw.githubusercontent.com/flutter/flutter/{sdk_hash}/bin/internal/engine.version"
    try:
        with _ureq.urlopen(url, timeout=10) as resp:  # noqa: S310
            content = resp.read().decode("utf-8").strip()
        # 文件里通常就是一行 hash
        if content and re.match(r"^[0-9a-f]{40}$", content):
            return content
    except Exception as exc:
        logger.debug("sdk_hash_to_engine_hash failed for %s: %s", sdk_hash, exc)
    return None


def _verify_engine_hash_against_build_id(engine_hash: str, build_id: str, platform: str) -> bool:
    """下载 engine_hash 对应的 Android symbols.zip，解压找 libflutter.so，验 build-id 匹配。"""
    plat = (platform or "").lower()
    if "android" not in plat and plat != "" and "flutter" not in plat:
        # iOS UUID 校验目前不支持自动反查（dSYM zip 太大，先跳过）
        return False

    cache = _flutter_engine_cache_dir() / f"android_engine_{engine_hash[:12]}"
    so_path = cache / "libflutter.so"
    if not so_path.exists():
        url = (
            f"https://storage.googleapis.com/flutter_infra_release/flutter/"
            f"{engine_hash}/android-arm64/symbols.zip"
        )
        result = _download_and_extract(url, cache, "libflutter.so")
        if not result or not Path(result).exists():
            return False
        so_path = Path(result)

    # 读 so 的 BuildId 与 stack 里给的对比
    try:
        out = subprocess.run(
            ["file", str(so_path)], capture_output=True, text=True, timeout=5,
        ).stdout.lower()
        if build_id.lower() in out:
            # 把这个 .so 同时链到 android_{build_id} 目录，让 _get_or_download_android_so 命中
            target_dir = _flutter_engine_cache_dir() / f"android_{build_id.lower()}"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_so = target_dir / "libflutter.so"
            if not target_so.exists():
                try:
                    target_so.symlink_to(so_path)
                except Exception:
                    import shutil as _sh
                    _sh.copy(so_path, target_so)
            return True
    except Exception as exc:
        logger.debug("verify_engine_hash_against_build_id failed: %s", exc)
    return False


def _download_and_extract(url: str, dest_dir: Path, target_name: str) -> Optional[str]:
    """下载 zip 并解压，返回 target_name 的路径，失败返回 None。"""
    import urllib.request
    import tempfile

    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / target_name
    if target.exists():
        return str(target)

    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name
        logger.info("downloading flutter engine symbols: %s", url)
        urllib.request.urlretrieve(url, tmp_path)  # noqa: S310

        with zipfile.ZipFile(tmp_path) as zf:
            members = zf.namelist()
            # 找 target_name（可能在子目录里）
            matched = [m for m in members if m.endswith(target_name) or target_name in m]
            if not matched:
                # 解压全部，再找
                zf.extractall(dest_dir)
            else:
                for m in matched:
                    zf.extract(m, dest_dir)

        # 递归找 target 文件
        candidates = list(dest_dir.rglob(target_name))
        if candidates:
            # 如果不在 dest_dir 根，建软链接方便后续访问
            if str(candidates[0]) != str(target):
                target.symlink_to(candidates[0])
            return str(target)
        return None
    except Exception as exc:
        logger.warning("failed to download/extract %s: %s", url, exc)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ── 用户上传符号包查找 ────────────────────────────────────────────────────────

def _find_user_dsym(uuid: str, platform: str) -> Optional[str]:
    """在用户上传的 dsym 包里按 UUID 查找 dSYM bundle。"""
    symbols_dir = _user_symbols_dir() / platform / "dsym"
    if not symbols_dir.exists():
        return None
    uid = _normalize_uuid(uuid)
    for version_dir in symbols_dir.iterdir():
        if not version_dir.is_dir():
            continue
        for p in version_dir.rglob("*.dSYM"):
            plist = p / "Contents" / "Info.plist"
            if plist.exists():
                try:
                    text = plist.read_text(encoding="utf-8")
                    if uid in text.replace("-", "").lower():
                        return str(p)
                except Exception:
                    continue
    return None


def _find_user_so(build_id: str, platform: str) -> Optional[str]:
    """在用户上传的包里按 BuildId 查找 libflutter.so（简单目录扫描）。"""
    symbols_dir = _user_symbols_dir() / platform
    if not symbols_dir.exists():
        return None
    bid = build_id.lower()
    for so in symbols_dir.rglob("libflutter.so"):
        # 尝试用 file 命令检查 build-id（可选，不影响功能）
        try:
            r = subprocess.run(
                ["file", str(so)], capture_output=True, text=True, timeout=3,
            )
            if bid in r.stdout.lower():
                return str(so)
        except Exception:
            pass
    return None


# ── Plan C：GitHub release 符号 ────────────────────────────────────────────────

def _symbolicate_ios_with_dir(stack: str, dsyms_dir: str) -> str:
    """
    用 GitHub release 里解压出的 dSYMs 目录对 iOS stack 做符号化。

    只对 module 名与某个 dSYM 的实际二进制名匹配的帧发起查询——2026-07-22 生产环境
    实测发现：不做这层过滤时，libsystem_kernel.dylib / BoardServices / ActivityKit
    等系统库帧（dSYM 包里根本没有对应符号）会被逐一拿去跟 App 自己的 dSYM 硬凑；
    llvm-symbolizer 的地址换算公式 `0x100000000 + (addr - base)` 换出来的地址几乎
    总能落在 App 巨大的符号表某个函数范围内，于是每一帧都"成功"吐出一个看似合理
    实则完全无关的 Plaud 符号——表现为"整段堆栈看起来像没符号化"（实际是错误符号化，
    比原样保留地址更具误导性）。Android 侧的 `_symbolicate_android_with_dir` 一直有
    BuildId 精确匹配做 gating，这里之前没有对应校验。
    """
    if not _ATOS and not _IS_LLVM_SYMBOLIZER:
        return stack

    import re as _re
    # 匹配尚未符号化的 iOS 帧：函数名为十六进制地址或 "???"
    _unsym_re = _re.compile(
        r"^(\s*\d+\s+)(\S+)(\s+)(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+\+\s+(\d+)(.*)",
        _re.MULTILINE,
    )

    dsyms = list(Path(dsyms_dir).rglob("*.dSYM"))
    if not dsyms:
        return stack

    # module 名（大小写不敏感）→ DWARF 路径。DWARF 文件名就是该 dSYM bundle 对应的
    # 二进制名（Contents/Resources/DWARF/<binary_name>），天然可以跟堆栈里的 module
    # 字段对上——同一个 dSYMs.zip 里可能有多个 bundle（主 App + extension），逐个收集。
    dwarf_by_module: dict = {}
    for dsym in dsyms:
        dwarf = _find_dwarf_in_dsym(str(dsym))
        if dwarf:
            dwarf_by_module[Path(dwarf).name.lower()] = dwarf
    if not dwarf_by_module:
        return stack

    lines = stack.splitlines(keepends=True)
    result = []
    for line in lines:
        m = _unsym_re.match(line)
        if not m:
            result.append(line)
            continue
        module = m.group(2)
        dwarf = dwarf_by_module.get(module.lower())
        if not dwarf:
            # module 不属于本次下载的任何 dSYM（多半是系统库）——原样保留地址，
            # 不要瞎猜，宁可不符号化也不要给错的符号。
            result.append(line)
            continue
        addr = m.group(4)
        base = m.group(5)
        sym = _atos_lookup(dwarf, base, addr)
        if sym:
            result.append(f"{m.group(1)}{module}{m.group(3)}{sym}{m.group(7)}\n")
        else:
            result.append(line)
    return "".join(result)


# ProGuard mapping 行格式：
#   com.original.Class -> a.b.C:
#       returnType originalMethod(params) -> x
_PG_CLASS_RE = re.compile(r"^(\S+)\s+->\s+(\S+):$")
_PG_METHOD_RE = re.compile(r"^\s+\S+\s+(\S+)\(.*?\)\s+->\s+(\S+)$")

def _build_proguard_index(mapping_path: str) -> dict:
    """解析 mapping.txt，构建 {obfuscated → original} 映射表（类名 + 方法名）。"""
    index: dict = {}  # obfuscated_class → original_class
    method_index: dict = {}  # (obfuscated_class, obfuscated_method) → original_method
    current_obf = ""
    try:
        with open(mapping_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                cm = _PG_CLASS_RE.match(line)
                if cm:
                    orig, obf = cm.group(1), cm.group(2).rstrip(":")
                    index[obf] = orig
                    current_obf = obf
                    continue
                if current_obf:
                    mm = _PG_METHOD_RE.match(line)
                    if mm:
                        orig_m, obf_m = mm.group(1), mm.group(2)
                        method_index[(current_obf, obf_m)] = orig_m
    except Exception as exc:
        logger.warning("failed to parse ProGuard mapping %s: %s", mapping_path, exc)
    return {"classes": index, "methods": method_index}


# 缓存 mapping 解析结果（按文件路径），避免每次重复解析 50MB 文件
_PG_INDEX_CACHE: dict = {}

def _get_proguard_index(mapping_path: str) -> dict:
    if mapping_path not in _PG_INDEX_CACHE:
        logger.info("parsing ProGuard mapping %s ...", mapping_path)
        _PG_INDEX_CACHE[mapping_path] = _build_proguard_index(mapping_path)
        logger.info("ProGuard mapping loaded: %d classes", len(_PG_INDEX_CACHE[mapping_path]["classes"]))
    return _PG_INDEX_CACHE[mapping_path]


# Android Java/Kotlin 堆栈帧格式：
#   at a.b.c.d(SourceFile:123)
#   at a.b.c.d(Unknown Source)
_ANDROID_FRAME_RE = re.compile(r"(\s+at\s+)([\w.$]+)\.([\w$]+)\(([^)]*)\)")

def _retrace_proguard(stack: str, mapping_path: str) -> str:
    """用 ProGuard mapping 对 Android stack 做 retrace（纯 Python，无需 retrace 工具）。"""
    idx = _get_proguard_index(mapping_path)
    classes = idx.get("classes", {})
    methods = idx.get("methods", {})
    if not classes:
        return stack

    def replace_frame(m: re.Match) -> str:
        prefix = m.group(1)
        obf_class = m.group(2)
        obf_method = m.group(3)
        rest = m.group(4)
        orig_class = classes.get(obf_class, obf_class)
        orig_method = methods.get((obf_class, obf_method), obf_method)
        return f"{prefix}{orig_class}.{orig_method}({rest})"

    return _ANDROID_FRAME_RE.sub(replace_frame, stack)
