"""符号包目录与可用性探测（2026-09-22）。

给 ad-hoc 符号化工作台提供两个能力：

1. `list_symbol_versions(platform)` —— 版本候选列表（前端下拉用）
2. `preflight_symbols(platform, app_version)` —— 三档符号可用性预检

**两者都只做本地 stat + 已缓存索引 + 最多一次 GitHub Release API 调用，
绝不下载任何符号字节。** 这是它们能秒级返回的前提，也是 preflight 存在的意义：
让用户在点「开始符号化」之前就知道会不会白等几分钟。

## 两个塑造本模块设计的硬约束

**约束 A：候选必须跨两个仓合并。** config.yaml 的 repo_routing 以 4.0.0 为界
（< 4.0.0 → Plaud-AI/Plaud-App 的 flutter band，>= 4.0.0 → Plaud-AI/plaud-native-app
的 native band）。"查哪个仓"取决于版本号，而版本号正是我们要列的东西 ——
所以只能两个仓都列，合并后标 family。

**约束 B：Release tag 里的 +NNN 不是真实 build 号。** 真实 build 号要从 dSYM 的
Info.plist 或 APK 的 assets/datadog.buildId 读。github_symbols 的
_resolve_release_build() / _read_dsym_build_via_range()（HTTP Range 读远端 zip）
干的就是这件事，结果缓存在 _build_index。本模块**只读 _build_index 缓存，
绝不现场发起 Range 请求**（30 个 release × 2 仓 = 60 次请求，太慢）。
未解析的 release 标 verified=False，前端显示「⚠ tag 未校验」——
不能静默假装 tag 是真版本号，否则用户选了个假 build 号会静默符号化失败。
"""
from __future__ import annotations

import logging
import re
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
    if not n:
        return False
    return any(h.lower() in n.lower() for h in _SYMBOL_ASSET_HINTS)


# Release tag → (semver, tag 后缀)。`v4.0.100+999-2026_08_11-203603-global`
# → ("4.0.100", "999")。与 github_symbols._version_to_tag_prefix 的构造规则互逆。
_TAG_SEMVER_BUILD_RE = re.compile(r"^v?(\d+(?:\.\d+){0,3})\+(\d+)")


def _parse_tag(tag: str) -> tuple:
    m = _TAG_SEMVER_BUILD_RE.match(tag or "")
    if not m:
        return "", ""
    return m.group(1), m.group(2)


def _release_app_version(
    tag: str, family: str, build_index: dict, assets: list,
) -> tuple:
    """Release tag → (app_version, verified)。

    app_version 必须是 Datadog @application.version 的 `semver-build` 形式
    （如 `4.0.100-1004`）——那才是 symbolicate / repo_router.resolve 认的输入。
    **绝不返回裸 build 号**（2026-09-22 在 102 实测踩过：候选显示成 "1171"，
    丢了语义版本，用户看不出是哪个版本，且排序把纯数字当主版本全乱）。

    两个仓规则不同（见 github_symbols._version_to_tag_prefix 上方那段注释）：

    - **flutter 仓**（Plaud-AI/Plaud-App）：tag 后缀 `+NNN` **就是**真实 build 号
    - **native 仓**（Plaud-AI/plaud-native-app）：`+NNN` 是假的（几十个 build 才
      冻结换一次，如 +813/+910/+999），真实 build 号要么在 _build_index 里
      （之前解析过并缓存的），要么能从 .aab 资产名抠出来（零额外请求）

    两处都拿不到 → 返回 (tag, False)，让前端显示「⚠ tag 未校验」。
    宁可标未校验，也绝不拿假 build 号冒充真版本号——用户选了个假的会静默
    符号化失败，正是本功能要消除的坑。
    """
    from app.crashguard.services.github_symbols import _aab_build_from_assets

    semver, tag_build = _parse_tag(tag)
    if not semver:
        return tag, False

    if (family or "").lower() == "flutter":
        real_build = tag_build
    else:
        real_build = (
            (build_index or {}).get(tag)
            or _aab_build_from_assets(assets or [])
            or ""
        )

    if real_build:
        return f"{semver}-{real_build}", True
    return tag, False


def invalidate_version_cache() -> None:
    """清进程内 TTL 缓存。测试用，也可供发版后手动刷新。"""
    _cache.clear()


