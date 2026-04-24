"""
Context Condenser (L1.5) — LLM-powered log context extraction.

Sits between L1 (deterministic grep) and L2 (analysis agent).
Uses a large-context, cheap model to read windowed logs and produce
structured context, so the analysis agent doesn't need to grep raw logs.

Supported providers:
  - gemini (default): Gemini 2.5 Flash — cheapest, 1M context
  - anthropic: Claude Haiku 4.5 — fast, 200K context
  - openai: GPT-4.1 mini — 1M context
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("jarvis.context_condenser")

# Max chars to send to the LLM per chunk (~800K tokens for Gemini Flash)
_DEFAULT_MAX_INPUT_CHARS = 2_800_000
# Max chars for models with smaller context (Haiku: 200K tokens)
_SMALL_CONTEXT_MAX_CHARS = 600_000


@dataclass
class CondensationResult:
    """Output of the L1.5 context condensation."""
    success: bool = False
    # Structured extraction from LLM
    structured_context: Dict[str, Any] = field(default_factory=dict)
    # Raw text output from LLM (fallback if JSON parsing fails)
    raw_output: str = ""
    # Metadata
    provider: str = ""
    model: str = ""
    input_chars: int = 0
    output_chars: int = 0
    duration_ms: int = 0
    error: str = ""


@dataclass
class CondensationConfig:
    """Configuration for the context condenser."""
    enabled: bool = True
    provider: str = "gemini"  # gemini, anthropic, openai
    model: str = ""           # empty = use default per provider
    api_key: str = ""
    api_base_url: str = ""    # custom endpoint (optional)
    max_input_chars: int = _DEFAULT_MAX_INPUT_CHARS
    timeout: int = 120        # seconds
    temperature: float = 0.0  # deterministic extraction


# Default models per provider
_DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash-preview-05-20",
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4.1-mini",
}

_API_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "openai": "https://api.openai.com/v1/chat/completions",
}


class ContextCondenser:
    """L1.5 layer: extract structured context from logs using a large-context LLM."""

    def __init__(self, config: CondensationConfig):
        self.config = config

    async def condense(
        self,
        log_paths: List[Path],
        issue_description: str,
        device_sn: str = "",
        problem_date: Optional[str] = None,
        l1_extraction: Optional[Dict[str, Any]] = None,
        rules_summary: str = "",
    ) -> CondensationResult:
        """
        Read log files and extract structured context using LLM.

        Args:
            log_paths: Paths to (windowed) log files.
            issue_description: The user's problem description.
            device_sn: Device serial number.
            problem_date: When the problem occurred.
            l1_extraction: Output from L1 deterministic extraction.
            rules_summary: Brief summary of matched rules.

        Returns:
            CondensationResult with structured context.
        """
        if not self.config.enabled:
            return CondensationResult(error="context_condensation_disabled")

        if not self.config.api_key:
            logger.warning("No API key configured for context condensation (provider: %s)", self.config.provider)
            return CondensationResult(error="no_api_key")

        # Read log content
        log_content = self._read_logs(log_paths)
        if not log_content.strip():
            return CondensationResult(error="no_log_content")

        # Build extraction prompt
        prompt = self._build_prompt(
            log_content=log_content,
            issue_description=issue_description,
            device_sn=device_sn,
            problem_date=problem_date,
            l1_extraction=l1_extraction,
            rules_summary=rules_summary,
        )

        logger.info(
            "Context condensation: provider=%s, model=%s, input=%d chars (log=%d chars)",
            self.config.provider,
            self.config.model or _DEFAULT_MODELS.get(self.config.provider, "?"),
            len(prompt),
            len(log_content),
        )

        # Call LLM
        start = time.monotonic()
        try:
            result = await self._call_llm(prompt)
        except Exception as e:
            logger.error("Context condensation LLM call failed: %s", e)
            return CondensationResult(
                error=str(e),
                provider=self.config.provider,
                model=self.config.model or _DEFAULT_MODELS.get(self.config.provider, ""),
                input_chars=len(prompt),
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        result.duration_ms = duration_ms
        result.input_chars = len(prompt)

        logger.info(
            "Context condensation done: success=%s, output=%d chars, duration=%dms",
            result.success, result.output_chars, duration_ms,
        )

        return result

    def _read_logs(self, log_paths: List[Path]) -> str:
        """Read log files into a single string, respecting max input size."""
        max_chars = self.config.max_input_chars
        # Reserve ~30% for prompt structure, L1 extraction, etc.
        max_log_chars = int(max_chars * 0.70)

        parts = []
        total = 0
        for lp in log_paths:
            if not lp.exists():
                continue
            try:
                content = lp.read_text(encoding="utf-8", errors="replace")
                if total + len(content) > max_log_chars:
                    remaining = max_log_chars - total
                    if remaining > 10000:
                        content = content[:remaining] + "\n... [truncated for context limit] ...\n"
                    else:
                        break
                parts.append(f"=== {lp.name} ({len(content)} chars) ===\n{content}")
                total += len(content)
            except Exception as e:
                logger.warning("Failed to read %s: %s", lp, e)
        return "\n".join(parts)

    def _build_prompt(
        self,
        log_content: str,
        issue_description: str,
        device_sn: str,
        problem_date: Optional[str],
        l1_extraction: Optional[Dict[str, Any]],
        rules_summary: str,
    ) -> str:
        """Build the extraction prompt for the L1.5 model."""
        l1_summary = ""
        if l1_extraction:
            # Include a concise L1 summary to guide the model
            patterns = l1_extraction.get("patterns", {})
            nonzero = {k: v.get("match_count", 0) for k, v in patterns.items()
                       if isinstance(v, dict) and v.get("match_count", 0) > 0}
            deterministic_keys = list(l1_extraction.get("deterministic", {}).keys())
            l1_summary = f"""
