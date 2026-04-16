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
    effort: str = ""                 # "low", "medium", "high", "max"
    fallback_model: str = ""         # auto-fallback when primary overloaded
    betas: List[str] = field(default_factory=list)  # beta headers
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
        language: str = "zh",
        previous_analysis: Optional[Dict[str, Any]] = None,
        followup_question: str = "",
        few_shot_examples: Optional[List[Dict[str, Any]]] = None,
        context_files: Optional[Dict[str, str]] = None,
    ) -> str:
        prompt, _meta = BaseAgent.build_prompt_with_meta(
            issue=issue,
            rules=rules,
            extraction=extraction,
            problem_date=problem_date,
            has_logs=has_logs,
            language=language,
            previous_analysis=previous_analysis,
            followup_question=followup_question,
            few_shot_examples=few_shot_examples,
            context_files=context_files,
        )
        return prompt

    @staticmethod
    def build_prompt_with_meta(
        issue: Issue,
        rules: List[Rule],
        extraction: Dict[str, Any],
        problem_date: Optional[str] = None,
        has_logs: bool = True,
        language: str = "zh",
        previous_analysis: Optional[Dict[str, Any]] = None,
        followup_question: str = "",
        few_shot_examples: Optional[List[Dict[str, Any]]] = None,
        context_files: Optional[Dict[str, str]] = None,
    ) -> tuple[str, Dict[str, Any]]:
        """Build the master prompt for the agent.

        Args:
            language: "zh" for Chinese output, "en" for English output.
        """
        issue_description = _trim_text(issue.description, _MAX_ISSUE_DESCRIPTION_CHARS)
        rules_section = _render_rules_section(rules, max_chars=_MAX_RULE_SECTION_CHARS)
        extraction_json = _trim_extraction(extraction, max_chars=_MAX_EXTRACTION_CHARS)
        extraction_summary = _summarize_extraction(extraction)
        context_files = context_files or {}
        issue_context_ref = context_files.get("issue", "context/issue_context.json")
        extraction_context_ref = context_files.get("extraction", "context/extraction_full.json")
        few_shot_context_ref = context_files.get("few_shot", "context/few_shot_examples.json")
        prev_analysis_context_ref = context_files.get("previous_analysis", "context/previous_analysis.json")
        followup_context_ref = context_files.get("followup_question", "context/followup_question.txt")

        if has_logs:
            role_and_principles = """你是 Plaud 设备日志分析专家。分析结果将展示给客服，他们会直接复制 user_reply 发给用户。

**行为规则见 CLAUDE.md**（探索式分析、grep 验证、置信度标准、输出 JSON Schema）。

分析流程：读 prompt 摘要和 `context/` 文件 → 列 3-5 个假设 → 主动 grep logs/ 验证（至少 3 次） → 写 output/result.json"""

            extraction_section = f"""## 预提取结果（仅作排查起点，必须自行 grep 验证）

以下是自动提取的初步日志摘要，**不能作为最终分析依据**，只用于帮助你快速定位方向。
- 完整 issue 上下文：`{issue_context_ref}`
- 完整 extraction：`{extraction_context_ref}`
- match_count > 0：有初步匹配，但样本有限，必须去 logs/ 查完整上下文
- match_count = 0：该模式无匹配，可能是模式不精确，请尝试其他关键词自行搜索
- deterministic.*：程序根据日志对齐出的结构化时间线/表格，可信度高，应优先利用，再回到 logs/ 补上下文

### 提取摘要

{extraction_summary}

```json
{extraction_json}
```"""

            workspace_section = """## 工作空间结构

```
logs/         ← 解密后的日志文件，可以直接 grep
images/       ← 用户提供的截图/图片（如果存在），请查看并结合分析
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
4. **查看图片**：如果 images/ 目录有截图，仔细查看并结合分析
5. **结果必须写文件**：分析完成后必须将 JSON 结果写入 output/result.json
6. **无法确认时说明**：如果没有日志无法确认根因，在回复中说明需要用户提供日志进一步排查"""

            extraction_section = """## 日志情况

**本工单未提供日志文件。** 请仅基于问题描述、图片（如有）、代码和规则进行分析。
如果问题需要日志才能定位，请在 user_reply 中引导用户提供日志。"""

            workspace_section = """## 工作空间结构

```
images/       ← 用户提供的截图/图片（如果存在），请查看并结合分析
rules/        ← 规则文件，供参考
code/         ← 代码仓库（如果存在），可搜索代码定位问题
output/       ← 请将 result.json 写入此目录
```"""

        # Build few-shot section if examples are provided
        few_shot_section = ""
        if few_shot_examples:
            few_shot_section = _render_few_shot_section(
                few_shot_examples,
                max_chars=_MAX_FEW_SHOT_SECTION_CHARS,
                context_file=few_shot_context_ref,
            )

        prompt = _compose_prompt(
            role_and_principles=role_and_principles,
            issue_description=issue_description,
            issue=issue,
            problem_date=problem_date,
            rules_section=rules_section,
            few_shot_section=few_shot_section,
            extraction_section=extraction_section,
            workspace_section=workspace_section,
            language=language,
        )
        prompt_meta: Dict[str, Any] = {
            "budget_chars": _MAX_PROMPT_CHARS,
            "compact_mode": False,
            "hard_trimmed": False,
            "has_logs": has_logs,
            "rule_ids": [rule.meta.id for rule in rules],
            "rule_count": len(rules),
            "few_shot_count": len(few_shot_examples or []),
            "has_previous_analysis": bool(previous_analysis),
            "has_followup_question": bool(followup_question),
            "context_files": context_files,
            "sections": {
                "issue_description_chars": len(issue_description),
                "rules_section_chars": len(rules_section),
                "extraction_summary_chars": len(extraction_summary),
                "extraction_json_chars": len(extraction_json),
                "few_shot_section_chars": len(few_shot_section),
            },
            "initial_prompt_chars": len(prompt),
        }

        # Append follow-up analysis section if this is a follow-up
        if previous_analysis and followup_question:
            prev_json = json.dumps(
                _trim_json_like(previous_analysis, max_string_chars=_MAX_PREVIOUS_ANALYSIS_VALUE_CHARS),
                ensure_ascii=False,
                indent=2,
            )
            prompt += f"""

## 追问分析

这是一次追问分析。用户看了之前的分析结果后，提出了新的问题。请基于之前的分析、同样的日志/代码、以及用户的追问，重新分析并给出更有针对性的回答。

### 之前的分析结果

```json
{_trim_text(prev_json, _MAX_PREVIOUS_ANALYSIS_JSON_CHARS)}
```

完整版本：`{prev_analysis_context_ref}`

### 用户追问

{_trim_text(followup_question, _MAX_FOLLOWUP_QUESTION_CHARS)}

完整追问：`{followup_context_ref}`

### 追问分析要求

1. **仔细阅读之前的分析结果**，理解已经做过的分析
2. **针对用户的追问**，从日志/代码中寻找更多相关证据
3. **如果之前的结论需要修正**，明确说明
4. **回复模板（user_reply / user_reply_en）必须直接回答用户的追问**，不要简单重复之前的回复
5. 仍然按照上面要求的 JSON 格式输出到 output/result.json
"""
            prompt_meta["sections"]["previous_analysis_json_chars"] = len(prev_json)
            prompt_meta["sections"]["followup_question_chars"] = len(_trim_text(followup_question, _MAX_FOLLOWUP_QUESTION_CHARS))
            prompt_meta["initial_prompt_chars"] = len(prompt)

        if len(prompt) > _MAX_PROMPT_CHARS:
            logger.warning(
                "Prompt exceeded budget (%d chars). Rebuilding in compact mode.",
                len(prompt),
            )
            prompt_meta["compact_mode"] = True
            compact_rules = _render_rules_section(rules, max_chars=_COMPACT_RULE_SECTION_CHARS)
            compact_extraction = _trim_extraction(extraction, max_chars=_COMPACT_EXTRACTION_CHARS)
            compact_extraction_section = extraction_section.replace(extraction_json, compact_extraction)
            prompt = _compose_prompt(
                role_and_principles=role_and_principles,
                issue_description=_trim_text(issue_description, _COMPACT_ISSUE_DESCRIPTION_CHARS),
                issue=issue,
                problem_date=problem_date,
                rules_section=compact_rules,
                few_shot_section="",
                extraction_section=compact_extraction_section,
                workspace_section=workspace_section,
                language=language,
                )
            prompt_meta["sections"]["compact_rules_section_chars"] = len(compact_rules)
            prompt_meta["sections"]["compact_extraction_json_chars"] = len(compact_extraction)
            if previous_analysis and followup_question:
                prev_json = json.dumps(
                    _trim_json_like(previous_analysis, max_string_chars=_COMPACT_PREVIOUS_ANALYSIS_VALUE_CHARS),
                    ensure_ascii=False,
                    indent=2,
                )
                prompt += f"""

## 追问分析

### 之前的分析结果

```json
{_trim_text(prev_json, _COMPACT_PREVIOUS_ANALYSIS_JSON_CHARS)}
```

完整版本：`{prev_analysis_context_ref}`

### 用户追问

{_trim_text(followup_question, _COMPACT_FOLLOWUP_QUESTION_CHARS)}

完整追问：`{followup_context_ref}`
"""
                prompt_meta["sections"]["compact_previous_analysis_json_chars"] = len(prev_json)
                prompt_meta["sections"]["compact_followup_question_chars"] = len(
                    _trim_text(followup_question, _COMPACT_FOLLOWUP_QUESTION_CHARS)
                )

        if len(prompt) > _MAX_PROMPT_CHARS:
            logger.warning(
                "Prompt still exceeded budget after compact mode (%d chars). Applying hard trim.",
                len(prompt),
            )
            prompt_meta["hard_trimmed"] = True
            prompt = _trim_text(prompt, _MAX_PROMPT_CHARS)

        prompt_meta["final_prompt_chars"] = len(prompt)
        return prompt, prompt_meta

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

        # Strategy 4: Claude output plain Markdown without JSON — salvage what we can
        if not data and raw_output and len(raw_output.strip()) > 50:
            data = _salvage_from_markdown(raw_output)
            if data:
                logger.info("Salvaged structured data from Markdown output (keys: %s)", list(data.keys()))

        if data:
            logger.info("Analysis result: problem_type=%s confidence=%s", data.get("problem_type"), data.get("confidence"))
        else:
            logger.warning("All 4 parsing strategies failed. raw_output length=%d", len(raw_output) if raw_output else 0)

        # When model produced text analysis but didn't write valid JSON,
        # use the cleaned raw output as root_cause so the frontend can
        # display meaningful content instead of an error message.
        if not data.get("root_cause") and raw_output:
            cleaned_output = _clean_system_lines(raw_output[:3000])
            if len(cleaned_output.strip()) > 50:
                # Model produced meaningful analysis text — use it as root_cause
                data.setdefault("root_cause", cleaned_output)
                data.setdefault("confidence", "medium")
                data.setdefault("confidence_reason", "从 Agent 文本输出中提取，未生成标准 result.json")
                data.setdefault("needs_engineer", True)
                logger.info("Using cleaned raw_output (%d chars) as fallback root_cause", len(cleaned_output))

        _raw_rc = data.get("root_cause") or "分析未产出结构化结果"
        _raw_rc_en = data.get("root_cause_en", "")

        return AnalysisResult(
            task_id="",
            issue_id="",
            problem_type=_clean_problem_type(data.get("problem_type", "未知")),
            problem_type_en=_clean_problem_type(data.get("problem_type_en", "")),
            problem_categories=_safe_problem_categories(data.get("problem_categories", [])),
            device_type=str(data.get("device_type", "")).strip(),
            root_cause=_clean_system_lines(_raw_rc),
            root_cause_en=_clean_system_lines(_raw_rc_en) if _raw_rc_en else "",
            confidence=_safe_confidence(data.get("confidence", "low")),
            confidence_reason=data.get("confidence_reason", ""),
            key_evidence=_safe_key_evidence(data.get("key_evidence", [])),
            user_reply=data.get("user_reply", ""),
            user_reply_en=data.get("user_reply_en", ""),
            needs_engineer=data.get("needs_engineer", True),
            fix_suggestion=data.get("fix_suggestion", ""),
            raw_output=raw_output[:10000],
        )


