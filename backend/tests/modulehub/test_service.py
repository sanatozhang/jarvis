import pytest

from app.modulehub.core import release_rules, states
from app.modulehub.ports import BuildStatus
from app.modulehub.service import MirrorService, ReleaseConflict, ReleaseNotFound, ReleaseService
from tests.modulehub.fakes import IOS_SHA, SETTINGS, SHA, TOML, FakeBuild, FakeScm, MemStore, RecNotifier, result_json

A_SHELL = "Plaud-AI/shell-android"
I_SHELL = "Plaud-AI/shell-ios"
MOD = "Plaud-AI/mobile_logger"
TOML_PATH = "modules.versions.toml"


@pytest.fixture
def env():
    store, build, scm, note = MemStore(), FakeBuild(), FakeScm(), RecNotifier()
    for shell in (A_SHELL, I_SHELL):
        scm.files[(shell, "main", TOML_PATH)] = TOML
        scm.files[(shell, "release/1.0", TOML_PATH)] = TOML
    svc = ReleaseService(store=store, build=build, scm=scm, notifier=note, settings=SETTINGS)
    return svc, store, build, scm, note


async def started(svc, **kw):
    args = dict(module="logger", platforms=["android", "ios"], branch="main", major=False, actor="me@plaud.ai")
    args.update(kw)
    return await svc.start(**args)


async def succeed(svc, build, rec, **result):
    build.next_status = BuildStatus("success", result_json=result_json(**result))
    await svc.tick(rec)


async def test_list_modules_merges_both_shells(env):
    svc, _, _, scm, _ = env
    scm.files[(I_SHELL, "main", TOML_PATH)] = TOML.replace("1.0.0", "1.2.0")
    assert await svc.list_modules() == [{"name": "logger", "repo": MOD, "platforms": {"android": "1.0.0", "ios": "1.2.0"}}]


async def test_start_triggers_one_job_for_the_module_and_locks_it(env):
    svc, store, build, *_ = env
    rec = await started(svc, platforms=["ios", "android"])
    assert rec.state == states.BUILDING and rec.build_url == "https://jenkins/1" and rec.platforms == ["android", "ios"]
    assert build.triggers == [dict(repo=MOD, branch="main", platforms=["android", "ios"], major=False, dry_run=False, resume=False)]
    with pytest.raises(ReleaseConflict):          # one version line per module: even another platform waits
        await started(svc, platforms=["ios"])


@pytest.mark.parametrize("kw", [dict(branch="feature/a/b"), dict(branch="release/1.0", major=True),
                                dict(platforms=["harmony"]), dict(platforms=[])])
async def test_start_validation(env, kw):
    svc, *_ = env
    with pytest.raises(release_rules.InvalidRequest):
        await started(svc, **kw)


async def test_start_checks_every_selected_shell(env):
    svc, _, _, scm, _ = env
    with pytest.raises(release_rules.InvalidRequest):
        await started(svc, branch="release/9.9")
    with pytest.raises(release_rules.InvalidRequest):
        await started(svc, module="network")
    del scm.files[(I_SHELL, "release/1.0", TOML_PATH)]
    with pytest.raises(release_rules.InvalidRequest):
        await started(svc, branch="release/1.0")
    assert (await started(svc, branch="release/1.0", platforms=["android"])).state == states.BUILDING
    scm.files[(I_SHELL, "main", TOML_PATH)] = TOML.replace("mobile_logger", "logger-ios")
    with pytest.raises(release_rules.InvalidRequest):          # the shells must name the same module repo
        await started(svc)
    assert (await started(svc, platforms=["android"], dry_run=True)).repo == MOD   # preview: no lock
    svc.settings = svc.settings.__class__(shell_repos={"android": A_SHELL})
    with pytest.raises(release_rules.InvalidRequest):
        await started(svc, platforms=["ios"])


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
    await succeed(svc, build, prev, dryRun=True, sha256="")
    got = await store.get(prev.id)
    assert got.state == states.DONE and got.version == "1.1.0" and scm.file_prs == []