## L1 自动提取摘要（参考方向）
- 有匹配的模式: {json.dumps(nonzero, ensure_ascii=False) if nonzero else '(无)'}
- 结构化数据块: {', '.join(deterministic_keys) if deterministic_keys else '(无)'}
"""

        return f"""你是一个日志分析预处理器。你的任务是从原始日志中提取与用户问题相关的所有关键信息，生成结构化的分析上下文。

## 工单信息

- **问题描述**: {issue_description}
- **设备SN**: {device_sn or '未知'}
- **问题日期**: {problem_date or '未知'}
{f'- **规则提示**: {rules_summary}' if rules_summary else ''}
{l1_summary}

## 你的任务

仔细阅读下面的日志内容，提取所有与问题相关的信息。输出一个 JSON 对象，包含以下字段：

```json
{{
    "event_timeline": [
        {{
            "time": "HH:MM:SS",
            "event": "事件描述",
            "raw_log": "原始日志行（关键的1-2行）",
            "relevance": "high/medium/low"
        }}
    ],
    "errors_and_warnings": [
        {{
            "time": "HH:MM:SS",
            "level": "error/warning/crash",
            "message": "错误信息",
            "context": "前后相关日志（2-3行）",
            "count": 1
        }}
    ],
    "device_state": {{
        "app_version": "",
        "firmware_version": "",
        "device_model": "",
        "os_version": "",
        "bluetooth_state": "",
        "network_state": "",
        "other": {{}}
    }},
    "key_log_sections": [
        {{
            "title": "段落标题（如：蓝牙连接过程、录音传输过程）",
            "time_range": "HH:MM:SS - HH:MM:SS",
            "content": "完整的关键日志段落（保留原始格式，每段最多50行）",
            "why_relevant": "为什么这段日志与问题相关"
        }}
    ],
    "summary": "一段话总结日志中发现的关键信息和可能的问题方向（3-5句话）"
}}
```

## 重要规则

