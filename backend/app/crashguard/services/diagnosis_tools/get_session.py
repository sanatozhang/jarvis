#!/usr/bin/env python3
"""拉取 RUM session 完整事件流，供 AI agent 分析崩溃前用户行为。

用法: python tools/get_session.py --session-id <id> [--limit 100]
输出: JSON {session_id, event_count, events: [...]}
"""
from __future__ import annotations
import argparse
import json
import os
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    api_key = os.environ.get("CRASHGUARD_DATADOG_API_KEY", "")
    app_key = os.environ.get("CRASHGUARD_DATADOG_APP_KEY", "")
    site = os.environ.get("CRASHGUARD_DATADOG_SITE", "datadoghq.com")

    if not api_key:
        print(json.dumps({"error": "CRASHGUARD_DATADOG_API_KEY not set"}))
        return

    dql = f"@session.id:{args.session_id}"
    url = f"https://api.{site}/api/v2/rum/events/search"
    payload = json.dumps({
        "filter": {"query": dql},
        "page": {"limit": min(args.limit, 200)},
        "sort": "timestamp",
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("DD-API-KEY", api_key)
    req.add_header("DD-APPLICATION-KEY", app_key or api_key)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            events = data.get("data", [])
            slim = []
            for ev in events:
                attrs = ev.get("attributes", {})
                slim.append({
                    "timestamp": attrs.get("timestamp", ""),
                    "type": attrs.get("type", ""),
                    "action": attrs.get("action", {}).get("type", ""),
                    "view": attrs.get("view", {}).get("name", ""),
                    "error": attrs.get("error", {}).get("message", ""),
                    "duration_ms": attrs.get("duration", 0),
                })
            print(json.dumps({
                "session_id": args.session_id,
                "event_count": len(slim),
                "events": slim,
            }, ensure_ascii=False))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(json.dumps({"error": f"HTTP {e.code}", "detail": body}))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))


if __name__ == "__main__":
    main()