async def test_two_platform_release_opens_one_bump_pr_per_shell(env):
    svc, store, build, scm, note = env
    rec = await started(svc)
    build.next_status = BuildStatus("running")
    await svc.tick(rec)
    assert (await store.get(rec.id)).state == states.BUILDING
    await succeed(svc, build, rec)
    got = await store.get(rec.id)
    assert got.state == states.DONE and got.version == "1.1.0"
    assert got.previous_versions == {"android": "1.0.0", "ios": "1.0.0"}
    assert got.artifacts["android"]["sha256"] == SHA and got.artifacts["ios"]["sha256"] == IOS_SHA
    a, i = scm.file_prs
    assert (a["repo"], a["branch"], a["base"]) == (A_SHELL, "chore/jarvis/bump-logger-android-main", "main")
    assert (i["repo"], i["branch"]) == (I_SHELL, "chore/jarvis/bump-logger-ios-main")
    assert a["title"] == "chore: bump logger-android 1.0.0 -> 1.1.0"
    assert 'sha256 = "%s"' % SHA in a["content"] and 'sha256 = "%s"' % IOS_SHA in i["content"]
    assert "flush() has been removed" in i["body"] and "flush()" not in a["body"] and "feat: a (#1)" in a["body"]
    assert scm.backport_prs == [] and set(got.bump_prs) == {"android", "ios"}
    assert any("released logger 1.1.0 (android,ios)" in m for m in note.messages)


async def test_one_platform_release_bumps_only_that_shell(env):
    svc, store, build, scm, _ = env
    rec = await started(svc, platforms=["ios"])
    assert build.triggers[-1]["platforms"] == ["ios"]
    await succeed(svc, build, rec, platforms=("ios",))
    got = await store.get(rec.id)
    assert got.state == states.DONE and [p["repo"] for p in scm.file_prs] == [I_SHELL] and list(got.bump_prs) == ["ios"]


async def test_result_must_cover_exactly_the_selected_platforms(env):
    svc, store, build, *_ = env
    rec = await started(svc, platforms=["android"])
    await succeed(svc, build, rec)                         # job shipped android and ios
    got = await store.get(rec.id)
    assert got.state == states.FAILED and "invalid publish result" in got.error


async def test_changelog_failure_does_not_block_bump(env):
    svc, store, build, scm, _ = env
    scm.fail_commits = True
    rec = await started(svc)
    await succeed(svc, build, rec)
    assert (await store.get(rec.id)).state == states.DONE and "No changelog" in scm.file_prs[0]["body"]


async def test_shell_already_pinning_the_version_needs_no_pr(env):
    svc, store, build, scm, _ = env
    scm.files[(I_SHELL, "main", TOML_PATH)] = TOML.replace("1.0.0", "1.1.0")
    rec = await started(svc)
    await succeed(svc, build, rec)
    got = await store.get(rec.id)
    assert got.state == states.DONE and [p["repo"] for p in scm.file_prs] == [A_SHELL] and got.bump_prs["ios"] == ""


async def test_release_branch_opens_one_backport_from_the_newest_previous_pin(env):
    svc, store, build, scm, _ = env
    scm.files[(I_SHELL, "release/1.0", TOML_PATH)] = TOML.replace("1.0.0", "1.0.1")   # an earlier iOS-only patch
    rec = await started(svc, branch="release/1.0")
    await succeed(svc, build, rec, version="1.0.2", branch="release/1.0")
    got = await store.get(rec.id)
    assert got.state == states.DONE and len(scm.backport_prs) == 1
    bp = scm.backport_prs[0]
    assert bp["repo"] == MOD and bp["commit_range"] == "v1.0.1..v1.0.2" and bp["branch"] == "chore/jarvis/backport-logger-1.0.2"
    assert got.backport_pr_url


async def test_backport_conflict_notifies_owner(env):
    svc, store, build, scm, note = env
    scm.conflict = True
    rec = await started(svc, branch="release/1.0")
    await succeed(svc, build, rec, version="1.0.1", branch="release/1.0")
    assert any("conflicts" in m for m in note.messages)


