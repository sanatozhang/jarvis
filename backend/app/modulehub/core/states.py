"""Release state machine: pending -> building -> tagged -> pr_opened -> (backport_opened) -> done | failed."""
from __future__ import annotations

from typing import Dict, Optional, Tuple

PENDING = "pending"
BUILDING = "building"
UPLOADED = "uploaded"          # only used as `failed_from`: artifacts exist but the tag step failed
TAGGED = "tagged"
PR_OPENED = "pr_opened"
BACKPORT_OPENED = "backport_opened"
DONE = "done"
FAILED = "failed"

ACTIVE = (PENDING, BUILDING, TAGGED, PR_OPENED, BACKPORT_OPENED)

_TRANSITIONS: Dict[Tuple[str, str], str] = {
    (PENDING, "build_triggered"): BUILDING,
    (BUILDING, "job_succeeded"): TAGGED,
    (TAGGED, "bump_pr_opened"): PR_OPENED,
    (PR_OPENED, "backport_opened"): BACKPORT_OPENED,
    (PR_OPENED, "finish"): DONE,
    (BACKPORT_OPENED, "finish"): DONE,
    (FAILED, "resume"): BUILDING,
}


class InvalidTransition(RuntimeError):
    pass


def next_state(state: str, event: str) -> str:
    if event == "failed" and state in ACTIVE:
        return FAILED
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition("%s --%s--> ?" % (state, event))


def is_active(state: str) -> bool:
    return state in ACTIVE


def failed_from_job(*, last_step: Optional[str]) -> str:
    """Where a failed publish job stopped: only a failed `tag` step means artifacts are already uploaded."""
    return UPLOADED if last_step == "tag" else BUILDING


def resume_action(failed_from: str) -> Optional[str]:
    return {UPLOADED: "rerun_job_resume", TAGGED: "open_bump_pr", PR_OPENED: "open_backport_pr"}.get(failed_from)
