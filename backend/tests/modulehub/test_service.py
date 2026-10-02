import pytest

from app.modulehub.core import release_rules, states
from app.modulehub.ports import BuildStatus
from app.modulehub.service import MirrorService, ReleaseConflict, ReleaseNotFound, ReleaseService
from tests.modulehub.fakes import SETTINGS, TOML, FakeBuild, FakeScm, MemStore, RecNotifier, result_json

SHELL = "Plaud-AI/shell-android"
MOD = "Plaud-AI/plaud-module-logger-android"


@pytest.fixture
def env():
    store, build, scm, note = MemStore(), FakeBuild(), FakeScm(), RecNotifier()
    scm.files[(SHELL, "main", "modules.versions.toml")] = TOML
    scm.files[(SHELL, "release/1.0", "modules.versions.toml")] = TOML
    svc = ReleaseService(store=store, build=build, scm=scm, notifier=note, settings=SETTINGS)
    return svc, store, build, scm, note


async def started(svc, **kw):
    args = dict(module="logger", platform="android", branch="main", major=False, actor="me@plaud.ai")
    args.update(kw)
    return await svc.start(**args)


async def test_list_modules(env):
    svc, *_ = env
    assert await svc.list_modules() == [{"name": "logger", "platform": "android", "version": "1.0.0", "repo": MOD}]


async def test_start_triggers_job_and_locks(env):
    svc, store, build, *_ = env
    rec = await started(svc)
    assert rec.state == states.BUILDING and rec.build_url == "https://jenkins/1"
    assert build.triggers == [dict(repo=MOD, branch="main", platform="android", major=False, dry_run=False, resume=False)]
    with pytest.raises(ReleaseConflict):
        await started(svc)
    # another platform / module is independent
    store2_ok = await store.find_active("logger", "ios")
    assert store2_ok is None


@pytest.mark.parametrize("kw", [dict(branch="feature/a/b"), dict(branch="release/1.0", major=True), dict(platform="harmony")])
async def test_start_validation(env, kw):
    svc, *_ = env
    with pytest.raises(release_rules.InvalidRequest):
        await started(svc, **kw)


async def test_start_requires_shell_branch_and_registered_module(env):
    svc, *_ = env
    with pytest.raises(release_rules.InvalidRequest):
        await started(svc, branch="release/9.9")
    with pytest.raises(release_rules.InvalidRequest):
        await started(svc, module="network")
    svc.settings = svc.settings.__class__(shell_repos={})
    with pytest.raises(release_rules.InvalidRequest):
        await started(svc)


async def test_trigger_failure_marks_release_failed(env):
    svc, store, build, _, note = env
    build.raise_on_trigger = RuntimeError("jenkins down")
    rec = await started(svc)
    assert rec.state == states.FAILED and "jenkins down" in rec.error and rec.failed_from == states.BUILDING
    assert note.messages and "failed" in note.messages[0]


async def test_preview_does_not_lock_and_finishes_without_prs(env):
    svc, store, build, scm, _ = env
    await started(svc)                                     # an active real release
    prev = await started(svc, dry_run=True)                # preview is allowed meanwhile
    assert build.triggers[-1]["dry_run"] is True
    build.next_status = BuildStatus("success", result_json=result_json(dryRun=True, sha256=""))
    await svc.tick(prev)
    got = await store.get(prev.id)
    assert got.state == states.DONE and got.version == "1.1.0" and scm.file_prs == []


async def test_full_main_release_opens_one_bump_pr_and_finishes(env):
    svc, store, build, scm, note = env
    rec = await started(svc)
    build.next_status = BuildStatus("running")
    await svc.tick(rec)
    assert (await store.get(rec.id)).state == states.BUILDING
    build.next_status = BuildStatus("success", result_json=result_json())
    await svc.tick(rec)
    got = await store.get(rec.id)
    assert got.state == states.DONE and got.version == "1.1.0" and got.previous_version == "1.0.0"
    pr = scm.file_prs[0]
    assert pr["branch"] == "chore/jarvis/bump-logger-android-main" and pr["base"] == "main" and pr["repo"] == SHELL
    assert pr["title"] == "chore: bump logger-android 1.0.0 -> 1.1.0"
    assert 'version = "1.1.0"' in pr["content"] and "feat: a (#1)" in pr["body"]
    assert scm.backport_prs == []
    assert got.bump_pr_url.endswith("/1") and any("released" in m for m in note.messages)


