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
import re
from typing import Dict, List, Optional

import frontmatter

from app.config import RULES_DIR
from app.models.schemas import (
    PreExtractPattern,
    Rule,
    RuleMeta,
    RuleTrigger,
)
from app.services.issue_text import normalize_description_for_matching

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
        """Sync file-based rules into the database.

        Existing DB rules are updated only when the file version is newer, so
        deliberate UI edits are not overwritten by older seed data.
        """
        from app.db.database import get_all_rules_from_db, upsert_rule_to_db

        db_rules = await get_all_rules_from_db()
        db_rule_map = {r["id"]: r for r in db_rules}

        synced = 0
        for rule_id, rule in self._rules.items():
            payload = {
                "id": rule.meta.id,
                "name": rule.meta.name,
                "version": rule.meta.version,
                "enabled": rule.meta.enabled,
                "triggers": rule.meta.triggers.model_dump(),
                "depends_on": rule.meta.depends_on,
                "pre_extract": [p.model_dump() for p in rule.meta.pre_extract],
                "needs_code": rule.meta.needs_code,
                "content": rule.content,
            }

            db_rule = db_rule_map.get(rule_id)
            if not db_rule:
                # New rule from file → insert to DB
                await upsert_rule_to_db(payload)
                synced += 1
                continue

            db_version = int(db_rule.get("version", 0) or 0)
            if int(rule.meta.version or 0) > db_version:
                await upsert_rule_to_db({
                    **payload,
                })
                synced += 1

        if synced:
            logger.info("Synced %d file rules to DB", synced)

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
    @staticmethod
    def _keyword_matches(description: str, keyword: str) -> bool:
        desc = description.lower()
        kw = keyword.lower().strip()
        if not kw:
            return False

        # English/alphanumeric keywords should respect token boundaries so
        # "connect" does not match "connection".
        if re.fullmatch(r"[a-z0-9][a-z0-9 _./-]*", kw):
            pattern = rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])"
            return re.search(pattern, desc) is not None

        return kw in desc

    def _ranked_matches(self, description: str) -> List[tuple[int, int, int, str]]:
        text = normalize_description_for_matching(description)
        matches: List[tuple[int, int, int, str]] = []

        for rule_id, rule in self._rules.items():
            if not rule.meta.enabled:
                continue

            hit_keywords = [
                kw for kw in rule.meta.triggers.keywords
                if self._keyword_matches(text, kw)
            ]
            if not hit_keywords:
                continue

            matches.append((
                rule.meta.triggers.priority,
                len(hit_keywords),
                max(len(kw) for kw in hit_keywords),
                rule_id,
            ))

        matches.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
        return matches

    def classify(self, description: str) -> str:
        matches = self._ranked_matches(description)
        if not matches:
            return "general"

        return matches[0][3]

    def match_rules(self, description: str, max_rules: int = 3) -> List[Rule]:
        """Return up to max_rules matching rules (sorted by priority) plus their dependencies.

        Instead of only picking the single best match, we accumulate all keyword-matching
        rules so that Claude receives richer context when a ticket spans multiple topics
        (e.g. "录音丢失" + "蓝牙断连" would previously only trigger recording-missing).
        """
        matched = [
            item for item in self._ranked_matches(description)
            if item[3] != "general"
        ]
        top_ids = [rule_id for _, _, _, rule_id in matched[:max_rules]]

        # Fallback to general if nothing matched
        if not top_ids:
            top_ids = ["general"]

        result: List[Rule] = []
        seen: set = set()

        for rule_id in top_ids:
            rule = self.get_rule(rule_id)
            if rule and rule_id not in seen:
                result.append(rule)
                seen.add(rule_id)
            # Pull in depends_on for each matched rule
            if rule:
                for dep_id in rule.meta.depends_on:
                    dep = self.get_rule(dep_id)
                    if dep and dep_id not in seen:
                        result.append(dep)
                        seen.add(dep_id)

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
        images_dir = workspace / "images"
        rules_dir = workspace / "rules"
        output_dir = workspace / "output"

        for d in (logs_dir, images_dir, rules_dir, output_dir):
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

        # Write CLAUDE.md for persistent behavioral instructions
        # This is auto-loaded by the Claude CLI as system context,
        # more reliable than embedding in prompt.md
        self._write_workspace_claude_md(workspace)

        return workspace

    @staticmethod
    def _write_workspace_claude_md(workspace: Path) -> None:
        """Write a CLAUDE.md in the workspace with core analysis behavior rules."""
        claude_md = workspace / "CLAUDE.md"
        claude_md.write_text(
            """\
# 日志分析行为规则

你正在分析 Plaud 设备用户工单。以下规则在整个分析过程中始终有效。

## 必须遵守

1. **探索式分析**：你必须像有经验的工程师一样主动 grep 日志，至少执行 3 次独立 grep 命令，交叉印证后再下结论。
2. **不信任预提取**：prompt.md 中的预提取摘要仅用于定方向，所有关键证据必须自己从 logs/ 目录 grep 验证。
3. **查看上下文**：对关键日志行使用 `grep -A 5 -B 5` 查看前后上下文，不能只看单行。
4. **诚实置信度**：证据不足时设 confidence: low 和 needs_engineer: true，禁止编造结论。
5. **结果写文件**：最终 JSON 必须写入 output/result.json。

## 禁止行为

- 看完预提取摘要就直接输出 result.json（跳过 grep 验证）
- 在没有日志证据支撑时给出 high confidence
- user_reply 中使用技术术语（用户是普通消费者）

## 工作空间

- `logs/` — 解密后的完整日志，可直接 grep
- `rules/` — 排查规则，按步骤执行
- `images/` — 用户截图（如有），请查看
- `code/` — 代码仓库（如有）
- `output/` — 将 result.json 写到这里
""",
            encoding="utf-8",
        )
