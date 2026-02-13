"""
Rule Engine - loads, matches, and manages analysis rules.

Data flow:
  1. On startup: load rules from files → sync to DB (file rules are seed data)
  2. Runtime: DB is the source of truth (CRUD reads/writes DB)
  3. In-memory cache for fast matching (refreshed on reload)

This ensures rules persist across deployments (DB) and can be version-controlled (files).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional

import frontmatter

from app.config import RULES_DIR
from app.models.schemas import (
    PreExtractPattern,
    Rule,
    RuleMeta,
    RuleTrigger,
)

logger = logging.getLogger("jarvis.rules")


class RuleEngine:
    """Load, match, and manage analysis rules (DB-backed)."""

    def __init__(self, rules_dir: Optional[Path] = None):
        self._rules_dir = rules_dir or RULES_DIR
        self._rules: Dict[str, Rule] = {}
        # Load from files synchronously on init (for first startup)
        self._load_from_files()

    # ------------------------------------------------------------------
    # Loading from files (seed data)
    # ------------------------------------------------------------------
    def _load_from_files(self):
        """Load rules from .md files (used as seed data)."""
        self._rules.clear()
        if not self._rules_dir.exists():
            return

        for md_path in self._rules_dir.rglob("*.md"):
            try:
                rule = self._parse_rule_file(md_path)
                if rule:
                    self._rules[rule.meta.id] = rule
            except Exception as e:
                logger.error("Failed to load rule file %s: %s", md_path, e)

        logger.info("Loaded %d rules from files: %s", len(self._rules), list(self._rules.keys()))

    @staticmethod
    def _parse_rule_file(path: Path) -> Optional[Rule]:
        post = frontmatter.load(str(path))
        meta_dict = dict(post.metadata)

        if "id" not in meta_dict:
            meta_dict["id"] = path.stem

        triggers_raw = meta_dict.pop("triggers", {})
        triggers = RuleTrigger(
            keywords=triggers_raw.get("keywords", []),
            priority=triggers_raw.get("priority", 5),
        )

        pe_raw = meta_dict.pop("pre_extract", [])
        pre_extract = [PreExtractPattern(**p) for p in pe_raw]

        meta = RuleMeta(
            triggers=triggers,
            pre_extract=pre_extract,
            **{k: v for k, v in meta_dict.items() if k in RuleMeta.model_fields},
        )

        return Rule(meta=meta, content=post.content, file_path=str(path))

    # ------------------------------------------------------------------
    # DB sync (called on startup after DB is initialized)
    # ------------------------------------------------------------------
    async def sync_files_to_db(self):
        """Sync file-based rules into the database. DB records take precedence."""
        from app.db.database import get_all_rules_from_db, upsert_rule_to_db

        db_rules = await get_all_rules_from_db()
        db_ids = {r["id"] for r in db_rules}

        synced = 0
        for rule_id, rule in self._rules.items():
            if rule_id not in db_ids:
                # New rule from file → insert to DB
                await upsert_rule_to_db({
                    "id": rule.meta.id,
                    "name": rule.meta.name,
                    "version": rule.meta.version,
                    "enabled": rule.meta.enabled,
                    "triggers": rule.meta.triggers.model_dump(),
                    "depends_on": rule.meta.depends_on,
                    "pre_extract": [p.model_dump() for p in rule.meta.pre_extract],
                    "needs_code": rule.meta.needs_code,
                    "content": rule.content,
                })
                synced += 1

        if synced:
            logger.info("Synced %d new file rules to DB", synced)

        # Reload from DB to get the complete set
        await self.reload_from_db()

    async def reload_from_db(self):
        """Reload in-memory rules from the database."""
        from app.db.database import get_all_rules_from_db

        db_rules = await get_all_rules_from_db()
        self._rules.clear()

        for r in db_rules:
            triggers = RuleTrigger(
                keywords=r.get("triggers", {}).get("keywords", []),
                priority=r.get("triggers", {}).get("priority", 5),
            )
            pre_extract = [PreExtractPattern(**p) for p in r.get("pre_extract", [])]
            meta = RuleMeta(
                id=r["id"],
                name=r.get("name", ""),
                version=r.get("version", 1),
                enabled=r.get("enabled", True),
                triggers=triggers,
                depends_on=r.get("depends_on", []),
                pre_extract=pre_extract,
                needs_code=r.get("needs_code", False),
            )
            self._rules[r["id"]] = Rule(meta=meta, content=r.get("content", ""))

        logger.info("Loaded %d rules from DB: %s", len(self._rules), list(self._rules.keys()))

    def reload(self):
        """Synchronous reload: from files. Use reload_from_db() for async DB reload."""
        self._load_from_files()

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------
    def classify(self, description: str) -> str:
        description_lower = description.lower()
        matches: List[tuple] = []

        for rule_id, rule in self._rules.items():
            if not rule.meta.enabled:
                continue
            for kw in rule.meta.triggers.keywords:
                if kw.lower() in description_lower:
                    matches.append((rule.meta.triggers.priority, rule_id))
                    break

        if not matches:
            return "general"

        matches.sort(key=lambda x: x[0], reverse=True)
        return matches[0][1]

    def match_rules(self, description: str) -> List[Rule]:
        primary_id = self.classify(description)
        primary = self.get_rule(primary_id)
        if not primary:
            fallback = self.get_rule("general")
            return [fallback] if fallback else []

        result = [primary]
        for dep_id in primary.meta.depends_on:
            dep = self.get_rule(dep_id)
            if dep and dep not in result:
                result.append(dep)

        return result

    # ------------------------------------------------------------------
    # CRUD (DB-backed)
    # ------------------------------------------------------------------
    def get_rule(self, rule_id: str) -> Optional[Rule]:
        return self._rules.get(rule_id)

    def list_rules(self) -> List[Rule]:
        return list(self._rules.values())

    async def save_rule(self, rule: Rule) -> Rule:
        """Save a rule to DB and update in-memory cache."""
        from app.db.database import upsert_rule_to_db

        await upsert_rule_to_db({
            "id": rule.meta.id,
            "name": rule.meta.name,
            "version": rule.meta.version,
            "enabled": rule.meta.enabled,
            "triggers": rule.meta.triggers.model_dump(),
            "depends_on": rule.meta.depends_on,
            "pre_extract": [p.model_dump() for p in rule.meta.pre_extract],
            "needs_code": rule.meta.needs_code,
            "content": rule.content,
        })

        self._rules[rule.meta.id] = rule
        logger.info("Saved rule to DB: %s", rule.meta.id)
        return rule

    async def delete_rule(self, rule_id: str) -> bool:
        from app.db.database import delete_rule_from_db

        ok = await delete_rule_from_db(rule_id)
        if ok:
            self._rules.pop(rule_id, None)
        return ok

    # ------------------------------------------------------------------
    # Workspace preparation
    # ------------------------------------------------------------------
    def prepare_workspace(
        self,
        workspace: Path,
        rules: List[Rule],
        log_paths: List[Path],
        code_repo: Optional[str] = None,
    ) -> Path:
        logs_dir = workspace / "logs"
        rules_dir = workspace / "rules"
        output_dir = workspace / "output"

        for d in (logs_dir, rules_dir, output_dir):
            d.mkdir(parents=True, exist_ok=True)

        for lp in log_paths:
            if lp.exists():
                dest = logs_dir / lp.name
                if not dest.exists():
                    shutil.copy2(lp, dest)

        for rule in rules:
            rule_dest = rules_dir / f"{rule.meta.id}.md"
            rule_dest.write_text(
                f"# {rule.meta.name or rule.meta.id}\n\n{rule.content}",
                encoding="utf-8",
            )

        if any(r.meta.needs_code for r in rules) and code_repo:
            code_link = workspace / "code"
            if not code_link.exists():
                code_link.symlink_to(code_repo)

        return workspace