def _version_sort_key(v: str) -> tuple:
    """`4.0.201-941` → ((4,0,201), 941)。

    非数字段落降级为 0，保证对 tag 形态（v4.0.400+1000）和畸形值都不抛。
    """
    head, _, build = (v or "").partition("-")
    parts = []
    for seg in head.lstrip("vV").split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    bd = "".join(ch for ch in build if ch.isdigit())
    return (tuple(parts[:3]), int(bd) if bd else 0)


def _repos_for_platform(platform: str) -> list:
    """该平台所有 band 的 github_repo（去重保序）。见约束 A。"""
    from app.config import get_repo_routing

    bands = (get_repo_routing() or {}).get(platform, {}).get("bands", []) or []
    out, seen = [], set()
    for b in bands:
        repo = (b or {}).get("github_repo") or ""
        if repo and repo not in seen:
            seen.add(repo)
            out.append({"repo": repo, "family": (b or {}).get("family") or ""})
    return out


# ── 三个取数源（模块级函数，测试靠 monkeypatch 它们）────────────────────────

def _list_cached_versions(platform: str) -> list:
    """github_cache/ 下已下载过符号文件的版本目录名。本地 stat，毫秒级。

    只含 .release_tag 的空目录不算 —— 那是 tag 缓存，不是符号包。
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
            real = [
                p for p in d.rglob("*")
                if p.is_file() and p.name != ".release_tag"
            ]
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
    return [{"app_version": v, "symbol_types": sorted(t)} for v, t in agg.items()]


async def _list_release_versions(platform: str) -> tuple:
    """两仓的 GitHub Release 列表 → 候选。返回 (candidates, warnings)。见约束 B。"""
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
        # _build_index 的结构是 {tag: build}（2026-09-22 在 102 实测确认，
        # 形如 {"v4.0.100+999-2026_08_11-203603-global": "1004"}）。
        # 不要反转——之前那版做了 {v:k} 反转，导致 get(tag) 必然 miss。
        index = _load_build_index(repo) or {}
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
                assets = rel.get("assets") or []
                app_version, verified = _release_app_version(
                    tag, family, index, assets,
                )
                # asset_size：符号资产里最大的那个（preflight 用它算下载耗时提示）。
                # 从 Release API 的 assets[].size 直接读，无需下载。
                sym_sizes = [
                    int(a.get("size") or 0) for a in assets
                    if _looks_like_symbol_asset(a.get("name") or "")
                ]
                cands.append({
                    "app_version": app_version,
                    "verified": verified,
                    "tag": tag,
                    "family": family,
                    "asset_size": max(sym_sizes) if sym_sizes else 0,
                    "symbol_types": [
                        a.get("name") for a in assets if a.get("name")
                    ],
                })
    return cands, warnings


# ── 版本候选列表 ────────────────────────────────────────────────────────────

async def list_symbol_versions(platform: str) -> dict:
    """合并三源的版本候选，按版本号倒序。5 分钟进程内缓存。

    任一源失败都降级而非报错 —— 下拉列表不该因为 GitHub 抖动就整个不可用。
    """
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
            base = {
                "app_version": ver, "source": source, "family": "",
                "verified": True, "symbol_types": [], "tag": "",
                "asset_size": 0,
            }
            # extra 放最后展开，确保能覆盖 base 的默认值（含 verified=False）
            base.update({k: v for k, v in extra.items() if v is not None})
            merged[ver] = base
            return
        if _SOURCE_RANK.get(source, 0) > _SOURCE_RANK.get(cur["source"], 0):
            cur["source"] = source
        for k, v in extra.items():
            if v is None:
                continue
            # verified=False 必须能覆盖默认 True（不能被 falsy 过滤掉）
            if k == "verified":
                if v is False:
                    cur[k] = False
                continue
            if v not in ("", [], 0) and not cur.get(k):
                cur[k] = v

    for ver in _list_cached_versions(platform) or []:
        _put(ver, "cached")

    try:
        for row in (await _list_uploaded_versions(platform)) or []:
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
                 family=row.get("family"), asset_size=row.get("asset_size"),
                 symbol_types=row.get("symbol_types") or [])
    except Exception as exc:
        logger.warning("release versions failed: %s", exc)
        warnings.append(f"GitHub Release 列表拉取失败，仅显示本地已有版本：{exc}")

    versions = sorted(
        merged.values(),
        key=lambda c: _version_sort_key(c["app_version"]),
        reverse=True,
    )
    payload = {"versions": versions, "warnings": warnings}
    _cache[platform] = (now, payload)
    return payload


# ── 三档符号可用性预检 ──────────────────────────────────────────────────────

def _human_mb(size_bytes: Optional[int]) -> str:
    """字节数 → 「约 N」MB 文案。拿不到体积时给经验值（dSYM 典型 ~90MB）。"""
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

    | status | 含义 | 前端表现 |
    |---|---|---|
    | cached | 本地已就绪（github_cache 或已上传包） | 🟢 秒级返回 |
    | available | Release 有符号 asset，需下载 | 🟡 约 N MB / 1–3 分钟 |
    | missing | 三处都没有 | 🔴 符号化不会有任何效果 + 出路建议 |

    missing 时 suggestions 给三条可执行出路：
      - nearby_versions：最近有符号包的版本（QA 记错 build 号是高频事件）
      - reason：为什么没有（有 Release 无符号 asset → 大概率不是线上包）
      - upload_hint：指向手动上传入口

    任一数据源失败都降级为 warning，不让预检本身失败。
    """
    warnings: list = []
    sources: list = []

    # 1) 本地 github_cache
    try:
        if app_version in (_list_cached_versions(platform) or []):
            sources.append({
                "source": "cached", "detail": "github_cache 已有符号文件",
            })
    except Exception as exc:
        logger.warning("preflight: cached check failed: %s", exc)
        warnings.append(f"本地缓存检查失败：{exc}")

    # 2) 已上传符号包（Plan B）
    uploaded_versions: list = []
    try:
        uploaded = (await _list_uploaded_versions(platform)) or []
        uploaded_versions = [u["app_version"] for u in uploaded]
        for u in uploaded:
            if u["app_version"] == app_version:
                sources.append({
                    "source": "uploaded",
                    "detail": f"已上传符号包：{', '.join(u.get('symbol_types') or [])}",
                })
    except Exception as exc:
        logger.warning("preflight: uploaded check failed: %s", exc)
        warnings.append(f"已上传符号包检查失败：{exc}")

    if sources:
        return {
            "status": "cached",
            "eta_hint": "符号包已就绪，预计秒级返回",
            "symbol_sources": sources,
            "suggestions": {},
            "warnings": warnings,
        }

    # 3) GitHub Release（Plan C）
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
        logger.warning("preflight: release check failed: %s", exc)
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

    # ── missing：给可执行的出路，而不是只说"没有" ──
    release_with_symbols = [
        r["app_version"] for r in (release_rows or [])
        if any(_looks_like_symbol_asset(n) for n in (r.get("symbol_types") or []))
    ]
    all_available = list(dict.fromkeys(
        list(_safe_cached(platform)) + uploaded_versions + release_with_symbols
    ))
    target = _version_sort_key(app_version)
    nearby = sorted(
        all_available,
        key=lambda v: (
            0 if _version_sort_key(v)[0] == target[0] else 1,   # 同 minor 优先
            abs(_version_sort_key(v)[1] - target[1]),           # build 号最接近
        ),
    )[:5]

    if release_hit:
        reason = (
            f"{release_hit.get('tag')} 这个 Release 存在，但没有符号资产。"
            "Jenkins 仅在 IS_ONLINE_PACKAGE=true 时上传符号包，该包可能不是线上包。"
        )
    else:
        reason = f"两个仓的 Release 列表里都没有 {app_version} 这个版本。"

    return {
        "status": "missing",
        "eta_hint": "该版本无符号表，符号化不会有任何效果",
        "symbol_sources": [],
        "suggestions": {
            "nearby_versions": nearby,
            "reason": reason,
            "upload_hint": "可在本页底部手动上传该版本的符号包（dSYM / mapping.txt）",
        },
        "warnings": warnings,
    }


def _safe_cached(platform: str) -> list:
    """_list_cached_versions 的不抛版本（missing 分支里再取一次用于邻近版本建议）。"""
    try:
        return _list_cached_versions(platform) or []
    except Exception:
        return []
