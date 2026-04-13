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

        # Write behavioral instructions for both Claude CLI and Codex
        self._write_workspace_instructions(workspace)

        return workspace

    @staticmethod
    def _write_workspace_instructions(workspace: Path) -> None:
        """Write behavioral instructions to the workspace.

        - CLAUDE.md: auto-loaded by Claude CLI as persistent system context.
        - AGENTS.md: read by Codex via prompt instruction.

        Both files have the same content so the rules apply regardless
        of which agent is selected.
        """
        content = """\
# 日志分析行为规则

你正在分析 Plaud 设备用户工单。以下规则在整个分析过程中始终有效。

## 必须遵守

1. **探索式分析**：主动 grep 日志，交叉印证后再下结论。
2. **不信任预提取**：预提取摘要仅用于定方向，关键证据必须从 logs/ 目录 grep 验证。
3. **查看上下文**：`grep -A 5 -B 5` 查看前后上下文，不能只看单行。
4. **诚实置信度**：证据不足时设 confidence: low 和 needs_engineer: true，禁止编造结论。
5. **必须写 result.json**：分析完成后**必须**用 Write 工具写入 `output/result.json`。不要只在 stdout 输出文字总结——系统**只从 result.json 读取结果**，不写此文件=分析失败。写完后立即 `cat output/result.json` 验证。
6. **效率优先**：不要做重复的 grep，不要浏览不相关的文件。聚焦于问题核心，尽量在 15 轮以内完成。

## 禁止行为

- 不写 result.json（系统无法读取纯文本输出，分析会被标记为失败）
- 看完预提取就直接输出 result.json（跳过 grep 验证）
- 没有日志证据支撑时给 high confidence
- user_reply 使用技术术语（用户是普通消费者）
- 超过 20 轮 grep 仍未写 result.json（说明方向错了，应尽快输出当前最佳判断）

## 工作空间

- `logs/` — 解密后的完整日志，可直接 grep
- `rules/` — 排查规则，按步骤执行
- `images/` — 用户截图（如有），请查看
- `code/` — 代码仓库（如有）
- `output/` — 将 result.json 写到这里

## 输出 JSON Schema

写入 `output/result.json`，同时 `cat output/result.json` 打印到 stdout。
**每个字段都必须同时提供中文和英文版本（_en 后缀），不能为空。**

```json
{
    "problem_type": "问题分类（中文）",
    "problem_type_en": "Problem Type (English)",
    "root_cause": "根本原因（中文，2-5 句话）",
    "root_cause_en": "Root cause (English, 2-5 sentences)",
    "confidence": "high / medium / low",
    "confidence_reason": "置信度理由",
    "key_evidence": ["关键日志行1", "关键日志行2（最多5条）"],
    "user_reply": "完整中文客服回复模板",
    "user_reply_en": "Complete English reply template",
    "needs_engineer": false,
    "fix_suggestion": ""
}
```

## Confidence 标准

- **high**: 日志有明确证据，root cause 确定，有 3 条以上 grep 佐证
- **medium**: 有线索但不完全确定，或多种可能原因
- **low**: 日志不足，需工程师介入

证据不足时**必须** low + needs_engineer: true。错误分析比"不确定"更有害。

## user_reply 格式要求

客服直接复制发给用户，必须完整、礼貌、非技术化。

好的示例:
```
您好，经过日志分析，您在 12月1日 的录音已成功传输到 APP。但由于设备时间偏移，该录音显示为 2023年9月24日。请在 APP 中查找该日期、时长约 39 分钟的录音。如需帮助请联系我们。
```

坏的示例（禁止）:
```
Timestamp drift caused keyId-sessionId mismatch.
```
（太技术化，用户看不懂）
"""
        # CLAUDE.md — auto-loaded by Claude CLI
        (workspace / "CLAUDE.md").write_text(content, encoding="utf-8")
        # AGENTS.md — referenced by Codex prompt
        (workspace / "AGENTS.md").write_text(content, encoding="utf-8")
