"""
API routes for rule management (CRUD).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException

from app.models.schemas import (
    Rule,
    RuleCreateRequest,
    RuleMeta,
    RuleTrigger,
    RuleUpdateRequest,
    PreExtractPattern,
)
from app.services.rule_engine import RuleEngine

logger = logging.getLogger("jarvis.api.rules")
router = APIRouter()

# Singleton rule engine (initialized once)
_engine: Optional[RuleEngine] = None


def _get_engine() -> RuleEngine:
    global _engine
    if _engine is None:
        _engine = RuleEngine()
    return _engine


@router.get("", response_model=List[Rule])
async def list_rules():
    """List all analysis rules."""
    return _get_engine().list_rules()


@router.get("/{rule_id}", response_model=Rule)
async def get_rule(rule_id: str):
    """Get a single rule by ID."""
    rule = _get_engine().get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    return rule


@router.post("", response_model=Rule)
async def create_rule(req: RuleCreateRequest):
    """Create a new analysis rule."""
    engine = _get_engine()
    if engine.get_rule(req.id):
        raise HTTPException(status_code=409, detail=f"Rule '{req.id}' already exists")

    meta = RuleMeta(
        id=req.id,
        name=req.name,
        triggers=req.triggers,
        depends_on=req.depends_on,
        pre_extract=req.pre_extract,
        needs_code=req.needs_code,
    )
    rule = Rule(meta=meta, content=req.content)
    return engine.save_rule(rule)


@router.put("/{rule_id}", response_model=Rule)
async def update_rule(rule_id: str, req: RuleUpdateRequest):
    """Update an existing rule."""
    engine = _get_engine()
    rule = engine.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")

    if req.name is not None:
        rule.meta.name = req.name
    if req.triggers is not None:
        rule.meta.triggers = req.triggers
    if req.depends_on is not None:
        rule.meta.depends_on = req.depends_on
    if req.pre_extract is not None:
        rule.meta.pre_extract = req.pre_extract
    if req.needs_code is not None:
        rule.meta.needs_code = req.needs_code
    if req.enabled is not None:
        rule.meta.enabled = req.enabled
    if req.content is not None:
        rule.content = req.content

    return engine.save_rule(rule)


@router.delete("/{rule_id}")
async def delete_rule(rule_id: str):
    """Delete a rule."""
    engine = _get_engine()
    if not engine.delete_rule(rule_id):
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    return {"deleted": rule_id}


@router.post("/reload")
async def reload_rules():
    """Reload all rules from disk."""
    engine = _get_engine()
    engine.reload()
    return {"reloaded": len(engine.list_rules()), "rules": [r.meta.id for r in engine.list_rules()]}


@router.post("/{rule_id}/test")
async def test_rule(rule_id: str, description: str = ""):
    """Test which rule would be matched for a given problem description."""
    engine = _get_engine()
    matched = engine.match_rules(description)
    return {
        "input": description,
        "matched_rules": [r.meta.id for r in matched],
        "primary": matched[0].meta.id if matched else None,
    }
