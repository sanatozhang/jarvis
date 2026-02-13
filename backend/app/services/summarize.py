"""
Summarize Zendesk ticket conversations using OpenAI ChatGPT.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import httpx

logger = logging.getLogger("jarvis.summarize")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_SUMMARY_MODEL", "gpt-4o-mini")


async def summarize_ticket_conversation(
    ticket_subject: str,
    comments: List[Dict[str, Any]],
) -> Dict[str, str]:
    """
    Use ChatGPT to summarize a Zendesk ticket conversation.

    Returns:
        {
            "description": "问题描述（AI 总结）",
            "category": "问题分类建议",
            "priority": "H 或 L",
            "device_sn": "从对话中提取的设备 SN（如有）",
            "firmware": "从对话中提取的固件版本（如有）",
            "app_version": "从对话中提取的 APP 版本（如有）",
        }
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")

    # Build conversation text
    conv_lines = []
    for c in comments:
        role = "客服" if not c.get("public", True) else "用户/客服"
        body = c.get("body", "")[:500]
        conv_lines.append(f"[{role}] {body}")

    conversation = "\n\n".join(conv_lines)

    prompt = f"""你是一个客服工单分析助手。请根据以下 Zendesk 工单的聊天记录，提取和总结关键信息。

## 工单标题
{ticket_subject}

## 聊天记录
{conversation}

## 请按以下 JSON 格式输出（不要包含 markdown 代码块标记）：
{{
    "description": "用 2-5 句话总结用户的核心问题，包括问题现象、发生时间、操作上下文等",
    "category": "从以下选项中选一个最匹配的分类：硬件交互（蓝牙连接，固件升级，文件传输，音频播放等）/ 文件首页 / 文件管理（转写，总结，文件编辑等）/ 用户系统与管理 / 商业化（会员购买等）/ 其他通用模块 / iZYREC 硬件问题",
    "priority": "根据问题严重程度判断：H（高，影响核心功能）或 L（低，体验问题）",
    "device_sn": "从对话中提取的设备序列号（SN），没有则为空字符串",
    "firmware": "从对话中提取的固件版本号，没有则为空字符串",
    "app_version": "从对话中提取的APP版本号，没有则为空字符串"
}}"""

    async with httpx.AsyncClient(timeout=60) as http:
        resp = await http.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": "你是一个专业的客服工单分析助手。请严格按照要求的 JSON 格式输出。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 1000,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    logger.info("ChatGPT summary response: %s", content[:200])

    # Parse JSON from response
    import json
    import re

    # Remove markdown code block if present
    content = re.sub(r"```json\s*\n?", "", content)
    content = re.sub(r"```\s*$", "", content)
    content = content.strip()

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Failed to parse ChatGPT response as JSON: %s", content[:200])
        result = {
            "description": content[:500],
            "category": "",
            "priority": "L",
            "device_sn": "",
            "firmware": "",
            "app_version": "",
        }

    return result
