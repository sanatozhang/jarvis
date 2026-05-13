"""回填 crash_hourly_snapshots — hourly_alerter SHoW-3h 基线源。

底层逻辑：hourly_alerter 完全依赖本地 crash_hourly_snapshots 做 SHoW-3h
（上周同 weekday 同 3h 块）对比。新机器部署或长时间停机后，该表会空白，
导致 SHoW + rolling-7d 基线全失效，告警系统"假静默"（拿不到基线无法判定突增）。

抓手：本脚本一次性从 Datadog Error Tracking API 回填指定天数（默认 7d）的
3h 块快照，让 SHoW/rolling 基线立即生效。

幂等：INSERT OR IGNORE，已有 (issue_id, hour_utc) 行保留不覆盖。
颗粒度：3h 块对齐 (UTC 00/03/06/09/12/15/18/21)，每块 × 2 query（fatal+nonfatal）。

用法：
  PYTHONPATH=/app python3 backend/scripts/backfill_hourly_snapshots.py [--days=7] [--dry-run]

闭环：跑完后调 `POST /api/crash/jobs/hourly_alert/run-now` 验证 surge 识别恢复。
"""
import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.crashguard.config import get_crashguard_settings
from app.crashguard.models import CrashHourlySnapshot  # noqa: F401  确保表注册
from app.db.database import init_db, get_session


BLOCKS_PER_DAY = (0, 3, 6, 9, 12, 15, 18, 21)


def _block_starts(days: int, now_utc: datetime) -> list[datetime]:
    """返回近 days 天所有已完成的 3h 块起点（UTC），不含未完成的当前块。"""
    cur_block = now_utc.replace(minute=0, second=0, microsecond=0)
    cur_block = cur_block.replace(hour=(cur_block.hour // 3) * 3)
    last_completed_end = cur_block
    out: list[datetime] = []
    span = days * 24
    block = last_completed_end - timedelta(hours=3)
    while (last_completed_end - block).total_seconds() <= span * 3600:
        out.append(block)
        block -= timedelta(hours=3)
    return sorted(out)


async def run_backfill(days: int = 7, dry_run: bool = False) -> dict:
    """对外可调用入口（cron / scheduler tick 也可调）。返回统计 dict。"""
    s = get_crashguard_settings()
    if not s.datadog_api_key:
        return {"ok": False, "error": "datadog_api_key 未配置"}

    from app.crashguard.services.datadog_client import DatadogClient
    client = DatadogClient(
        api_key=s.datadog_api_key,
        app_key=s.datadog_app_key,
        site=s.datadog_site,
    )

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    blocks = _block_starts(days, now_utc)
    queries = [
        ("fatal", s.datadog_query_fatal),
        ("nonfatal", s.datadog_query_nonfatal),
    ]
    total = len(blocks) * len(queries)
    print(f"== plan: {days}d × {len(BLOCKS_PER_DAY)}blocks × {len(queries)}q = {total} calls ==")
    print(f"== block range: {blocks[0]} → {blocks[-1]} (UTC) ==")
    if dry_run:
        return {"ok": True, "dry_run": True, "planned_calls": total}

    INSERT_SQL = text(
        "INSERT OR IGNORE INTO crash_hourly_snapshots "
        "(datadog_issue_id, hour_utc, events_count, captured_at) "
        "VALUES (:iid, :hu, :ev, :now)"
    )

    written = skipped = call_n = 0
    t0 = time.time()
    for block in blocks:
        start_ms = int(block.replace(tzinfo=timezone.utc).timestamp() * 1000)
        end_ms = start_ms + 3 * 3600 * 1000
        for qname, q in queries:
            call_n += 1
            try:
                items = await client.list_issues_for_window(
                    start_ms=start_ms, end_ms=end_ms,
                    tracks=s.datadog_tracks, query=q,
                    use_cache=False,
                )
            except Exception as exc:
                print(f"  [{call_n:>3}/{total}] {block} {qname}: FETCH_ERR {exc}")
                await asyncio.sleep(2.0)
                continue
            rows = [
                {"iid": it.get("id") or "", "hu": block,
                 "ev": int(it.get("attributes", {}).get("events_count", 0) or 0),
                 "now": datetime.utcnow()}
                for it in items if it.get("id")
            ]
            if rows:
                async with get_session() as sess:
                    res = await sess.execute(INSERT_SQL, rows)
                    await sess.commit()
                    inserted = res.rowcount or 0
                    written += inserted
                    skipped += len(rows) - inserted
            print(
                f"  [{call_n:>3}/{total}] {block} {qname}: "
                f"items={len(items)} written={written}",
                flush=True,
            )
            await asyncio.sleep(0.3)
    dt = time.time() - t0
    print(f"\n== done in {dt:.1f}s, written={written} skipped_existing={skipped} ==")
    return {"ok": True, "written": written, "skipped": skipped, "duration_s": dt}


async def _cli() -> None:
    await init_db()
    days = 7
    dry = False
    for a in sys.argv[1:]:
        if a.startswith("--days="):
            days = int(a.split("=", 1)[1])
        elif a == "--dry-run":
            dry = True
    r = await run_backfill(days=days, dry_run=dry)
    print(r)


if __name__ == "__main__":
    asyncio.run(_cli())
