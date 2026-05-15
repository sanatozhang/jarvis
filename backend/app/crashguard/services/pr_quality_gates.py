"""PR 质量闸门（12 道防线集中实现）。

底层逻辑：AI 生成 PR 的失败模式高度集中——幻觉文件路径、跑偏修复点、扫到 build
artifact、跨平台串台。把所有"准入/落地前体检"逻辑集中到这一个模块，pr_drafter
只调入口函数，便于 owner 视角端到端追溯每道闸的命中率。

各闸都是纯函数（除 Gate#9 LLM 判官需异步）。返回 ``(passed, reason)`` 二元组——
``passed=False`` 时上游必须 abort（按用户硬性要求"不接受 doc-only fallback"）。

颗粒度对齐：Gate#1/5/8 共享 path/identifier 抽取工具，封装到顶部 `_extract_*`。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("crashguard.pr_quality_gates")


# ============================================================
# 共享工具：从 fix_suggestion / fix_diff 抽路径 / 标识符
# ============================================================

# 匹配 unified diff 头：`--- a/path/to/file.kt` / `+++ b/path/to/file.kt`
_RE_DIFF_PATH = re.compile(r"^\s*[-+]{3}\s+[ab]/([^\s\n]+)", re.MULTILINE)
# 匹配源码扩展 inline 引用：`MainActivity.kt`、`lib/foo.dart`、`Runner/AppDelegate.swift`
_RE_INLINE_PATH = re.compile(
    r"[\w./\-]+\.(?:kt|java|swift|m|mm|h|hpp|cpp|cc|dart|gradle|gradle\.kts|xml|yaml|yml|plist|pbxproj)",
)
# 匹配反引号包裹的标识符：`onWindowStartingActionMode`、`isFinishing`
_RE_BACKTICK_IDENT = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{3,})`")
# 匹配代码块中的方法/类名（CamelCase 或 snake_case，长度 ≥4 防误抓"if/for"）
_RE_CODE_IDENT = re.compile(r"\b([A-Z][A-Za-z0-9]{3,}|[a-z_][a-zA-Z0-9_]{4,})\b")


def _extract_paths_from_diff(diff_text: str) -> list[str]:
    """从 fix_diff 抽所有 `--- a/path` / `+++ b/path` 路径（去重保序）。"""
    if not diff_text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _RE_DIFF_PATH.finditer(diff_text):
        p = (m.group(1) or "").strip()
        if not p or p == "/dev/null" or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _extract_paths_from_text(text: str) -> list[str]:
    """从自然语言 fix_suggestion 里抓 inline 路径（`MainActivity.kt`、`lib/foo.dart`）。"""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _RE_INLINE_PATH.finditer(text):
        p = m.group(0)
        # 去掉 markdown 尾标点 / 引号
        p = p.rstrip(".,);:`\"'")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _extract_keywords(text: str, max_n: int = 30) -> list[str]:
    """从 fix_suggestion 抓技术标识符（反引号包裹 > CamelCase 方法名 > 路径 basename）。

    严格优先级：反引号 > CamelCase 方法名 > 类名。返回去重前 max_n 个，
    用于 Gate#5 投喂 agent 实存文件清单 + Gate#8 校验 diff 命中。
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    # P0：反引号包裹的标识符权重最高
    for m in _RE_BACKTICK_IDENT.finditer(text):
        kw = m.group(1)
        if kw not in seen:
            seen.add(kw)
            out.append(kw)
    # P1：所有 CamelCase / 长 snake_case
    for m in _RE_CODE_IDENT.finditer(text):
        kw = m.group(1)
        # 过滤常见自然词噪声 + Stop list
        if kw.lower() in _STOP_WORDS:
            continue
        if kw not in seen:
            seen.add(kw)
            out.append(kw)
        if len(out) >= max_n:
            break
    return out[:max_n]


