"""
VOC tag classifier — assigns 1 primary + up to 2 secondary VOC Portal tags to a
ticket, using the active taxonomy (definition + positive/negative examples +
MECE rules) as the system prompt.

This is a plain text-classification call — no workspace, no logs, no tools —
so it talks to the Messages API directly rather than going through the agent
CLI (see backend/scripts/spike_claude_api.py for the existing direct-API
precedent in this repo). It deliberately does NOT use the official `anthropic`
SDK: this deployment only has network access to the company's internal
Vertex AI proxy (app/agents/claude_api.py `_MessagesClient`), which speaks the
Vertex `rawPredict` wire format (model in the URL path, `x-api-key` auth,
`anthropic_version` field) rather than the public Anthropic API — the SDK's
default client would 404 against it. Used by both the historical backfill
script and (optionally) the live analysis path.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings
from app.services import voc_taxonomy
from app.services.categories import category_label
from app.services.issue_text import strip_leading_metadata

logger = logging.getLogger("jarvis.voc_classifier")

# Same wire format as app/agents/claude_api.py's _MessagesClient (duplicated,
# not imported — that module is agent/tool-loop specific and its constants
# are private; this classifier is a much smaller, standalone caller of the
# same company Vertex proxy).
_VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"
_VERTEX_MODEL_PATH_TPL = "/publishers/anthropic/models/{model}:rawPredict"

# Local-only sentinel for "no active tag fit at all" — NOT a real VOC tag id.
# Kept distinct from any VOC id (which are like "ai-01") so it's unmistakable
# in stats as "classifier gave up" rather than a real category.
#
# Checked against the real taxonomy (backend/seeds/voc_taxonomy_seed.json,
# 158 tags pulled via MCP): VOC has no single global "uncategorized" tag —
# instead each L1 group has its own "-00" catch-all (ai-00, hw-00, sw-00,
# mon-00, b2b-00, gen-00, log-00, mkt-00, op-00, pri-00, sup-00). Those are
# real, group-scoped fallbacks the classifier should already reach for via
# the system prompt/taxonomy payload when it can place a ticket's group but
# not its label/diagnosis. FALLBACK_TAG_ID stays a separate sentinel for the
# stricter case this module handles — no group identifiable at all (HTTP
# failure, refusal, malformed output, empty active taxonomy).
FALLBACK_TAG_ID = "uncategorized"

_MAX_SECONDARY = 2

_CLASSIFY_TOOL = {
    "name": "classify_ticket",
    "description": "Assign VOC taxonomy tags to this ticket.",
    "input_schema": {
        "type": "object",
        "properties": {
            "primary": {
                "type": "object",
                "properties": {
                    "tag_id": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {"type": "string"},
                },
                "required": ["tag_id", "confidence", "reason"],
            },
            "secondary": {
                "type": "array",
                "maxItems": _MAX_SECONDARY,
                "items": {
                    "type": "object",
                    "properties": {
                        "tag_id": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["tag_id", "confidence", "reason"],
                },
            },
        },
        "required": ["primary", "secondary"],
    },
}


@dataclass
class TicketEvidence:
    """Evidence package handed to the classifier for one analysis row.

    Field names match app.db.database.get_analyses_for_voc_backfill()'s
    return shape so the backfill script can pass that dict straight through
    (via from_analysis_row) without reshaping it.
    """
    description: str = ""
    category: str = ""          # issues.category (提单分类, may be key or legacy CN string)
    problem_type: str = ""
    root_cause: str = ""
    device_type: str = ""
    platform: str = ""

    @classmethod
    def from_analysis_row(cls, row: Dict[str, Any]) -> "TicketEvidence":
        return cls(
            description=row.get("description", ""),
            category=row.get("category", ""),
            problem_type=row.get("problem_type", ""),
            root_cause=row.get("root_cause", ""),
            device_type=row.get("device_type", ""),
            platform=row.get("platform", ""),
        )

    def to_prompt_text(self) -> str:
        desc = strip_leading_metadata(self.description).strip()
        category_readable = category_label(self.category, lang="en") if self.category else ""
        lines = [
            f"Ticket description: {desc or '(no description)'}",
            f"Submitted category: {category_readable or '(none)'}",
            f"AI problem_type: {self.problem_type or '(none)'}",
            f"AI root_cause: {self.root_cause or '(none)'}",
            f"Device type: {self.device_type or '(unknown)'}",
            f"Platform: {self.platform or '(unknown)'}",
        ]
        return "\n".join(lines)


def _build_system_prompt() -> str:
    payload = voc_taxonomy.to_prompt_payload()
    return (
        "You classify customer support tickets against a fixed taxonomy of tags.\n\n"
        "Rules:\n"
        "- Choose exactly 1 primary tag: the single best-matching tag_id.\n"
        f"- Choose 0 to {_MAX_SECONDARY} secondary tags for other clearly-relevant aspects "
        "of the ticket. Do not repeat the primary tag as a secondary.\n"
        "- Respect each tag's mece_rules — if the ticket matches a tag's negative_examples "
        "or a mece_rules.distinct_from tag fits better, prefer that one instead.\n"
        "- Only use tag_id values that appear in the taxonomy below. If nothing fits, "
        f'use tag_id "{FALLBACK_TAG_ID}" as primary with confidence "low" and no secondary.\n\n'
        "Taxonomy:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _validate_tags(
    result: Dict[str, Any], active_ids: set,
) -> List[Dict[str, Any]]:
    """Validate the model's tool_use input against the active-tag set and the
    cardinality rules (1 primary, <=2 secondary, no duplicate with primary).
    Returns the flat list to store in analyses.voc_tags_json, or the fallback
    tag on any violation — never raises, this is a quality gate not a crash
    point for a backfill loop processing hundreds of rows.
    """
    primary = result.get("primary") or {}
    primary_id = primary.get("tag_id")

    if not primary_id or primary_id not in active_ids:
        logger.warning("VOC classifier returned unknown/missing primary tag_id=%r — falling back", primary_id)
        return [{
            "tag_id": FALLBACK_TAG_ID,
            "level_1_category": "", "level_2_label": "", "level_3_diagnosis": "",
            "role": "primary", "confidence": "low",
            "reason": "classifier output failed validation",
        }]

    tags_by_id = {t["id"]: t for t in voc_taxonomy.active_tags()}
    out = [_to_stored_tag(primary, primary_id, "primary", tags_by_id)]

    secondary = result.get("secondary") or []
    seen = {primary_id}
    count = 0
    for s in secondary:
        if count >= _MAX_SECONDARY:
            break
        sid = s.get("tag_id")
        if not sid or sid not in active_ids or sid in seen:
            continue
        out.append(_to_stored_tag(s, sid, "secondary", tags_by_id))
        seen.add(sid)
        count += 1

    return out


def _to_stored_tag(
    raw: Dict[str, Any], tag_id: str, role: str, tags_by_id: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    tag = tags_by_id.get(tag_id, {})
    return {
        "tag_id": tag_id,
        "level_1_category": tag.get("level_1_category", ""),
        "level_2_label": tag.get("level_2_label", ""),
        "level_3_diagnosis": tag.get("level_3_diagnosis", ""),
        "role": role,
        "confidence": raw.get("confidence", "medium"),
        "reason": raw.get("reason", ""),
    }


def validate_flat_voc_tags(raw_tags: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Validate a flat list of `{tag_id, role, confidence, reason}` dicts —
    the shape the CLI/CLAUDE.md agent path writes into result.json's
    `voc_tags` field (as opposed to `classify_ticket()`'s structured
    primary/secondary tool-call shape). Same quality gate: unknown tag_id →
    dropped, at most 1 primary (first valid one wins, rest demoted to
    secondary) + up to 2 secondary, deduped. Empty/all-invalid input returns
    `[]` (not a fallback tag) — an agent that legitimately found no match
    already omits voc_tags per the base.py prompt instruction, and forcing
    a fallback here would misrepresent "the agent didn't try" as "the
    agent tried and found nothing", which the backfill's only_empty scan
    needs to tell apart. classify_ticket() is the one place a fallback tag
    always applies, since it's the dedicated backfill/tagging call.
    """
    if not isinstance(raw_tags, list) or not raw_tags:
        return []

    active_ids = {t["id"] for t in voc_taxonomy.active_tags()}
    if not active_ids:
        return []

    tags_by_id = {t["id"]: t for t in voc_taxonomy.active_tags()}
    primary_id: Optional[str] = None
    primary_raw: Optional[Dict[str, Any]] = None
    secondary_candidates: List[Dict[str, Any]] = []

    for item in raw_tags:
        if not isinstance(item, dict):
            continue
        tag_id = item.get("tag_id")
        if not tag_id or tag_id not in active_ids:
            continue
        if item.get("role") == "primary" and primary_id is None:
            primary_id, primary_raw = tag_id, item
        else:
            secondary_candidates.append(item)

    if primary_id is None:
        return []

    out = [_to_stored_tag(primary_raw, primary_id, "primary", tags_by_id)]
    seen = {primary_id}
    count = 0
    for item in secondary_candidates:
        if count >= _MAX_SECONDARY:
            break
        sid = item.get("tag_id")
        if not sid or sid in seen or sid not in active_ids:
            continue
        out.append(_to_stored_tag(item, sid, "secondary", tags_by_id))
        seen.add(sid)
        count += 1

    return out


