"""
VOC Portal taxonomy — seed loading, DB sync, in-memory cache.

Data flow (mirrors app.services.rule_engine's "files are seed, DB is runtime
source of truth" pattern):
  1. On first startup (DB table empty): load the checked-in seed snapshot →
     upsert into voc_tags table.
  2. Runtime: DB is the source of truth. `sync_from_voc()` (manual trigger, or
     a daily loop when voc.sync_enabled) refreshes it from the live VOC API.
  3. In-memory cache of active tags for fast per-request access (agent context
     files, the classifier's system prompt) — refreshed via reload_from_db().

The seed file is a point-in-time snapshot (checked into git at
backend/seeds/voc_taxonomy_seed.json), NOT kept live-synced with VOC — it only
exists to bootstrap a fresh DB before real credentials/sync are wired up.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import BACKEND_ROOT, get_settings

logger = logging.getLogger("jarvis.voc_taxonomy")

SEED_PATH = BACKEND_ROOT / "seeds" / "voc_taxonomy_seed.json"

# In-memory cache of active (non-retired) tags, refreshed by reload_from_db().
_cache: List[Dict[str, Any]] = []
_cache_loaded = False


def load_seed() -> Dict[str, Any]:
    """Read the checked-in seed snapshot. Returns {} if it doesn't exist yet
    (e.g. before the first VOC MCP pull) — callers must handle that gracefully,
    not crash startup over a missing seed."""
    if not SEED_PATH.exists():
        logger.warning("VOC taxonomy seed not found at %s — starting with an empty taxonomy "
                        "until sync_from_voc() runs or the seed is populated.", SEED_PATH)
        return {}
    with open(SEED_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


async def sync_seed_to_db(force: bool = False) -> Dict[str, Any]:
    """Bootstrap (or, with force=True, forcibly re-upsert) voc_tags from the
    checked-in seed file.

    Default (force=False): ONLY runs if the table is currently empty — a
    one-time bootstrap, not a recurring sync, so a stale seed file can't
    clobber newer VOC-side edits once real data exists.

    force=True: re-upserts the seed regardless of existing data (added/
    changed/retired diff, same semantics as sync_from_voc()) — the manual
    substitute for the VOC service-account sync loop
    (voc.sync_enabled=False until Keycloak credentials are provisioned):
    pull a fresh snapshot via the voc-portal MCP tool, overwrite
    backend/seeds/voc_taxonomy_seed.json, redeploy, then call this with
    force=True (exposed as POST /api/voc/taxonomy/reseed).

    Returns {"added": [...], "changed": [...], "retired": [...], "skipped": bool}.
    skipped=True means nothing was written (already-bootstrapped DB with
    force=False, or no seed file/tags available).
    """
    from app.db.database import get_voc_tags, upsert_voc_tags

    existing = await get_voc_tags(include_retired=True)
    if existing and not force:
        return {"added": [], "changed": [], "retired": [], "skipped": True}

    seed = load_seed()
    tags = seed.get("tags", [])
    if not tags:
        return {"added": [], "changed": [], "retired": [], "skipped": True}

    diff = await upsert_voc_tags(tags)
    await reload_from_db()
    logger.info(
        "VOC taxonomy %s from %s: %d added, %d changed, %d retired",
        "reseeded" if (existing and force) else "seeded", SEED_PATH,
        len(diff["added"]), len(diff["changed"]), len(diff["retired"]),
    )
    return {**diff, "skipped": False}


async def reload_from_db() -> None:
    """Refresh the in-memory active-tags cache from the DB."""
    global _cache, _cache_loaded
    from app.db.database import get_voc_tags

    _cache = await get_voc_tags(include_retired=False)
    _cache_loaded = True


def active_tags() -> List[Dict[str, Any]]:
    """Cached active (non-retired) tags. Call reload_from_db() first at startup
    and after every sync — this returns whatever was last loaded, it does not
    hit the DB itself (keeps per-request agent-context writes cheap)."""
    if not _cache_loaded:
        logger.warning("active_tags() called before reload_from_db() — returning empty list")
    return _cache


def to_prompt_payload(tags: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Compact structure for the LLM: id + 3-level path + definition + examples
    + MECE rules. Used both by the classifier's system prompt and by the
    workspace context file (context/voc_taxonomy.json) future tickets read."""
    tags = tags if tags is not None else active_tags()
    return {
        "version": "voc-portal",
        "instructions": (
            "从 tags 中选择 1 个最匹配的 primary（主分类）+ 至多 2 个 secondary（次分类）。"
            "每个 tag 的 definition 是官方归类定义，mece_rules 标注了与哪些 tag 互斥、"
            "negative_examples 标注了容易误分到这里但实际应归到别处的反例——都要参考。"
            "找不到合适 tag 时不要编造，用 fallback tag。"
        ),
        "tags": [
            {
                "id": t["id"],
                "path": [t.get("level_1_category", ""), t.get("level_2_label", ""), t.get("level_3_diagnosis", "")],
                "definition": t.get("definition", ""),
                "positive_examples": t.get("positive_examples", []),
                "mece_rules": t.get("mece_rules", []),
                "negative_examples": t.get("negative_examples", []),
            }
            for t in tags
        ],
    }


async def sync_from_voc() -> Dict[str, List[str]]:
    """Pull the live taxonomy from VOC Portal, upsert into DB, refresh cache.

    Raises whatever app.services.voc_client raises (VocCredentialsMissing /
    VocAuthError / VocApiError) — callers (manual trigger endpoint, daily loop)
    decide how to surface/log that; we don't swallow it here.
    """
    from app.db.database import upsert_voc_tags
    from app.services.voc_client import fetch_taxonomy_tags

    tags = await fetch_taxonomy_tags()
    diff = await upsert_voc_tags(tags)
    await reload_from_db()
    logger.info(
        "VOC taxonomy synced: %d added, %d changed, %d retired (now %d active)",
        len(diff["added"]), len(diff["changed"]), len(diff["retired"]), len(_cache),
    )
    return diff


async def voc_sync_loop() -> None:
    """Daily sync loop — only started (from main.py lifespan) when
    settings.voc.sync_enabled is True. Off by default until a service account
    is provisioned; see backend/app/config.py VOCSettings docstring."""
    settings = get_settings().voc
    interval_seconds = max(1, settings.sync_interval_hours) * 3600
    while True:
        try:
            await sync_from_voc()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("VOC taxonomy sync failed (will retry in %dh): %s",
                            settings.sync_interval_hours, e)
        await asyncio.sleep(interval_seconds)