def _compose_prompt(
    *,
    role_and_principles: str,
    issue_description: str,
    issue: Issue,
    problem_date: Optional[str],
    rules_section: str,
    few_shot_section: str,
    extraction_section: str,
    workspace_section: str,
    language: str,
) -> str:
    return f"""{role_and_principles}

## 工单信息

- **问题描述**: {issue_description}
- **设备SN**: {issue.device_sn}
- **固件版本**: {issue.firmware}
- **APP版本**: {issue.app_version}
- **Zendesk**: {issue.zendesk}
{f"- **问题日期**: {problem_date}" if problem_date else ""}

## 分析规则

请先阅读 rules/ 目录下的规则文件，严格按照规则中的排查步骤执行分析。
以下是规则摘要：

{rules_section}
{few_shot_section}
{extraction_section}

{workspace_section}

## 输出要求（最重要！）

**必须**使用 Write 工具将以下格式的 JSON 写入 `output/result.json`，然后 `cat output/result.json` 打印到 stdout。
不写 result.json 等同于分析失败。

```json
{{
    "problem_type": "问题分类（中文，如：蓝牙连接异常、录音丢失、固件升级失败）",
    "problem_type_en": "Problem Type (English)",
    "problem_categories": [
        {{"category": "一级分类", "subcategory": "二级细分"}}
    ],
    "device_type": "设备型号（从日志 device/bind 或 device/info API 提取，无法确认填空串）",
    "root_cause": "根本原因分析（中文，5-10 句话，包含：1. 问题现象总结 2. 具体根因 3. 关键日志证据引用 4. 影响范围）",
    "root_cause_en": "Root cause analysis (English, 5-10 sentences with same structure)",
    "confidence": "high / medium / low",
    "confidence_reason": "置信度理由（为什么是这个级别）",
    "key_evidence": [
        "[说明] 具体日志行或证据内容（每条证据格式：先用一句话解释这条证据说明了什么问题，然后引用原始日志行或数据。最多5条，每条100-200字）"
    ],
    "user_reply": "完整的中文客服回复模板（200-500字，包含：1. 问题确认和共情 2. 原因解释（用用户能理解的语言，避免技术术语）3. 解决方案或操作步骤 4. 如果无法解决，说明后续计划。语气礼貌专业，可直接复制发给用户）",
    "user_reply_en": "Complete English reply template (200-500 words, same structure as Chinese version)",
    "needs_engineer": false,
    "fix_suggestion": "工程师修复建议（如有）"
}}
```

problem_categories 和 device_type 的完整分类体系见 `context/classification_taxonomy.json`，**分析前必须先读取**。一个问题可属于多个分类。

**主要语言: {"English" if language == "en" else "中文"}** — 确保主要语言的内容最详细。
每个字段都必须填写，不能为空。problem_type 必须是具体的问题分类，不能写"分析完成"之类的。

**质量要求**：
- user_reply 是客服直接发给用户的内容，必须完整、有用、可直接解决或解释用户的问题。不能只写一两句话敷衍。
- root_cause 需要有足够的技术深度和日志证据支撑，让工程师能理解问题全貌。
- key_evidence 每条必须包含两部分：**分析说明**（这条证据说明了什么）+ **原始日志/数据**（引用具体内容）。不要只贴裸日志行，必须解释每条证据的意义。示例：`"蓝牙连接在配对阶段超时断开，说明设备端 BLE 栈未正确响应 —— 日志: [2025-04-10 14:32:05] BLE GATT connection timeout after 30s, peer=XX:XX:XX:XX"`
- 如果问题复杂，宁可写得更详细，也不要遗漏关键信息。
"""


