"""
Crashguard 早晚报调度。

简化策略：每 60 秒 tick 一次，解析 morning_cron / evening_cron 的 "M H * * *"。
当前小时分钟匹配 → 触发对应 report；用 last_fired_at 防同一分钟重发。
不引入 apscheduler/croniter 依赖，保留模块独立性。

支持的 cron 形式：
- "M H * * *"           — 固定时刻
- "*/N * * * *"         — 每 N 分钟（仅 minute 字段）
- "M */N * * *"         — 每 N 小时的第 M 分钟
其他复杂表达式不支持，直接 skip。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Optional, Tuple

logger = logging.getLogger("crashguard.scheduler")

_TICK_INTERVAL_SEC = 60
# 每日报告 catch-up 宽限：到点后这段时间内只要当天没发过就补发，容忍长任务/重启吞掉
# 精确那一分钟。根因——单线程 60s loop 顺序 await，一个长任务（如 ~9min 的 analyze_tick）
# 会让 loop 跳过整分钟，而早报 cron `0 8` 一天只有 08:00 这一分钟的机会，错过即全天不发。
# 超过宽限视为过期不补，避免重启后深夜补发"早报"。
_DAILY_CATCHUP_GRACE_SEC = 2 * 3600
_last_fired: dict[str, str] = {}  # report_type → "YYYY-MM-DD HH:MM"（非固定 cron 兜底，分钟级幂等）
_daily_fired_date: dict[str, str] = {}  # report_type → "YYYY-MM-DD"（固定每日 cron 的 catch-up 幂等）
_pr_sync_last_fired: str = ""    # "YYYY-MM-DD HH:MM" 防同分钟重跑
_analyze_last_fired: str = ""    # 定时分析 tick 防同分钟重跑
_hourly_alert_last_fired: str = ""  # 进程级幂等；DB UNIQUE(hour_utc) 兜多机
_core_metric_last_fired: str = ""   # 10min tick 进程级幂等；DB UNIQUE(window_start) 兜多机
_job_health_last_fired: str = ""    # 兜底告警 tick 进程级幂等
_top_crash_auto_pr_last_fired: str = ""  # Top crash 自动 PR 进程级幂等
_backfill_last_fired: str = ""      # 周度 baseline 回填 tick 进程级幂等
_deep_analyze_auto_last_fired: str = ""  # Phase 1 深度诊断自动 tick 进程级幂等
_repo_sync_last_fired: str = ""      # 每日仓库同步 tick 进程级幂等


async def _run_analyze_tick(max_per_tick: int) -> dict:
    """分批跑今日 attention 中未 success 的 issue，最多 max_per_tick 个。

    设计目标：单次只跑 1-2 个 issue（~90s/个），避免一次性 20 个被 OS/网络 timeout 杀。
    复用 _auto_analyze_attention 的去重 + 串行 + auto-PR 逻辑。
    """
    from datetime import date
    from app.crashguard.services.daily_report import _auto_analyze_attention
    from app.crashguard.workers.warmup import _collect_attention_ids

    today = date.today()
    full = await _collect_attention_ids(today)
    if not full:
        return {"picked": 0, "completed": 0, "remaining": 0}

    # _auto_analyze_attention 内部还有 dedup 闸再过滤 success/running/pending —— 这里限量挑前 N 个传进去
    # （内部会过滤掉已跑过的）
    picked = full[: max(1, int(max_per_tick))]
    completed = await _auto_analyze_attention(picked)
    return {"picked": len(picked), "completed": completed, "remaining": max(0, len(full) - completed)}


def _cron_matches(expr: str, now: datetime) -> bool:
    """cron 表达式匹配（M H DOM MON DOW）。

    支持：`*` / `N` / `N-M` range / `N,M,K` list / `*/N` step。
    DOW: Unix cron 标准 Sun=0, Mon=1, ..., Sat=6（python weekday+1 %7）。
    """
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    minute_f, hour_f, dom_f, month_f, dow_f = parts

    def field_match(field: str, value: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            try:
                step = int(field[2:])
                return step > 0 and value % step == 0
            except ValueError:
                return False
        if "," in field:
            return any(field_match(f.strip(), value) for f in field.split(","))
        if "-" in field:
            try:
                a, b = field.split("-", 1)
                return int(a) <= value <= int(b)
            except ValueError:
                return False
        try:
            return int(field) == value
        except ValueError:
            return False

    cron_dow = (now.weekday() + 1) % 7
    return (
        field_match(minute_f, now.minute)
        and field_match(hour_f, now.hour)
        and field_match(dom_f, now.day)
        and field_match(month_f, now.month)
        and field_match(dow_f, cron_dow)
    )


def _parse_fixed_daily(expr: str) -> Optional[Tuple[int, int]]:
    """固定每日 cron 'M H * * *'（M/H 纯整数、DOM/MON/DOW 均 *）→ (minute, hour)，否则 None。"""
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    m, h, dom, mon, dow = parts
    if dom != "*" or mon != "*" or dow != "*":
        return None
    try:
        mi, ho = int(m), int(h)
    except ValueError:
        return None
    if 0 <= mi < 60 and 0 <= ho < 24:
        return (mi, ho)
    return None


def _daily_fire_decision(
    cron_expr: str, now: datetime, last_fired_date: Optional[str]
) -> Optional[Tuple[bool, str]]:
    """每日报告 catch-up 触发判定（纯函数，便于单测）。

    返回：
    - None → 非固定每日 cron，调用方回退精确分钟匹配 (_cron_matches)
    - (should_fire, today_tag) → 固定每日 cron 判定；should_fire 时把 today_tag 写回幂等

    规则：到点(now>=scheduled) 且未超 grace 且当天未发过 → 补发。容忍长任务/重启吞掉
    精确那一分钟（如 08:00 被跳过，08:05 的下一个 tick 仍能补上）。
    """
    fixed = _parse_fixed_daily(cron_expr)
    if fixed is None:
        return None
    mi, ho = fixed
    scheduled = now.replace(hour=ho, minute=mi, second=0, microsecond=0)
    today_tag = now.strftime("%Y-%m-%d")
    if now < scheduled:
        return (False, today_tag)                              # 还没到点
    if (now - scheduled).total_seconds() > _DAILY_CATCHUP_GRACE_SEC:
        return (False, today_tag)                              # 超过宽限，过期不补
    if last_fired_date == today_tag:
        return (False, today_tag)                              # 今天已发过
    return (True, today_tag)


# ---------------------------------------------------------------------------
# Heavy-job 串行 worker
#
# 根因：主 cron loop 单线程 60s tick 顺序 await 所有任务，一个长任务（analyze_tick
# ~90s 甚至 ~9min、deep_analyze_auto 最长 30min、pr_sync ~105s）会阻塞整个 loop，
# 让它跳过整分钟 → 精确分钟匹配的任务（最严重是一天一次的早报）被漏掉。
#
# 方案：主 loop 只做「到点判定 + 入队」（瞬时不阻塞），耗时任务丢进一个单消费者队列
# 串行执行。保留「同一时刻最多一个 heavy job 在跑」的互斥语义（原本靠顺序 await 实现），
# 避免 analyze 自动建 PR 与 top_crash_auto_pr 等并发 git push 撞车。
# 时间敏感且短的任务（早晚报 / hourly_alert / core_metric / job_health）仍内联执行。
# ---------------------------------------------------------------------------
_job_queue: Optional["asyncio.Queue"] = None
_queued_jobs: set = set()       # 已入队/执行中的 heavy job 名——防重复入队（替代旧的 _xxx_running 标志）
_worker_started: bool = False


def _get_job_queue() -> "asyncio.Queue":
    global _job_queue
    if _job_queue is None:
        _job_queue = asyncio.Queue()
    return _job_queue


def _enqueue_job(job_name: str, coro_factory) -> None:
    """把 heavy job 丢进串行 worker 队列。同名 job 上次还没跑完则跳过（reentrancy guard）。"""
    if job_name in _queued_jobs:
        logger.info("%s skipped: previous run still queued/running", job_name)
        return
    _queued_jobs.add(job_name)
    _get_job_queue().put_nowait((job_name, coro_factory))


async def _job_worker_loop() -> None:
    """单消费者：串行执行 heavy job。异常在此兜底（不让 task 静默丢异常）。"""
    logger.info("crashguard job_worker_loop started")
    q = _get_job_queue()
    while True:
        job_name, coro_factory = await q.get()
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("crashguard job worker: %s failed", job_name)
        finally:
            _queued_jobs.discard(job_name)
            q.task_done()


async def _tick_once() -> None:
    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.services.daily_report import send_daily_report

    s = get_crashguard_settings()
    if not s.enabled or not s.feishu_enabled:
        return
    # 多实例部署兜底：只有显式开启 scheduler 的机器跑 cron 触发；
    # 即使两台都开了，下游 send_daily_report 也有 DB 抢锁去重保护。
    if not getattr(s, "scheduler_enabled", True):
        return
    if not s.feishu_target_chat_id:
        return

    now = datetime.now()
    tag = now.strftime("%Y-%m-%d %H:%M")
    # 早晚报独立 enabled 闸（2026-05-21）：speed-bump 优先于 cron 匹配。
    # 速报 = evening 已下线（与早报冗余 + 打扰），日内增量信号交给 hourly_alert。
    # 想恢复 evening：config.yaml feishu.evening_enabled: true。
    schedule = (
        ("morning", s.morning_cron, getattr(s, "morning_enabled", True)),
        ("evening", s.evening_cron, getattr(s, "evening_enabled", False)),
    )
    from app.crashguard.services.job_heartbeat import record_heartbeat
    for report_type, cron_expr, enabled in schedule:
        if not enabled:
            continue
        decision = _daily_fire_decision(cron_expr, now, _daily_fired_date.get(report_type))
        if decision is None:
            # 非固定每日 cron：精确分钟匹配 + 分钟级幂等
            if _last_fired.get(report_type) == tag or not _cron_matches(cron_expr, now):
                continue
            _last_fired[report_type] = tag
        else:
            should_fire, today_tag = decision
            if not should_fire:
                continue
            # 先打当天幂等再 send——异常/重启后由 send_daily_report 的
            # DB UNIQUE(report_date, report_type) 兜底防重发。
            _daily_fired_date[report_type] = today_tag
        job_name = "morning_daily" if report_type == "morning" else "evening_daily"
        try:
            async with record_heartbeat(job_name) as hb:
                res = await send_daily_report(report_type, target_date=now.date())
                hb.set_summary(res)
                hb.set_status_from_result(res)
                logger.info(
                    "crashguard daily_report fired: type=%s ok=%s sent=%s reason=%s",
                    report_type, res.get("ok"), res.get("sent"), res.get("skipped_reason"),
                )
        except Exception:
            logger.exception("crashguard daily_report tick failed: type=%s", report_type)

    # PR 状态同步（独立 cron，默认 */15）
    global _pr_sync_last_fired
    pr_cron = getattr(s, "pr_sync_cron", "") or ""
    if pr_cron and _pr_sync_last_fired != tag and _cron_matches(pr_cron, now):
        _pr_sync_last_fired = tag
        async def _pr_sync_job():
            async with record_heartbeat("pr_sync") as hb:
                from app.crashguard.services.pr_sync import sync_all_open_prs
                res = await sync_all_open_prs()
                hb.set_summary(res)
                # 三态判定：全部成功→success / 部分失败→degraded / 全部失败→failed
                # 部分失败常见为 transient（GitHub 偶发 502、单个 PR 被人删），
                # 不应触发立刻告警，进 degraded 弱信号通道累积观察。
                checked = int(res.get("checked", 0) or 0)
                errors = int(res.get("errors", 0) or 0)
                hb.set_status_from_partial(
                    success_count=checked - errors,
                    total_count=checked,
                    error_hint=f"errors={errors}/{checked}" if errors else "",
                )
                logger.info(
                    "crashguard pr_sync fired: checked=%d changed=%d errors=%d status=%s",
                    checked, res.get("changed", 0), errors, hb.status,
                )
        _enqueue_job("pr_sync", _pr_sync_job)

    # Top crash 自动 PR（专属低门槛 + 节流，默认每 2h）
    global _top_crash_auto_pr_last_fired
    top_pr_cron = getattr(s, "top_crash_auto_pr_cron", "") or ""
    if top_pr_cron and _top_crash_auto_pr_last_fired != tag and _cron_matches(top_pr_cron, now):
        _top_crash_auto_pr_last_fired = tag
        async def _top_crash_auto_pr_job():
            async with record_heartbeat("top_crash_auto_pr") as hb:
                from app.crashguard.services.top_crash_auto_pr import (
                    run_top_crash_auto_pr_tick,
                )
                res = await run_top_crash_auto_pr_tick()
                hb.set_summary(res)
                if res.get("actioned", 0) == 0 and res.get("total_scanned", 0) == 0:
                    hb.status = "skipped"
                logger.info(
                    "crashguard top_crash_auto_pr fired: actioned=%d scanned=%d urls=%s",
                    res.get("actioned", 0), res.get("total_scanned", 0),
                    (res.get("pr_urls") or [])[:3],
                )
        _enqueue_job("top_crash_auto_pr", _top_crash_auto_pr_job)

    # AI 分析定时小步分批（独立 cron，默认 */5）
    global _analyze_last_fired
    analyze_cron = getattr(s, "analyze_cron", "") or ""
    if analyze_cron and _analyze_last_fired != tag and _cron_matches(analyze_cron, now):
        _analyze_last_fired = tag  # 先打 tag 防异常重试
        max_per_tick = int(getattr(s, "analyze_max_per_tick", 1) or 1)
        # 入队串行执行：单个 issue ~90s，多个可能跨多分钟。worker 的 _queued_jobs 去重
        # 等价于旧的 _analyze_running——上一批没跑完就不重复入队。
        async def _analyze_job(mpt=max_per_tick):
            async with record_heartbeat("analyze_tick") as hb:
                res = await _run_analyze_tick(max_per_tick=mpt)
                hb.set_summary(res)
                if res.get("picked", 0) == 0:
                    hb.status = "skipped"
                logger.info(
                    "crashguard analyze tick fired: picked=%d completed=%d remaining=%d",
                    res.get("picked", 0), res.get("completed", 0), res.get("remaining", 0),
                )
        _enqueue_job("analyze_tick", _analyze_job)

    # Hourly alert（SHoW 对比；独立 cron，默认每小时第 5 分钟）
    global _hourly_alert_last_fired
    if getattr(s, "hourly_alert_enabled", False):
        hourly_cron = getattr(s, "hourly_alert_cron", "") or ""
        if hourly_cron and _hourly_alert_last_fired != tag and _cron_matches(hourly_cron, now):
            _hourly_alert_last_fired = tag  # 先打 tag 防异常重试
            try:
                async with record_heartbeat("hourly_alert") as hb:
                    from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
                    res = await run_hourly_alert_tick()
                    hb.set_summary(res)
                    hb.set_status_from_result(res)
                    logger.info(
                        "crashguard hourly_alert tick fired: alerted=%s new=%s surge=%s reason=%s",
                        res.get("alerted"), res.get("new"), res.get("surge"),
                        res.get("reason") or res.get("skipped") or res.get("error", ""),
                    )
            except Exception:
                logger.exception("crashguard hourly_alert tick failed")

    # 核心指标告警（crash-free sessions %，10 分钟粒度）
    global _core_metric_last_fired
    if getattr(s, "core_metric_enabled", False):
        cm_cron = getattr(s, "core_metric_cron", "") or ""
        if cm_cron and _core_metric_last_fired != tag and _cron_matches(cm_cron, now):
            _core_metric_last_fired = tag
            try:
                async with record_heartbeat("core_metric") as hb:
                    from app.crashguard.services.core_metric_alerter import run_core_metric_tick
                    res = await run_core_metric_tick()
                    hb.set_summary(res)
                    hb.set_status_from_result(res)
                logger.info(
                    "crashguard core_metric tick fired: alerted=%s direction=%s reason=%s",
                    res.get("alerted"), res.get("direction"),
                    res.get("reason") or res.get("skipped") or res.get("error", ""),
                )
            except Exception:
                logger.exception("crashguard core_metric tick failed")

    # Baseline 回填（每周一次扫近 3 天，补 hourly_alert tick 漏掉的窗口 + 早晚报 DB fallback）
    # 底层逻辑：日常 pipeline / hourly_alert tick 若因 Datadog 限流或重启失败，会留下基线
    # 窗口空洞。每周扫一遍最近 3 天，幂等 INSERT OR IGNORE 补齐缺口，保证 SHoW 基线始终有数据。
    global _backfill_last_fired
    if getattr(s, "baseline_backfill_enabled", True):
        bf_cron = getattr(s, "baseline_backfill_cron", "") or "0 18 * * 0"
        if bf_cron and _backfill_last_fired != tag and _cron_matches(bf_cron, now):
            _backfill_last_fired = tag
            async def _backfill_job():
                async with record_heartbeat("baseline_backfill") as hb:
                    from app.crashguard.scripts_runtime import run_backfill_all
                    res = await run_backfill_all(days_hourly=3, days_daily=3)
                    hb.set_summary(res)
                    hb.set_status_from_result(res)
                    logger.info(
                        "crashguard baseline_backfill fired: hourly_written=%s daily_written=%s",
                        res.get("hourly", {}).get("written"),
                        res.get("daily", {}).get("written"),
                    )
            _enqueue_job("baseline_backfill", _backfill_job)

    # 每日仓库同步（保证 crashguard auto-PR 的本地 checkout 不变旧；默认关，见 config.py 说明）
    global _repo_sync_last_fired
    if getattr(s, "repo_sync_enabled", False):
        rs_cron = getattr(s, "repo_sync_cron", "") or "0 3 * * *"
        if rs_cron and _repo_sync_last_fired != tag and _cron_matches(rs_cron, now):
            _repo_sync_last_fired = tag
            async def _repo_sync_job():
                async with record_heartbeat("repo_sync") as hb:
                    from app.crashguard.services.repo_sync import run_repo_sync
                    res = await run_repo_sync()
                    hb.set_summary(res)
                    # 批量任务用 counts 三态判定（同 pr_sync 模式）——res["ok"] 是成功计数
                    # 而非布尔值，set_status_from_result 会误判它恒为 truthy → 永远 success。
                    total = int(res.get("total", 0) or 0)
                    failed = int(res.get("failed", 0) or 0)
                    hb.set_status_from_partial(
                        success_count=total - failed,
                        total_count=total,
                        error_hint=f"failed={failed}/{total}" if failed else "",
                    )
                    logger.info(
                        "crashguard repo_sync fired: total=%s ok=%s failed=%s",
                        res.get("total"), res.get("ok"), res.get("failed"),
                    )
            _enqueue_job("repo_sync", _repo_sync_job)

    # Phase 1 深度诊断自动触发：对 no-PR + 低置信度 issue 自动跑深度调查
    # 默认每 35 分钟 1 个（Phase 1 最长 30min，留 5min buffer 防重叠）
    # kill switch: deep_analysis_auto_enabled=false（默认 false，需显式开启）
    global _deep_analyze_auto_last_fired
    if getattr(s, "deep_analysis_auto_enabled", False):
        da_cron = getattr(s, "deep_analyze_auto_cron", "") or "*/35 * * * *"
        if da_cron and _deep_analyze_auto_last_fired != tag and _cron_matches(da_cron, now):
            _deep_analyze_auto_last_fired = tag
            # Phase 1 最长 30min；入队串行 + _queued_jobs 去重等价于旧的 _deep_analyze_auto_running。
            async def _deep_analyze_job():
                async with record_heartbeat("deep_analyze_auto") as hb:
                    from app.crashguard.workers.warmup import run_deep_analysis_auto_tick
                    res = await run_deep_analysis_auto_tick()
                    hb.set_summary(res)
                    if res.get("triggered", 0) == 0:
                        hb.status = "skipped"
                    logger.info(
                        "crashguard deep_analyze_auto tick fired: triggered=%d scanned=%d skipped=%d",
                        res.get("triggered", 0), res.get("scanned", 0), res.get("skipped", 0),
                    )
            _enqueue_job("deep_analyze_auto", _deep_analyze_job)

    # 定时任务健康度兜底告警（每 5min 扫描 heartbeat 表）
    global _job_health_last_fired
    if getattr(s, "job_health_alert_enabled", True):
        jh_cron = getattr(s, "job_health_alert_cron", "") or ""
        if jh_cron and _job_health_last_fired != tag and _cron_matches(jh_cron, now):
            _job_health_last_fired = tag
            try:
                async with record_heartbeat("job_health_alert") as hb:
                    from app.crashguard.services.job_health_alerter import run_job_health_check
                    res = await run_job_health_check()
                    hb.set_summary(res)
                    hb.set_status_from_result(res)
                    logger.info(
                        "crashguard job_health_alert fired: alerted=%s unhealthy=%s",
                        res.get("alerted"),
                        res.get("unhealthy_jobs") or res.get("skipped") or res.get("error", ""),
                    )
            except Exception:
                logger.exception("crashguard job_health_alert tick failed")


async def report_scheduler_loop() -> None:
    """主循环。每 60 秒 tick 一次（只做到点判定 + 入队，瞬时不阻塞）。"""
    logger.info("crashguard report_scheduler_loop started")
    # 懒启动 heavy-job 串行 worker（与主 loop 同生命周期，不必改 main.py）
    global _worker_started
    if not _worker_started:
        asyncio.create_task(_job_worker_loop())
        _worker_started = True
    while True:
        try:
            await _tick_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("crashguard scheduler tick error (continuing)")
        try:
            await asyncio.sleep(_TICK_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
