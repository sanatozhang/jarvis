"""存量回填：给已转交但群链接已过期的升级工单重新生成「永久」飞书群链接。

背景：升级建群时 `create_escalation_group` 调飞书 `chats/{chat_id}/link` 没传
validity_period，飞书默认 "week"（7 天后失效）。链接只在 escalate 时生成一次、
存进 `escalation_share_link`，前端直接当 <a href> 用，所以超过 7 天的升级工单
点「加入飞书群」就是「链接已失效」（例：fb_cd8638bc87）。

源头修复（create_chat_link 改 permanently）只对**未来**建的群生效；库里这批存量
旧链接不会自动刷新，故有此一次性脚本：用存库的 escalation_chat_id 重新生成一条
permanently 链接回填 escalation_share_link。群本身没变，只是换一张不过期的入场券。

安全约束：
- **默认 dry-run**，只列出待回填工单，不调飞书、不写 DB；`--execute` 才真跑。
- 只挑「已转交 + 未 resolved + 有 chat_id」的工单（resolved 的群已无需再进）。
- 幂等：可重复跑，每次只是把链接刷成最新的永久链接。

用法（容器内 / backend 目录）：
    python -m scripts.backfill_escalation_links            # dry-run，打印计划
    python -m scripts.backfill_escalation_links --execute  # 真正回填
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any, Dict, List

logger = logging.getLogger("oncall.backfill_escalation_links")


async def _select_targets() -> List[Dict[str, Any]]:
    """选出待回填工单：已转交 + in_progress（未 resolved）+ 有 chat_id。"""
    from app.db.database import get_escalated_issues

    rows = await get_escalated_issues(status="in_progress")
    return [r for r in rows if (r.get("escalation_chat_id") or "").strip()]


async def run_backfill(execute: bool = False) -> Dict[str, Any]:
    """回填主入口。execute=False 只预览，True 才真正调飞书 + 写 DB。"""
    from app.db.database import init_db, update_escalation_share_link
    from app.services.feishu_cli import create_chat_link

    await init_db()

    targets = await _select_targets()
    results: List[Dict[str, Any]] = []
    refreshed = 0

    for t in targets:
        issue_id = t["record_id"]
        chat_id = t["escalation_chat_id"]
        row: Dict[str, Any] = {
            "issue_id": issue_id,
            "chat_id": chat_id,
            "old_link": t.get("escalation_share_link") or "",
        }
        if not execute:
            logger.info("[dry-run] would refresh %s (chat %s)", issue_id, chat_id)
            results.append(row)
            continue

        link = await create_chat_link(chat_id, validity_period="permanently")
        if not link:
            row["status"] = "link_failed"
            logger.warning("[execute] %s: failed to generate link (chat %s)", issue_id, chat_id)
        elif await update_escalation_share_link(issue_id, link):
            row["status"] = "ok"
            row["new_link"] = link
            refreshed += 1
            logger.info("[execute] %s -> refreshed", issue_id)
        else:
            row["status"] = "db_failed"
            logger.warning("[execute] %s: link generated but DB update failed", issue_id)
        results.append(row)

    return {"targets": len(targets), "refreshed": refreshed, "results": results}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Backfill permanent Feishu group links for escalated tickets")
    parser.add_argument("--execute", action="store_true",
                        help="真正调飞书生成永久链接 + 写 DB（默认只 dry-run）")
    args = parser.parse_args()

    summary = asyncio.run(run_backfill(execute=args.execute))

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"\n=== backfill {mode}: {summary['targets']} target ticket(s) ===")
    if not args.execute:
        for r in summary["results"]:
            print(f"  {r['issue_id']:<16} chat={r['chat_id']:<20} "
                  f"old_link={'(empty)' if not r['old_link'] else r['old_link'][:48]}")
        print("\n(dry-run，未做任何写操作；确认无误后加 --execute 真正回填)")
    else:
        print(f"  refreshed={summary['refreshed']}/{summary['targets']}（详见日志）")
        for r in summary["results"]:
            if r.get("status") != "ok":
                print(f"  ⚠️  {r['issue_id']}: {r.get('status')}")


if __name__ == "__main__":
    main()