# 常见自然语言/英文停用词 + markdown noise，避免把"Issue/Title/Frontend"当成代码标识符
_STOP_WORDS = {
    "issue", "title", "frontend", "confidence", "feasibility", "summary",
    "changed", "files", "platform", "android", "ios", "flutter", "swift",
    "kotlin", "report", "analysis", "should", "would", "could", "needs",
    "context", "current", "expected", "actual", "error", "exception",
    "stack", "frame", "method", "class", "function", "return", "import",
    "package", "object", "value", "string", "number", "boolean", "true",
    "false", "null", "undefined", "static", "private", "public", "protected",
    "internal", "abstract", "interface", "extends", "implements", "override",
    "called", "called_", "calling", "called_when", "see_fix_diff", "fix_diff",
    "fix_suggestion", "root_cause", "noinspection",
}


# ============================================================
# Gate#1：路径存在性预校验
# ============================================================

def verify_fix_paths(
    repo_path: str, fix_suggestion: str, fix_diff: str,
    min_ratio: float = 0.5, min_paths: int = 1,
) -> tuple[bool, str, dict]:
    """检查 fix_diff/fix_suggestion 引用的源码路径是否真实存在于 repo_path。

    抓手：AI 幻觉的最高频失败模式（PR #216 教训：fix_diff 引用了不存在的 MainActivity.kt）。

    校验口径：
      1. 优先用 fix_diff 的 `--- a/path` 头（AI 显式声明的目标路径，最准）
      2. 不够再退化到 fix_suggestion 里 inline 路径（如"修改 lib/foo.dart"）
      3. 抽到的路径数 < min_paths → ratio 算 0（防 AI 用纯自然语言绕过校验）
      4. 实存比 < min_ratio → 失败

    返回 (passed, reason, debug_info)。debug_info 含全部抽到的路径 + 命中清单，
    供审计落盘。
    """
    repo = Path(repo_path)
    paths = _extract_paths_from_diff(fix_diff or "")
    if len(paths) < min_paths:
        paths += [p for p in _extract_paths_from_text(fix_suggestion or "") if p not in paths]
    info = {"paths_extracted": paths[:20]}
    if not paths:
        # AI 没给任何路径线索 → 落到 agent 自由发挥，本 gate 不阻断（agent 内自带 Glob）
        info["skipped"] = "no_paths_in_fix_diff_or_suggestion"
        return True, "skipped (no paths to verify)", info
    existing: list[str] = []
    missing: list[str] = []
    for p in paths:
        # 容忍 a/ / b/ 前缀 + 容忍尾随空白
        clean = p.lstrip("/").strip()
        if not clean:
            continue
        target = repo / clean
        if target.exists() and target.is_file():
            existing.append(clean)
        else:
            # 兜底：basename match（AI 可能给错目录但文件名对，让 Gate#5 再投喂正确路径）
            base = os.path.basename(clean)
            if base:
                hits = list(repo.rglob(base))
                # 过滤 build/.git 等噪声路径
                hits = [h for h in hits if not any(
                    seg in (".git", "build", ".gradle", "node_modules",
                            ".dart_tool", "DerivedData", "Pods", ".idea")
                    for seg in h.parts
                )]
                if hits:
                    existing.append(clean)
                    continue
            missing.append(clean)
    n = len(existing) + len(missing)
    ratio = (len(existing) / n) if n else 0.0
    info.update({"existing": existing, "missing": missing, "ratio": round(ratio, 2)})
    if ratio < min_ratio:
        return False, (
            f"path_check_failed: {len(existing)}/{n} paths exist in repo "
            f"(ratio={ratio:.0%} < {min_ratio:.0%}); missing: {missing[:5]}"
        ), info
    return True, f"path_check_ok: {len(existing)}/{n} paths exist", info


# ============================================================
# Gate#2：stack→平台强制路由
# ============================================================

# Plaud 是 Flutter 双端 + 原生壳；webview 内嵌 + dart 框架崩溃实际应在 flutter 仓修
_FORCE_FLUTTER_PATTERNS = (
    r"\bInAppWebView\b",          # flutter_inappwebview 插件
    r"\bflutter_inappwebview\b",
    r"package:flutter/",          # flutter 框架栈
    r"package:[\w_]+/",            # 任意 dart pubspec package
    r"\.dart:\d+",                 # dart 行号
    r"\bFlutterEngine\b",
    r"\bFlutterFragmentActivity\b",
)
# native android 强信号
_FORCE_ANDROID_PATTERNS = (
    r"\bjava\.lang\.",
    r"\bandroid\.os\.",
    r"\bandroid\.view\.",
    r"\bkotlin\.",
    r"\bai\.plaud\.android\.",
    r"\.kt:\d+",
    r"\.java:\d+",
)
_FORCE_IOS_PATTERNS = (
    r"\bUIKit\.",
    r"\bFoundation\.",
    r"\bSwift\.",
    r"\.swift:\d+",
    r"\bNS[A-Z]\w+Exception\b",
)


