"""崩溃栈 → 源码路径预解析。

底层逻辑：AI 分析时最高频的失败模式是路径幻觉——agent 凭脑补吐出 `e.m` /
`item.m` / 半路径 `upload_service.dart`，下游 Gate#1 拦截后整个 issue 进入
no_action 状态。治本：在 analyzer 进 prompt 前，**我们自己**用 Glob 把栈帧
里的文件名跑一遍仓库实存，命中后塞回 prompt 作为"已定位候选"，强制 agent
从候选中选——把"agent 自由发挥"收敛成"agent 在白名单里选"。

抓手：
1. 从 representative_stack 抓出 source-file token（.dart/.kt/.swift/.m/.mm/.cpp 等）
2. 用 pathlib.Path.rglob 在 workspace/code/ 下搜真实路径
3. 去噪（.git / build / .dart_tool / DerivedData / Pods / node_modules）
4. 返回每个 token 的 top-3 候选 + 命中数；agent 看到的不再是"我以为存在的文件"，
   而是"确实在子仓里的真实路径"

颗粒度对齐：只做"路径预解析"这一件事，不做语义分析；语义留给 agent。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("crashguard.stack_path_resolver")


# Flutter / Dart：`package:plaud_flutter_common/app/data/foo.dart:123:7`
_RE_DART_PACKAGE = re.compile(
    r"package:([\w_]+)/([\w/_\-.]+\.dart)(?::(\d+))?",
)
# 通用 source-file 引用：`Foo.kt`、`UploadService.dart`、`LoginVC.swift`、`bar.m`
# 长度 ≥3 防 `a.m` / `b.h` 噪声（但仍允许 `e.m`、`item.m` 这种短文件名进入候选阶段，
# 由 Glob 实际命中过滤——找不到就丢弃）
_RE_SOURCE_FILE = re.compile(
    r"\b([A-Za-z_][\w\-]{0,80})\.(dart|kt|java|swift|m|mm|h|hpp|cpp|cc)\b",
)

# Glob 时跳过的噪声目录段（出现在 Path.parts 即过滤）
_NOISE_SEGMENTS = frozenset({
    ".git", ".github", ".gradle", ".idea", ".vscode",
    "build", "node_modules", ".dart_tool",
    "DerivedData", "Pods", "Carthage",
    "__pycache__", ".pytest_cache",
})


def _is_noisy(p: Path) -> bool:
    return any(seg in _NOISE_SEGMENTS for seg in p.parts)


def _glob_basename(code_root: Path, basename: str, max_hits: int = 8) -> List[str]:
    """Path.rglob basename 命中过滤噪声，返回相对 code_root 的路径列表。

    Note: rglob 在大仓库可能跑几秒，调用方需自己控总次数。
    """
    if not basename or "/" in basename:
        return []
    hits: List[str] = []
    try:
        for p in code_root.rglob(basename):
            if not p.is_file() or _is_noisy(p):
                continue
            try:
                rel = p.relative_to(code_root).as_posix()
            except ValueError:
                continue
            hits.append(rel)
            if len(hits) >= max_hits:
                break
    except Exception as exc:
        logger.debug("rglob %s in %s failed: %s", basename, code_root, exc)
    return hits


def _extract_tokens(stack: str, platform: str, max_tokens: int = 8) -> List[Dict[str, Any]]:
    """从堆栈抓 source-file token，去重保序。

    token 顺序优先级：
      1. Dart package 路径（最准——含完整子路径）
      2. 通用 source-file basename（按出现顺序）

    返回 [{"token": "lib/.../foo.dart" 或 "Foo.kt", "kind": "dart_full" | "basename",
           "package": "plaud_flutter_common" (仅 dart_full), "line": int|None}]
    """
    if not stack:
        return []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    # 已被 dart_full 覆盖的 basename，避免重复
    basenames_covered: set[str] = set()

    # P0: Dart package 路径（带完整相对路径）
    for m in _RE_DART_PACKAGE.finditer(stack):
        pkg = m.group(1) or ""
        rel = m.group(2) or ""
        line = m.group(3)
        key = f"{pkg}::{rel}"
        if key in seen or not rel:
            continue
        seen.add(key)
        # 把 dart_full 的 basename 标记为已覆盖（rel 形如 "app/data/foo.dart"，basename = foo.dart）
        from os.path import basename as _basename
        basenames_covered.add(_basename(rel))
        out.append({
            "token": rel,                          # 例: app/data/foo.dart
            "kind": "dart_full",
            "package": pkg,
            "line": int(line) if line else None,
        })
        if len(out) >= max_tokens:
            return out

    # P1: 通用 basename（跳过已被 dart_full 覆盖的同名文件）
    for m in _RE_SOURCE_FILE.finditer(stack):
        base = m.group(0)
        if base in seen or base in basenames_covered:
            continue
        seen.add(base)
        out.append({
            "token": base,
            "kind": "basename",
            "package": None,
            "line": None,
        })
        if len(out) >= max_tokens:
            break
    return out


def resolve_stack_paths(
    stack: str,
    platform: str,
    workspace: Path,
    max_tokens: int = 8,
    max_glob_calls: int = 12,
) -> List[Dict[str, Any]]:
    """对 representative_stack 跑路径预解析。

    返回 [{"token": ..., "kind": ..., "candidates": ["lib/foo/bar.dart", ...],
           "hits": int, "line": Optional[int]}]
    candidates 为空说明该 token 在仓库中无实存——agent 应避开。

    workspace: 与 analyzer 同 workspace，含 `code/` 软链到各平台 sub-repo。
    """
    code_root = workspace / "code"
    if not code_root.exists() or not stack:
        return []

    tokens = _extract_tokens(stack, platform, max_tokens=max_tokens)
    if not tokens:
        return []

    glob_calls = 0
    results: List[Dict[str, Any]] = []
    for tok in tokens:
        if glob_calls >= max_glob_calls:
            break
        token_str = tok["token"]
        # dart_full token 自带相对路径——先试 exact match，再 fallback basename
        if tok["kind"] == "dart_full":
            candidates: List[str] = []
            # 先精确匹配：code/<repo>/lib/<rel>
            for repo_dir in code_root.iterdir():
                if not repo_dir.is_dir() or repo_dir.name.startswith("."):
                    continue
                exact = repo_dir / "lib" / token_str
                if exact.exists() and exact.is_file() and not _is_noisy(exact):
                    try:
                        candidates.append(exact.relative_to(code_root).as_posix())
                    except ValueError:
                        pass
            if not candidates:
                # fallback：用 basename rglob
                base = Path(token_str).name
                candidates = _glob_basename(code_root, base, max_hits=5)
                glob_calls += 1
        else:
            candidates = _glob_basename(code_root, token_str, max_hits=5)
            glob_calls += 1

        results.append({
            "token": token_str,
            "kind": tok["kind"],
            "package": tok.get("package"),
            "line": tok.get("line"),
            "candidates": candidates[:3],   # top-3 已经够 agent 选
            "hits": len(candidates),
        })
    return results


def format_stack_paths_block(resolved: List[Dict[str, Any]]) -> str:
    """把解析结果格式化成 prompt 注入块（markdown）。

    空结果返回 ""——调用方按需在模板里 setdefault 空串。
    """
    if not resolved:
        return ""
    lines: List[str] = [
        "## 已为你预解析的栈帧候选路径（**强制从这里选**）",
        "",
        "下游 Gate#1 会校验你输出的路径必须存在于 sub-repo。**为减少你的 Glob 成本**，",
        "我们已经把堆栈里的 source-file token 在仓库里跑过一遍，结果如下：",
        "",
    ]
    has_any = False
    for r in resolved:
        token = r["token"]
        kind = r["kind"]
        cands = r.get("candidates") or []
        line = r.get("line")
        line_hint = f" (line {line})" if line else ""
        if cands:
            has_any = True
            lines.append(f"- `{token}`{line_hint} → 候选：")
            for c in cands:
                lines.append(f"  - `code/{c}`")
        else:
            lines.append(
                f"- `{token}`{line_hint} → **未在仓库中找到任何实存文件**——"
                f"agent 不要在 fix_diff 里用此名"
            )
    if not has_any:
        lines.append("")
        lines.append(
            "⚠️ 所有 token 都未命中——堆栈帧可能引用了第三方 package 或符号化损失，"
            "请把 fix_suggestion 写在模块层（不给具体路径），并 `code_pointer: \"\"`。"
        )
    lines.append("")
    lines.append(
        "**规则**：fix_diff / code_pointer / fix_suggestion 引用的源码路径，"
        "**必须**来自上方候选列表，或你自己用 Read/Glob 验证过的其它真实路径。"
        "不在候选 + 没验证 = 禁止输出。"
    )
    lines.append("")
    return "\n".join(lines)
