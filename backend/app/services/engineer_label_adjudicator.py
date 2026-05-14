"""
T2: needs_engineer 二次 LLM 复核层

底层逻辑：
主分析 agent 经常自相矛盾——既给了完整 user_reply 又把 needs_engineer 设为 true。
本模块用一个廉价模型（claude-haiku-4-5）专门对 needs_engineer=true 的工单做二次判定，
破解语义矛盾，把"看起来应该研发但其实客服自助能解决"的 case 转回 false。

抓手：
- 只对 needs_engineer=true 的工单调用（约 30% 流量），不增加全量成本
- 单次 < 1 分钱，每天 ~200 次调用 ≈ 每天 ¥1.5
- 安全降级：API 失败 / 超时 → 保持原判，绝不把 true 改成 false 引发漏判
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import httpx

from app.models.schemas import AnalysisResult

logger = logging.getLogger("jarvis.adjudicator")

_ADJUDICATOR_MODEL = "claude-haiku-4-5-20251001"
_ADJUDICATOR_TIMEOUT = 15.0
_ADJUDICATOR_URL = "https://api.anthropic.com/v1/messages"

_PROMPT_TEMPLATE = """你是 Plaud 工单分析的"工程师介入判定"复核员。

主分析 Agent 给了下面这份分析结果，并把 `needs_engineer` 标成 true。
但 AI 经常自相矛盾——已经给出完整客服回复模板，却又说要研发介入。
你的任务：判定这个工单**是否真的需要研发同学介入**。

# 判定规则（按顺序检查，命中即决定）

**判 false（不需要研发）**：
1. `user_reply` 完整（>100 字）且能让客服直接复制发用户 → false
2. 问题是用户操作问题、网络问题、设备充电问题、账号问题、付费问题 → false
3. `fix_suggestion` 是给用户的指引（重启、重连、重装 APP）而不是改代码 → false
4. 已经定位到具体根因且方案明确（如"清缓存"、"升级固件"）→ false

**判 true（确实需要研发）**：
1. 根因是代码 bug、需要发版修复 → true
2. 涉及后端服务/算法/固件层面、客服无法验证 → true
3. AI 明确说"无法定位"、"需要进一步排查"、"需要源码分析" → true
4. 涉及数据丢失且无法恢复、需要 DB 操作 → true

# 当前工单分析结果

**问题分类**：{problem_type}

**置信度**：{confidence}

**根因**：
{root_cause}

**客服回复模板**：
{user_reply}

**修复建议**：
{fix_suggestion}

# 输出要求

只输出一个 JSON，不要任何其他文字：

```json
{{"needs_engineer": true/false, "reason": "简短中文理由，30 字以内"}}
```
"""


async def adjudicate(result: AnalysisResult, api_key: str = "") -> Optional[dict]:
    """对 needs_engineer=true 的结果做二次复核。

    Returns:
        - {"needs_engineer": bool, "reason": str} 复核成功
        - None 复核失败 / API 不可用 / 超时 — 调用方应保持原判
    """
    if not result.needs_engineer:
        return None  # 只复核 true 的

    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.debug("adjudicator: no ANTHROPIC_API_KEY, skip")
        return None

    prompt = _PROMPT_TEMPLATE.format(
        problem_type=(result.problem_type or "未知")[:200],
        confidence=str(result.confidence),
        root_cause=(result.root_cause or "")[:1500],
        user_reply=(result.user_reply or "(空)")[:1500],
        fix_suggestion=(result.fix_suggestion or "(无)")[:500],
    )

    body = {
        "model": _ADJUDICATOR_MODEL,
        "max_tokens": 200,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    try:
        async with httpx.AsyncClient(timeout=_ADJUDICATOR_TIMEOUT) as client:
            resp = await client.post(_ADJUDICATOR_URL, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        text = data["content"][0]["text"]
    except Exception as e:
        logger.warning("adjudicator API call failed (保持原判): %s", e)
        return None

    # 抽取 JSON
    m = re.search(r"\{[^{}]*needs_engineer[^{}]*\}", text, re.DOTALL)
    if not m:
        logger.warning("adjudicator: no JSON in response: %s", text[:200])
        return None
    try:
        parsed = json.loads(m.group(0))
        new_value = bool(parsed.get("needs_engineer", True))
        reason = str(parsed.get("reason", ""))[:100]
        return {"needs_engineer": new_value, "reason": reason}
    except Exception as e:
        logger.warning("adjudicator: JSON parse failed: %s", e)
        return None


async def apply_adjudication(result: AnalysisResult) -> AnalysisResult:
    """对 result 做二次复核，原地修改。安全降级——失败不影响主流程。

    只在 needs_engineer=true 时触发；如果复核翻转为 false，
    把复核理由写到 confidence_reason 末尾留存证据。
    """
    if not result.needs_engineer:
        return result

    adj = await adjudicate(result)
    if adj is None:
        return result  # 复核失败 → 保持原判

    if adj["needs_engineer"] is False:
        # 翻转标签：记录复核理由
        original = result.needs_engineer
        result.needs_engineer = False
        suffix = f" [adjudicator: 翻转为 false — {adj['reason']}]"
        if result.confidence_reason and suffix not in result.confidence_reason:
            result.confidence_reason = (result.confidence_reason + suffix)[:2000]
        else:
            result.confidence_reason = (result.confidence_reason or "") + suffix
        logger.info(
            "adjudicator: needs_engineer %s → False (reason: %s)",
            original, adj["reason"],
        )
    else:
        logger.debug("adjudicator: kept needs_engineer=true (reason: %s)", adj["reason"])

    return result
