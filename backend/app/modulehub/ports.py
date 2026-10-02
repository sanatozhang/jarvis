"""Ports: everything modulehub needs from the outside world.

Only `adapters/` implements these against jarvis infrastructure; moving modulehub to another system
means re-implementing the adapters, nothing else (see docs/modulehub/migration-guide.md).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class BuildHandle:
    ref: str            # opaque reference the BuildRunner can poll
    url: str = ""


@dataclass(frozen=True)
class BuildStatus:
    state: str                         # queued | running | success | failure | aborted
    url: str = ""
    log_tail: str = ""                 # tail of the console log (carries the STEP= marker lines)
    result_json: Optional[str] = None  # content of publish-result.json when the job succeeded


class BuildRunner(Protocol):
    async def trigger(self, *, repo: str, branch: str, platforms: List[str], major: bool, dry_run: bool, resume: bool) -> BuildHandle: ...

    async def status(self, ref: str) -> BuildStatus: ...


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    created: bool = True       # False when an existing open PR was updated
    conflict: bool = False


class ScmHost(Protocol):
    async def read_file(self, repo: str, ref: str, path: str) -> Optional[str]: ...

    async def list_branches(self, repo: str, prefix: str) -> List[str]: ...

    async def create_branch(self, repo: str, name: str, from_ref: str) -> None: ...

    async def branch_contains(self, repo: str, branch: str, ref: str) -> bool: ...

    async def commits_between(self, repo: str, base_ref: str, head_ref: str) -> List[str]: ...

    async def changed_files(self, repo: str, base_ref: str, head_ref: str) -> List[str]: ...

    async def open_or_update_file_pr(self, *, repo: str, base: str, branch: str, path: str, content: str,
                                     title: str, body: str, commit_message: str) -> PullRequest: ...

    async def open_backport_pr(self, *, repo: str, branch: str, base: str, commit_range: str,
                               title: str, body: str) -> PullRequest: ...

    async def range_applied_on(self, repo: str, base: str, commit_range: str) -> bool: ...


class Notifier(Protocol):
    async def notify(self, text: str) -> None: ...


@dataclass
class ReleaseRecord:
    """One release of one module repo: the selected platforms ship as one version (one tag)."""
    id: Optional[int] = None
    module: str = ""
    repo: str = ""                                               # module repo, from the shells' modules.versions.toml
    platforms: List[str] = field(default_factory=list)          # canonical order: android, ios
    branch: str = ""
    major: bool = False
    kind: str = "release"              # release | preview
    state: str = "pending"
    failed_from: str = ""
    version: str = ""
    git_sha: str = ""
    # platform -> {"coordinate", "sha256", "apiChanges"} from publish-result.json
    artifacts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    previous_versions: Dict[str, str] = field(default_factory=dict)  # platform -> version its shell pinned before
    bump_prs: Dict[str, str] = field(default_factory=dict)           # platform -> PR url ("" = shell already pinned it)
    build_ref: str = ""
    build_url: str = ""
    backport_pr_url: str = ""
    error: str = ""
    requested_by: str = ""
    resume_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Store(Protocol):
    async def create(self, rec: ReleaseRecord) -> ReleaseRecord: ...

    async def get(self, release_id: int) -> Optional[ReleaseRecord]: ...

    async def save(self, rec: ReleaseRecord, event: str = "") -> None: ...

    async def find_active(self, module: str) -> Optional[ReleaseRecord]: ...

    async def list_in_flight(self) -> List[ReleaseRecord]: ...

    async def list_recent(self, limit: int = 50) -> List[ReleaseRecord]: ...

    async def log_mirror(self, module: str, branch: str, action: str, outcome: str) -> None: ...


@dataclass(frozen=True)
class ModuleHubSettings:
    """Plain settings the core services need (the adapter layer builds this from jarvis config)."""
    shell_repos: Dict[str, str]                 # platform -> "owner/shell-repo"
    versions_path: str = "modules.versions.toml"
    base_url: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
