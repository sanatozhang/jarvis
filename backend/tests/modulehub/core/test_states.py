import pytest

from app.modulehub.core import states as s


def test_happy_path_main():
    st = s.PENDING
    st = s.next_state(st, "build_triggered"); assert st == s.BUILDING
    st = s.next_state(st, "job_succeeded"); assert st == s.TAGGED
    st = s.next_state(st, "bump_pr_opened"); assert st == s.PR_OPENED
    st = s.next_state(st, "finish"); assert st == s.DONE


def test_release_branch_goes_through_backport():
    assert s.next_state(s.PR_OPENED, "backport_opened") == s.BACKPORT_OPENED
    assert s.next_state(s.BACKPORT_OPENED, "finish") == s.DONE


def test_invalid_transition_raises():
    with pytest.raises(s.InvalidTransition):
        s.next_state(s.PENDING, "job_succeeded")
    with pytest.raises(s.InvalidTransition):
        s.next_state(s.DONE, "build_triggered")


def test_any_active_state_can_fail():
    for st in (s.PENDING, s.BUILDING, s.TAGGED, s.PR_OPENED, s.BACKPORT_OPENED):
        assert s.next_state(st, "failed") == s.FAILED


def test_failed_from_after_job_depends_on_step():
    assert s.failed_from_job(last_step="tag") == s.UPLOADED
    for step in ("preflight", "test", "build", "api", "upload", "", None):
        assert s.failed_from_job(last_step=step) == s.BUILDING


def test_resume_plan():
    assert s.resume_action(s.UPLOADED) == "rerun_job_resume"
    assert s.resume_action(s.TAGGED) == "open_bump_pr"
    assert s.resume_action(s.PR_OPENED) == "open_backport_pr"
    assert s.resume_action(s.BUILDING) is None
    assert s.resume_action(s.PENDING) is None


def test_resume_transition_requires_failed():
    assert s.next_state(s.FAILED, "resume") == s.BUILDING
    with pytest.raises(s.InvalidTransition):
        s.next_state(s.BUILDING, "resume")


def test_is_active():
    assert s.is_active(s.BUILDING) and s.is_active(s.PR_OPENED)
    assert not s.is_active(s.DONE) and not s.is_active(s.FAILED)
