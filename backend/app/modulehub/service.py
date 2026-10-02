"""Use cases: start / tick / resume a release, and reconcile release branches.

Depends only on `core` and `ports`; every side effect goes through a port.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from app.modulehub.core import backport, naming, release_rules, states, versions_toml
from app.modulehub.core.result import PublishResult, ResultInvalid, last_step, parse_result
from app.modulehub.ports import BuildRunner, ModuleHubSettings, Notifier, ReleaseRecord, ScmHost, Store

logger = logging.getLogger("jarvis.modulehub")


class ReleaseConflict(RuntimeError):
    """Another release of the same (module, platform) is active, or the release cannot be resumed."""


class ReleaseNotFound(LookupError):
    pass


class ReleaseService:
    def __init__(self, *, store: Store, build: BuildRunner, scm: ScmHost, notifier: Notifier, settings: ModuleHubSettings):
        self.store, self.build, self.scm, self.notifier, self.settings = store, build, scm, notifier, settings

    # ---- queries -----------------------------------------------------------------------------
    async def list_modules(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for platform, shell in sorted(self.settings.shell_repos.items()):
            text = await self.scm.read_file(shell, "main", self.settings.versions_path)
            if text is None:
                continue
            for name, table in versions_toml.parse(text).items():
                out.append({"name": name, "platform": platform, "version": table.get("version", ""), "repo": table.get("repo", "")})
        return out

    # ---- start -------------------------------------------------------------------------------
    async def start(self, *, module: str, platform: str, branch: str, major: bool, actor: str, dry_run: bool = False) -> ReleaseRecord:
        release_rules.validate_platform(platform)
        release_rules.validate_request(branch, major)
        shell = self.settings.shell_repos.get(platform)
        if not shell:
            raise release_rules.InvalidRequest("no shell repository configured for %s" % platform)
        text = await self.scm.read_file(shell, branch, self.settings.versions_path)
        if text is None:
            raise release_rules.InvalidRequest("%s has no %s on branch %s" % (shell, self.settings.versions_path, branch))
        if module not in versions_toml.parse(text):
            raise release_rules.InvalidRequest("module %r is not listed in %s on %s" % (module, shell, branch))
        if not dry_run:
            active = await self.store.find_active(module, platform)
            if active:
                raise ReleaseConflict("release %s of %s-%s is already in progress" % (active.id, module, platform))
        rec = await self.store.create(ReleaseRecord(module=module, platform=platform, branch=branch, major=major,
                                                    kind="preview" if dry_run else "release", requested_by=actor))
        await self._trigger(rec, dry_run=dry_run, resume=False)
        return rec

    async def _trigger(self, rec: ReleaseRecord, *, dry_run: bool, resume: bool) -> None:
        repo = release_rules.module_repo(self.settings.module_repo_owner, rec.module, rec.platform)
        try:
            handle = await self.build.trigger(repo=repo, branch=rec.branch, platform=rec.platform, major=rec.major,
                                              dry_run=dry_run, resume=resume)
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
            await self._open_bump_pr(rec)
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
        rec.version, rec.sha256, rec.coordinate, rec.git_sha = res.version, res.sha256, res.coordinate, res.git_sha
        rec.api_changes = res.api_changes
        if rec.kind == "preview":
            rec.state = states.DONE
            await self.store.save(rec, "preview ready: %s" % res.version)
            return
        rec.state = states.next_state(rec.state, "job_succeeded")
        await self.store.save(rec, "published %s" % res.version)

    @staticmethod
    def _check_result(rec: ReleaseRecord, res: PublishResult) -> None:
        if res.name != rec.module or res.platform != rec.platform:
            raise ValueError("result is for %s-%s, expected %s-%s" % (res.name, res.platform, rec.module, rec.platform))
        if rec.kind == "release" and res.dry_run:
            raise ValueError("job ran as a dry run")
        if rec.kind == "release" and not res.sha256:
            raise ValueError("release result has no sha256")

    async def _open_bump_pr(self, rec: ReleaseRecord) -> None:
        shell = self.settings.shell_repos[rec.platform]
        try:
            text = await self.scm.read_file(shell, rec.branch, self.settings.versions_path)
            if text is None:
                raise ValueError("%s has no %s on %s" % (shell, self.settings.versions_path, rec.branch))
            old = versions_toml.read_module(text, rec.module)["version"]
            rec.previous_version = old
            if old == rec.version:
                rec.bump_pr_url = ""
                rec.state = states.next_state(rec.state, "bump_pr_opened")
                await self.store.save(rec, "shell already pins %s" % rec.version)
                return
            module_repo = release_rules.module_repo(self.settings.module_repo_owner, rec.module, rec.platform)
            try:
                changelog = await self.scm.commits_between(module_repo, "v" + old, "v" + rec.version)
            except Exception:  # changelog is decoration, never a reason to fail the bump
                changelog = []
            title = naming.bump_title(rec.module, rec.platform, old, rec.version)
            body = naming.bump_body(name=rec.module, platform=rec.platform, old=old, new=rec.version, sha256=rec.sha256,
                                    coordinate=rec.coordinate, changelog=changelog, api_changes=rec.api_changes,
                                    release_url="%s/modulehub/releases/%s" % (self.settings.base_url.rstrip("/"), rec.id) if self.settings.base_url else "")
            pr = await self.scm.open_or_update_file_pr(
                repo=shell, base=rec.branch, branch=naming.bump_branch(rec.module, rec.platform, rec.branch),
                path=self.settings.versions_path, content=versions_toml.rewrite_module(text, rec.module, version=rec.version, sha256=rec.sha256),
                title=title, body=body, commit_message=title)
        except Exception as e:
            await self._fail(rec, states.TAGGED, "bump PR failed: %s" % e)
            return
        rec.bump_pr_url = pr.url
        rec.state = states.next_state(rec.state, "bump_pr_opened")
        await self.store.save(rec, "bump PR %s" % ("opened" if pr.created else "updated"))

    async def _backport_or_finish(self, rec: ReleaseRecord) -> None:
        module_repo = release_rules.module_repo(self.settings.module_repo_owner, rec.module, rec.platform)
        try:
            plan = backport.plan_backport(rec.module, rec.branch, rec.version, rec.previous_version, already_on_main=False)
            if plan is not None and await self.scm.range_applied_on(module_repo, "main", plan.commit_range):
                plan = None
            if plan is None:
                await self._finish(rec)
                return
            pr = await self.scm.open_backport_pr(repo=module_repo, branch=plan.branch, base=plan.base,
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
        await self.notifier.notify("modulehub: released %s-%s %s (bump PR: %s)" % (rec.module, rec.platform, rec.version, rec.bump_pr_url or "none needed"))

    async def _fail(self, rec: ReleaseRecord, failed_from: str, error: str) -> None:
        rec.state = states.FAILED
        rec.failed_from, rec.error = failed_from, error
        await self.store.save(rec, error)
        await self.notifier.notify("modulehub: release %s of %s-%s failed: %s (%s)" % (rec.id, rec.module, rec.platform, error, rec.build_url))

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
        active = await self.store.find_active(rec.module, rec.platform)
        if active:
            raise ReleaseConflict("release %s of %s-%s is already in progress" % (active.id, rec.module, rec.platform))
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
        """Reconcile module release branches with the shell's. Returns human-readable results."""
        from app.modulehub.core import mirror

        report: List[str] = []
        for platform, shell in sorted(self.settings.shell_repos.items()):
            main_text = await self.scm.read_file(shell, "main", self.settings.versions_path)
            if main_text is None:
                continue
            shell_branches = await self.scm.list_branches(shell, "release/")
            texts: Dict[str, Optional[str]] = {b: await self.scm.read_file(shell, b, self.settings.versions_path) for b in shell_branches}
            for module in versions_toml.module_names(main_text):
                repo = release_rules.module_repo(self.settings.module_repo_owner, module, platform)
                pins: Dict[str, Optional[str]] = {}
                for b, t in texts.items():
                    try:
                        pins[b] = versions_toml.read_module(t, module)["version"] if t else None
                    except versions_toml.ModuleNotInToml:
                        pins[b] = None
                module_branches = await self.scm.list_branches(repo, "release/")
                for act in mirror.plan_mirror(shell_branches=shell_branches, module_branches=module_branches, pins=pins):
                    outcome = await self._execute(repo, act)
                    await self.store.log_mirror(module, platform, act.branch, act.kind, outcome)
                    report.append("%s-%s %s %s: %s" % (module, platform, act.kind, act.branch, outcome))
        return report

    async def _execute(self, repo: str, act) -> str:
        try:
            if act.kind == "create":
                await self.scm.create_branch(repo, act.branch, act.tag)
                return "created from %s" % act.tag
            if act.kind == "verify":
                if await self.scm.branch_contains(repo, act.branch, act.tag):
                    return "ok"
                msg = "%s exists in %s but does not contain %s" % (act.branch, repo, act.tag)
                await self.notifier.notify("modulehub: " + msg)
                return "MISMATCH: " + msg
            return "shell branch does not pin this module"
        except Exception as e:
            return "error: %s" % e
