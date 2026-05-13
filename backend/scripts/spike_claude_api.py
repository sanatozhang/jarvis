"""End-to-end spike: run ClaudeApiAgent against the real Vertex proxy with
a tiny synthetic workspace. Prints the trace and verifies result.json is
written.

Usage:
    cd backend && PYTHONPATH=. python scripts/spike_claude_api.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure project .env is loaded
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from app.agents.base import AgentConfig
from app.agents.claude_api import ClaudeApiAgent

PROMPT = """You are a log analysis assistant. Your task:

1. Use the grep tool to search for the pattern "BLE.*timeout" in the logs/ directory.
2. Use read_file to read the matching line in context (offset/limit are fine).
3. Use write_file to write `output/result.json` with the following exact JSON:
   {"problem_type": "BLE Timeout", "root_cause": "Bluetooth GATT connection timeout observed at 10:00:05.", "confidence": "high", "user_reply": "Your device's Bluetooth pairing failed because the GATT connection timed out after 30 seconds. Please retry pairing in a quieter RF environment."}
4. End your turn after writing the file.

Be efficient — minimize turns.
"""

async def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(2)

    workspace = Path("/tmp/jarvis_spike_workspace")
    if workspace.exists():
        import shutil
        shutil.rmtree(workspace)
    (workspace / "logs").mkdir(parents=True)
    (workspace / "logs" / "main.log").write_text(
        "2026-05-13 10:00:00 BLE connect start\n"
        "2026-05-13 10:00:05 BLE GATT connection timeout after 30s\n"
        "2026-05-13 10:00:10 BLE reconnect\n",
        encoding="utf-8",
    )
    (workspace / "output").mkdir()

    config = AgentConfig(
        agent_type="claude_api",
        model="claude-sonnet-4-6",
        fallback_model="claude-haiku-4-5",
        base_url="http://34.216.169.232:30001/vertex",
        api_key=api_key,
        per_turn_timeout=60,
        max_tokens=2048,
        max_turns=10,
        enable_cache=True,
    )

    print(f"=== Spike: ClaudeApiAgent against real proxy ===")
    print(f"workspace: {workspace}")
    print(f"base_url:  {config.base_url}")
    print(f"model:     {config.model}")
    print()

    agent = ClaudeApiAgent(config)

    async def on_progress(pct, msg):
        print(f"  [{pct}%] {msg}")

    result = await agent.analyze(workspace=workspace, prompt=PROMPT, on_progress=on_progress)

    print()
    print("=== Final result ===")
    print(f"agent_type:   {result.agent_type}")
    print(f"problem_type: {result.problem_type}")
    print(f"confidence:   {result.confidence}")
    print(f"root_cause:   {result.root_cause[:200]}")
    print()
    print("=== Agent trace (output/agent_trace.jsonl) ===")
    trace_path = workspace / "output" / "agent_trace.jsonl"
    for line in trace_path.read_text().splitlines():
        rec = json.loads(line)
        if "event" in rec:
            print(f"  EVENT: {rec.get('event')} model={rec.get('model','-')} prompt_chars={rec.get('prompt_chars','-')} parsed={rec.get('parsed_problem_type','-')}")
        else:
            calls = rec.get("tool_calls") or []
            usage = rec.get("usage") or {}
            print(f"  Turn {rec['turn']}: stop={rec.get('stop_reason')} "
                  f"tools={[c['name'] for c in calls]} "
                  f"tok={usage.get('input_tokens',0)}->{usage.get('output_tokens',0)} "
                  f"cache={usage.get('cache_read_input_tokens',0)}r/{usage.get('cache_creation_input_tokens',0)}c "
                  f"dur={rec.get('duration_ms',0)}ms")
    print()
    print("=== output/result.json ===")
    rp = workspace / "output" / "result.json"
    if rp.exists():
        print(rp.read_text())
    else:
        print("(not written)")


if __name__ == "__main__":
    asyncio.run(main())
