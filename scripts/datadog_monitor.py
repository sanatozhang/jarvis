#!/usr/bin/env python3
"""Datadog monitor CLI：sync / list / mute / dry-run。

用法（在 jarvis/backend 下）:
  python ../scripts/datadog_monitor.py sync            # 同步全部 def（create/update）
  python ../scripts/datadog_monitor.py sync --dry-run  # 只打印 payload
  python ../scripts/datadog_monitor.py list            # 列出 source:coreguard 的 monitor
  python ../scripts/datadog_monitor.py mute --id 123
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.coreguard.monitors.client import DatadogMonitorClient  # noqa: E402
from app.coreguard.monitors.sync import sync_all                 # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")


class _NoopClient:
    """dry-run 占位：sync_def(dry_run=True) 不会调用任何方法。"""
    pass


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--dry-run", action="store_true")

    sub.add_parser("list")

    p_mute = sub.add_parser("mute")
    p_mute.add_argument("--id", type=int, required=True)

    args = parser.parse_args()

    if args.cmd == "sync":
        client = _NoopClient() if args.dry_run else DatadogMonitorClient()
        sync_all(client, dry_run=args.dry_run)
    elif args.cmd == "list":
        for m in DatadogMonitorClient().list(monitor_tags="source:coreguard"):
            print(m.get("id"), m.get("name"), "| overall:", m.get("overall_state"))
    elif args.cmd == "mute":
        DatadogMonitorClient().mute(args.id)
        print("muted", args.id)


if __name__ == "__main__":
    main()