_MAX_PROMPT_CHARS = 80_000
_MAX_ISSUE_DESCRIPTION_CHARS = 4_000
_COMPACT_ISSUE_DESCRIPTION_CHARS = 2_000
_MAX_RULE_SECTION_CHARS = 20_000
_COMPACT_RULE_SECTION_CHARS = 10_000
_MAX_FEW_SHOT_SECTION_CHARS = 4_000
_MAX_PREVIOUS_ANALYSIS_JSON_CHARS = 6_000
_COMPACT_PREVIOUS_ANALYSIS_JSON_CHARS = 3_000
_MAX_PREVIOUS_ANALYSIS_VALUE_CHARS = 800
_COMPACT_PREVIOUS_ANALYSIS_VALUE_CHARS = 400
_MAX_FOLLOWUP_QUESTION_CHARS = 3_000
_COMPACT_FOLLOWUP_QUESTION_CHARS = 1_500


def _clean_system_lines(text: str) -> str:
    """Remove internal system instruction lines that shouldn't be shown to users."""
    import re

    # Patterns that are agent-internal meta-commentary, not analysis content
    _SYSTEM_PATTERNS = [
        r"分析结果已保存至.*$",
        r"已将.*(?:写入|保存|输出).*result\.json.*$",
        r".*可供客服直接使用.*$",
        r"^✅\s*问题定位完成[：:].*$",
        r"^分析总结\s*$",
        # "结果保存在某个文件下" 类废话
        r".*result\.json.*(?:已写入|已保存|已输出|已生成).*$",
        r".*(?:客服|用户).*(?:可以|可直接).*(?:使用|复制|发送).*user_reply.*$",
        r"^根据\s*CLAUDE\.md\s*规则.*$",
        r"^分析结果已按要求.*$",
        r".*output/result\.json.*$",
        r"^以上分析.*已完成.*$",
    ]

    cleaned = text
    for pat in _SYSTEM_PATTERNS:
        cleaned = re.sub(pat, "", cleaned, flags=re.MULTILINE)

    # Collapse multiple blank lines left by removals
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# Values that agents sometimes write as problem_type but are not real categories
_INVALID_PROBLEM_TYPES = {
    "analysis complete", "分析完成", "分析总结", "未知", "unknown",
    "问题定位完成", "分析结果", "completed", "done", "n/a",
}


