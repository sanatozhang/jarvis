#!/usr/bin/env python3
"""查询历史相似 crash 的根因分析和修复方案，供 AI agent 复用经验。

用法: python tools/find_similar.py --fingerprint <sha1> [--limit 5]
输出: JSON {results: [{datadog_issue_id, root_cause, fix_suggestion, fix_diff, confidence, created_at}]}
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    # 解析 DB 路径
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("sqlite:///"):
        db_path = db_url[10:]
    else:
        ws_dir = os.environ.get("WORKSPACE_DIR", "workspaces")
        parent = os.path.abspath(os.path.join(ws_dir, "..", "data", "appllo.db"))
        if os.path.exists(parent):
            db_path = parent
        else:
            print(json.dumps({"error": "cannot locate database", "results": []}))
            return

    if not os.path.exists(db_path):
        print(json.dumps({"error": f"db not found: {db_path}", "results": []}))
        return

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT datadog_issue_id FROM crash_issues WHERE stack_fingerprint = ? LIMIT 10",
            (args.fingerprint,),
        )
        issue_ids = [r[0] for r in cur.fetchall()]
        if not issue_ids:
            print(json.dumps({"fingerprint": args.fingerprint, "results": []}))
            conn.close()
            return
        placeholders = ",".join("?" * len(issue_ids))
        cur = conn.execute(
            f"""SELECT datadog_issue_id, root_cause, fix_suggestion, fix_diff,
                       confidence, created_at
                FROM crash_analyses
                WHERE datadog_issue_id IN ({placeholders})
                  AND status = 'success'
                  AND root_cause != ''
                ORDER BY created_at DESC
                LIMIT ?""",
            issue_ids + [args.limit],
        )
        results = [dict(r) for r in cur.fetchall()]
        for r in results:
            r["root_cause"] = (r.get("root_cause") or "")[:500]
            r["fix_suggestion"] = (r.get("fix_suggestion") or "")[:500]
            r["fix_diff"] = (r.get("fix_diff") or "")[:800]
        conn.close()
        print(json.dumps({"fingerprint": args.fingerprint, "results": results}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "results": []}))


if __name__ == "__main__":
    main()