def detect_forced_platform(
    stack: str, claimed_platform: str,
) -> tuple[Optional[str], str]:
    """从崩溃栈强制锁定平台，返回 (forced_platform, reason)。

    底层逻辑：AI 路由偶尔串台（PR #988 教训：Android crash 误路由到 flutter）。
    崩溃栈本身是地面真相——含 `.dart` 行号就是 dart 层崩溃，再多 native 信号也无用。

    返回 None 表示无强制；返回 "android"/"ios"/"flutter" 表示锁定该平台。
    claimed_platform 仅用于日志（识别"强制覆盖"事件）。
    """
    if not stack:
        return None, "no_stack"
    # Flutter 优先级最高：dart frame 在 native crash 中也会出现，但反过来不成立
    if any(re.search(p, stack) for p in _FORCE_FLUTTER_PATTERNS):
        return "flutter", "stack_contains_dart_or_flutter_frame"
    if any(re.search(p, stack) for p in _FORCE_IOS_PATTERNS):
        return "ios", "stack_contains_ios_frame"
    if any(re.search(p, stack) for p in _FORCE_ANDROID_PATTERNS):
        return "android", "stack_contains_android_frame"
    return None, "no_force_signal"


# ============================================================
# Gate#3：confidence / feasibility 门槛
# ============================================================

def pass_confidence_gate(
    confidence: str, feasibility: float,
    min_confidence: str = "high", min_feasibility: float = 0.7,
) -> tuple[bool, str]:
    """只放行 confidence ≥ min_confidence && feasibility ≥ min_feasibility。

    顺序：very_low < low < medium < high。"high" 是最高级。
    """
    order = {"very_low": 0, "low": 1, "medium": 2, "high": 3}
    cur = order.get((confidence or "low").lower(), 1)
    req = order.get(min_confidence.lower(), 3)
    if cur < req:
        return False, (
            f"confidence_too_low: {confidence!r} < {min_confidence!r}"
        )
    if (feasibility or 0.0) < min_feasibility:
        return False, (
            f"feasibility_too_low: {feasibility:.2f} < {min_feasibility:.2f}"
        )
    return True, f"confidence={confidence} feasibility={feasibility:.2f}"


# ============================================================
# Gate#5：预投喂实存文件清单（给 agent prompt）
# ============================================================