def _clean_problem_type(value: str) -> str:
    """Return the value if it looks like a real problem category, else '未知'."""
    v = (value or "").strip()
    if not v or v.lower() in _INVALID_PROBLEM_TYPES:
        return "未知"
    return v


def _safe_confidence(value: Any) -> Confidence:
    """Convert confidence value to Confidence enum, tolerating case/format variations."""
    if isinstance(value, Confidence):
        return value
    raw = str(value).strip().lower()
    mapping = {"high": Confidence.HIGH, "medium": Confidence.MEDIUM, "low": Confidence.LOW}
    # Also handle Chinese or partial matches
    if "高" in raw or "high" in raw:
        return Confidence.HIGH
    if "中" in raw or "med" in raw:
        return Confidence.MEDIUM
    return mapping.get(raw, Confidence.LOW)


def _safe_problem_categories(value: Any) -> list:
    """Ensure problem_categories is a list of ProblemCategory dicts."""
    from app.models.schemas import ProblemCategory
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            result.append(ProblemCategory(
                category=str(item.get("category", "")).strip(),
                subcategory=str(item.get("subcategory", "")).strip(),
            ))
    return result


def _is_file_path_not_evidence(s: str) -> bool:
    """Return True if the string looks like a file path rather than log evidence."""
    import re
    s = s.strip()
    if not s:
        return True
    # Pure file paths: "output/result.json", "backend/app/api/local.py", etc.
    if re.match(r'^[\w./_\\-]+\.\w{1,5}$', s):
        return True
    # Paths with common code extensions at the end
    if re.search(r'\.(py|js|ts|tsx|json|md|yaml|yml|toml|cfg|sh)$', s) and ' ' not in s:
        return True
    return False


