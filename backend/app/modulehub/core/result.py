"""Parsing of publish-result.json (schema: module-kit docs/orchestrator/publish-result.schema.json, v2)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

PLATFORMS = ("android", "ios")


class ResultInvalid(ValueError):
    pass


@dataclass(frozen=True)
class Artifact:
    coordinate: str
    sha256: str
    api_changes: List[str] = field(default_factory=list)
    download_urls: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {"coordinate": self.coordinate, "sha256": self.sha256, "apiChanges": list(self.api_changes)}


@dataclass(frozen=True)
class PublishResult:
    name: str
    version: str
    git_sha: str
    branch: str
    dry_run: bool
    platforms: Dict[str, Artifact]
    tag: str = ""


_REQUIRED = ("schemaVersion", "name", "version", "gitSha", "branch", "dryRun", "platforms")


def _artifact(platform: str, d: object) -> Artifact:
    if not isinstance(d, dict):
        raise ResultInvalid("platforms.%s must be an object" % platform)
    sha = d.get("sha256", "")
    if not isinstance(sha, str) or not re.match(r"^([0-9a-f]{64})?$", sha):
        raise ResultInvalid("platforms.%s: bad sha256" % platform)
    if not isinstance(d.get("coordinate", ""), str):
        raise ResultInvalid("platforms.%s: bad coordinate" % platform)
    return Artifact(coordinate=d.get("coordinate", ""), sha256=sha, api_changes=[str(x) for x in d.get("apiChanges", [])],
                    download_urls=[str(x) for x in d.get("downloadUrls", [])])


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
        (d["schemaVersion"] == 2, "schemaVersion must be 2"),
        (isinstance(d["name"], str) and re.match(r"^[a-z][a-z0-9]*$", d["name"]), "bad name"),
        (isinstance(d["version"], str) and re.match(r"^\d+\.\d+\.\d+$", d["version"]), "bad version"),
        (isinstance(d["gitSha"], str) and re.match(r"^[0-9a-f]{40}$", d["gitSha"]), "bad gitSha"),
        (isinstance(d["branch"], str), "bad branch"),
        (isinstance(d["dryRun"], bool), "dryRun must be a boolean"),
        (isinstance(d["platforms"], dict) and d["platforms"], "platforms must be a non-empty object"),
    ]
    for ok, msg in checks:
        if not ok:
            raise ResultInvalid(msg)
    unknown = [p for p in d["platforms"] if p not in PLATFORMS]
    if unknown:
        raise ResultInvalid("unknown platforms: %s" % unknown)
    return PublishResult(
        name=d["name"], version=d["version"], git_sha=d["gitSha"], branch=d["branch"], dry_run=d["dryRun"],
        platforms={p: _artifact(p, d["platforms"][p]) for p in PLATFORMS if p in d["platforms"]}, tag=str(d.get("tag", "")),
    )


def last_step(log: str) -> Optional[str]:
    """Last `STEP=` marker of `./mkw release` (the platform pipelines print `PLATFORM_STEP=`, which is ignored)."""
    steps = re.findall(r"^STEP=(\w+)\s*$", log, flags=re.M)
    return steps[-1] if steps else None