async def test_backport_skipped_when_already_on_main(env):
    svc, store, build, scm, _ = env
    scm.applied = True
    rec = await started(svc, branch="release/1.0")
    await succeed(svc, build, rec, version="1.0.1", branch="release/1.0")
    assert (await store.get(rec.id)).state == states.DONE and scm.backport_prs == []


async def test_job_failure_before_upload_is_not_resumable(env):
    svc, store, build, _, _ = env
    rec = await started(svc)
    build.next_status = BuildStatus("failure", log_tail="STEP=preflight\nSTEP=build\nPLATFORM_STEP=android:test\nboom")
    await svc.tick(rec)
    got = await store.get(rec.id)
    assert got.state == states.FAILED and got.failed_from == states.BUILDING
    with pytest.raises(ReleaseConflict):
        await svc.resume(rec.id, "me")


@pytest.mark.parametrize("tail", ["STEP=build\nPLATFORM_STEP=ios:test\nSTEP=upload", "STEP=upload\nSTEP=tag\nfatal"])
async def test_failure_after_an_upload_is_resumed_with_the_resume_flag(env, tail):
    svc, store, build, _, _ = env
    rec = await started(svc)
    build.next_status = BuildStatus("failure", log_tail=tail)
    await svc.tick(rec)
    assert (await store.get(rec.id)).failed_from == states.UPLOADED
    again = await svc.resume(rec.id, "you@plaud.ai")
    assert again.state == states.BUILDING and again.resume_count == 1 and again.requested_by == "you@plaud.ai"
    assert build.triggers[-1]["resume"] is True and build.triggers[-1]["platforms"] == ["android", "ios"]


async def test_bump_failure_on_one_shell_then_resume_only_opens_the_missing_pr(env):
    svc, store, build, scm, _ = env
    rec = await started(svc)
    real = scm.open_or_update_file_pr

    async def ios_down(**kw):
        if kw["repo"] == I_SHELL:
            raise RuntimeError("github 502")
        return await real(**kw)

    scm.open_or_update_file_pr = ios_down
    await succeed(svc, build, rec)
    got = await store.get(rec.id)
    assert got.state == states.FAILED and got.failed_from == states.TAGGED and "ios" in got.error and "502" in got.error
    assert list(got.bump_prs) == ["android"]
    scm.open_or_update_file_pr = real
    resumed = await svc.resume(rec.id, "me")
    assert resumed.state == states.DONE and [p["repo"] for p in scm.file_prs] == [A_SHELL, I_SHELL]


async def test_backport_failure_then_resume(env):
    svc, store, build, scm, _ = env
    scm.fail_backport = RuntimeError("push rejected")
    rec = await started(svc, branch="release/1.0")
    await succeed(svc, build, rec, version="1.0.1", branch="release/1.0")
    got = await store.get(rec.id)
    assert got.state == states.FAILED and got.failed_from == states.PR_OPENED
    scm.fail_backport = None
    assert (await svc.resume(rec.id, "me")).state == states.DONE


