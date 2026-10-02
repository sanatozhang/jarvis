"""Branch names, titles and bodies of the PRs modulehub opens (contracts: bump-pr.md, backport-pr.md)."""
from __future__ import annotations

import re
from typing import List


def slug(branch: str) -> str:
    return re.sub(r"[^a-z0-9._]+", "-", branch.lower()).strip("-")


def bump_branch(name: str, platform: str, target_branch: str) -> str:
    return "chore/jarvis/bump-%s-%s-%s" % (name, platform, slug(target_branch))


def bump_title(name: str, platform: str, old: str, new: str) -> str:
    return "chore: bump %s-%s %s -> %s" % (name, platform, old, new)


def backport_branch(name: str, version: str) -> str:
    return "chore/jarvis/backport-%s-%s" % (name, version)


def bump_body(*, name: str, platform: str, old: str, new: str, sha256: str, coordinate: str,
              changelog: List[str], api_changes: List[str], release_url: str) -> str:
    lines = ["## Bump %s-%s: %s -> %s" % (name, platform, old, new), ""]
    lines += ["### Changelog"] + (["- " + c for c in changelog] or ["No changelog entries."]) + [""]
    lines += ["### API changes"] + (["- " + a for a in api_changes] or ["No API changes reported."]) + [""]
    lines += ["### Artifact", "- coordinate: `%s`" % coordinate, "- sha256: `%s`" % sha256]
    if release_url:
        lines += ["", "Release record: %s" % release_url]
    lines += ["", "Only `modules.versions.toml` changes in this PR; roll back by reverting it."]
    return "\n".join(lines) + "\n"
