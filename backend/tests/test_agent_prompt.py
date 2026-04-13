"""Tests for prompt budget enforcement."""

import json
from pathlib import Path

from app.agents.base import BaseAgent
from app.models.schemas import Issue, Rule, RuleMeta, RuleTrigger
from app.services.agent_orchestrator import _materialize_analysis_context, _write_prompt_meta


def test_build_prompt_enforces_budget_with_large_inputs(tmp_path: Path):
    issue = Issue(
        record_id="issue_1",
        description="上传失败 " * 1000,
        device_sn="SN123",
        firmware="1.0.0",
        app_version="2.0.0",
    )
    rule = Rule(
        meta=RuleMeta(
            id="cloud-sync",
            name="Cloud Sync",
            triggers=RuleTrigger(keywords=["上传失败", "unable to upload", "cloud sync"]),
            required_output=["timeline", "failure_modes"],
        ),
        content="规则正文 " * 5000,
        file_path="/tmp/cloud-sync.md",
    )
    extraction = {
        "patterns": {
            "upload_errors": {
                "match_count": 5000,
                "matches": [f"line {idx} " + ("x" * 200) for idx in range(5000)],
            }
        },
        "deterministic": {
            "cloud_sync_summary": {
                "summary_lines": [f"summary {idx} " + ("y" * 200) for idx in range(100)],
            }
        },
    }
    previous_analysis = {
        "problem_type": "云同步失败",
        "root_cause": "z" * 5000,
        "user_reply": "r" * 5000,
        "key_evidence": ["k" * 2000] * 20,
    }
    few_shot_examples = [
        {
            "description": "示例描述 " * 400,
            "problem_type": "云同步失败",
            "root_cause": "根因 " * 300,
            "user_reply": "回复 " * 300,
        }
        for _ in range(3)
    ]

    context_files = _materialize_analysis_context(
        workspace=tmp_path,
        issue=issue,
        extraction=extraction,
        rules=[rule],
        problem_date="2026-03-20",
        has_logs=True,
        previous_analysis=previous_analysis,
        followup_question="请解释为什么还是上传失败 " * 300,
        few_shot_examples=few_shot_examples,
    )

    prompt, prompt_meta = BaseAgent.build_prompt_with_meta(
        issue=issue,
        rules=[rule],
        extraction=extraction,
        has_logs=True,
        previous_analysis=previous_analysis,
        followup_question="请解释为什么还是上传失败 " * 300,
        few_shot_examples=few_shot_examples,
        context_files=context_files,
    )

    assert len(prompt) <= 36000
    assert prompt_meta["final_prompt_chars"] == len(prompt)
    assert prompt_meta["compact_mode"] is False
    assert sorted(prompt_meta["context_files"].keys()) == [
        "classification",
        "extraction",
        "few_shot",
        "followup_question",
        "issue",
        "previous_analysis",
    ]
    assert "rules/cloud-sync.md" in prompt
    assert "context/extraction_full.json" in prompt
    assert "context/previous_analysis.json" in prompt


def test_materialize_analysis_context_writes_full_context(tmp_path: Path):
    issue = Issue(record_id="issue_2", description="录音找不到")
    rule = Rule(
        meta=RuleMeta(id="recording-missing", name="Recording Missing", triggers=RuleTrigger(keywords=["录音找不到"])),
        content="规则正文",
        file_path="/tmp/recording-missing.md",
    )
    extraction = {"patterns": {"timeline": {"match_count": 2, "matches": ["a", "b"]}}}

    context_files = _materialize_analysis_context(
        workspace=tmp_path,
        issue=issue,
        extraction=extraction,
        rules=[rule],
        problem_date="2026-03-21",
        has_logs=True,
        previous_analysis={"root_cause": "previous"},
        followup_question="还要看什么？",
        few_shot_examples=[{"description": "示例"}],
    )

    extraction_path = tmp_path / context_files["extraction"]
    issue_path = tmp_path / context_files["issue"]
    previous_path = tmp_path / context_files["previous_analysis"]

    assert extraction_path.exists()
    assert issue_path.exists()
    assert previous_path.exists()
    assert json.loads(extraction_path.read_text(encoding="utf-8"))["patterns"]["timeline"]["match_count"] == 2


def test_write_prompt_meta_persists_json(tmp_path: Path):
    _write_prompt_meta(tmp_path, {"final_prompt_chars": 123, "compact_mode": False})
    payload = json.loads((tmp_path / "output" / "prompt_meta.json").read_text(encoding="utf-8"))
    assert payload["final_prompt_chars"] == 123