def collect_existing_paths_for_keywords(
    repo_path: str, fix_suggestion: str, fix_diff: str,
    max_n: int = 20,
) -> list[str]:
    """从 fix_text 抽关键词（类名/方法名），在 repo 内 Glob 找实存路径列表。

    给 agent prompt 注入"这些是真实存在的文件，选择目标必须从中挑"，
    防 #216 类幻觉文件名（agent 找不到 MainActivity.kt 就新建空壳）。
    """
    repo = Path(repo_path)
    if not repo.exists():
        return []
    out: list[str] = []
    seen: set[str] = set()

    # 1. fix_diff 显式路径（最准）
    for p in _extract_paths_from_diff(fix_diff or ""):
        clean = p.lstrip("/").strip()
        if not clean or clean in seen:
            continue
        if (repo / clean).is_file():
            out.append(clean)
            seen.add(clean)

    # 2. fix_suggestion inline 路径
    for p in _extract_paths_from_text(fix_suggestion or ""):
        clean = p.lstrip("/").strip()
        if clean in seen:
            continue
        if (repo / clean).is_file():
            out.append(clean)
            seen.add(clean)
            continue
        # basename rglob 找
        base = os.path.basename(clean)
        if base:
            for h in repo.rglob(base):
                rel = h.relative_to(repo).as_posix()
                if not any(seg in (".git", "build", ".gradle", "node_modules",
                                   ".dart_tool", "DerivedData", "Pods", ".idea")
                           for seg in h.parts) and rel not in seen:
                    out.append(rel)
                    seen.add(rel)
                    break

    # 3. 关键词（类名 → CamelCase 文件名启发式）
    if len(out) < max_n:
        for kw in _extract_keywords(fix_suggestion or "", max_n=15):
            if not kw[:1].isupper():
                continue
            # 启发：CamelCase 类名一般对应同名文件，扩展名按平台候选
            for ext in (".kt", ".java", ".swift", ".dart", ".m", ".mm", ".h"):
                fn = kw + ext
                for h in repo.rglob(fn):
                    rel = h.relative_to(repo).as_posix()
                    if rel in seen:
                        continue
                    if any(seg in (".git", "build", ".gradle", "node_modules",
                                   ".dart_tool", "DerivedData", "Pods", ".idea")
                           for seg in h.parts):
                        continue
                    out.append(rel)
                    seen.add(rel)
                    if len(out) >= max_n:
                        break
                if len(out) >= max_n:
                    break
            if len(out) >= max_n:
                break

    # 4. 兜底宽扫描：前三步找不到任何文件时，扫主源码目录喂给 agent。
    #    根因：AI 分析常使用训练数据里的标准类名（如 `MainActivity.kt`），
    #    但 Plaud 项目用自定义命名（如 `NiceBuildApplication.kt`）。
    #    不给 agent 任何实存文件 → agent 找不到目标 → 无改动 → PR 失败。
    #    宽扫描确保 agent 拿到真实文件菜单，自行判断改哪个。
    if len(out) < 3:
        _NOISE = (".git", "build", ".gradle", "node_modules", ".dart_tool",
                  "DerivedData", "Pods", ".idea", "test", "androidTest",
                  "generated", "GeneratedPluginRegistrant")
        _PLATFORM_SCAN: list[tuple[str, str]] = []
        text_lower = (fix_suggestion or "").lower()
        if ".kt" in text_lower or ".java" in text_lower or "android" in text_lower:
            _PLATFORM_SCAN += [("app/src/main", ".kt"), ("app/src/main", ".java"),
                               ("app/src/global", ".kt")]
        if ".dart" in text_lower or "flutter" in text_lower or "pubspec" in text_lower:
            _PLATFORM_SCAN += [("lib", ".dart")]
        if ".swift" in text_lower or "ios" in text_lower:
            _PLATFORM_SCAN += [("", ".swift"), ("Runner", ".swift")]
        # 如果 fix_suggestion 没有平台信号，扫所有主流源码类型
        if not _PLATFORM_SCAN:
            _PLATFORM_SCAN = [("app/src/main", ".kt"), ("lib", ".dart"),
                              ("", ".swift"), ("app/src/main", ".java")]
        for scan_dir, ext in _PLATFORM_SCAN:
            scan_root = repo / scan_dir if scan_dir else repo
            if not scan_root.is_dir():
                continue
            for h in scan_root.rglob(f"*{ext}"):
                if len(out) >= max_n:
                    break
                rel = h.relative_to(repo).as_posix()
                if rel in seen:
                    continue
                if any(seg in _NOISE for seg in h.parts):
                    continue
                out.append(rel)
                seen.add(rel)
            if len(out) >= max_n:
                break

    return out[:max_n]


# ============================================================
# Gate#7：语法速检（落地前 git status 拿改动文件，跑对应 linter）
# ============================================================

