"""
Analysis pipeline worker.

Orchestrates the full flow:
  Feishu fetch → Download → Decrypt → Rule match → Extract
  → L1.5 context condense → Agent analyze → Result
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.config import get_settings, get_repo_routing
from app.services import repo_router
from app.db import database as db
from app.models.schemas import AnalysisResult, Issue
from app.services.agent_orchestrator import AgentOrchestrator
from app.services.decrypt import process_log_file_for_platform
from app.services.extractor import extract_for_rules, extract_log_metadata
from app.services.feishu import FeishuClient
from app.services.issue_text import guess_problem_date, normalize_description_for_matching
from app.services.rule_engine import RuleEngine

logger = logging.getLogger("jarvis.worker")

# Singletons
_rule_engine: Optional[RuleEngine] = None
_orchestrator: Optional[AgentOrchestrator] = None


def _get_rule_engine() -> RuleEngine:
    global _rule_engine
    if _rule_engine is None:
        _rule_engine = RuleEngine()
    return _rule_engine


def _get_orchestrator() -> AgentOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AgentOrchestrator()
    return _orchestrator


def _os_name_from_issue(issue: object) -> str:
    """Extract OS name string from an issue's log_metadata_json (JSON string).

    Checks keys in order: 'os_version', 'os'. Returns '' on any error or if
    the attribute is missing. normalize_platform() matches 'Android 14', 'iOS 17'
    etc. case-insensitively via substring match, so the raw value is fine to pass.
    """
    try:
        raw = getattr(issue, "log_metadata_json", None)
        if not raw:
            return ""
        meta = json.loads(raw)
        return (meta.get("os_version") or meta.get("os") or "").strip()
    except Exception:
        return ""


# ====================== ① 日志时效性预检 ======================

def _parse_problem_ref_time(problem_date: Optional[str], issue: Issue) -> Optional[datetime]:
    """问题参考时间：优先 problem_date，回退 issue.occurred_at / created_at。"""
    if problem_date:
        s = problem_date.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:len(fmt) + 2].strip(), fmt)
            except ValueError:
                continue
    for attr in ("occurred_at", "created_at"):
        val = getattr(issue, attr, None)
        if isinstance(val, datetime):
            return val
    return None


def _check_log_coverage(
    log_paths: List[Path],
    problem_date: Optional[str],
    issue: Issue,
    max_gap_days: int,
) -> Optional[Dict[str, Any]]:
    """判定上传日志是否覆盖问题时段。

    返回 None 表示"覆盖正常 / 无法判定（缺时间戳或缺参考时间）→ 不拦截"；
    返回 dict 表示"日志最新事件比问题时间早超过 max_gap_days 天 → 需用户重传"。
    设计为保守：任何不确定都放行，只拦截铁证（如激活日旧日志 vs 4 个月后的问题）。
    """
    from app.services.log_windower import get_log_time_range

    ref = _parse_problem_ref_time(problem_date, issue)
    if ref is None:
        return None

    last_event: Optional[datetime] = None
    first_event: Optional[datetime] = None
    for lp in log_paths:
        try:
            f, l = get_log_time_range(Path(lp))
        except Exception:
            continue
        if l and (last_event is None or l > last_event):
            last_event = l
        if f and (first_event is None or f < first_event):
            first_event = f

    if last_event is None:
        return None  # 日志里没有可解析时间戳 → 不敢妄断，放行

    gap_days = (ref - last_event).days
    if gap_days > max_gap_days:
        return {
            "ref": ref,
            "last_event": last_event,
            "first_event": first_event,
            "gap_days": gap_days,
        }
    return None


def _build_stale_log_result(
    issue: Issue,
    task_id: str,
    coverage: Dict[str, Any],
    log_metadata: Dict[str, Any],
) -> AnalysisResult:
    """日志未覆盖问题时段 → 直接产出"需用户重传"结果，不跑 agent。"""
    last = coverage["last_event"].strftime("%Y-%m-%d")
    ref = coverage["ref"].strftime("%Y-%m-%d")
    gap = coverage["gap_days"]

    zh_reply = (
        f"您好，我们排查发现：本次上传的日志最新记录截止到 {last}，而问题大约发生在 {ref}"
        f"（相差约 {gap} 天）。日志没有覆盖到问题发生的时间段，因此暂时无法定位具体原因。\n\n"
        "烦请在问题**复现后**，重新导出并上传**最新的设备日志**，我们会第一时间为您分析。"
    )
    en_reply = (
        f"We found the uploaded log only covers up to {last}, while the issue occurred around {ref} "
        f"(~{gap} days apart). The log does not cover the time window of the problem, so we cannot "
        "pinpoint the cause yet.\n\nPlease reproduce the issue, then export and upload the latest "
        "device log so we can analyze it right away."
    )
    rc = (
        f"日志时段不匹配：上传日志最新事件为 {last}，而问题发生约在 {ref}，相差 {gap} 天，"
        "日志未覆盖问题时间段，无法据此定位根因，需用户重传问题时段的日志。"
    )
    rc_en = (
        f"Log time range mismatch: latest log event is {last}, but the issue occurred around {ref} "
        f"({gap} days apart). The log does not cover the problem window, so the root cause cannot be "
        "determined from it — the user needs to re-upload logs from the problem period."
    )
    result = AnalysisResult(
        task_id=task_id,
        issue_id=issue.id,
        problem_type="日志时段不匹配",
        problem_type_en="Log Time Range Mismatch",
        root_cause=rc,
        root_cause_en=rc_en,
        confidence="low",
        confidence_reason=f"日志最新事件 {last} 早于问题时间 {ref} 约 {gap} 天（阈值 precheck）",
        key_evidence=[
            f"log latest event: {last}",
            f"problem time: {ref}",
            f"gap: {gap} days (> threshold)",
        ],
        user_reply=zh_reply,
        user_reply_en=en_reply,
        needs_engineer=False,
        system_failure=False,
        needs_user_retry=True,  # ① 路由到"需用户重传"，不进 done/inaccurate 主流
        fix_suggestion="",
        agent_type="precheck",
    )
    result.issue = issue
    result.log_metadata = log_metadata
    return result


# ====================== ④ 追问污染护栏 ======================

_FOLLOWUP_NARRATIVE_MARKERS = (
    "针对追问", "针对用户的追问", "针对用户追问", "针对您的追问",
    "客服处理方案", "客服三步", "以下是针对", "的核心结论",
)


def _looks_like_followup_narrative(text: str) -> bool:
    """root_cause 是否被追问的"处理方案/客服话术"叙述污染了。"""
    head = (text or "").lstrip()
    if head.startswith("---"):  # markdown 分隔符开头：典型 salvage 整段灌入
        return True
    head = head[:300]
    return any(m in head for m in _FOLLOWUP_NARRATIVE_MARKERS)


def _sanitize_followup_result(
    result: AnalysisResult,
    previous_analysis: Optional[Dict[str, Any]],
    issue_id: str,
) -> AnalysisResult:
    """追问结果护栏：若 root_cause 被追问叙述污染、且上次有可用技术根因，则恢复之。

    历史 bug：fb_48030779f7 的 root_cause 被写成"以下是针对追问…客服处理方案总结"，
    技术根因丢失。这里把追问叙述挪到 user_reply（若 user_reply 为空），root_cause 恢复
    为上次分析的技术根因。
    """
    if not previous_analysis:
        return result
    prev_rc = (previous_analysis.get("root_cause") or "").strip()
    if not _looks_like_followup_narrative(result.root_cause) or len(prev_rc) <= 40:
        return result

    polluted = result.root_cause
    # 追问的针对性回答属于 user_reply；若 user_reply 还空着，把叙述挪过去不丢信息
    if not (result.user_reply or "").strip():
        result.user_reply = polluted
    result.root_cause = prev_rc
    if previous_analysis.get("problem_type"):
        result.problem_type = previous_analysis["problem_type"]
    logger.warning(
        "④ followup pollution guard: root_cause looked like a followup narrative — "
        "restored technical root_cause from previous analysis for %s",
        issue_id,
    )
    return result


# 追问重裁日志：追问代表上次回复不满意（日志很可能裁错/不全），故不复用上次裁好的日志，
# 改为按追问深度递进放宽时间窗重裁，到阈值直接给全量原始日志（跳过 windowing）。
FOLLOWUP_WIDEN_FACTOR = 2          # 每加深一层追问，时间窗 ×N
FOLLOWUP_FULL_LOGS_AT_DEPTH = 3    # 追问深度 ≥ 此值 → 全量原始日志（不裁）


def _followup_window_params(depth: int) -> "tuple[float, bool]":
    """追问深度 → (窗口放大系数, 是否直接给全量原始日志)。depth=0 表示非追问。"""
    scale = float(FOLLOWUP_WIDEN_FACTOR ** max(0, depth))
    force_full = depth >= FOLLOWUP_FULL_LOGS_AT_DEPTH
    return scale, force_full


async def run_analysis_pipeline(
    issue_id: str,
    task_id: str,
    agent_override: Optional[str] = None,
    on_progress: Optional[Callable[[int, str], Any]] = None,
    followup_question: str = "",
    pipeline_timeout: Optional[int] = None,
    deep_analysis: bool = False,
) -> AnalysisResult:
    """
    Run the complete analysis pipeline for a single issue.

    Steps (full analysis):
    1. Fetch issue from Feishu
    2. Download log files
    3. Decrypt / process logs
    4. Match rules
    5. Pre-extract (grep)
    6. Prepare workspace
    7. Run agent
    8. Parse result

    For follow-up questions: reuse the previous task's workspace (logs, rules,
    code, extraction) and skip steps 2-6.  Falls back to full pipeline if the
    previous workspace is unavailable.
    """
    settings = get_settings()
    is_local = issue_id.startswith("fb_")    # locally submitted via feedback form
    is_linear = issue_id.startswith("lin_")  # Linear-sourced issue

    # --- Step 1: Fetch issue ---
    if on_progress:
        await on_progress(5, "获取工单信息...")

    if is_local or is_linear:
        # Local / Linear issue — read from DB (already saved by webhook handler)
        from app.db.database import get_session, IssueRecord
        import json as _json
        async with get_session() as session:
            rec = await session.get(IssueRecord, issue_id)
        if not rec:
            raise RuntimeError(f"Issue {issue_id} not found in local DB")
        log_files_raw = _json.loads(rec.log_files_json) if rec.log_files_json else []
        issue = Issue(
            record_id=rec.id,
            description=rec.description or "",
            device_sn=rec.device_sn or "",
            firmware=rec.firmware or "",
            app_version=rec.app_version or "",
            priority=rec.priority or "",
            zendesk=rec.zendesk or "",
            zendesk_id=rec.zendesk_id or "",
            platform=rec.platform or "",
            source=rec.source or ("linear" if is_linear else "local"),
            feishu_link="",
            linear_issue_id=rec.linear_issue_id or "",
            linear_issue_url=rec.linear_issue_url or "",
            occurred_at=rec.occurred_at,
            log_files=[],
        )
        logger.info("Processing %s issue %s: %s", issue.source, issue_id, issue.description[:80])
    else:
        # Feishu issue — fetch from API
        client = FeishuClient()
        issue = await client.get_issue(issue_id)
        log_files_raw = [lf.model_dump() for lf in issue.log_files]
        logger.info("Processing issue %s: %s", issue_id, issue.description[:80])
        await db.upsert_issue(issue.model_dump(), status="analyzing")

    # ── Follow-up：不复用上次裁好的日志，重新裁剪 ──
    # 追问 = 上次回复不满意，日志很可能不对/不全。故走完整管线重裁（raw 仍走缓存不重下载），
    # 并按追问深度递进放宽时间窗，到阈值直接给全量原始日志。锚点 / prompt 都纳入历史追问。
    followup_depth = 0
    followup_window_scale = 1.0
    followup_force_full = False
    followup_anchor_text = ""
    followup_question_for_agent = followup_question
    if followup_question:
        followup_depth, prior_questions = await db.get_prior_followup_history(
            issue_id, exclude_task_id=task_id
        )
        followup_window_scale, followup_force_full = _followup_window_params(followup_depth)
        # 重裁锚点文本：原始描述 + 历史追问 + 本次（新输入里若有时间/日期线索 → 重定位窗口中心）
        followup_anchor_text = " ".join(
            [issue.description or "", *prior_questions, followup_question]
        ).strip()
        # prompt 带上历史追问，让 agent 知道前几次问过什么、为何不满意
        if prior_questions:
            history_block = "\n".join(f"- {q}" for q in prior_questions)
            followup_question_for_agent = (
                f"{followup_question}\n\n"
                f"[历史追问，按时间顺序]\n{history_block}\n"
                "（用户连续追问说明前几次回答未让其满意，已放宽日志范围重裁，"
                "请结合更大范围的日志重新审视，不要重复之前的结论。）"
            )
        logger.info(
            "Follow-up re-window for %s: depth=%d window_scale=%.1f force_full=%s",
            issue_id, followup_depth, followup_window_scale, followup_force_full,
        )

    # --- Step 2: Download / locate logs ---
    if on_progress:
        await on_progress(10, "准备日志文件...")

    workspace = Path(settings.storage.workspace_dir) / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    raw_dir = workspace / "raw"
    raw_dir.mkdir(exist_ok=True)
    images_dir = workspace / "images"
    images_dir.mkdir(exist_ok=True)

    # Log cache: reuse previously downloaded logs for the same issue
    cache_dir = Path(settings.storage.workspace_dir) / "_cache" / issue_id / "raw"
    downloaded_files: List[Path] = []

    if is_local:
        local_raw = Path(settings.storage.workspace_dir) / issue_id / "raw"
        if local_raw.exists():
            for f in local_raw.iterdir():
                if f.is_file():
                    dest = raw_dir / f.name
                    if not dest.exists():
                        import shutil
                        shutil.copy2(f, dest)
                    downloaded_files.append(dest)

        local_images = Path(settings.storage.workspace_dir) / issue_id / "images"
        if local_images.exists():
            import shutil
            for img in local_images.iterdir():
                if img.is_file():
                    dest = images_dir / img.name
                    if not dest.exists():
                        shutil.copy2(img, dest)
    elif cache_dir.exists() and any(cache_dir.iterdir()):
        # Reuse cached logs
        import shutil
        for f in cache_dir.iterdir():
            if f.is_file():
                dest = raw_dir / f.name
                if not dest.exists():
                    shutil.copy2(f, dest)
                downloaded_files.append(dest)
        logger.info("Reusing cached logs for issue %s (%d files)", issue_id, len(downloaded_files))
    else:
        # Download from Feishu / Linear
        if not is_linear:
            client = FeishuClient()
            for lf_dict in log_files_raw:
                name = lf_dict.get("name", "")
                token = lf_dict.get("token", "")
                if not token:
                    continue
                save_path = raw_dir / name
                if not save_path.exists():
                    try:
                        await client.download_file(token, str(save_path))
                        downloaded_files.append(save_path)
                    except Exception as e:
                        logger.error("Failed to download %s: %s", name, e)
                else:
                    downloaded_files.append(save_path)

        # Save to cache for future re-analysis
        if downloaded_files:
            cache_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            for f in downloaded_files:
                cache_dest = cache_dir / f.name
                if not cache_dest.exists():
                    shutil.copy2(f, cache_dest)
            logger.info("Cached %d log files for issue %s", len(downloaded_files), issue_id)

    # Cleanup: raw cache keeps many entries (files are small, compressed)
    _cleanup_log_cache(Path(settings.storage.workspace_dir) / "_cache", max_issues=500)
    # Cleanup: processed/logs dirs are large (decompressed); keep only recent 50
    _cleanup_workspace_processed(Path(settings.storage.workspace_dir), max_tasks=50)

    if on_progress:
        await on_progress(25, f"已准备 {len(downloaded_files)} 个文件")

    # --- Step 3: Decrypt / process ---
    if on_progress:
        await on_progress(30, "解密日志...")

    log_paths: list[Path] = []
    log_parse_issues: list[str] = []

    platform = (getattr(issue, "platform", "") or "").strip().lower()
    logger.info("Platform: %s (issue %s)", platform or "app (default)", issue_id)

    processed_dir = workspace / "processed"

    # 🎁 解密结果按 issue 级缓存：重试/追问会建新 task workspace 并重新解密同一份 23MB→146MB
    # 原始日志（纯阻塞重活）。这里用 manifest 精确 replay 上次解密产出的 log_paths（不靠盲 glob，
    # 避免 process_log_file 的选文件/合并逻辑被错位还原），命中即整目录拷回，跳过解密。
    decrypt_cache_root = Path(settings.storage.workspace_dir) / "_cache" / issue_id
    decrypt_cache_processed = decrypt_cache_root / "processed"
    decrypt_manifest = decrypt_cache_root / "decrypt_manifest.json"
    reused_decrypt = False

    if decrypt_manifest.exists() and decrypt_cache_processed.exists():
        try:
            manifest = json.loads(decrypt_manifest.read_text(encoding="utf-8"))
            if manifest.get("platform", "") == platform:
                await asyncio.to_thread(
                    shutil.copytree, decrypt_cache_processed, processed_dir, dirs_exist_ok=True
                )
                cached = [processed_dir / rel for rel in manifest.get("log_paths", [])]
                cached = [p for p in cached if p.exists()]
                if cached:
                    log_paths = cached
                    reused_decrypt = True
                    logger.info(
                        "Reused cached decryption for issue %s (%d log files) — skipped re-decrypt (省阻塞重活)",
                        issue_id, len(log_paths),
                    )
        except Exception as e:
            logger.warning("Decrypt cache reuse failed (%s) — falling back to fresh decrypt", e)
            log_paths = []

    if not reused_decrypt:
        # Option A：解密是 subprocess + 大文件 IO 的同步阻塞调用，丢线程池避免冻结事件循环
        for fp in downloaded_files:
            log_path, incorrect, reason = await asyncio.to_thread(
                process_log_file_for_platform, fp, processed_dir, platform,
            )
            if log_path:
                log_paths.append(log_path)
            if incorrect and reason:
                log_parse_issues.append(reason)

        # 解密成功 → 写 issue 级缓存供后续重试复用
        if log_paths and processed_dir.exists():
            try:
                decrypt_cache_root.mkdir(parents=True, exist_ok=True)
                if decrypt_cache_processed.exists():
                    await asyncio.to_thread(shutil.rmtree, decrypt_cache_processed, True)
                await asyncio.to_thread(
                    shutil.copytree, processed_dir, decrypt_cache_processed, dirs_exist_ok=True
                )
                rel_paths = [str(p.relative_to(processed_dir)) for p in log_paths]
                decrypt_manifest.write_text(
                    json.dumps({"platform": platform, "log_paths": rel_paths}, ensure_ascii=False),
                    encoding="utf-8",
                )
                # 解密产物大（~146MB/份），缓存按 issue 数封顶清理，防止撑爆磁盘
                _cleanup_decrypt_cache(Path(settings.storage.workspace_dir) / "_cache", max_issues=15)
            except Exception as e:
                logger.warning("Failed to write decrypt cache for issue %s (non-fatal): %s", issue_id, e)

    has_logs = len(log_paths) > 0
    # 区分两种 "no logs" 场景：
    #   (a) 本来就没上传文件 → has_logs=False, logs_corrupted=False（让 AI 走"凭描述分析"模式）
    #   (b) 上传了文件但全部解密失败 → has_logs=False, logs_corrupted=True（让 AI 显式让用户重传，禁止瞎猜根因）
    logs_corrupted = bool(downloaded_files) and not has_logs

    # Extract log metadata (app version, OS, UID, device model, etc.)
    log_metadata: Dict[str, Any] = {}
    if has_logs:
        log_metadata = await asyncio.to_thread(extract_log_metadata, log_paths)
        logger.info("Extracted log metadata: %s", {k: v for k, v in log_metadata.items() if k != "file_ids"})

    if has_logs:
        if on_progress:
            await on_progress(40, f"解密完成，{len(log_paths)} 个日志文件")
    else:
        if log_parse_issues:
            logger.warning("Log parse issues: %s", log_parse_issues)
        if downloaded_files:
            logger.warning("Had %d files but none produced usable logs (logs_corrupted=True)", len(downloaded_files))
        if on_progress:
            msg = "日志文件损坏（解密失败），将提示用户重传" if logs_corrupted else "无日志文件，将基于描述和代码分析..."
            await on_progress(40, msg)

    # --- Step 4: Match rules ---
    if on_progress:
        await on_progress(45, "匹配分析规则...")

    engine = _get_rule_engine()
    routing_text = normalize_description_for_matching(issue.description)
    rules = engine.match_rules(routing_text)
    rule_type = engine.classify(routing_text)

    logger.info("Matched rules: %s (primary: %s), has_logs: %s", [r.meta.id for r in rules], rule_type, has_logs)

    # 追问时锚点用「描述+历史追问+本次」组合文本——新输入里若带时间/日期则重定位窗口中心
    problem_date = guess_problem_date(
        normalize_description_for_matching(followup_anchor_text) if followup_anchor_text else routing_text,
        issue.occurred_at,
    )

    # --- Step 4.5: ① 日志时效性预检 ---
    # 日志最新事件远早于问题时间（如设备激活日的旧日志 vs 4 个月后的问题）→ 日志没覆盖问题时段，
    # 硬跑 agent 只会拿旧数据瞎猜根因、落进 inaccurate 桶。直接出"需用户重传"结果，省掉最贵的 agent。
    if has_logs:
        try:
            _max_gap = getattr(settings.concurrency, "log_stale_gap_days", 30) or 30
            coverage = await asyncio.to_thread(
                _check_log_coverage, log_paths, problem_date, issue, _max_gap
            )
        except Exception as e:
            logger.warning("Log coverage precheck failed (non-fatal), skipping: %s", e)
            coverage = None
        if coverage:
            logger.warning(
                "① log coverage precheck: STALE logs for %s — latest=%s, problem=%s, gap=%d days "
                "(> %d). Short-circuit to needs_user_retry, skipping agent.",
                issue_id,
                coverage["last_event"].strftime("%Y-%m-%d"),
                coverage["ref"].strftime("%Y-%m-%d"),
                coverage["gap_days"], _max_gap,
            )
            if on_progress:
                await on_progress(100, "日志未覆盖问题时段，需用户重传最新日志")
            return _build_stale_log_result(issue, task_id, coverage, log_metadata)

    # --- Step 5: Pre-extract ---
    extraction = {}
    if has_logs:
        if on_progress:
            await on_progress(50, "预提取关键日志...")
        extraction = await asyncio.to_thread(
            lambda: extract_for_rules(rules, log_paths, problem_date=problem_date)
        )

    # --- Step 5.5: L1.5 Context Condensation ---
    condensation_result = None
    workspace_log_paths = log_paths  # default: use original logs

    if has_logs:
        condensation_result = await _run_context_condensation(
            log_paths=log_paths,
            workspace=workspace,
            issue=issue,
            extraction=extraction,
            rules=rules,
            problem_date=problem_date,
            on_progress=on_progress,
            deep_analysis=deep_analysis or followup_force_full,  # 追问深到阈值 → 全量原始日志
            window_scale=followup_window_scale,                  # 追问递进放宽时间窗
        )
        if condensation_result is not None:
            workspace_log_paths = condensation_result["log_paths"]

    if on_progress:
        await on_progress(60, "准备 Agent 工作空间..." if has_logs else "准备代码分析...")

    # --- Step 6: Prepare workspace ---
    version = (getattr(issue, "app_version", "") or "").strip()
    # os_name: prefer already-parsed log_metadata dict (available when has_logs);
    # fall back to issue.log_metadata_json for no-log / eval paths.
    os_name = (
        (log_metadata.get("os_version") or log_metadata.get("os") or "").strip()
        if log_metadata
        else _os_name_from_issue(issue)
    )
    res = repo_router.resolve(platform, version, get_repo_routing(), os_name=os_name)
    code_repo = repo_router.analysis_path(res)
    if code_repo is None and (platform in ("", "app", "flutter")):
        # Coexistence fallback: ambiguous/empty app ticket → flutter app monorepo (analysis wants broad context).
        _fb = repo_router.resolve("app", version, get_repo_routing(), os_name=os_name)
        code_repo = repo_router.analysis_path(_fb)
        if code_repo is None:
            from app.config import get_settings as _gs
            code_repo = (_gs().code_repo_app or _gs().code_repo_path) or None
    if code_repo:
        logger.info("repo_router(analysis): %s v%s os=%s -> %s (family=%s)",
                    platform or "?", version or "?", os_name or "?", code_repo,
                    res.family if res else "fallback-app")
    else:
        logger.info("repo_router(analysis): no repo for platform=%s version=%s os=%s -> logs-only",
                    platform, version, os_name)
    engine.prepare_workspace(workspace, rules, workspace_log_paths, code_repo=code_repo)

    # --- Step 7: Run agent ---
    # For follow-ups that fell through from fast path, still load previous analysis
    previous_analysis = None
    if followup_question:
        prev = await db.get_analysis_by_issue(issue_id)
        if prev:
            import json as _json2
            previous_analysis = {
                "problem_type": prev.problem_type or "",
                "root_cause": prev.root_cause or "",
                "confidence": prev.confidence or "",
                "key_evidence": _json2.loads(prev.key_evidence_json) if prev.key_evidence_json else [],
                "user_reply": prev.user_reply or "",
                "fix_suggestion": prev.fix_suggestion or "",
            }

    orchestrator = _get_orchestrator()
    result = await orchestrator.run_analysis(
        workspace=workspace,
        issue=issue,
        rules=rules,
        extraction=extraction,
        rule_type=rule_type,
        agent_override=agent_override,
        problem_date=problem_date,
        has_logs=has_logs,
        logs_corrupted=logs_corrupted,
        on_progress=on_progress,
        previous_analysis=previous_analysis,
        followup_question=followup_question_for_agent,  # 含历史追问，供 agent 上下文
        condensation_context=condensation_result.get("structured_context") if condensation_result else None,
        pipeline_timeout=pipeline_timeout,
        deep_analysis=deep_analysis,
    )

    result.task_id = task_id
    result.issue = issue
    result.log_metadata = log_metadata
    result.is_deep_analysis = bool(deep_analysis)
    if followup_question:
        result.followup_question = followup_question
        result = _sanitize_followup_result(result, previous_analysis, issue_id)  # ④

    # 计量：聚合 agent（usage_tokens/agent_cost_usd/cost_source）+ condenser 用量 → 落库字段
    try:
        from app.services.cost import build_usage_record
        rec = build_usage_record(
            agent_usage=result.usage_tokens or {},
            agent_cost_usd=result.agent_cost_usd,
            agent_cost_source=result.cost_source,
            agent_model=result.agent_model,
            condenser_usage=(condensation_result or {}).get("condenser_usage", {}),
            condenser_model=(condensation_result or {}).get("condenser_model", ""),
        )
        result.total_tokens = rec["total_tokens"]
        result.total_cost_usd = rec["total_cost_usd"]
        result.usage_breakdown = rec["usage_breakdown"]
        result.cost_source = rec["cost_source"]
    except Exception as e:
        logger.warning("计量聚合失败（non-fatal）: %s", e)

    if on_progress:
        await on_progress(100, "Analysis complete")

    return result


def purge_issue_cache(workspace_dir: str, issue_id: str) -> None:
    """删除某 issue 的全部 per-issue 缓存（raw 日志 + 解密产物 + 本地 issue 目录）。

    背景（2026-06-19 修）：下载/解密都按 issue_id 缓存在 `_cache/<issue>/`（raw + processed +
    decrypt_manifest.json）和本地 issue 目录 `<workspace>/<issue>/`，重新触发时无条件复用。
    工单因日志非法被删除、用户重新导入新日志后，worker 仍命中旧缓存复用旧日志（复现
    rec27CyKMwcZ5l）。删除是「丢弃这条 issue 数据、用户将重传」的明确信号 → 一并清缓存，
    让下次导入重新下载 + 重新解密新日志。空目录/不存在时静默跳过。
    """
    root = Path(workspace_dir)
    for target in (root / "_cache" / issue_id, root / issue_id):
        try:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                logger.info("Purged per-issue cache: %s", target)
        except Exception as e:
            logger.warning("Failed to purge cache %s: %s", target, e)


def _cleanup_log_cache(cache_root: Path, max_issues: int = 500):
    """Keep only the last N issues' cached raw logs, remove oldest.

    Raw files are small (compressed .plaud/.zip), so we keep many.
    """
    if not cache_root.exists():
        return
    try:
        issue_dirs = sorted(
            [d for d in cache_root.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if len(issue_dirs) > max_issues:
            import shutil
            for old_dir in issue_dirs[max_issues:]:
                shutil.rmtree(old_dir, ignore_errors=True)
            logger.info("Cleaned up log cache: removed %d old entries", len(issue_dirs) - max_issues)
    except Exception as e:
        logger.warning("Log cache cleanup failed: %s", e)


def _cleanup_decrypt_cache(cache_root: Path, max_issues: int = 15):
    """只清解密产物缓存（_cache/<issue>/processed + manifest），保留 raw（raw 小、由
    _cleanup_log_cache 管 500 份）。解密产物大（~146MB/份），只留最近 max_issues 个 issue。
    """
    if not cache_root.exists():
        return
    try:
        entries = []
        for d in cache_root.iterdir():
            proc = d / "processed"
            if d.is_dir() and proc.exists():
                entries.append((proc, d / "decrypt_manifest.json", proc.stat().st_mtime))
        entries.sort(key=lambda t: t[2], reverse=True)
        for proc, manifest, _ in entries[max_issues:]:
            shutil.rmtree(proc, ignore_errors=True)
            try:
                manifest.unlink(missing_ok=True)
            except Exception:
                pass
        if len(entries) > max_issues:
            logger.info("Cleaned up decrypt cache: pruned %d old processed dirs", len(entries) - max_issues)
    except Exception as e:
        logger.warning("Decrypt cache cleanup failed: %s", e)


def _cleanup_workspace_processed(workspace_root: Path, max_tasks: int = 50):
    """Delete processed/ and logs/ dirs from old task workspaces to save disk.

    These contain decompressed logs (tens of MB each) and are the main
    disk consumers. We keep raw/ (small, compressed originals) and
    output/ (analysis results) intact so re-analysis can rebuild from raw.
    """
    if not workspace_root.exists():
        return
    try:
        task_dirs = sorted(
            [d for d in workspace_root.iterdir()
             if d.is_dir() and d.name.startswith("task_")],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if len(task_dirs) <= max_tasks:
            return

        import shutil
        cleaned = 0
        for old_dir in task_dirs[max_tasks:]:
            for subdir_name in ("processed", "logs"):
                subdir = old_dir / subdir_name
                if subdir.exists():
                    shutil.rmtree(subdir, ignore_errors=True)
                    cleaned += 1
        if cleaned:
            logger.info(
                "Cleaned up %d processed/logs dirs from %d old workspaces (kept raw/ and output/)",
                cleaned, len(task_dirs) - max_tasks,
            )
    except Exception as e:
        logger.warning("Workspace processed cleanup failed: %s", e)


async def _run_context_condensation(
    log_paths: List[Path],
    workspace: Path,
    issue: Issue,
    extraction: Dict[str, Any],
    rules: list,
    problem_date: Optional[str],
    on_progress: Optional[Callable[[int, str], Any]],
    deep_analysis: bool = False,
    window_scale: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """Run L1.5 context condensation: time-window + optional LLM extraction.

    window_scale: 时间窗放大系数（追问递进放宽用，默认 1.0 不放大）。

    Returns a dict with:
        - "log_paths": list of (possibly windowed) log paths to use in workspace
        - "structured_context": dict from LLM extraction (or None)
        - "windowing_metadata": list of per-file windowing stats
    Or None if condensation is not applicable.
    """
    import json as _json

    settings = get_settings()
    cc = settings.context_condensation

    # Override with DB-persisted settings (user-configurable via Settings page)
    try:
        import json as _json_cc
        raw_cc = await db.get_oncall_config("condensation_config", "")
        if raw_cc:
            db_cc = _json_cc.loads(raw_cc)
            cc.enabled = db_cc.get("enabled", cc.enabled)
            cc.provider = db_cc.get("provider", cc.provider)
            cc.model = db_cc.get("model", cc.model)
            if db_cc.get("api_key"):
                cc.api_key = db_cc["api_key"]
            cc.log_size_threshold_mb = db_cc.get("log_size_threshold_mb", cc.log_size_threshold_mb)
            cc.time_window_hours_before = db_cc.get("time_window_hours_before", cc.time_window_hours_before)
            cc.time_window_hours_after = db_cc.get("time_window_hours_after", cc.time_window_hours_after)
            cc.timeout = db_cc.get("timeout", cc.timeout)
    except Exception as e:
        logger.warning("Failed to load condensation config from DB: %s", e)

    # 无 ANTHROPIC_API_KEY 时（用户已删除）强制走 OAuth CLI 回退：清掉来自 config.yaml/DB 的
    # 残留 api_key，否则 condenser 仍会拿失效 key 打 vertex/api 拿 401（实测 fb_17b4fa0293），
    # 既慢又使 agent 退化为硬啃原始日志。context_condenser 在 provider=anthropic 且 api_key 为空
    # 时自动用 claude CLI（OAuth，已验证可用）→ 压缩照常工作。
    import os as _os
    if cc.provider == "anthropic" and not _os.environ.get("ANTHROPIC_API_KEY"):
        if cc.api_key:
            logger.info("ANTHROPIC_API_KEY absent — clearing condenser api_key to use OAuth CLI fallback")
        cc.api_key = ""

    # 深度分析：跳过 windowing，把完整原始日志交给 agent 自由探索
    if deep_analysis:
        logger.info("Deep analysis: skipping windowing, using full raw logs")
        return {
            "log_paths": log_paths,
            "structured_context": None,
            "windowing_metadata": [{"deep_mode": True, "windowed": False}],
        }

    # Check if any log file exceeds the size threshold
    threshold_bytes = int(cc.log_size_threshold_mb * 1024 * 1024)
    large_logs = [lp for lp in log_paths if lp.exists() and lp.stat().st_size > threshold_bytes]
    if not large_logs:
        return None  # All logs are small, no condensation needed

    total_size = sum(lp.stat().st_size for lp in log_paths if lp.exists())
    logger.info(
        "L1.5: %d log files (%d large, total %.1fMB), threshold=%.1fMB",
        len(log_paths), len(large_logs), total_size / 1024 / 1024, cc.log_size_threshold_mb,
    )

    if on_progress:
        await on_progress(52, "L1.5: 日志时间窗口切割...")

    # --- Step A: Time-window extraction (always, free) ---
    from app.services.log_windower import (
        window_log_files,
        rewindow_on_signal_lines,
        signal_lines_from_extraction,
        window_coverage_ratio,
    )

    center_time = None
    date_only = False

    # Priority 1: explicit problem_date from issue
    if problem_date:
        try:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    center_time = datetime.strptime(problem_date.strip()[:19], fmt)
                    if fmt == "%Y-%m-%d":
                        date_only = True
                        center_time = center_time.replace(hour=12)
                    break
                except ValueError:
                    continue
        except Exception:
            pass

    # No problem_date → anchor on the most recent logs (center_time stays None →
    # window_log_file uses the log tail). Rationale: users report a problem after it
    # happens, so the latest activity is the meaningful slice. We deliberately do NOT
    # infer a center from L1-extraction timestamps or the error-dense hour here —
    # those guesses can anchor on stale/old regions of a multi-month log; "recent" is
    # the robust default when the user gave us no date.
    # (infer_center_time_from_extraction / find_error_dense_window are retained for
    #  callers/tests but no longer drive the default windowing path.)

    # Fallback: center_time None → windower uses last N hours of log (most recent)

    windowed_dir = workspace / "windowed"
    # If only a date was provided (no time), use wider window to cover the full day
    hours_before = cc.time_window_hours_before if not date_only else max(cc.time_window_hours_before, 14)
    hours_after = cc.time_window_hours_after if not date_only else max(cc.time_window_hours_after, 14)
    # 追问递进放宽：每加深一层窗口 ×window_scale（>1 时生效）
    if window_scale and window_scale > 1.0:
        hours_before *= window_scale
        hours_after *= window_scale
        logger.info("Follow-up widen: window ×%.1f → %.0f/%.0fh", window_scale, hours_before, hours_after)

    windowed_paths, windowing_meta = await asyncio.to_thread(
        lambda: window_log_files(
            log_paths=log_paths,
            output_dir=windowed_dir,
            center_time=center_time,
            hours_before=hours_before,
            hours_after=hours_after,
            size_threshold=threshold_bytes,
        )
    )

    # Log windowing results
    for meta in windowing_meta:
        if meta.get("windowed"):
            logger.info(
                "L1.5 windowed: %s → %d/%d lines (%.1f%% reduction)",
                meta.get("original_path", "?"),
                meta.get("kept_lines", 0),
                meta.get("total_lines", 0),
                meta.get("reduction_pct", 0),
            )

    # Completeness cross-check (orthogonal to truncation): did the window keep the
    # L1 high-signal lines? If center_time/window bounds are wrong, the decisive
    # region is excluded entirely — folding/sampling can't recover what time-
    # windowing left out. Low coverage → fall back to the full logs.
    signal_lines = signal_lines_from_extraction(extraction or {})
    if signal_lines and any(m.get("windowed") for m in windowing_meta):
        def _coverage() -> float:
            text = ""
            for wp in windowed_paths:
                try:
                    text += wp.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            return window_coverage_ratio(text, signal_lines)

        coverage = await asyncio.to_thread(_coverage)
        if coverage < 0.5:
            # 窗口漏了 L1 高信号行 → 决定性证据不在当前窗口内（problem_date 多半选偏了）。
            # 不再裸退「全量日志」——跨月超大日志原样丢给 agent 会超时（rec27zFZSkfFpN 的病）。
            # 改为围绕信号行实际时间戳重切一个有界窗口把证据包回来；无可解析时间戳则锚最近。
            logger.warning(
                "L1.5 window retained only %.0f%% of %d L1 high-signal lines — "
                "re-centering on signal-line timestamps (bounded) instead of dumping full logs",
                coverage * 100, len(signal_lines),
            )
            windowed_paths, windowing_meta = await asyncio.to_thread(
                lambda: rewindow_on_signal_lines(
                    log_paths=log_paths,
                    output_dir=windowed_dir,
                    extraction=extraction or {},
                    hours_before=hours_before,
                    hours_after=hours_after,
                    size_threshold=threshold_bytes,
                )
            )
            for m in windowing_meta:
                m["reason"] = "low_l1_coverage_recentered"

    # --- Step B: LLM context extraction (optional, costs money) ---
    # anthropic provider falls back to Claude CLI (OAuth, no api_key required);
    # other providers still require an api_key.
    structured_context = None
    condenser_usage: Dict[str, int] = {}  # 计量：L1.5 预提取 token 用量（anthropic 路径）
    if cc.enabled and (cc.api_key or cc.provider == "anthropic"):
        if on_progress:
            await on_progress(55, "L1.5: LLM 上下文提取...")

        from app.services.context_condenser import ContextCondenser, CondensationConfig

        condenser_config = CondensationConfig(
            enabled=cc.enabled,
            provider=cc.provider,
            model=cc.model,
            api_key=cc.api_key,
            api_base_url=cc.api_base_url,
            max_input_chars=cc.max_input_chars,
            timeout=cc.timeout,
            temperature=cc.temperature,
        )
        condenser = ContextCondenser(condenser_config)

        rules_summary = ", ".join(r.meta.name or r.meta.id for r in rules[:3])

        try:
            result = await condenser.condense(
                log_paths=windowed_paths,
                issue_description=issue.description,
                device_sn=issue.device_sn,
                problem_date=problem_date,
                l1_extraction=extraction,
                rules_summary=rules_summary,
            )
            condenser_usage = dict(result.usage or {})  # 计量：无论成功与否都计费

            if result.success:
                structured_context = result.structured_context
                # Save to workspace for reference
                context_dir = workspace / "context"
                context_dir.mkdir(parents=True, exist_ok=True)
                (context_dir / "llm_extraction.json").write_text(
                    _json.dumps(structured_context, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info(
                    "L1.5 LLM extraction success: provider=%s, duration=%dms, output=%d chars",
                    result.provider, result.duration_ms, result.output_chars,
                )
                if on_progress:
                    await on_progress(58, f"L1.5 浓缩成功（{result.provider}, {result.output_chars} chars）")
            else:
                # P0 #1: surface failure to operators — agent will fall back to raw logs and may hit max_turns
                logger.warning(
                    "L1.5 LLM extraction failed: %s — agent will analyze raw windowed logs (risk of max_turns)",
                    result.error,
                )
                if on_progress:
                    await on_progress(58, f"L1.5 浓缩失败（{result.error[:80]}），agent 将直接分析原始日志")
                if result.raw_output:
                    # Save raw output even if JSON parsing failed
                    context_dir = workspace / "context"
                    context_dir.mkdir(parents=True, exist_ok=True)
                    (context_dir / "llm_extraction_raw.txt").write_text(
                        result.raw_output, encoding="utf-8",
                    )
                # Persist failure reason for downstream debugging / UI
                try:
                    context_dir = workspace / "context"
                    context_dir.mkdir(parents=True, exist_ok=True)
                    (context_dir / "llm_extraction_failure.json").write_text(
                        _json.dumps({
                            "provider": result.provider,
                            "model": result.model,
                            "error": result.error,
                            "duration_ms": result.duration_ms,
                            "input_chars": result.input_chars,
                        }, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error("L1.5 LLM extraction error: %s", e, exc_info=True)
            if on_progress:
                await on_progress(58, f"L1.5 浓缩异常（{type(e).__name__}），agent 将直接分析原始日志")

    # Save windowing metadata
    try:
        context_dir = workspace / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "windowing_meta.json").write_text(
            _json.dumps(windowing_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    return {
        "log_paths": windowed_paths,
        "structured_context": structured_context,
        "windowing_metadata": windowing_meta,
        "condenser_usage": condenser_usage,
        "condenser_model": cc.model,
    }