1. **保留原始日志行**: key_log_sections 中的 content 必须是原始日志的直接复制，不要改写
2. **关注问题相关性**: 只提取与问题描述相关的内容，忽略无关的正常日志
3. **时间线完整**: event_timeline 应该覆盖问题发生前后的关键事件
4. **错误优先**: 所有 error、exception、failure、crash 都要记录
5. **上下文充足**: key_log_sections 每段保留足够上下文（前后各几行），不要只截取单行
6. **只输出 JSON**: 不要输出任何其他内容，只输出上面格式的 JSON

## 日志内容

{log_content}
"""

    async def _call_llm(self, prompt: str) -> CondensationResult:
        """Call the LLM API based on configured provider."""
        provider = self.config.provider
        model = self.config.model or _DEFAULT_MODELS.get(provider, "")

        if provider == "gemini":
            return await self._call_gemini(prompt, model)
        elif provider == "anthropic":
            return await self._call_anthropic(prompt, model)
        elif provider == "openai":
            return await self._call_openai(prompt, model)
        else:
            return CondensationResult(error=f"unknown_provider: {provider}")

    async def _call_gemini(self, prompt: str, model: str) -> CondensationResult:
        """Call Google Gemini API."""
        url = _API_URLS["gemini"].format(model=model)
        params = {"key": self.config.api_key}
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self.config.temperature,
                "responseMimeType": "application/json",
            },
        }

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.post(url, params=params, json=body)
            resp.raise_for_status()
            data = resp.json()

        # Extract text from Gemini response
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            return CondensationResult(
                error=f"gemini_parse_error: {e}",
                provider="gemini", model=model,
            )

        return self._parse_llm_output(text, provider="gemini", model=model)

    async def _call_anthropic(self, prompt: str, model: str) -> CondensationResult:
        """Call Anthropic Messages API."""
        url = self.config.api_base_url or _API_URLS["anthropic"]
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": 16384,
            "temperature": self.config.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        try:
            text = data["content"][0]["text"]
        except (KeyError, IndexError) as e:
            return CondensationResult(
                error=f"anthropic_parse_error: {e}",
                provider="anthropic", model=model,
            )

        return self._parse_llm_output(text, provider="anthropic", model=model)

    async def _call_openai(self, prompt: str, model: str) -> CondensationResult:
        """Call OpenAI Chat Completions API."""
        url = self.config.api_base_url or _API_URLS["openai"]
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "temperature": self.config.temperature,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            return CondensationResult(
                error=f"openai_parse_error: {e}",
                provider="openai", model=model,
            )

        return self._parse_llm_output(text, provider="openai", model=model)

    def _parse_llm_output(self, text: str, provider: str, model: str) -> CondensationResult:
        """Parse the LLM output text into structured context."""
        result = CondensationResult(
            raw_output=text,
            provider=provider,
            model=model,
            output_chars=len(text),
        )

        # Try to parse as JSON
        parsed = _extract_json(text)
        if parsed:
            result.structured_context = parsed
            result.success = True
        else:
            logger.warning("Failed to parse L1.5 output as JSON (%d chars)", len(text))
            result.error = "json_parse_failed"
            # Still usable as raw text
            result.success = False

        return result


def _extract_json(text: str) -> Optional[Dict]:
    """Extract JSON from text, handling markdown code blocks."""
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from ```json ... ``` block
    m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding the largest { ... } block
    brace_depth = 0
    start = -1
    candidates = []
    for i, ch in enumerate(text):
        if ch == "{":
            if brace_depth == 0:
                start = i
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0 and start >= 0:
                candidates.append(text[start:i + 1])
                start = -1

    for block in sorted(candidates, key=len, reverse=True):
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            # Try fixing trailing commas
            try:
                fixed = re.sub(r",\s*([}\]])", r"\1", block)
                return json.loads(fixed)
            except json.JSONDecodeError:
                continue

    return None
