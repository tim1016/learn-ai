"""Which commands a study permits now, and the plain reason for each one it refuses (#2696).

One rule set answers both the detail view (``permitted_actions`` /
``action_refusals``) and the command handler, so the workbench never infers
eligibility on its own and a command the view did not offer is refused for
the reason the view showed.
"""

from __future__ import annotations

from app.research.golden_search.models import COMMANDS, RUNNING_STATES, CommandName, StudyRow
from app.research.persistence.lifecycle import presented_status_for

LIVE_PRESENTATIONS = frozenset({"queued", "running"})
STOPPED_PRESENTATIONS = frozenset({"failed", "cancelled", "interrupted"})
_RETAIN_STATES = frozenset({"awaiting_validation", "awaiting_candidate", "candidate_locked", "awaiting_review", "qualification_failed"})
_CLOSE_STATES = frozenset({"locked", "awaiting_validation", "awaiting_candidate", "candidate_locked", "awaiting_review"})


def unclaimed(row: StudyRow) -> bool:
    """A stage a command authorized that no worker has bound yet."""
    return row.status == "queued" and row.job_id is None


def presented_status(row: StudyRow, *, live: bool | None) -> str:
    """The stage run as the owner sees it; an authorized stage waiting for its first worker is ``queued``, not interrupted."""
    if unclaimed(row):
        return "queued"
    return presented_status_for(row.status, live=live)


def action_refusals(row: StudyRow, *, presented: str, resume_refusal: str | None) -> dict[CommandName, str | None]:
    """``None`` for a permitted command, else why it is not available now."""
    running = row.state in RUNNING_STATES
    live = running and presented in LIVE_PRESENTATIONS
    stopped = running and presented in STOPPED_PRESENTATIONS
    has_evidence = "evidence" in row.results
    reasons: dict[CommandName, str | None] = dict.fromkeys(COMMANDS)
    if row.state not in ("locked", "awaiting_validation"):
        reasons["continue"] = "Continue starts the next stage of a study that is waiting for it."
    if not (row.state == "awaiting_candidate" or (row.state == "candidate_locked" and not row.exam_locked)) or not has_evidence:
        reasons["select_candidate"] = "A candidate is chosen after the candidate evidence is ready and before the final test opens."
    if row.state != "candidate_locked" or row.exam_locked:
        reasons["open_exam"] = "Lock a candidate first; the final test opens once per study."
    if row.state not in ("awaiting_review", "qualification_failed"):
        reasons["approve"] = "Approval follows the final test."
    if row.state not in _RETAIN_STATES:
        reasons["retain"] = "Keeping the current settings is a decision made between stages, not while one runs or after the study ended."
    if not (row.state in _CLOSE_STATES or (stopped and row.state != "qualification_pending")):
        reasons["close"] = "This study cannot be closed in its current state."
    if not live:
        reasons["cancel"] = "No stage is running."
    if not stopped:
        reasons["finish"] = (
            "The stage is waiting for its worker to start; Cancel withdraws it."
            if running and unclaimed(row)
            else "Finish resumes a stage that stopped before it finished."
        )
    elif resume_refusal is not None:
        reasons["finish"] = resume_refusal
    if live:
        reasons["revise"] = "Wait for the running stage to stop before revising the plan."
    return reasons


def permitted(refusals: dict[CommandName, str | None]) -> list[CommandName]:
    return [command for command in COMMANDS if refusals[command] is None]