def _safe_key_evidence(value: Any) -> list:
    """Ensure key_evidence is always a list of strings, filtering out file paths."""
    items: list[str] = []
    if isinstance(value, list):
        items = [str(item) for item in value[:10]]
    elif isinstance(value, str):
        # Model sometimes writes a single string instead of a list
        items = [line.strip() for line in value.split("\n") if line.strip()][:10]

    # Filter out entries that are just file paths (not actual log evidence)
    return [item for item in items if not _is_file_path_not_evidence(item)]


def _extract_json_from_text(text: str) -> Dict:
    """Try to extract a JSON object from text that may contain markdown."""
    import re

    # Strategy A: look for ```json ... ``` blocks (including with trailing content)
    patterns = [
        r"```json\s*\n(.*?)\n\s*```",
        r"```\s*\n(\{.*?\})\s*\n\s*```",
    ]
    for pat in patterns:
        for match in re.finditer(pat, text, re.DOTALL):
            try:
                candidate = json.loads(match.group(1).strip())
                if "problem_type" in candidate or "root_cause" in candidate:
                    return candidate
            except Exception:
                continue

    # Strategy B: find the largest { ... } block containing key fields
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
                if "problem_type" in block or "root_cause" in block:
                    candidates.append(block)
                start = -1

    for block in sorted(candidates, key=len, reverse=True):
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            # Try fixing common JSON issues: trailing commas, single quotes
            try:
                fixed = re.sub(r",\s*([}\]])", r"\1", block)  # trailing commas
                return json.loads(fixed)
            except Exception:
                continue

    return {}


