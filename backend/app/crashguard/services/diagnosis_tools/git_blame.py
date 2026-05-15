#!/usr/bin/env python3
"""git blame 单行工具，供 AI agent 查询代码行的提交历史。

用法: python tools/git_blame.py --file <相对路径> --line <行号> [--repo-path <绝对路径>]
输出: JSON {commit, author, date, summary, line_content}
"""
from __future__ import annotations
import argparse
import json
import subprocess
import os


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="相对 repo 根的文件路径")
    parser.add_argument("--line", type=int, required=True, help="行号（1-based）")
    parser.add_argument("--repo-path", default=".", help="git repo 根目录")
    args = parser.parse_args()

    cwd = os.path.abspath(args.repo_path)
    try:
        r = subprocess.run(
            ["git", "blame", "-L", f"{args.line},{args.line}", "--porcelain", args.file],
            cwd=cwd, capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            print(json.dumps({"error": r.stderr.strip()[:300]}))
            return
        lines = r.stdout.splitlines()
        if not lines:
            print(json.dumps({"error": "no output from git blame"}))
            return
        commit = lines[0].split()[0] if lines else ""
        info: dict = {"commit": commit, "author": "", "date": "", "summary": "", "line_content": ""}
        for ln in lines[1:]:
            if ln.startswith("author "):
                info["author"] = ln[7:].strip()
            elif ln.startswith("author-time "):
                import datetime
                info["date"] = datetime.datetime.fromtimestamp(
                    int(ln[12:].strip())
                ).strftime("%Y-%m-%d")
            elif ln.startswith("summary "):
                info["summary"] = ln[8:].strip()
            elif ln.startswith("\t"):
                info["line_content"] = ln[1:]
        print(json.dumps(info, ensure_ascii=False))
    except FileNotFoundError:
        print(json.dumps({"error": "git not found in PATH"}))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))


if __name__ == "__main__":
    main()
