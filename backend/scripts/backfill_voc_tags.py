"""存量回填：给 2026-07-05 起的历史工单打上 VOC Portal taxonomy 分类标签。

背景：Jarvis 的 AI 分析分类改用公司 VOC Portal 的 taxonomy（三层 + definition +
MECE 规则，见 backend/app/services/voc_taxonomy.py）取代原来硬编码在
backend/app/classification_taxonomy.py 里的 12 类体系。历史工单只重新归类，
**不重跑日志分析**——读已有的 analyses.problem_type / root_cause 结论 +
issues.description + 提单分类，喂给一次轻量 LLM 调用（backend/app/services/
voc_classifier.py），比重新跑一遍 agent CLI 快得多也便宜得多。

新字段 analyses.voc_tags_json 与旧的 problem_categories_json 并存——旧字段冻结
不动，可以随时对比迁移矩阵、也可以随时回滚（清空 voc_tags_json 即可）。

安全约束：
- **默认 dry-run**，只统计/预览，不调 LLM、不写 DB；`--execute` 才真跑。
- 默认 `--since 2026-07-05`（历史工单只补这天起的，见设计决策）。
- 默认 `--only-empty`（跳过已有 voc_tags_json 的行）——**幂等**：重复跑只会
  处理上次失败/中断遗留的空行，不会重复打标已成功的行。用 `--include-tagged`
  强制重新跑全部（比如换了 taxonomy 版本要重新分类时）。
- 单条一次 LLM 请求，`--concurrency` 控并发（默认 4），不批量塞多条到一个
  请求里——批塞会明显掉准确率（见 voc_classifier.py 里的说明）。

用法（容器内 / backend 目录）：
    python -m scripts.backfill_voc_tags                       # dry-run，只统计
    python -m scripts.backfill_voc_tags --limit 10 --execute   # 小批真跑
    python -m scripts.backfill_voc_tags --execute              # 全量真跑
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from collections import Counter
from typing import Any, Dict, List

logger = logging.getLogger("voc.backfill_voc_tags")


async def _bootstrap_taxonomy() -> int:
    """确保内存里的 active taxonomy cache 是新鲜的。返回当前 active tag 数，
    方便 dry-run 时提醒"taxonomy 还没灌数据"这种一眼能看出的配置问题。"""
    from app.services import voc_taxonomy

    await voc_taxonomy.sync_seed_to_db()   # no-op if DB already has tags
    await voc_taxonomy.reload_from_db()
    return len(voc_taxonomy.active_tags())


async def run_backfill(
    since: str,
    limit: int,
    concurrency: int,
    only_empty: bool,
    execute: bool,
) -> Dict[str, Any]:
    """回填主入口。execute=False 只统计/预览，True 才真正调 LLM + 写 DB。"""
    from app.db.database import init_db, get_analyses_for_voc_backfill, update_analysis_voc_tags
    from app.services.voc_classifier import TicketEvidence, classify_ticket, FALLBACK_TAG_ID

    await init_db()
    active_tag_count = await _bootstrap_taxonomy()
    if active_tag_count == 0:
        logger.warning(
            "Active VOC taxonomy is empty (seed not pulled yet / DB not synced) — "
            "every row will fall back to '%s'. See backend/seeds/voc_taxonomy_seed.json.",
            FALLBACK_TAG_ID,
        )

    rows = await get_analyses_for_voc_backfill(since=since, limit=limit, only_empty=only_empty)

    if not execute:
        group_counts: Counter = Counter()
        for r in rows:
            group_counts[r.get("category") or "(未标)"] += 1
        return {
            "mode": "dry-run",
            "active_tag_count": active_tag_count,
            "scanned": len(rows),
            "submitted_category_distribution": dict(group_counts.most_common(15)),
        }

    sem = asyncio.Semaphore(max(1, concurrency))
    results: List[Dict[str, Any]] = []

    async def _process(row: Dict[str, Any]) -> Dict[str, Any]:
        async with sem:
            evidence = TicketEvidence.from_analysis_row(row)
            tags = await classify_ticket(evidence)
            ok = await update_analysis_voc_tags(row["analysis_id"], tags)
            primary = next((t for t in tags if t.get("role") == "primary"), {})
            return {
                "analysis_id": row["analysis_id"],
                "issue_id": row["issue_id"],
                "primary_tag_id": primary.get("tag_id", ""),
                "primary_group": primary.get("level_1_category", ""),
                "fallback": primary.get("tag_id") == FALLBACK_TAG_ID,
                "db_written": ok,
            }

    results = await asyncio.gather(*(_process(r) for r in rows))

    group_counts: Counter = Counter(r["primary_group"] or "(空)" for r in results)
    fallback_count = sum(1 for r in results if r["fallback"])
    write_failures = [r for r in results if not r["db_written"]]

    return {
        "mode": "execute",
        "active_tag_count": active_tag_count,
        "scanned": len(rows),
        "classified": len(results),
        "fallback_count": fallback_count,
        "group_distribution": dict(group_counts.most_common(15)),
        "write_failures": write_failures,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Backfill VOC Portal taxonomy tags for historical tickets")
    parser.add_argument("--since", default="2026-07-05", help="只回填这天起创建的 analyses（默认 2026-07-05）")
    parser.add_argument("--limit", type=int, default=500, help="单次最多处理多少条（默认 500）")
    parser.add_argument("--concurrency", type=int, default=4, help="并发 LLM 请求数（默认 4）")
    parser.add_argument("--include-tagged", action="store_true",
                         help="连已有 voc_tags_json 的行也重新跑（默认跳过，见幂等说明）")
    parser.add_argument("--execute", action="store_true", help="真正调 LLM + 写 DB（默认只 dry-run 统计）")
    args = parser.parse_args()

    summary = asyncio.run(run_backfill(
        since=args.since,
        limit=args.limit,
        concurrency=args.concurrency,
        only_empty=not args.include_tagged,
        execute=args.execute,
    ))

    if summary["mode"] == "dry-run":
        print(f"\n=== backfill DRY-RUN: since={args.since} limit={args.limit} ===")
        print(f"  active VOC tags loaded: {summary['active_tag_count']}")
        print(f"  rows that would be classified: {summary['scanned']}")
        print("  by submitted category (issues.category):")
        for cat, n in summary["submitted_category_distribution"].items():
            print(f"    {cat:<20} {n}")
        print("\n(dry-run，未调用 LLM、未做任何写操作；确认无误后加 --execute 真正回填)")
    else:
        print(f"\n=== backfill EXECUTE: since={args.since} ===")
        print(f"  active VOC tags loaded: {summary['active_tag_count']}")
        print(f"  scanned={summary['scanned']} classified={summary['classified']} "
              f"fallback={summary['fallback_count']}")
        print("  by primary L1 group:")
        for group, n in summary["group_distribution"].items():
            print(f"    {group:<20} {n}")
        if summary["write_failures"]:
            print(f"\n  ⚠️  {len(summary['write_failures'])} row(s) classified but DB write failed:")
            for r in summary["write_failures"]:
                print(f"    analysis_id={r['analysis_id']} issue_id={r['issue_id']}")


if __name__ == "__main__":
    main()