async def test_changelog_failure_does_not_block_bump(env):
    svc, store, build, scm, _ = env
    scm.fail_commits = True
    rec = await started(svc)
    build.next_status = BuildStatus("success", result_json=result_json())
    await svc.tick(rec)
    assert (await store.get(rec.id)).state == states.DONE and "No changelog" in scm.file_prs[0]["body"]


async def test_shell_already_pins_version_needs_no_pr(env):
    svc, store, build, scm, _ = env
    rec = await started(svc)
    build.next_status = BuildStatus("success", result_json=result_json(version="1.0.0"))
    await svc.tick(rec)
    got = await store.get(rec.id)
    assert got.state == states.DONE and scm.file_prs == [] and got.bump_pr_url == ""


async def test_release_branch_opens_backport_pr(env):
    svc, store, build, scm, _ = env
    rec = await started(svc, branch="release/1.0")
    build.next_status = BuildStatus("success", result_json=result_json(version="1.0.1", branch="release/1.0"))
    await svc.tick(rec)
    got = await store.get(rec.id)
    assert got.state == states.DONE
    bp = scm.backport_prs[0]
    assert bp["repo"] == MOD and bp["commit_range"] == "v1.0.0..v1.0.1" and bp["branch"] == "chore/jarvis/backport-logger-1.0.1"
    assert got.backport_pr_url


async def test_backport_conflict_notifies_owner(env):
    svc, store, build, scm, note = env
    scm.conflict = True
    rec = await started(svc, branch="release/1.0")
    build.next_status = BuildStatus("success", result_json=result_json(version="1.0.1", branch="release/1.0"))
    await svc.tick(rec)
    assert any("conflicts" in m for m in note.messages)


async def test_backport_skipped_when_already_on_main(env):
    svc, store, build, scm, _ = env
    scm.applied = True
    rec = await started(svc, branch="release/1.0")
    build.next_status = BuildStatus("success", result_json=result_json(version="1.0.1", branch="release/1.0"))
    await svc.tick(rec)
    assert (await store.get(rec.id)).state == states.DONE and scm.backport_prs == []


async def test_job_failure_before_upload_is_not_resumable(env):
    svc, store, build, _, _ = env
    rec = await started(svc)
    build.next_status = BuildStatus("failure", log_tail="STEP=test\nboom")
    await svc.tick(rec)
    got = await store.get(rec.id)
    assert got.state == states.FAILED and got.failed_from == states.BUILDING
    with pytest.raises(ReleaseConflict):
        await svc.resume(rec.id, "me")


async def test_tag_failure_is_resumable_with_resume_flag(env):
    svc, store, build, _, _ = env
    rec = await started(svc)
    build.next_status = BuildStatus("failure", log_tail="STEP=upload\nSTEP=tag\nfatal")
    await svc.tick(rec)
    got = await store.get(rec.id)
    assert got.failed_from == states.UPLOADED
    again = await svc.resume(rec.id, "you@plaud.ai")
    assert again.state == states.BUILDING and again.resume_count == 1 and again.requested_by == "you@plaud.ai"
    assert build.triggers[-1]["resume"] is True and build.triggers[-1]["dry_run"] is False


async def test_bump_failure_then_resume_reopens_pr(env):
    svc, store, build, scm, _ = env
    scm.fail_file_pr = RuntimeError("github 502")
    rec = await started(svc)
    build.next_status = BuildStatus("success", result_json=result_json())
    await svc.tick(rec)
    got = await store.get(rec.id)
    assert got.state == states.FAILED and got.failed_from == states.TAGGED and "502" in got.error
    scm.fail_file_pr = None
    resumed = await svc.resume(rec.id, "me")
    assert resumed.state == states.DONE and scm.file_prs


async def test_backport_failure_then_resume(env):
    svc, store, build, scm, _ = env
    scm.fail_backport = RuntimeError("push rejected")
    rec = await started(svc, branch="release/1.0")
    build.next_status = BuildStatus("success", result_json=result_json(version="1.0.1", branch="release/1.0"))
    await svc.tick(rec)
    got = await store.get(rec.id)
    assert got.state == states.FAILED and got.failed_from == states.PR_OPENED
    scm.fail_backport = None
    assert (await svc.resume(rec.id, "me")).state == states.DONE