@pytest.mark.parametrize("res", [
    "not json", result_json(name="network"), result_json(dryRun=True), result_json(sha256=""),
    result_json(schemaVersion=1), result_json(platforms=("android",)),
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
    del scm.files[(A_SHELL, "main", TOML_PATH)]
    await succeed(svc, build, rec)
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
    await started(svc, platforms=["android"])   # a new active release of the module took the lock
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


async def test_tick_finishes_a_release_stuck_in_backport_opened(env):
    svc, store, *_ = env
    rec = await started(svc)
    rec.state = states.BACKPORT_OPENED
    await store.save(rec)
    await svc.tick(rec)
    assert (await store.get(rec.id)).state == states.DONE


# ---- mirror ---------------------------------------------------------------------------------
def toml(version):
    return TOML.replace("1.0.0", version)


@pytest.fixture
def menv():
    store, scm, note = MemStore(), FakeScm(), RecNotifier()
    for shell in (A_SHELL, I_SHELL):
        scm.files[(shell, "main", TOML_PATH)] = TOML
    scm.files[(A_SHELL, "release/1.0", TOML_PATH)] = toml("1.2.0")
    scm.files[(I_SHELL, "release/1.0", TOML_PATH)] = toml("1.2.0")
    scm.files[(A_SHELL, "release/2.0", TOML_PATH)] = "[other]\nversion = \"1.0.0\"\nsha256 = \"%s\"\nrepo = \"x\"\n" % ("c" * 64)
    scm.branches[A_SHELL] = ["main", "release/1.0", "release/2.0", "release/3.0"]
    scm.branches[I_SHELL] = ["main", "release/1.0"]
    scm.branches[MOD] = ["main", "release/3.0"]
    return MirrorService(store=store, scm=scm, notifier=note, settings=SETTINGS), store, scm, note


async def test_mirror_creates_verifies_and_reports(menv):
    svc, store, scm, note = menv
    report = await svc.sync()
    assert scm.created == [(MOD, "release/1.0", "v1.2.0")]
    kinds = {(m[1], m[2]) for m in store.mirror}
    assert kinds == {("release/1.0", "create"), ("release/2.0", "no_pin"), ("release/3.0", "no_pin")}
    assert any("created from v1.2.0" in r for r in report)


async def test_mirror_cuts_from_the_newer_pin_when_the_older_platform_did_not_change(menv):
    svc, store, scm, note = menv
    scm.files[(A_SHELL, "release/1.0", TOML_PATH)] = toml("1.1.0")          # android pins an older release
    scm.changed[(MOD, "v1.1.0", "v1.2.0")] = ["ios/Sources/Logger/PLogger.swift"]
    report = await svc.sync()
    assert scm.created == [(MOD, "release/1.0", "v1.2.0")] and not note.messages, report


async def test_mirror_reports_divergence_and_changes_nothing(menv):
    svc, store, scm, note = menv
    scm.files[(A_SHELL, "release/1.0", TOML_PATH)] = toml("1.1.0")
    scm.changed[(MOD, "v1.1.0", "v1.2.0")] = ["android/logger/src/main/kotlin/A.kt", "ios/X.swift"]
    report = await svc.sync()
    assert scm.created == [] and any("DIVERGED" in r and "android" in r for r in report)
    assert any("diverged" in m for m in note.messages)
    scm.changed.clear()
    scm.contains[(MOD, "v1.2.0", "v1.1.0")] = False                         # not even an ancestor
    report = await svc.sync()
    assert scm.created == [] and any("does not contain v1.1.0" in r for r in report)


async def test_mirror_verify_mismatch_alerts(menv):
    svc, store, scm, note = menv
    scm.files[(A_SHELL, "release/3.0", TOML_PATH)] = TOML
    scm.contains[(MOD, "release/3.0", "v1.0.0")] = False
    report = await svc.sync()
    assert any("MISMATCH" in r for r in report) and any("does not contain" in m for m in note.messages)


async def test_mirror_verify_ok_and_execution_errors(menv):
    svc, store, scm, note = menv
    scm.files[(A_SHELL, "release/3.0", TOML_PATH)] = TOML
    report = await svc.sync()
    assert any(r.endswith("verify release/3.0: ok") for r in report)

    async def boom(*a, **k):
        raise RuntimeError("no permission")

    scm.create_branch = boom
    report = await svc.sync()
    assert any("error: no permission" in r for r in report)


async def test_mirror_reports_a_module_whose_shells_disagree_on_the_repo(menv):
    svc, store, scm, note = menv
    scm.files[(I_SHELL, "main", TOML_PATH)] = TOML.replace("mobile_logger", "other")
    report = await svc.sync()
    assert any("different repositories" in r for r in report) and scm.created == []


async def test_mirror_skips_platform_without_toml():
    store, scm, note = MemStore(), FakeScm(), RecNotifier()
    assert await MirrorService(store=store, scm=scm, notifier=note, settings=SETTINGS).sync() == []
    svc = ReleaseService(store=store, build=FakeBuild(), scm=scm, notifier=note, settings=SETTINGS)
    assert await svc.list_modules() == []
