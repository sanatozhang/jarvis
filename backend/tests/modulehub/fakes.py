"""In-memory fakes of the modulehub ports."""
from __future__ import annotations

import copy
import json
from typing import Dict, List, Optional

from app.modulehub.ports import BuildHandle, BuildStatus, ModuleHubSettings, PullRequest, ReleaseRecord

SHA = "a" * 64
GIT = "b" * 40

TOML = '[logger]\nversion = "1.0.0"\nsha256 = "%s"\nrepo = "Plaud-AI/mobile_logger"\n' % ("c" * 64)

SETTINGS = ModuleHubSettings(shell_repos={"android": "Plaud-AI/shell-android", "ios": "Plaud-AI/shell-ios"},
                             base_url="https://jarvis.example")


IOS_SHA = "d" * 64


def result_json(platforms=("android", "ios"), sha256=None, **kw) -> str:
    """A v2 publish-result for `platforms`; `sha256` overrides every platform's sha (e.g. "" for a dry run)."""
    version = kw.get("version", "1.1.0")
    arts = {
        "android": {"coordinate": "ai.plaud.module:logger:%s" % version, "sha256": SHA, "apiChanges": []},
        "ios": {"coordinate": "ai/plaud/module/logger-ios/%s/Logger-%s.xcframework.zip" % (version, version),
                "sha256": IOS_SHA, "apiChanges": ["Func LoggerAPI.flush() has been removed"], "product": "Logger"},
    }
    d = {"schemaVersion": 2, "name": "logger", "version": version, "gitSha": GIT, "branch": "main", "dryRun": False,
         "major": False, "tag": "v" + version, "platforms": {p: arts[p] for p in platforms}}
    if sha256 is not None:
        for a in d["platforms"].values():
            a["sha256"] = sha256
    d.update(kw)
    return json.dumps(d)


class MemStore:
    def __init__(self):
        self.recs: Dict[int, ReleaseRecord] = {}
        self.events: List[tuple] = []
        self.mirror: List[tuple] = []
        self._id = 0

    async def create(self, rec):
        self._id += 1
        rec.id = self._id
        self.recs[rec.id] = copy.copy(rec)
        return rec

    async def get(self, release_id):
        r = self.recs.get(release_id)
        return copy.copy(r) if r else None

    async def save(self, rec, event=""):
        self.recs[rec.id] = copy.copy(rec)
        self.events.append((rec.id, rec.state, event))

    async def find_active(self, module):
        for r in self.recs.values():
            if r.kind == "release" and r.module == module and r.state in ("pending", "building", "tagged", "pr_opened", "backport_opened"):
                return copy.copy(r)
        return None

    async def list_in_flight(self):
        return [copy.copy(r) for r in self.recs.values() if r.state in ("pending", "building", "tagged", "pr_opened", "backport_opened")]

    async def list_recent(self, limit=50):
        return [copy.copy(r) for r in list(self.recs.values())[-limit:]][::-1]

    async def log_mirror(self, module, branch, action, outcome):
        self.mirror.append((module, branch, action, outcome))


class FakeBuild:
    def __init__(self):
        self.triggers: List[dict] = []
        self.next_status = BuildStatus("queued")
        self.raise_on_trigger: Optional[Exception] = None

    async def trigger(self, **kw):
        if self.raise_on_trigger:
            raise self.raise_on_trigger
        self.triggers.append(copy.deepcopy(kw))
        return BuildHandle(ref="job-%d" % len(self.triggers), url="https://jenkins/%d" % len(self.triggers))

    async def status(self, ref):
        return self.next_status


class FakeScm:
    def __init__(self):
        self.files: Dict[tuple, str] = {}
        self.branches: Dict[str, List[str]] = {}
        self.contains: Dict[tuple, bool] = {}
        self.changed: Dict[tuple, List[str]] = {}
        self.created: List[tuple] = []
        self.file_prs: List[dict] = []
        self.backport_prs: List[dict] = []
        self.commits: List[str] = ["feat: a (#1)"]
        self.applied = False
        self.conflict = False
        self.fail_file_pr: Optional[Exception] = None
        self.fail_backport: Optional[Exception] = None
        self.fail_commits = False

    async def read_file(self, repo, ref, path):
        return self.files.get((repo, ref, path))

    async def list_branches(self, repo, prefix):
        return [b for b in self.branches.get(repo, []) if b.startswith(prefix)]

    async def create_branch(self, repo, name, from_ref):
        self.created.append((repo, name, from_ref))

    async def branch_contains(self, repo, branch, ref):
        return self.contains.get((repo, branch, ref), True)

    async def commits_between(self, repo, base_ref, head_ref):
        if self.fail_commits:
            raise RuntimeError("compare failed")
        return self.commits

    async def changed_files(self, repo, base_ref, head_ref):
        return self.changed.get((repo, base_ref, head_ref), [])

    async def open_or_update_file_pr(self, **kw):
        if self.fail_file_pr:
            raise self.fail_file_pr
        self.file_prs.append(kw)
        return PullRequest(number=len(self.file_prs), url="https://github/pr/%d" % len(self.file_prs))

    async def open_backport_pr(self, **kw):
        if self.fail_backport:
            raise self.fail_backport
        self.backport_prs.append(kw)
        return PullRequest(number=90 + len(self.backport_prs), url="https://github/pr/9%d" % len(self.backport_prs), conflict=self.conflict)

    async def range_applied_on(self, repo, base, commit_range):
        return self.applied


class RecNotifier:
    def __init__(self):
        self.messages: List[str] = []

    async def notify(self, text):
        self.messages.append(text)
