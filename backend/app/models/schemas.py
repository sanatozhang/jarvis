"""
Pydantic schemas used across the application.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class TaskStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DECRYPTING = "decrypting"
    EXTRACTING = "extracting"
    ANALYZING = "analyzing"
    DONE = "done"
    FAILED = "failed"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AgentType(str, Enum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


# ---------------------------------------------------------------------------
# Issue (from Feishu)
# ---------------------------------------------------------------------------
class LogFile(BaseModel):
    name: str = ""
    token: str = ""
    size: int = 0


class IssueStatus(str, Enum):
    """Issue status derived from Feishu fields."""
    PENDING = "pending"             # 开始处理=false
    IN_PROGRESS = "in_progress"     # 开始处理=true, 确认提交=false
    DONE = "done"                   # 确认提交=true


class Issue(BaseModel):
    record_id: str
    description: str = ""
    device_sn: str = ""
    firmware: str = ""
    app_version: str = ""
    priority: str = ""          # "H" or "L"
    zendesk: str = ""
    zendesk_id: str = ""        # Extracted ticket number e.g. "#378794"
    feishu_link: str = ""       # Direct link to Feishu record
    feishu_status: IssueStatus = IssueStatus.PENDING
    result_summary: str = ""    # 处理结果 from Feishu
    root_cause_summary: str = ""  # 一句话归因 from Feishu
    created_at_ms: int = 0      # 创建日期 (Unix ms from Feishu)
    log_files: List[LogFile] = Field(default_factory=list)


class IssueListResponse(BaseModel):
    generated_at: str
    stats: Dict[str, Any]
    issues: List[Issue]


# ---------------------------------------------------------------------------
# Analysis Task
# ---------------------------------------------------------------------------
class TaskCreate(BaseModel):
    issue_id: str               # Feishu record_id
    agent_type: Optional[AgentType] = None  # Override agent selection
    username: str = ""          # Who triggered this analysis


class TaskProgress(BaseModel):
    task_id: str
    issue_id: str
    status: TaskStatus = TaskStatus.QUEUED
    progress: int = 0           # 0-100
    message: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    error: Optional[str] = None


class BatchAnalyzeRequest(BaseModel):
    issue_ids: List[str]
    agent_type: Optional[AgentType] = None


# ---------------------------------------------------------------------------
# Analysis Result
# ---------------------------------------------------------------------------
class AnalysisResult(BaseModel):
    task_id: str
    issue_id: str
    problem_type: str = ""
    root_cause: str = ""
    confidence: Confidence = Confidence.MEDIUM
    confidence_reason: str = ""
    key_evidence: List[str] = Field(default_factory=list)
    user_reply: str = ""
    needs_engineer: bool = False
    fix_suggestion: str = ""
    rule_type: str = ""
    agent_type: str = ""
    raw_output: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Issue context (denormalized for convenience)
    issue: Optional[Issue] = None


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------
class RuleTrigger(BaseModel):
    keywords: List[str] = Field(default_factory=list)
    priority: int = 5


class PreExtractPattern(BaseModel):
    name: str
    pattern: str
    date_filter: bool = False


class RuleMeta(BaseModel):
    id: str
    name: str = ""
    version: int = 1
    author: str = ""
    updated: str = ""
    enabled: bool = True
    triggers: RuleTrigger = Field(default_factory=RuleTrigger)
    depends_on: List[str] = Field(default_factory=list)
    pre_extract: List[PreExtractPattern] = Field(default_factory=list)
    needs_code: bool = False
    required_output: List[str] = Field(default_factory=list)


class Rule(BaseModel):
    meta: RuleMeta
    content: str = ""           # Markdown body
    file_path: str = ""         # Absolute path to rule file


class RuleCreateRequest(BaseModel):
    id: str
    name: str
    triggers: RuleTrigger
    depends_on: List[str] = Field(default_factory=list)
    pre_extract: List[PreExtractPattern] = Field(default_factory=list)
    needs_code: bool = False
    content: str


class RuleUpdateRequest(BaseModel):
    name: Optional[str] = None
    triggers: Optional[RuleTrigger] = None
    depends_on: Optional[List[str]] = None
    pre_extract: Optional[List[PreExtractPattern]] = None
    needs_code: Optional[bool] = None
    enabled: Optional[bool] = None
    content: Optional[str] = None


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------
class AgentConfigUpdate(BaseModel):
    default_agent: Optional[str] = None
    timeout: Optional[int] = None
    max_turns: Optional[int] = None
    routing: Optional[Dict[str, str]] = None


# ---------------------------------------------------------------------------
# Daily Report
# ---------------------------------------------------------------------------
class DailyReport(BaseModel):
    date: str
    total_issues: int = 0
    analyses: List[AnalysisResult] = Field(default_factory=list)
    category_stats: Dict[str, int] = Field(default_factory=dict)
    markdown: str = ""
