"""
Flutter pubspec.yaml `version:` line rewriter.

Used by the release-branch creation flow. Two operations in one pass:

  1. Replace the semver `X.Y.Z` with the version parsed from the release
     branch name (e.g. `release/3.18.0_0521` → write `3.18.0`).
  2. Increment the `+N` build counter by 1 (so each pushed release branch
     gets a fresh, unique upload number for the stores).

Example:
  pubspec.yaml `version: 3.17.1+712`,
  branch `release/3.18.0_0521`
  → `version: 3.18.0+713`
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Tuple

# Match the `version:` line, capturing the X.Y.Z semver, the +N build
# counter, and the trailing whitespace/comment so we can preserve it.
_VERSION_RE = re.compile(
    r"^(?P<prefix>version:[ \t]*)(?P<semver>\d+\.\d+\.\d+)\+(?P<build>\d+)(?P<suffix>[ \t]*(?:#.*)?)$",
    re.MULTILINE,
)


class PubspecBumpError(ValueError):
    """Raised when pubspec.yaml has no parseable `version: X.Y.Z+N` line."""


def bump_to(pubspec_path: Path, new_semver: str) -> Tuple[str, str]:
    """Replace semver with `new_semver` and bump +N. Return (before, after).

    Raises PubspecBumpError if the file doesn't exist or has no parseable
    version line.
    """
    if not pubspec_path.exists():
        raise PubspecBumpError(f"pubspec.yaml not found: {pubspec_path}")
    if not re.match(r"^\d+\.\d+\.\d+$", new_semver):
        raise PubspecBumpError(f"new_semver must be X.Y.Z, got: {new_semver!r}")

    content = pubspec_path.read_text(encoding="utf-8")
    m = _VERSION_RE.search(content)
    if not m:
        raise PubspecBumpError(
            "Cannot find a `version: X.Y.Z+N` line in pubspec.yaml. "
            "Expected format e.g. `version: 3.18.1+715`."
        )

    old_semver = m.group("semver")
    old_build = int(m.group("build"))
    version_before = f"{old_semver}+{old_build}"
    version_after = f"{new_semver}+{old_build + 1}"

    new_line = f"{m.group('prefix')}{version_after}{m.group('suffix')}"
    new_content = content[: m.start()] + new_line + content[m.end():]
    pubspec_path.write_text(new_content, encoding="utf-8")
    return version_before, version_after


def read_current_version(pubspec_path: Path) -> str:
    """Read `X.Y.Z+N` without modifying. Used for pre-bump display / audit."""
    if not pubspec_path.exists():
        raise PubspecBumpError(f"pubspec.yaml not found: {pubspec_path}")
    content = pubspec_path.read_text(encoding="utf-8")
    m = _VERSION_RE.search(content)
    if not m:
        raise PubspecBumpError("Cannot parse current version from pubspec.yaml")
    return f"{m.group('semver')}+{m.group('build')}"
