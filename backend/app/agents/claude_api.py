"""
Claude API agent — calls Anthropic Messages API directly (no `claude` CLI).

Talks to the company Vertex proxy at `base_url` using a custom auth header
(`x-api-key`). The proxy translates / forwards to Anthropic and bypasses
standard GCP auth.

Differences from `ClaudeCodeAgent`:
- No subprocess; pure async HTTP. Eliminates Node.js + CLI install.
- We own the tool loop. Tools defined in `app/agents/tools/`.
- Each turn is written to `output/agent_trace.jsonl` for diagnostics.
- Errors come back as typed HTTP responses (429, 529, 400) instead of stderr text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from app.agents.base import AgentConfig, BaseAgent
from app.agents.tools import TOOL_SCHEMAS, ToolError, execute_tool
from app.models.schemas import AnalysisResult

logger = logging.getLogger("jarvis.agent.claude_api")

# Vertex AI rawPredict format. base_url must already include
# `/v1/projects/{project}/locations/{location}` (the company proxy prepends
# this automatically). We only append the publisher/model segment.
_VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"
_VERTEX_MODEL_PATH_TPL = "/publishers/anthropic/models/{model}:rawPredict"


class _TraceWriter:
    """Append-only jsonl trace logger.

    Each call to `write` produces one line. Failures to write trace lines must
    never break analysis — they are logged at WARNING and swallowed.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate any pre-existing file from a previous attempt
        self.path.write_text("", encoding="utf-8")

    def write(self, record: Dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("trace write failed: %s", e)


class _MessagesClient:
    """Thin httpx wrapper for POST {base_url}{messages_path}.

    Uses x-api-key auth (company proxy convention; standard Anthropic also
    accepts x-api-key). Caller is responsible for `close()`.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float):
        if not base_url:
            raise ValueError("ClaudeApiAgent: base_url is empty")
        if not api_key:
            raise ValueError("ClaudeApiAgent: api_key is empty (set ANTHROPIC_API_KEY)")
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "x-api-key": api_key,
                "content-type": "application/json",
            },
        )

    async def create_message(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Send a Vertex-format message and return the parsed response.

        The Vertex AI rawPredict endpoint takes the model name in the URL
        path, NOT in the body. Caller passes `body["model"]` as a convenience;
        we pop it here and rewrite the body to Vertex shape.
        """
        body = dict(body)  # don't mutate caller's dict
        model = body.pop("model", "")
        if not model:
            raise ValueError("create_message: body must include 'model'")
        body["anthropic_version"] = _VERTEX_ANTHROPIC_VERSION

        url = self._base_url + _VERTEX_MODEL_PATH_TPL.format(model=model)
        resp = await self._client.post(url, json=body)
        if resp.status_code >= 400:
            # Raise with the response text attached so caller can map error types
            raise _HttpError(resp.status_code, resp.text)
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()


class _HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


# ── Error classification ────────────────────────────────────────────────────
_RATE_LIMIT_KEYWORDS = ("rate_limit", "quota", "credit", "billing")
_OVERLOADED_KEYWORDS = ("overloaded", "overload_error")


def _is_rate_limit(err: _HttpError) -> bool:
    if err.status == 429:
        return True
    body = err.body.lower()
    return any(k in body for k in _RATE_LIMIT_KEYWORDS)


def _is_overloaded(err: _HttpError) -> bool:
    if err.status == 529:
        return True
    body = err.body.lower()
    return any(k in body for k in _OVERLOADED_KEYWORDS)


# ── Agent ───────────────────────────────────────────────────────────────────
class ClaudeApiAgent(BaseAgent):
    """Agent that calls the Anthropic Messages API directly via httpx."""

    async def analyze(
        self,
        workspace: Path,
        prompt: str,
        on_progress: Optional[Callable[[int, str], Any]] = None,
    ) -> AnalysisResult:
        cfg = self.config
        trace = _TraceWriter(workspace / "output" / "agent_trace.jsonl")
        trace.write({
            "event": "start",
            "model": cfg.model,
            "max_turns": cfg.max_turns,
            "prompt_chars": len(prompt),
            "ts": time.time(),
        })

        # Save prompt for debugging parity with ClaudeCodeAgent
        try:
            (workspace / "prompt.md").write_text(prompt, encoding="utf-8")
        except Exception:
            pass

        client = _MessagesClient(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            timeout=float(cfg.per_turn_timeout),
        )

        # Initial user message — first content block carries cache_control so
        # subsequent turns hit the prefix cache (rules + context don't change).
        first_block: Dict[str, Any] = {"type": "text", "text": prompt}
        if cfg.enable_cache:
            first_block["cache_control"] = {"type": "ephemeral"}
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": [first_block]},
        ]

        current_model = cfg.model
        last_stop_reason = ""

        try:
            if on_progress:
                await _maybe_await(on_progress(60, "Claude API 分析中..."))

            for turn in range(cfg.max_turns):
                t0 = time.perf_counter()
                body = {
                    "model": current_model,
                    "max_tokens": cfg.max_tokens,
                    "tools": TOOL_SCHEMAS,
                    "messages": messages,
                }
                try:
                    resp = await asyncio.wait_for(
                        client.create_message(body),
                        timeout=cfg.per_turn_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Turn %d: messages.create timed out after %ds", turn, cfg.per_turn_timeout)
                    trace.write({
                        "turn": turn,
                        "error": "per_turn_timeout",
                        "duration_ms": int((time.perf_counter() - t0) * 1000),
                    })
                    break
                except _HttpError as e:
                    if _is_rate_limit(e):
                        logger.error("Turn %d: rate limit hit. body=%s", turn, e.body[:300])
                        trace.write({"turn": turn, "error": "rate_limit", "status": e.status, "msg": e.body[:500]})
                        return _quota_exhausted_result(e.body)
                    if _is_overloaded(e):
                        logger.warning("Turn %d: overloaded, switching to fallback model %s", turn, cfg.fallback_model or "(none)")
                        trace.write({"turn": turn, "error": "overloaded", "status": e.status})
                        if cfg.fallback_model and current_model != cfg.fallback_model:
                            current_model = cfg.fallback_model
                            continue  # retry same turn with fallback model
                        break
                    logger.error("Turn %d: HTTP %d. body=%s", turn, e.status, e.body[:500])
                    trace.write({"turn": turn, "error": "http_error", "status": e.status, "msg": e.body[:500]})
                    break
                except httpx.RequestError as e:
                    logger.error("Turn %d: network error: %s", turn, e)
                    trace.write({"turn": turn, "error": "network", "msg": str(e)})
                    break

                stop_reason = resp.get("stop_reason", "")
                last_stop_reason = stop_reason
                usage = resp.get("usage", {}) or {}
                content_blocks = resp.get("content", []) or []

                # Append assistant message verbatim so tool_use_id stays consistent
                messages.append({"role": "assistant", "content": content_blocks})

                if stop_reason == "tool_use":
                    tool_calls_log: List[Dict[str, Any]] = []
                    tool_results: List[Dict[str, Any]] = []
                    for block in content_blocks:
                        if block.get("type") != "tool_use":
                            continue
                        name = block.get("name", "")
                        tool_input = block.get("input", {}) or {}
                        try:
                            result = await execute_tool(name, tool_input, workspace=workspace)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.get("id"),
                                "content": result.content,
                            })
                            tool_calls_log.append({
                                "name": name,
                                "input": _summarize_input(tool_input),
                                "ok": True,
                                "summary": result.result_summary,
                            })
                        except ToolError as e:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.get("id"),
                                "content": f"ERROR: {e}",
                                "is_error": True,
                            })
                            tool_calls_log.append({
                                "name": name,
                                "input": _summarize_input(tool_input),
                                "ok": False,
                                "error": str(e),
                            })
                        except Exception as e:
                            logger.exception("Turn %d: unexpected tool error %s", turn, name)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.get("id"),
                                "content": f"INTERNAL ERROR: {e}",
                                "is_error": True,
                            })
                            tool_calls_log.append({
                                "name": name,
                                "input": _summarize_input(tool_input),
                                "ok": False,
                                "error": f"internal: {e}",
                            })

                    messages.append({"role": "user", "content": tool_results})

                    trace.write({
                        "turn": turn,
                        "stop_reason": "tool_use",
                        "model": current_model,
                        "tool_calls": tool_calls_log,
                        "usage": usage,
                        "duration_ms": int((time.perf_counter() - t0) * 1000),
                    })

                    if on_progress:
                        names = ",".join(c["name"] for c in tool_calls_log)
                        pct = min(60 + turn + 1, 89)
                        await _maybe_await(on_progress(pct, f"Turn {turn+1}: {names}"))
                    continue

                # Non tool_use stop: end_turn / max_tokens / stop_sequence / pause_turn
                trace.write({
                    "turn": turn,
                    "stop_reason": stop_reason,
                    "model": current_model,
                    "usage": usage,
                    "duration_ms": int((time.perf_counter() - t0) * 1000),
                    "final_text_chars": sum(
                        len(b.get("text", "")) for b in content_blocks if b.get("type") == "text"
                    ),
                })
                break

            if on_progress:
                await _maybe_await(on_progress(90, "解析分析结果..."))

            # Collect any text the assistant produced for parse_result salvage path
            raw_text_chunks: List[str] = []
            for msg in messages:
                if msg.get("role") != "assistant":
                    continue
                for block in msg.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        raw_text_chunks.append(block.get("text", ""))
            raw_output = "\n".join(raw_text_chunks)[:20_000]

            result = self.parse_result(workspace, raw_output)
            result.agent_type = "claude_api"
            trace.write({
                "event": "end",
                "stop_reason": last_stop_reason,
                "parsed_problem_type": result.problem_type,
                "parsed_confidence": str(result.confidence),
                "ts": time.time(),
            })
            return result

        finally:
            await client.close()


def _summarize_input(inp: Dict[str, Any]) -> Dict[str, Any]:
    """Keep tool inputs short for the trace (pattern/path stay, content gets truncated)."""
    out: Dict[str, Any] = {}
    for k, v in inp.items():
        if isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + f"...[+{len(v)-200} chars]"
        else:
            out[k] = v
    return out


def _quota_exhausted_result(raw_err: str) -> AnalysisResult:
    return AnalysisResult(
        task_id="",
        issue_id="",
        problem_type="Claude API Quota Exhausted",
        problem_type_en="Claude API Quota Exhausted",
        root_cause=(
            "Claude API quota has been exhausted; analysis could not complete.\n\n"
            f"Original error: {raw_err[:500]}\n\n"
            "Please check account balance or wait for quota reset, then retry."
        ),
        root_cause_en=(
            "Claude API quota has been exhausted; analysis could not complete.\n\n"
            f"Original error: {raw_err[:500]}\n\n"
            "Please check account balance or wait for quota reset, then retry."
        ),
        confidence="low",
        needs_engineer=False,
        system_failure=True,
        agent_type="claude_api",
    )


async def _maybe_await(val):
    if asyncio.iscoroutine(val):
        await val