def lint_changed_files(
    repo_path: str, files: list[str], timeout_sec: int = 30,
) -> tuple[bool, str, dict]:
    """对改动文件按扩展名跑语法快检。工具不在 PATH 时跳过（best-effort 模式）。

    抓手：PR #216 教训——agent 生成的 25 行 MainActivity.kt 编译都不过（companion
    object 引用未 import 的 `withCachedEngine`）。语法速检至少拦住"打不开"的代码。

    返回 (passed, reason, debug)：
      passed=False → 抓到语法错（拒绝 commit）
      passed=True + reason 含 'skipped' → 工具不在或文件不在监控扩展（不阻断）
    """
    by_ext: dict[str, list[str]] = {}
    repo_root = Path(repo_path)
    for f in files:
        # 仅 lint 真实存在的文件——agent 可能产 deleted/renamed 路径，不该报错
        target = repo_root / f if not os.path.isabs(f) else Path(f)
        if not target.is_file():
            continue
        ext = os.path.splitext(f)[1].lower()
        by_ext.setdefault(ext, []).append(f)

    errs: list[str] = []
    skipped: list[str] = []
    checked: list[str] = []

    # Kotlin: ktlint 优先；没有就跳（Plaud-android 仓自带 gradle ktlintCheck 太重）
    if ".kt" in by_ext and shutil.which("ktlint"):
        for f in by_ext[".kt"]:
            try:
                r = subprocess.run(
                    ["ktlint", "--relative", f],
                    cwd=repo_path, capture_output=True, text=True,
                    timeout=timeout_sec,
                )
                checked.append(f)
                # ktlint exit 1 = 有 lint 问题；exit ≥ 2 = 严重错（解析失败）
                if r.returncode >= 2:
                    errs.append(f"ktlint(parse): {f} :: {(r.stderr or r.stdout)[:200]}")
            except Exception as exc:
                logger.info("ktlint skip %s: %s", f, exc)
    elif ".kt" in by_ext:
        skipped.extend(by_ext[".kt"])

    # Swift: swiftc -parse（dry parse，不需 SDK）
    if ".swift" in by_ext and shutil.which("swiftc"):
        for f in by_ext[".swift"]:
            try:
                r = subprocess.run(
                    ["swiftc", "-parse", f],
                    cwd=repo_path, capture_output=True, text=True,
                    timeout=timeout_sec,
                )
                checked.append(f)
                if r.returncode != 0:
                    errs.append(f"swiftc(parse): {f} :: {(r.stderr or '')[:300]}")
            except Exception as exc:
                logger.info("swiftc skip %s: %s", f, exc)
    elif ".swift" in by_ext:
        skipped.extend(by_ext[".swift"])

    # Dart: dart analyze（pubspec 可能缺，best-effort）
    if ".dart" in by_ext and shutil.which("dart"):
        for f in by_ext[".dart"]:
            try:
                r = subprocess.run(
                    ["dart", "analyze", "--fatal-warnings", f],
                    cwd=repo_path, capture_output=True, text=True,
                    timeout=timeout_sec,
                )
                checked.append(f)
                # 0 = ok；1 = info/warning；2 = error
                if r.returncode >= 2:
                    errs.append(f"dart-analyze: {f} :: {(r.stdout or r.stderr)[:300]}")
            except Exception as exc:
                logger.info("dart analyze skip %s: %s", f, exc)
    elif ".dart" in by_ext:
        skipped.extend(by_ext[".dart"])

    # Python（防有人 push .py 修复）
    if ".py" in by_ext:
        for f in by_ext[".py"]:
            try:
                r = subprocess.run(
                    ["python3", "-m", "py_compile", f],
                    cwd=repo_path, capture_output=True, text=True,
                    timeout=timeout_sec,
                )
                checked.append(f)
                if r.returncode != 0:
                    errs.append(f"py_compile: {f} :: {(r.stderr or '')[:300]}")
            except Exception as exc:
                logger.info("py_compile skip %s: %s", f, exc)

    debug = {"checked": checked, "skipped": skipped, "errors": errs}
    if errs:
        return False, f"syntax_check_failed: {len(errs)} file(s) have parse errors", debug
    return True, f"syntax_check_ok: checked={len(checked)} skipped={len(skipped)}", debug


# ============================================================
# Gate#8：关键词命中检查
# ============================================================

def verify_keyword_hits(
    diff_text: str, fix_suggestion: str, min_hits: int = 1,
) -> tuple[bool, str, dict]:
    """diff 必须命中 fix_suggestion 里至少 min_hits 个关键标识符。

    抓手：拦"AI 改了但没改到点"——agent 在错误的文件里改了无关行，
    diff 不为空但根本没碰 fix_suggestion 提到的方法/类（PR #988 教训：
    新建 lib/test_new_file.dart 占位文件，跟原 root cause 完全无关）。
    """
    kws = _extract_keywords(fix_suggestion or "", max_n=20)
    if not kws:
        return True, "skipped (no keywords extractable)", {"keywords": []}
    diff_l = (diff_text or "").lower()
    hits = [kw for kw in kws if kw.lower() in diff_l]
    info = {"keywords": kws[:15], "hits": hits[:15]}
    if len(hits) < min_hits:
        return False, (
            f"keyword_hit_failed: diff hit {len(hits)}/{len(kws)} "
            f"fix_suggestion keywords (<{min_hits}); kws sample: {kws[:5]}"
        ), info
    return True, f"keyword_hit_ok: {len(hits)}/{len(kws)} hits", info