def _salvage_from_markdown(text: str) -> Dict:
    """Last-resort: extract structured fields from Markdown when Claude didn't write JSON.

    Parses headings, bold labels, and bullet points to populate problem_type, root_cause, etc.
    """
    import re

    result: Dict[str, Any] = {}

    # --- problem_type: try multiple patterns ---
    # Pattern 1: **问题类型/诊断**: xxx or **Problem Type**: xxx
    type_match = re.search(
        r"\*\*(?:问题[类分]型|问题诊断|problem.?type)[：:\s*]*\*\*[：:\s]*(.+)",
        text, re.IGNORECASE,
    )
    if not type_match:
        # Pattern 2: heading (skip generic headings like "分析完成")
        for m in re.finditer(r"#{1,3}\s*(.+)", text):
            heading = m.group(1).strip()
            if heading.lower() not in _INVALID_PROBLEM_TYPES and len(heading) > 2:
                type_match = m
                break
    if not type_match:
        # Pattern 3: 问题确定/问题定位/问题诊断: xxx
        type_match = re.search(r"(?:问题(?:确定|定位|诊断)|关键发现)[：:]\s*(.+)", text)
    if type_match:
        result["problem_type"] = type_match.group(1).strip().strip("*")[:128]

    # --- root_cause: try labeled section first, then full text ---
    rc_match = re.search(
        r"(?:\*\*)?(?:根[本因]原因|root.?cause|根因分析|原因分析)[：:\s*]*(?:\*\*)?[：:\s]*\n?(.*?)(?=\n\*\*|\n#{1,3}|\Z)",
        text, re.DOTALL | re.IGNORECASE,
    )
    if rc_match and len(rc_match.group(1).strip()) > 20:
        result["root_cause"] = _clean_system_lines(rc_match.group(1).strip())[:2000]
    else:
        # Fall back to cleaned full text
        cleaned = re.sub(r"#{1,3}\s+", "", text).strip()
        cleaned = _clean_system_lines(cleaned)
        result["root_cause"] = cleaned[:2000]

    # --- user_reply: try multiple label patterns ---
    reply_patterns = [
        r"(?:\*\*)?(?:用户回复|user.?reply|回复模板|建议回复|客服回复)[：:\s*]*(?:\*\*)?[：:\s]*\n(.*?)(?=\n\*\*|\n#{1,3}|\Z)",
        r"(?:用户回复|user.?reply|回复模板|建议回复)[：:]\s*\n(.*?)(?:\n#{1,3}|\n\*\*|\Z)",
    ]
    for pat in reply_patterns:
        reply_match = re.search(pat, text, re.DOTALL | re.IGNORECASE)
        if reply_match and len(reply_match.group(1).strip()) > 10:
            result["user_reply"] = reply_match.group(1).strip()[:1500]
            break

    # --- key_evidence: extract log lines (monospace/code blocks or bullet points with log-like content) ---
    evidence = []
    code_blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
    for block in code_blocks:
        for line in block.strip().split("\n"):
            line = line.strip()
            if line and len(line) > 20 and not _is_file_path_not_evidence(line):
                evidence.append(line)
    # Also look for bullet points with timestamps or log-like patterns
    log_bullets = re.findall(r"[-*]\s+(`[^`]+`|.*?\d{2}:\d{2}.*)", text)
    for b in log_bullets:
        cleaned = b.strip("`")
        if not _is_file_path_not_evidence(cleaned):
            evidence.append(cleaned)
    if evidence:
        result["key_evidence"] = evidence[:5]

    result["confidence"] = "medium"
    result["confidence_reason"] = "Agent 未生成 result.json，从 Markdown 输出中提取"
    result["needs_engineer"] = True

    return result if result.get("root_cause") else {}


