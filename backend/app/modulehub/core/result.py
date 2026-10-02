"""Parsing of publish-result.json (schema: module-kit docs/orchestrator/publish-result.schema.json)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional


class ResultInvalid(ValueError):
    pass


@dataclass(frozen=True)
class PublishResult:
    platform: str
    name: str
    version: str
    sha256: str
    git_sha: str
    branch: str
    dry_run: bool
    coordinate: str = ""
    download_urls: List[str] = field(default_factory=list)
    api_changes: List[str] = field(default_factory=list)


_REQUIRED = ("schemaVersion", "platform", "name", "version", "sha256", "gitSha", "branch", "dryRun")


def parse_result(text: str) -> PublishResult:
    try:
        d = json.loads(text)
    except ValueError as e:
        raise ResultInvalid("not JSON: %s" % e)
    if not isinstance(d, dict):
        raise ResultInvalid("result must be a JSON object")
    missing = [k for k in _REQUIRED if k not in d]
    if missing:
        raise ResultInvalid("missing fields: %s" % missing)
    checks = [
        (d["schemaVersion"] == 1, "schemaVersion must be 1"),
        (d["platform"] in ("android", "ios"), "platform must be android or ios"),
        (isinstance(d["name"], str) and re.match(r"^[a-z][a-z0-9]*$", d["name"]), "bad name"),
        (isinstance(d["version"], str) and re.match(r"^\d+\.\d+\.\d+$", d["version"]), "bad version"),
        (isinstance(d["sha256"], str) and re.match(r"^([0-9a-f]{64})?$", d["sha256"]), "bad sha256"),
        (isinstance(d["gitSha"], str) and re.match(r"^[0-9a-f]{40}$", d["gitSha"]), "bad gitSha"),
        (isinstance(d["branch"], str), "bad branch"),
        (isinstance(d["dryRun"], bool), "dryRun must be a boolean"),
    ]
    for ok, msg in checks:
        if not ok:
            raise ResultInvalid(msg)
    return PublishResult(
        platform=d["platform"], name=d["name"], version=d["version"], sha256=d["sha256"],
        git_sha=d["gitSha"], branch=d["branch"], dry_run=d["dryRun"], coordinate=d.get("coordinate", ""),
        download_urls=list(d.get("downloadUrls", [])), api_changes=list(d.get("apiChanges", [])),
    )


def last_step(log: str) -> Optional[str]:
    steps = re.findall(r"^STEP=(\w+)\s*$", log, flags=re.M)
    return steps[-1] if steps else None
