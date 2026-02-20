"""
Abstract base class for analysis agents.

All agent implementations (Claude Code, Codex, etc.) conform to this interface.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.models.schemas import AnalysisResult, Confidence, Issue, Rule

logger = logging.getLogger("jarvis.agent")


@dataclass
class AgentConfig:
    """Configuration for an agent session."""
    agent_type: str                  # "claude_code" or "codex"
    model: str = ""
    timeout: int = 300
    max_turns: int = 25
    allowed_tools: List[str] = field(default_factory=list)
    approval_mode: str = "auto-edit"


class BaseAgent(ABC):
    """Abstract agent interface."""

    def __init__(self, config: AgentConfig):
        self.config = config

    @abstractmethod
    async def analyze(
        self,
        workspace: Path,
        prompt: str,
        on_progress: Optional[Callable[[int, str], Any]] = None,
    ) -> AnalysisResult:
        """
        Run analysis in the given workspace.

        Args:
            workspace: Path to the prepared workspace directory.
            prompt: The full analysis prompt.
            on_progress: Optional callback(progress_pct, message).

        Returns:
            Parsed AnalysisResult.
        """
        ...

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------
    @staticmethod
    def build_prompt(
        issue: Issue,
        rules: List[Rule],
        extraction: Dict[str, Any],
        problem_date: Optional[str] = None,
        has_logs: bool = True,
    ) -> str:
        """Build the master prompt for the agent."""

        rules_section = ""
        for rule in rules:
            rules_section += f"\n### 规则: {rule.meta.name or rule.meta.id}\n\n"
            rules_section += rule.content + "\n"

        extraction_json = json.dumps(extraction, ensure_ascii=False, indent=2)

        if has_logs:
            role_and_principles = f"""你是 Plaud 设备日志分析专家，专门帮助客服团队分析用户工单。
你的分析结果将直接展示给客服人员，他们会复制你生成的回复模板发送给用户。

## 重要原则

1. **先看预提取结果**：L1 层已经自动提取了关键日志行，先基于这些信息判断
2. **不够再 grep**：只有预提取信息不足时，才使用 grep 进一步分析 logs/ 目录下的日志
3. **规则优先**：严格按照 rules/ 下的规则文件中的排查步骤执行
4. **结果必须写文件**：分析完成后必须将 JSON 结果写入 output/result.json"""

            extraction_section = f"""## 预提取结果（L1 层已自动提取）

以下是根据规则中的 grep 模式从日志中提取的关键信息。
- 如果 match_count > 0，说明日志中有匹配的内容，请仔细阅读 matches 数组
- 如果 match_count = 0，说明日志中没有相关记录

```json
{extraction_json}
```"""

            workspace_section = """## 工作空间结构

```
logs/         ← 解密后的日志文件，可以直接 grep
rules/        ← 规则文件，供参考
code/         ← 代码仓库（如果存在），可搜索代码定位问题
output/       ← 请将 result.json 写入此目录
```"""
        else:
            role_and_principles = f"""你是 Plaud 产品和技术专家，专门帮助客服团队解答用户疑问。
**注意：本工单没有提供日志文件**，你需要基于问题描述、代码仓库和产品知识来分析和回答。
你的分析结果将直接展示给客服人员，他们会复制你生成的回复模板发送给用户。

## 重要原则

1. **代码优先**：查看 code/ 目录下的代码仓库，理解产品功能和设计逻辑
2. **规则参考**：阅读 rules/ 下的规则文件，了解常见问题和解决方案
3. **基于经验**：结合产品知识，给出专业的解答和建议
4. **结果必须写文件**：分析完成后必须将 JSON 结果写入 output/result.json
5. **无法确认时说明**：如果没有日志无法确认根因，在回复中说明需要用户提供日志进一步排查"""

            extraction_section = """## 日志情况

**本工单未提供日志文件。** 请仅基于问题描述、代码和规则进行分析。
如果问题需要日志才能定位，请在 user_reply 中引导用户提供日志。"""

            workspace_section = """## 工作空间结构

```
rules/        ← 规则文件，供参考
code/         ← 代码仓库（如果存在），可搜索代码定位问题
output/       ← 请将 result.json 写入此目录
```"""

        prompt = f"""{role_and_principles}

## 工单信息

- **问题描述**: {issue.description}
- **设备SN**: {issue.device_sn}
- **固件版本**: {issue.firmware}
- **APP版本**: {issue.app_version}
- **Zendesk**: {issue.zendesk}
{f"- **问题日期**: {problem_date}" if problem_date else ""}

## 分析规则

请先阅读 rules/ 目录下的规则文件，严格按照规则中的排查步骤执行分析。
以下是规则摘要：

{rules_section}

{extraction_section}

{workspace_section}

## 输出要求

分析完成后，请将结果以 JSON 格式写入 `output/result.json`。
**重要：root_cause 和 user_reply 必须同时提供中文和英文两个版本。**