# Prompt budget. Sonnet-4 supports 200K tokens; 80K chars ≈ 20K tokens is safe.
_MAX_EXTRACTION_CHARS = 25_000
_COMPACT_EXTRACTION_CHARS = 12_000


def _trim_text(text: str, max_chars: int) -> str:
    value = text or ""
    if len(value) <= max_chars:
        return value
    keep = max(max_chars - 32, 0)
    return value[:keep] + "\n...[trimmed for prompt size]..."


def _trim_json_like(value: Any, max_string_chars: int) -> Any:
    if isinstance(value, dict):
        return {k: _trim_json_like(v, max_string_chars=max_string_chars) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim_json_like(v, max_string_chars=max_string_chars) for v in value[:10]]
    if isinstance(value, str):
        return _trim_text(value, max_string_chars)
    return value


def _render_rules_section(rules: List[Rule], max_chars: int) -> str:
    parts: List[str] = []
    for rule in rules[:3]:
        keywords = ", ".join(rule.meta.triggers.keywords[:8]) or "(none)"
        pre_extract = ", ".join(p.name for p in rule.meta.pre_extract[:8]) or "(none)"
        depends_on = ", ".join(rule.meta.depends_on[:6]) or "(none)"
        required_output = ", ".join(rule.meta.required_output[:6]) or "(none)"
        summary = f"""
### 规则: {rule.meta.name or rule.meta.id}
- rule_id: {rule.meta.id}
- 触发词: {keywords}
- pre_extract: {pre_extract}
- depends_on: {depends_on}
- required_output: {required_output}
- needs_code: {"yes" if rule.meta.needs_code else "no"}
- 详细排查步骤见 `rules/{rule.meta.id}.md`
"""
        candidate = "".join(parts) + summary
        if len(candidate) > max_chars:
            remaining = len(rules) - len(parts)
            if not parts:
                return _trim_text(summary, max_chars)
            parts.append(f"\n...[{remaining} more rule summaries omitted for prompt size]...\n")
            break
        parts.append(summary)
    return "".join(parts).strip()


def _render_few_shot_section(examples: List[Dict[str, Any]], max_chars: int, context_file: str = "") -> str:
    header = """
## 参考案例（历史准确分析）

以下是与当前工单相似的历史分析案例，仅供参考，请结合当前工单的实际日志进行独立分析。
"""
    if context_file:
        header += f"完整案例集：`{context_file}`\n"
    parts = [header]
    for idx, ex in enumerate(examples[:3], 1):
        block = f"""
### 案例 {idx}
- 问题描述: {_trim_text(ex.get("description", ""), 120)}
- 问题分类: {_trim_text(ex.get("problem_type", ""), 80)}
- 根因分析: {_trim_text(ex.get("root_cause", ""), 180)}
- 用户回复: {_trim_text(ex.get("user_reply", ""), 160)}
"""
        candidate = "".join(parts) + block
        if len(candidate) > max_chars:
            break
        parts.append(block)
    return "".join(parts).strip() if len(parts) > 1 else ""


