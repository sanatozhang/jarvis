"""Use cases: start / tick / resume a release, and reconcile release branches.

Depends only on `core` and `ports`; every side effect goes through a port.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from app.modulehub.core import backport, mirror, naming, release_rules, states, versions_toml
from app.modulehub.core.result import PublishResult, ResultInvalid, last_step, parse_result
from app.modulehub.ports import BuildRunner, ModuleHubSettings, Notifier, ReleaseRecord, ScmHost, Store

logger = logging.getLogger("jarvis.modulehub")


class ReleaseConflict(RuntimeError):
    """Another release of the same module is active (one module = one version line), or it cannot be resumed."""


class ReleaseNotFound(LookupError):
    pass


class ReleaseService:
    def __init__(self, *, store: Store, build: BuildRunner, scm: ScmHost, notifier: Notifier, settings: ModuleHubSettings):
        self.store, self.build, self.scm, self.notifier, self.settings = store, build, scm, notifier, settings

    def _repo(self, module: str) -> str:
        return release_rules.module_repo(self.settings.module_repo_owner, module)

    # ---- queries -----------------------------------------------------------------------------
    async def list_modules(self) -> List[Dict[str, object]]:
        """One entry per module repo with the version each shell pins on main."""
        modules: Dict[str, Dict[str, object]] = {}
        for platform, shell in sorted(self.settings.shell_repos.items()):
            text = await self.scm.read_file(shell, "main", self.settings.versions_path)
            if text is None:
                continue
            for name, table in versions_toml.parse(text).items():
                entry = modules.setdefault(name, {"name": name, "repo": table.get("repo", ""), "platforms": {}})
                entry["platforms"][platform] = table.get("version", "")  # type: ignore[index]
        return [modules[n] for n in sorted(modules)]

    # ---- start -------------------------------------------------------------------------------
    async def start(self, *, module: str, platforms: List[str], branch: str, major: bool, actor: str,
                    dry_run: bool = False) -> ReleaseRecord:
        platforms = release_rules.normalize_platforms(platforms)
        release_rules.validate_request(branch, major)
        for platform in platforms:
            shell = self.settings.shell_repos.get(platform)
            if not shell:
                raise release_rules.InvalidRequest("no shell repository configured for %s" % platform)
            text = await self.scm.read_file(shell, branch, self.settings.versions_path)
            if text is None:
                raise release_rules.InvalidRequest("%s has no %s on branch %s" % (shell, self.settings.versions_path, branch))
            if module not in versions_toml.parse(text):
                raise release_rules.InvalidRequest("module %r is not listed in %s on %s" % (module, shell, branch))
        if not dry_run:
            active = await self.store.find_active(module)
            if active:
                raise ReleaseConflict("release %s of %s is already in progress" % (active.id, module))
        rec = await self.store.create(ReleaseRecord(module=module, platforms=platforms, branch=branch, major=major,
                                                    kind="preview" if dry_run else "release", requested_by=actor))
        await self._trigger(rec, dry_run=dry_run, resume=False)
        return rec

    async def _trigger(self, rec: ReleaseRecord, *, dry_run: bool, resume: bool) -> None:
        try:
            handle = await self.build.trigger(repo=self._repo(rec.module), branch=rec.branch, platforms=list(rec.platforms),
                                              major=rec.major, dry_run=dry_run, resume=resume)
        except Exception as e:  # the build runner is an external system: any failure becomes a failed release
            await self._fail(rec, states.BUILDING, "could not trigger the publish job: %s" % e)
            return
        rec.build_ref, rec.build_url = handle.ref, handle.url
        rec.state = states.next_state(rec.state, "resume") if rec.state == states.FAILED else states.next_state(rec.state, "build_triggered")
        rec.failed_from, rec.error = "", ""
        await self.store.save(rec, "build triggered")

    # ---- progress ----------------------------------------------------------------------------
    async def tick_all(self) -> None:
        for rec in await self.store.list_in_flight():
            try:
                await self.tick(rec)
            except Exception:  # one bad release must never stop the others
                logger.exception("modulehub tick failed for release %s", rec.id)

    async def tick(self, rec: ReleaseRecord) -> ReleaseRecord:
        if rec.state == states.BUILDING:
            await self._poll_build(rec)
        if rec.state == states.TAGGED:
            await self._open_bump_prs(rec)
        if rec.state == states.PR_OPENED:
            await self._backport_or_finish(rec)
        if rec.state == states.BACKPORT_OPENED:
            await self._finish(rec)
        return rec

    async def _poll_build(self, rec: ReleaseRecord) -> None:
        st = await self.build.status(rec.build_ref)
        rec.build_url = st.url or rec.build_url
        if st.state in ("queued", "running"):
            await self.store.save(rec)
            return
        if st.state != "success":
            failed_from = states.failed_from_job(last_step=last_step(st.log_tail))
            await self._fail(rec, failed_from, "publish job %s (last step: %s)" % (st.state, last_step(st.log_tail) or "unknown"))
            return
        try:
            res = parse_result(st.result_json or "")
            self._check_result(rec, res)
        except (ResultInvalid, ValueError) as e:
            await self._fail(rec, states.BUILDING, "invalid publish result: %s" % e)
            return
        rec.version, rec.git_sha = res.version, res.git_sha
        rec.artifacts = {p: a.as_dict() for p, a in res.platforms.items()}
        if rec.kind == "preview":
            rec.state = states.DONE
            await self.store.save(rec, "preview ready: %s" % res.version)
            return
        rec.state = states.next_state(rec.state, "job_succeeded")
        await self.store.save(rec, "published %s (%s)" % (res.version, ",".join(rec.platforms)))

    @staticmethod
    def _check_result(rec: ReleaseRecord, res: PublishResult) -> None:
        if res.name != rec.module or sorted(res.platforms) != sorted(rec.platforms):
            raise ValueError("result is for %s %s, expected %s %s" % (res.name, sorted(res.platforms), rec.module, rec.platforms))
        if rec.kind == "release" and res.dry_run:
            raise ValueError("job ran as a dry run")
        if rec.kind == "release" and not all(a.sha256 for a in res.platforms.values()):
            raise ValueError("release result lacks a sha256")

    async def _open_bump_prs(self, rec: ReleaseRecord) -> None:
        """One bump PR per shipped platform. Idempotent: platforms already handled are skipped on resume."""
        for platform in rec.platforms:
            if platform in rec.bump_prs:
                continue
            try:
                await self._open_bump_pr(rec, platform)
            except Exception as e:
                await self._fail(rec, states.TAGGED, "bump PR for %s failed: %s" % (platform, e))
                return
            await self.store.save(rec, "bump PR %s: %s" % (platform, rec.bump_prs[platform] or "shell already pins %s" % rec.version))
        rec.state = states.next_state(rec.state, "bump_pr_opened")
        await self.store.save(rec, "bump PRs ready")

    async def _open_bump_pr(self, rec: ReleaseRecord, platform: str) -> None:
        shell = self.settings.shell_repos[platform]
        text = await self.scm.read_file(shell, rec.branch, self.settings.versions_path)
        if text is None:
            raise ValueError("%s has no %s on %s" % (shell, self.settings.versions_path, rec.branch))
        old = versions_toml.read_module(text, rec.module)["version"]
        rec.previous_versions[platform] = old
        if old == rec.version:
            rec.bump_prs[platform] = ""
            return
        try:
            changelog = await self.scm.commits_between(self._repo(rec.module), "v" + old, "v" + rec.version)
        except Exception:  # changelog is decoration, never a reason to fail the bump
            changelog = []
        art = rec.artifacts.get(platform, {})
        title = naming.bump_title(rec.module, platform, old, rec.version)
        body = naming.bump_body(name=rec.module, platform=platform, old=old, new=rec.version, sha256=str(art.get("sha256", "")),
                                coordinate=str(art.get("coordinate", "")), changelog=changelog,
                                api_changes=list(art.get("apiChanges", [])),
                                release_url="%s/modulehub/releases/%s" % (self.settings.base_url.rstrip("/"), rec.id) if self.settings.base_url else "")
        pr = await self.scm.open_or_update_file_pr(
            repo=shell, base=rec.branch, branch=naming.bump_branch(rec.module, platform, rec.branch),
            path=self.settings.versions_path,
            content=versions_toml.rewrite_module(text, rec.module, version=rec.version, sha256=str(art.get("sha256", ""))),
            title=title, body=body, commit_message=title)
        rec.bump_prs[platform] = pr.url

    def _previous_version(self, rec: ReleaseRecord) -> str:
        """Newest version the shells pinned on this branch before the release: start of the backport range."""
        known = [v for v in rec.previous_versions.values() if v and v != rec.version]
        return max(known, key=release_rules.semver_key) if known else ""

    async def _backport_or_finish(self, rec: ReleaseRecord) -> None:
        repo = self._repo(rec.module)
        try:
            plan = backport.plan_backport(rec.module, rec.branch, rec.version, self._previous_version(rec), already_on_main=False)
            if plan is not None and await self.scm.range_applied_on(repo, "main", plan.commit_range):
                plan = None
            if plan is None:
                await self._finish(rec)
                return
            pr = await self.scm.open_backport_pr(repo=repo, branch=plan.branch, base=plan.base,
                                                 commit_range=plan.commit_range, title=plan.title, body=plan.body)
        except Exception as e:
            await self._fail(rec, states.PR_OPENED, "backport PR failed: %s" % e)
            return
        rec.backport_pr_url = pr.url
        rec.state = states.next_state(rec.state, "backport_opened")
        await self.store.save(rec, "backport PR %s" % ("opened with CONFLICT" if pr.conflict else "opened"))
        if pr.conflict:
            await self.notifier.notify("modulehub: backport of %s %s into main conflicts, resolve manually: %s" % (rec.module, rec.version, pr.url))
        await self._finish(rec)

    async def _finish(self, rec: ReleaseRecord) -> None:
        rec.state = states.next_state(rec.state, "finish")
        await self.store.save(rec, "done")
        prs = ", ".join("%s: %s" % (p, rec.bump_prs.get(p) or "none needed") for p in rec.platforms)
        await self.notifier.notify("modulehub: released %s %s (%s); bump PRs: %s" % (rec.module, rec.version, ",".join(rec.platforms), prs))

    async def _fail(self, rec: ReleaseRecord, failed_from: str, error: str) -> None:
        rec.state = states.FAILED
        rec.failed_from, rec.error = failed_from, error
        await self.store.save(rec, error)
        await self.notifier.notify("modulehub: release %s of %s (%s) failed: %s (%s)" % (rec.id, rec.module, ",".join(rec.platforms), error, rec.build_url))

    # ---- resume ------------------------------------------------------------------------------
    async def resume(self, release_id: int, actor: str) -> ReleaseRecord:
        rec = await self.store.get(release_id)
        if rec is None:
            raise ReleaseNotFound(release_id)
        if rec.state != states.FAILED:
            raise ReleaseConflict("release %s is %s, only failed releases can be resumed" % (release_id, rec.state))
        action = states.resume_action(rec.failed_from)
        if action is None:
            raise ReleaseConflict("release %s failed before anything was uploaded; start a new release" % release_id)
        active = await self.store.find_active(rec.module)
        if active:
            raise ReleaseConflict("release %s of %s is already in progress" % (active.id, rec.module))
        rec.resume_count += 1
        rec.requested_by = actor
        if action == "rerun_job_resume":
            await self._trigger(rec, dry_run=False, resume=True)
        else:
            rec.state = states.TAGGED if action == "open_bump_pr" else states.PR_OPENED
            rec.failed_from, rec.error = "", ""
            await self.store.save(rec, "resumed: %s" % action)
            await self.tick(rec)
        return rec


class MirrorService:
    def __init__(self, *, store: Store, scm: ScmHost, notifier: Notifier, settings: ModuleHubSettings):
        self.store, self.scm, self.notifier, self.settings = store, scm, notifier, settings

    async def sync(self) -> List[str]:
        """Reconcile each module repo's release branches with both shells'. Returns human-readable results."""
        mains: Dict[str, str] = {}
        branch_texts: Dict[str, Dict[str, Optional[str]]] = {}   # platform -> shell branch -> toml
        for platform, shell in sorted(self.settings.shell_repos.items()):
            text = await self.scm.read_file(shell, "main", self.settings.versions_path)
            if text is None:
                continue
            mains[platform] = text
            branch_texts[platform] = {b: await self.scm.read_file(shell, b, self.settings.versions_path)
                                      for b in await self.scm.list_branches(shell, "release/")}
        modules = sorted({m for text in mains.values() for m in versions_toml.module_names(text)})
        report: List[str] = []
        for module in modules:
            repo = release_rules.module_repo(self.settings.module_repo_owner, module)
            pins: Dict[str, Dict[str, Optional[str]]] = {}
            for platform, texts in branch_texts.items():
                for b, t in texts.items():
                    pins.setdefault(b, {})[platform] = self._pin(t, module)
            module_branches = await self.scm.list_branches(repo, "release/")
            for act in mirror.plan_mirror(pins=pins, module_branches=module_branches):
                outcome = await self._execute(repo, act)
                await self.store.log_mirror(module, act.branch, act.kind, outcome)
                report.append("%s %s %s: %s" % (module, act.kind, act.branch, outcome))
        return report

    @staticmethod
    def _pin(text: Optional[str], module: str) -> Optional[str]:
        if not text:
            return None
        try:
            return versions_toml.read_module(text, module)["version"]
        except versions_toml.ModuleNotInToml:
            return None

    async def _diverged(self, repo: str, act: mirror.Action) -> Optional[str]:
        """Why the newest pinned tag cannot serve the older pins, or None when it can."""
        for tag, platforms in act.older:
            if not await self.scm.branch_contains(repo, act.tag, tag):
                return "%s does not contain %s" % (act.tag, tag)
            changed = await self.scm.changed_files(repo, tag, act.tag)
            touched = sorted({p for p in platforms for f in changed if f.startswith(p + "/")})
            if touched:
                return "%s pins %s but %s changed up to %s" % ("/".join(platforms), tag, "/".join(touched), act.tag)
        return None

    async def _execute(self, repo: str, act: mirror.Action) -> str:
        try:
            if act.kind == "no_pin":
                return "no shell branch pins this module"
            why = await self._diverged(repo, act)
            if why:
                msg = "%s in %s diverged: %s; cut the branch by hand" % (act.branch, repo, why)
                await self.notifier.notify("modulehub: " + msg)
                return "DIVERGED: " + msg
            if act.kind == "create":
                await self.scm.create_branch(repo, act.branch, act.tag)
                return "created from %s" % act.tag
            missing = [t for t in [act.tag] + [t for t, _ in act.older] if not await self.scm.branch_contains(repo, act.branch, t)]
            if not missing:
                return "ok"
            msg = "%s exists in %s but does not contain %s" % (act.branch, repo, ", ".join(missing))
            await self.notifier.notify("modulehub: " + msg)
            return "MISMATCH: " + msg
        except Exception as e:
            return "error: %s" % e