```json
{{
    "problem_type": "问题分类（中文）",
    "problem_type_en": "Problem Type (English)",
    "root_cause": "根本原因详细分析（中文，2-5 句话）",
    "root_cause_en": "Root cause analysis (English, 2-5 sentences)",
    "confidence": "high 或 medium 或 low",
    "confidence_reason": "为什么是这个置信度",
    "key_evidence": ["关键日志行1", "关键日志行2（最多5条）"],
    "user_reply": "完整的中文客服回复模板（见下方示例）",
    "user_reply_en": "Complete English customer reply template (see example below)",
    "needs_engineer": false,
    "fix_suggestion": ""
}}
```

## user_reply 格式要求（非常重要！）

客服会直接复制 user_reply / user_reply_en 发给用户，必须完整、礼貌。

### 中文 user_reply 示例

```
您好，经过日志分析，您在 12月1日 的录音已成功传输到 APP。但由于设备时间偏移，该录音在 APP 中显示为 2023年9月24日 10:13 的录音（时长约 39 分钟）。

请在 APP 中按照以下步骤查找：
1. 打开 APP 录音列表
2. 向下滚动到 2023年9月 附近
3. 查找时长约 39 分钟的录音

如需进一步帮助，请随时联系我们。
```

### English user_reply_en 示例

```
Hello, based on our log analysis, your recording from December 1st was successfully transferred to the APP. However, due to a device clock offset, it appears as a recording from September 24, 2023 at 10:13 (approximately 39 minutes long).

Please follow these steps to find it in the APP:
1. Open the recording list
2. Scroll down to around September 2023
3. Look for a recording approximately 39 minutes long

If you need further assistance, please don't hesitate to contact us.
```

### 差的 user_reply 示例（禁止）

```
时间戳偏移导致 keyId 对应的 sessionId 有误。
```
（过于技术化，用户无法理解）

## 置信度判断标准

- **high**: 日志中有明确证据，根因清晰，解决方案明确
- **medium**: 日志有一些线索但不完全确定，或问题有多种可能原因
- **low**: 日志信息不足以确定根因，需要更多信息或工程师介入

当 confidence 为 low 时，设 needs_engineer 为 true。
"""
        return prompt

    # ------------------------------------------------------------------
    # Result parsing
    # ------------------------------------------------------------------
    @staticmethod
    def parse_result(workspace: Path, raw_output: str = "") -> AnalysisResult:
        """Parse the agent's result from output/result.json or raw output."""
        data = {}

        # Strategy 1: read output/result.json (expected path)
        result_file = workspace / "output" / "result.json"
        if result_file.exists():
            try:
                content = result_file.read_text(encoding="utf-8")
                content = content.lstrip("\ufeff")  # strip BOM
                data = json.loads(content)
                logger.info("Parsed result.json (%d bytes, keys: %s)", len(content), list(data.keys()))
            except Exception as e:
                logger.warning("Failed to parse result.json at %s: %s", result_file, e)
        else:
            logger.warning("result.json not found at %s", result_file)

        # Strategy 2: search recursively for any result.json in workspace
        if not data:
            for p in sorted(workspace.rglob("result.json")):
                if p == result_file:
                    continue
                try:
                    content = p.read_text(encoding="utf-8").lstrip("\ufeff")
                    candidate = json.loads(content)
                    if "problem_type" in candidate or "root_cause" in candidate:
                        data = candidate
                        logger.info("Found result.json at alternate path: %s", p)
                        break
                except Exception:
                    continue

        # Strategy 3: extract JSON from raw stdout
        if not data and raw_output:
            data = _extract_json_from_text(raw_output)
            if data:
                logger.info("Extracted JSON from raw output (keys: %s)", list(data.keys()))

        if data:
            logger.info("Analysis result: problem_type=%s confidence=%s", data.get("problem_type"), data.get("confidence"))

        return AnalysisResult(
            task_id="",
            issue_id="",
            problem_type=data.get("problem_type", "未知"),
            problem_type_en=data.get("problem_type_en", ""),
            root_cause=data.get("root_cause", raw_output[:2000] if raw_output else "分析未产出结构化结果"),
            root_cause_en=data.get("root_cause_en", ""),
            confidence=Confidence(data.get("confidence", "low")),
            confidence_reason=data.get("confidence_reason", ""),
            key_evidence=data.get("key_evidence", []),
            user_reply=data.get("user_reply", ""),
            user_reply_en=data.get("user_reply_en", ""),
            needs_engineer=data.get("needs_engineer", True),
            fix_suggestion=data.get("fix_suggestion", ""),
            raw_output=raw_output[:10000],
        )


def _extract_json_from_text(text: str) -> Dict:
    """Try to extract a JSON object from text that may contain markdown."""
    import re

    # Strategy A: look for ```json ... ``` blocks
    patterns = [
        r"```json\s*\n(.*?)\n```",
        r"```\s*\n(\{.*?\})\n```",
    ]
    for pat in patterns:
        match = re.search(pat, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                continue

    # Strategy B: find the largest { ... } block containing "problem_type"
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
                block = text[start : i + 1]
                if "problem_type" in block:
                    candidates.append(block)
                start = -1

    for block in sorted(candidates, key=len, reverse=True):
        try:
            return json.loads(block)
        except Exception:
            continue

    return {}