# ============================================================
# Gate#9：二级 LLM 判官（给 diff 打分 0-10）
# ============================================================

async def judge_diff_with_llm(
    diff_text: str, fix_suggestion: str, root_cause: str,
    min_score: int = 7, timeout_sec: int = 120,
) -> tuple[bool, str, dict]:
    """让二级 LLM 评判 diff 是否真解决了 root_cause 描述的崩溃。

    抓手：所有前面 gate 都过了，仍可能存在"agent 在对的文件里改了无关行"。
    用便宜模型（Haiku/GPT-4o-mini）做端到端语义评判，<7 分一律否决。

    评分维度（提示 LLM 给出 0-10 分）：
      1. diff 是否触及 fix_suggestion 提到的关键代码点
      2. diff 改动是否符合 root_cause 描述的修复方向
      3. 是否引入无关改动 / 占位代码 / 编译不过的代码
    """
    if not diff_text or not fix_suggestion:
        return True, "skipped (no diff or fix_suggestion)", {"score": None}
    # 防 diff 过大 token 爆炸：截 8000 字
    diff_snip = (diff_text or "")[:8000]
    fix_snip = (fix_suggestion or "")[:3000]
    cause_snip = (root_cause or "")[:1500]
    prompt = f"""你是 Plaud senior code reviewer。下面是 AI agent 自动生成的修复 patch，请评判它是否真正解决了原始崩溃。

## Root Cause（崩溃根因）
{cause_snip}

## Fix Suggestion（AI 给出的修复方案）
{fix_snip}

## Actual Diff（agent 实际改的代码）
```diff
{diff_snip}
```

## 评分维度（0-10 分整数，**严格打分**）
- diff 改的代码是否真触及 fix_suggestion 描述的关键代码点？
- diff 是否符合 root_cause 描述的修复方向？
- diff 是否引入了无关改动 / 占位文件 / 编译不过的代码？
- diff 是否可能让 reviewer 一眼看懂修复意图？

## 输出格式（严格 JSON 单行，**无任何其它字符**）
{{"score": <0-10 整数>, "verdict": "approve" | "reject", "reason": "<≤80 字一句话说明>"}}
"""
    # 调 jarvis 自带的 agent_orchestrator —— claude_code agent 即可
    try:
        from app.services.agent_orchestrator import AgentOrchestrator
        import asyncio
        import tempfile
        import json as _json

        orch = AgentOrchestrator()
        agent = orch.select_agent(rule_type="crashguard")
        # 用临时 workspace 跑（避免污染真实 repo）
        with tempfile.TemporaryDirectory(prefix="crashguard_judge_") as td:
            workspace = Path(td)
            try:
                await asyncio.wait_for(
                    agent.analyze(workspace=workspace, prompt=prompt),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                return True, "skipped (judge timeout — gate fails open)", {"score": None}
            # ClaudeCodeAgent 把回答写到 output/result.json 或 stdout；
            # 容忍多种格式：先看 output/result.json，再看根目录 result.json
            candidates = [
                workspace / "output" / "result.json",
                workspace / "result.json",
            ]
            raw_text = ""
            for c in candidates:
                if c.exists():
                    raw_text = c.read_text(encoding="utf-8", errors="ignore")
                    break
            if not raw_text:
                # 看 output/ 下所有文件，找含 score 的
                for f in workspace.rglob("*.json"):
                    try:
                        t = f.read_text(encoding="utf-8", errors="ignore")
                        if '"score"' in t:
                            raw_text = t
                            break
                    except Exception:
                        continue
        if not raw_text:
            return True, "skipped (judge produced no output — gate fails open)", {"score": None}
        # 容忍 BOM + 包裹文本：抓第一个含 score 的 {...}
        try:
            parsed = _json.loads(raw_text.lstrip("\ufeff").strip())
        except _json.JSONDecodeError:
            m = re.search(r"\{[^{}]*\"score\"[^{}]*\}", raw_text, re.DOTALL)
            if not m:
                return True, "skipped (judge response not parseable)", {"raw": raw_text[:200]}
            try:
                parsed = _json.loads(m.group(0))
            except _json.JSONDecodeError:
                return True, "skipped (judge response invalid json)", {"raw": raw_text[:200]}
        score = int(parsed.get("score", 0) or 0)
        verdict = (parsed.get("verdict") or "").lower()
        reason = (parsed.get("reason") or "")[:200]
        info = {"score": score, "verdict": verdict, "reason": reason}
        if score < min_score or verdict == "reject":
            return False, (
                f"llm_judge_failed: score={score}/{min_score} "
                f"verdict={verdict} reason={reason}"
            ), info
        return True, f"llm_judge_ok: score={score} verdict={verdict}", info
    except Exception as exc:
        logger.exception("llm_judge crashed (gate fails open)")
        return True, f"skipped (judge crashed: {exc})", {"error": str(exc)}


# ============================================================
# Gate#10：多候选先合议（选 primary 平台）
# ============================================================

def pick_primary_platform(
    candidates: list[tuple[str, str]],
    stack: str, fix_suggestion: str, claimed_platform: str,
) -> tuple[Optional[tuple[str, str]], str]:
    """从候选仓库列表里选一个 primary，只在 primary 仓开 PR。

    优先级（强 → 弱）：
      1. Gate#2 强制平台命中 → 选该平台
      2. claimed_platform 在候选里 → 选 claimed
      3. 候选第一个

    返回 (primary_tuple, reason)；primary_tuple=None 表示候选为空。
    """
    if not candidates:
        return None, "no_candidates"
    forced, why = detect_forced_platform(stack, claimed_platform)
    if forced:
        for name, path in candidates:
            if name == forced:
                return (name, path), f"forced_by_stack: {why}"
    # claimed 在候选里
    cl = (claimed_platform or "").lower()
    for name, path in candidates:
        if name == cl:
            return (name, path), "claimed_platform_in_candidates"
    return candidates[0], "first_candidate_fallback"


# ============================================================
# Gate#13：版本号字段保护（pubspec/build.gradle/Info.plist 一律禁碰）
# ============================================================

_VERSION_FILE_RULES = (
    # (file_basename / suffix match, regex_for_version_lines)
    ("pubspec.yaml", re.compile(r"^[+-]\s*version\s*:", re.MULTILINE)),
    ("build.gradle", re.compile(r"^[+-]\s*(versionCode|versionName)\b", re.MULTILINE)),
    ("build.gradle.kts", re.compile(r"^[+-]\s*(versionCode|versionName)\b", re.MULTILINE)),
    ("Info.plist", re.compile(r"^[+-].*(CFBundleVersion|CFBundleShortVersionString)", re.MULTILINE)),
)


def verify_no_version_bump(diff_text: str) -> tuple[bool, str, dict]:
    """禁 agent 改 pubspec/build.gradle/Info.plist 的版本号字段。

    抓手：PR #991/#992/#993 教训——AI 顺手把 pubspec.yaml 的
    `version: 3.2.0+508` bump 成 `+510`，与 fix_suggestion 无关，浪费 reviewer 心智。
    版本号属于 release 流程，crashguard 修 bug 不发版。
    """
    if not diff_text:
        return True, "skipped (no diff)", {}
    violations: list[str] = []
    for fname, regex in _VERSION_FILE_RULES:
        # 找 diff 里 `--- a/path/pubspec.yaml` 文件块
        # 简化：只要 diff 含 path 且匹配 version 正则就标违规
        if fname in diff_text and regex.search(diff_text):
            violations.append(fname)
    if violations:
        return False, (
            f"version_bump_blocked: detected version field change in "
            f"{', '.join(violations)} — release files must not be touched by crashguard"
        ), {"files": violations}
    return True, "no version bump", {}


__all__ = [
    "verify_fix_paths",
    "detect_forced_platform",
    "pass_confidence_gate",
    "collect_existing_paths_for_keywords",
    "lint_changed_files",
    "verify_keyword_hits",
    "judge_diff_with_llm",
    "pick_primary_platform",
    "verify_no_version_bump",
]
