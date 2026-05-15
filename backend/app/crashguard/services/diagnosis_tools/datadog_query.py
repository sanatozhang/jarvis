#!/usr/bin/env python3
"""Datadog RUM 事件查询工具，供 AI agent 通过 Bash 调用。

用法: python tools/datadog_query.py --dql "<DQL 语句>" [--limit 50]
输出: JSON（事件列表或 error）
"""
from __future__ import annotations
import argparse
import json
import os
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dql", required=True, help="Datadog search query")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    api_key = os.environ.get("CRASHGUARD_DATADOG_API_KEY", "")
    app_key = os.environ.get("CRASHGUARD_DATADOG_APP_KEY", "")
    site = os.environ.get("CRASHGUARD_DATADOG_SITE", "datadoghq.com")

    if not api_key:
        print(json.dumps({"error": "CRASHGUARD_DATADOG_API_KEY not set"}))
        return

    url = f"https://api.{site}/api/v2/rum/events/search"
    payload = json.dumps({
        "filter": {"query": args.dql},
        "page": {"limit": min(args.limit, 100)},
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("DD-API-KEY", api_key)
    req.add_header("DD-APPLICATION-KEY", app_key or api_key)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            events = data.get("data", [])
            print(json.dumps({
                "count": len(events),
                "events": events[:args.limit],
            }, ensure_ascii=False))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(json.dumps({"error": f"HTTP {e.code}", "detail": body}))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))


if __name__ == "__main__":
    main()