def _summarize_extraction(extraction: Dict[str, Any]) -> str:
    patterns = extraction.get("patterns", {}) if isinstance(extraction, dict) else {}
    deterministic = extraction.get("deterministic", {}) if isinstance(extraction, dict) else {}

    nonzero_patterns = []
    for name, value in patterns.items():
        if not isinstance(value, dict):
            continue
        match_count = value.get("match_count", 0)
        if match_count:
            nonzero_patterns.append((name, match_count))
    nonzero_patterns.sort(key=lambda item: item[1], reverse=True)

    lines = [
        f"- nonzero_patterns: {len(nonzero_patterns)}",
        f"- deterministic_blocks: {', '.join(sorted(deterministic.keys())) or '(none)'}",
    ]
    for name, match_count in nonzero_patterns[:5]:
        lines.append(f"- {name}: match_count={match_count}")
    return "\n".join(lines)


def _dedup_matches(matches: list) -> list:
    """Remove duplicate/near-duplicate log lines and collapse repetitive patterns."""
    if not matches:
        return matches
    seen = set()
    unique = []
    for line in matches:
        # Normalize: strip timestamps and whitespace for dedup comparison
        normalized = line.strip()
        # Skip very short lines (noise)
        if len(normalized) < 10:
            continue
        if normalized not in seen:
            seen.add(normalized)
            unique.append(line)
    return unique


def _trim_extraction(extraction: dict, max_chars: int = _MAX_EXTRACTION_CHARS) -> str:
    """Serialize extraction dict to JSON, trimming matches if too large.

    Strategy:
    1. Drop patterns with match_count=0 (no information value).
    2. Deduplicate matches within each pattern.
    3. If still too large, progressively halve the longest matches list.
    The deterministic section is never trimmed as it is high-value structured data.
    """
    import copy

    trimmed = copy.deepcopy(extraction)
    patterns = trimmed.get("patterns", {})

    # Step 1: drop zero-match patterns to save space
    zero_keys = [k for k, v in patterns.items() if isinstance(v, dict) and v.get("match_count", 0) == 0]
    for k in zero_keys:
        del patterns[k]
    if zero_keys:
        patterns["_note"] = f"{len(zero_keys)} patterns with 0 matches omitted"

    # Step 2: deduplicate matches within each pattern
    for key, val in patterns.items():
        if not isinstance(val, dict):
            continue
        matches = val.get("matches", [])
        if matches:
            orig_len = len(matches)
            deduped = _dedup_matches(matches)
            if len(deduped) < orig_len:
                val["matches"] = deduped
                val["_deduped"] = f"{orig_len} → {len(deduped)} unique"

    full = json.dumps(trimmed, ensure_ascii=False, indent=2)
    if len(full) <= max_chars:
        return full

    # Step 3: iteratively halve the longest matches list until under limit
    for _round in range(10):
        # Find the pattern with the most matches
        longest_key = None
        longest_len = 0
        for key, val in patterns.items():
            if not isinstance(val, dict):
                continue
            m = val.get("matches", [])
            if len(m) > longest_len:
                longest_len = len(m)
                longest_key = key

        if not longest_key or longest_len <= 5:
            break

        # Halve it
        new_len = max(longest_len // 2, 5)
        orig_count = patterns[longest_key].get("match_count", longest_len)
        patterns[longest_key]["matches"] = patterns[longest_key]["matches"][:new_len]
        patterns[longest_key]["_trimmed"] = f"showing {new_len}/{orig_count} matches"

        result = json.dumps(trimmed, ensure_ascii=False, indent=2)
        if len(result) <= max_chars:
            return result

    return json.dumps(trimmed, ensure_ascii=False, indent=2)
