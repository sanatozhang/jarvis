"""回填 crash_snapshots — classifier surge 判定 + 早晚报 SHoW-24h DB fallback 用。

底层逻辑：
- classifier.py 用过去 `surge_baseline_days`（默认 7）的 crash_snapshots events 均值做
  surge 判定（识别突增 issue 进 attention pool）
- daily_report.py 优先 Datadog 直查 baseline；Datadog 失败时 fallback 查
  crash_snapshots 7 天前那一天 → DB 兜底

抓手：本脚本回填指定天数（默认 14d）每日 24h events 快照，让 surge 判定 +
早晚报 fallback 立即生效。仅写 events_count，其它字段保留 default（不充当全量真相）。

幂等：INSERT OR IGNORE，已有 (issue_id, snapshot_date) 行保留不覆盖。

用法：
  PYTHONPATH=/app python3 backend/scripts/backfill_daily_snapshots.py [--days=14] [--dry-run]
"""
import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.crashguard.config import get_crashguard_settings
from app.crashguard.models import CrashSnapshot  # noqa: F401
from app.db.database import init_db, get_session


async def run_backfill(days: int = 14, dry_run: bool = False) -> dict:
    """对外入口。返回统计 dict。"""
    s = get_crashguard_settings()
    if not s.datadog_api_key:
        return {"ok": False, "error": "datadog_api_key 未配置"}

    from app.crashguard.services.datadog_client import DatadogClient
    client = DatadogClient(
        api_key=s.datadog_api_key,
        app_key=s.datadog_app_key,
        site=s.datadog_site,
    )

    today_utc = datetime.now(timezone.utc).date()
    target_dates = sorted(
        today_utc - timedelta(days=i) for i in range(1, days + 1)
    )
    queries = [
        ("fatal", s.datadog_query_fatal),
        ("nonfatal", s.datadog_query_nonfatal),
    ]
    total = len(target_dates) * len(queries)
    print(f"== plan: {days}d × {len(queries)}q = {total} calls ==")
    print(f"== date range: {target_dates[0]} → {target_dates[-1]} (UTC) ==")
    if dry_run:
        return {"ok": True, "dry_run": True, "planned_calls": total}

    INSERT_SQL = text(
        "INSERT OR IGNORE INTO crash_snapshots "
        "(datadog_issue_id, snapshot_date, events_count, created_at) "
        "VALUES (:iid, :sd, :ev, :now)"
    )

    written = skipped = call_n = 0
    t0 = time.time()
    for d in target_dates:
        day_start_dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        day_end_dt = day_start_dt + timedelta(days=1)
        start_ms = int(day_start_dt.timestamp() * 1000)
        end_ms = int(day_end_dt.timestamp() * 1000)
        for qname, q in queries:
            call_n += 1
            try:
                items = await client.list_issues_for_window(
                    start_ms=start_ms, end_ms=end_ms,
                    tracks=s.datadog_tracks, query=q,
                    use_cache=False,
                )
            except Exception as exc:
                print(f"  [{call_n:>3}/{total}] {d} {qname}: FETCH_ERR {exc}")
                await asyncio.sleep(2.0)
                continue
            rows = [
                {"iid": it.get("id") or "", "sd": d,
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
                f"  [{call_n:>3}/{total}] {d} {qname}: items={len(items)} written={written}",
                flush=True,
            )
            await asyncio.sleep(0.5)
    dt = time.time() - t0
    print(f"\n== done in {dt:.1f}s, written={written} skipped_existing={skipped} ==")
    return {"ok": True, "written": written, "skipped": skipped, "duration_s": dt}


async def _cli() -> None:
    await init_db()
    days = 14
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
