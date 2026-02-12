"""
Rule Engine - loads, matches, and manages analysis rules.

Rules live in backend/rules/ as Markdown files with YAML front matter.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional

import frontmatter
import yaml

from app.config import RULES_DIR
from app.models.schemas import (
    PreExtractPattern,
    Rule,
    RuleMeta,
    RuleTrigger,
)

logger = logging.getLogger("jarvis.rules")


class RuleEngine:
    """Load, match, and manage analysis rules."""

    def __init__(self, rules_dir: Optional[Path] = None):
        self._rules_dir = rules_dir or RULES_DIR
        self._rules: Dict[str, Rule] = {}
        self.reload()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def reload(self):
        """(Re)load all rules from disk."""
        self._rules.clear()
        if not self._rules_dir.exists():
            logger.warning("Rules dir does not exist: %s", self._rules_dir)
            return

        for md_path in self._rules_dir.rglob("*.md"):
            try:
                rule = self._load_rule_file(md_path)
                if rule:
                    self._rules[rule.meta.id] = rule
            except Exception as e:
                logger.error("Failed to load rule %s: %s", md_path, e)

        # Also scan custom/ subdir
        custom_dir = self._rules_dir / "custom"
        if custom_dir.exists():
            for md_path in custom_dir.rglob("*.md"):
                try:
                    rule = self._load_rule_file(md_path)
                    if rule:
                        self._rules[rule.meta.id] = rule
                except Exception as e:
                    logger.error("Failed to load custom rule %s: %s", md_path, e)

        logger.info("Loaded %d rules: %s", len(self._rules), list(self._rules.keys()))

    def _load_rule_file(self, path: Path) -> Optional[Rule]:
        post = frontmatter.load(str(path))
        meta_dict = dict(post.metadata)

        if "id" not in meta_dict:
            meta_dict["id"] = path.stem

        # Parse triggers
        triggers_raw = meta_dict.pop("triggers", {})
        triggers = RuleTrigger(
            keywords=triggers_raw.get("keywords", []),
            priority=triggers_raw.get("priority", 5),
        )

        # Parse pre_extract patterns
        pe_raw = meta_dict.pop("pre_extract", [])
        pre_extract = [PreExtractPattern(**p) for p in pe_raw]

        meta = RuleMeta(
            triggers=triggers,
            pre_extract=pre_extract,
            **{k: v for k, v in meta_dict.items() if k in RuleMeta.model_fields},
        )

        return Rule(meta=meta, content=post.content, file_path=str(path))

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------
    def classify(self, description: str) -> str:
        """Return the best-matching rule ID based on problem description."""
        description_lower = description.lower()
        matches: List[tuple] = []  # (priority, rule_id)

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
        """Return matched rules including dependencies, ordered by priority."""
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
    # CRUD
    # ------------------------------------------------------------------
    def get_rule(self, rule_id: str) -> Optional[Rule]:
        return self._rules.get(rule_id)

    def list_rules(self) -> List[Rule]:
        return list(self._rules.values())

    def save_rule(self, rule: Rule) -> Rule:
        """Save (create or update) a rule to disk."""
        # Determine path
        if rule.file_path and Path(rule.file_path).exists():
            path = Path(rule.file_path)
        else:
            path = self._rules_dir / f"{rule.meta.id}.md"

        # Build YAML front matter
        meta_dict = rule.meta.model_dump(exclude_none=True)
        # Convert triggers and pre_extract back to simple dicts
        meta_dict["triggers"] = {
            "keywords": rule.meta.triggers.keywords,
            "priority": rule.meta.triggers.priority,
        }
        meta_dict["pre_extract"] = [p.model_dump() for p in rule.meta.pre_extract]

        post = frontmatter.Post(content=rule.content, **meta_dict)
        path.write_text(frontmatter.dumps(post), encoding="utf-8")

        rule.file_path = str(path)
        self._rules[rule.meta.id] = rule
        logger.info("Saved rule: %s → %s", rule.meta.id, path)
        return rule

    def delete_rule(self, rule_id: str) -> bool:
        rule = self._rules.pop(rule_id, None)
        if rule and rule.file_path:
            p = Path(rule.file_path)
            if p.exists():
                p.unlink()
                return True
        return False

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
        """
        Prepare an isolated workspace directory for an Agent session.

        workspace/
        ├── logs/           ← decrypted log files
        ├── rules/          ← matched rule files
        ├── code/           ← symlink to code repo (if needed)
        └── output/         ← Agent writes result here
        """
        logs_dir = workspace / "logs"
        rules_dir = workspace / "rules"
        output_dir = workspace / "output"

        for d in (logs_dir, rules_dir, output_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Copy logs
        for lp in log_paths:
            if lp.exists():
                dest = logs_dir / lp.name
                if not dest.exists():
                    shutil.copy2(lp, dest)

        # Copy rules
        for rule in rules:
            rule_dest = rules_dir / f"{rule.meta.id}.md"
            rule_dest.write_text(
                f"# {rule.meta.name or rule.meta.id}\n\n{rule.content}",
                encoding="utf-8",
            )

        # Symlink code repo if any rule needs it
        if any(r.meta.needs_code for r in rules) and code_repo:
            code_link = workspace / "code"
            if not code_link.exists():
                code_link.symlink_to(code_repo)

        return workspace
