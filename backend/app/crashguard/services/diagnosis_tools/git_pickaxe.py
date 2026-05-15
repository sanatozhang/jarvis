#!/usr/bin/env python3
"""git log -S 搜索关键词引入时机，供 AI agent 查明"是谁/何时引入了这段代码"。

用法: python tools/git_pickaxe.py --keyword <字符串> [--repo-path <路径>] [--limit 20]
输出: JSON {commits: [{hash, author, date, subject}]}
"""
from __future__ import annotations
import argparse
import json
import subprocess
import os


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    cwd = os.path.abspath(args.repo_path)
    if not os.path.isdir(cwd):
        print(json.dumps({"error": f"repo-path does not exist: {cwd}", "commits": []}))
        return

    try:
        r = subprocess.run(
            [
                "git", "log", f"-S{args.keyword}",
                f"--max-count={args.limit}",
                "--pretty=format:%H|%an|%ad|%s",
                "--date=short",
            ],
            cwd=cwd, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            print(json.dumps({"error": r.stderr.strip()[:300], "commits": []}))
            return
        commits = []
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({
                    "hash": parts[0][:8],
                    "author": parts[1],
                    "date": parts[2],
                    "subject": parts[3],
                })
        print(json.dumps({"keyword": args.keyword, "commits": commits}, ensure_ascii=False))
    except FileNotFoundError:
        print(json.dumps({"error": "git not found in PATH", "commits": []}))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "commits": []}))


if __name__ == "__main__":
    main()