async def classify_ticket(evidence: TicketEvidence) -> List[Dict[str, Any]]:
    """Classify one ticket. Returns the voc_tags_json list (1 primary + <=2
    secondary), already validated against the active taxonomy — always a
    non-empty list (falls back to FALLBACK_TAG_ID rather than raising) so
    callers can write the result unconditionally.
    """
    active = voc_taxonomy.active_tags()
    active_ids = {t["id"] for t in active}
    if not active_ids:
        logger.warning("VOC classifier called with an empty active taxonomy — falling back")
        return [{
            "tag_id": FALLBACK_TAG_ID,
            "level_1_category": "", "level_2_label": "", "level_3_diagnosis": "",
            "role": "primary", "confidence": "low",
            "reason": "no active VOC taxonomy loaded",
        }]

    settings = get_settings()
    provider_cfg = settings.agent.providers.get("claude_api")
    base_url = (provider_cfg.base_url if provider_cfg else "") or ""
    timeout = float(provider_cfg.per_turn_timeout) if provider_cfg else 120.0
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not base_url or not api_key:
        logger.warning(
            "VOC classifier misconfigured (base_url=%r, api_key_set=%s) — falling back",
            base_url, bool(api_key),
        )
        return [{
            "tag_id": FALLBACK_TAG_ID,
            "level_1_category": "", "level_2_label": "", "level_3_diagnosis": "",
            "role": "primary", "confidence": "low",
            "reason": "claude_api provider not configured (missing base_url/ANTHROPIC_API_KEY)",
        }]

    system_prompt = _build_system_prompt()
    body = {
        "anthropic_version": _VERTEX_ANTHROPIC_VERSION,
        "max_tokens": 1024,
        "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        "tools": [_CLASSIFY_TOOL],
        "tool_choice": {"type": "tool", "name": "classify_ticket"},
        "messages": [{"role": "user", "content": evidence.to_prompt_text()}],
    }
    url = base_url.rstrip("/") + _VERTEX_MODEL_PATH_TPL.format(model=settings.voc.classifier_model)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"x-api-key": api_key, "content-type": "application/json"},
            )
        if resp.status_code >= 400:
            logger.warning("VOC classifier HTTP %d: %s — falling back", resp.status_code, resp.text[:300])
            return [{
                "tag_id": FALLBACK_TAG_ID,
                "level_1_category": "", "level_2_label": "", "level_3_diagnosis": "",
                "role": "primary", "confidence": "low",
                "reason": f"http {resp.status_code} from classifier model",
            }]
        response = resp.json()
    except httpx.RequestError as e:
        logger.warning("VOC classifier network error: %s — falling back", e)
        return [{
            "tag_id": FALLBACK_TAG_ID,
            "level_1_category": "", "level_2_label": "", "level_3_diagnosis": "",
            "role": "primary", "confidence": "low",
            "reason": f"network error: {e}",
        }]

    if response.get("stop_reason") == "refusal":
        logger.warning("VOC classifier call refused — falling back")
        return [{
            "tag_id": FALLBACK_TAG_ID,
            "level_1_category": "", "level_2_label": "", "level_3_diagnosis": "",
            "role": "primary", "confidence": "low",
            "reason": "model refused classification",
        }]

    content_blocks = response.get("content", []) or []
    tool_use = next((b for b in content_blocks if b.get("type") == "tool_use"), None)
    if tool_use is None:
        logger.warning("VOC classifier returned no tool_use block — falling back")
        return [{
            "tag_id": FALLBACK_TAG_ID,
            "level_1_category": "", "level_2_label": "", "level_3_diagnosis": "",
            "role": "primary", "confidence": "low",
            "reason": "no structured output from model",
        }]

    return _validate_tags(tool_use.get("input", {}) or {}, active_ids)