@pytest.mark.parametrize("res", [
    "not json", result_json(name="network"), result_json(dryRun=True), result_json(sha256=""),
])
async def test_invalid_results_fail_the_release(env, res):
    svc, store, build, *_ = env
    rec = await started(svc)
    build.next_status = BuildStatus("success", result_json=res)
    await svc.tick(rec)
    got = await store.get(rec.id)
    assert got.state == states.FAILED and "invalid publish result" in got.error


async def test_bump_fails_when_shell_lost_the_file(env):
    svc, store, build, scm, _ = env
    rec = await started(svc)
    build.next_status = BuildStatus("success", result_json=result_json())
    del scm.files[(SHELL, "main", "modules.versions.toml")]
    await svc.tick(rec)
    assert (await store.get(rec.id)).failed_from == states.TAGGED


async def test_resume_errors(env):
    svc, store, build, *_ = env
    with pytest.raises(ReleaseNotFound):
        await svc.resume(999, "me")
    rec = await started(svc)
    with pytest.raises(ReleaseConflict):
        await svc.resume(rec.id, "me")          # still building
    build.next_status = BuildStatus("failure", log_tail="STEP=tag")
    await svc.tick(rec)
    await started(svc)                          # a new active release took the lock
    with pytest.raises(ReleaseConflict):
        await svc.resume(rec.id, "me")


async def test_tick_all_survives_a_bad_release(env):
    svc, store, build, *_ = env
    a = await started(svc)

    async def boom(*a, **k):
        raise RuntimeError("x")

    build.status = boom
    await svc.tick_all()           # must not raise
    assert (await store.get(a.id)).state == states.BUILDING


# ---- mirror ---------------------------------------------------------------------------------
@pytest.fixture
def menv():
    store, scm, note = MemStore(), FakeScm(), RecNotifier()
    scm.files[(SHELL, "main", "modules.versions.toml")] = TOML
    scm.files[(SHELL, "release/1.0", "modules.versions.toml")] = TOML.replace("1.0.0", "1.2.0")
    scm.files[(SHELL, "release/2.0", "modules.versions.toml")] = "[other]\nversion = \"1.0.0\"\nsha256 = \"%s\"\nrepo = \"x\"\n" % ("c" * 64)
    scm.branches[SHELL] = ["main", "release/1.0", "release/2.0", "release/3.0"]
    scm.branches[MOD] = ["main", "release/3.0"]
    return MirrorService(store=store, scm=scm, notifier=note, settings=SETTINGS), store, scm, note


async def test_mirror_creates_verifies_and_reports(menv):
    svc, store, scm, note = menv
    scm.contains[(MOD, "release/3.0", "v1.0.0")] = False   # release/3.0 has no toml in the fake -> no_pin
    report = await svc.sync()
    assert (MOD, "release/1.0", "v1.2.0") in scm.created
    kinds = {(m[2], m[3]) for m in store.mirror}
    assert ("release/1.0", "create") in kinds and ("release/2.0", "no_pin") in kinds and ("release/3.0", "no_pin") in kinds
    assert any("created from v1.2.0" in r for r in report)


async def test_mirror_verify_mismatch_alerts(menv):
    svc, store, scm, note = menv
    scm.files[(SHELL, "release/3.0", "modules.versions.toml")] = TOML
    scm.contains[(MOD, "release/3.0", "v1.0.0")] = False
    report = await svc.sync()
    assert any("MISMATCH" in r for r in report) and any("does not contain" in m for m in note.messages)


async def test_mirror_verify_ok_and_execution_errors(menv):
    svc, store, scm, note = menv
    scm.files[(SHELL, "release/3.0", "modules.versions.toml")] = TOML
    report = await svc.sync()
    assert any(r.endswith("verify release/3.0: ok") for r in report)

    async def boom(*a, **k):
        raise RuntimeError("no permission")

    scm.create_branch = boom
    report = await svc.sync()
    assert any("error: no permission" in r for r in report)


async def test_mirror_skips_platform_without_toml():
    store, scm, note = MemStore(), FakeScm(), RecNotifier()
    assert await MirrorService(store=store, scm=scm, notifier=note, settings=SETTINGS).sync() == []
    svc = ReleaseService(store=store, build=FakeBuild(), scm=scm, notifier=note, settings=SETTINGS)
    assert await svc.list_modules() == []


async def test_tick_finishes_a_release_stuck_in_backport_opened(env):
    svc, store, *_ = env
    rec = await started(svc)
    rec.state = states.BACKPORT_OPENED
    await store.save(rec)
    await svc.tick(rec)
    assert (await store.get(rec.id)).state == states.DONE
