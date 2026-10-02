"""Which commands a study permits now, and the plain reason for each one it refuses (#2696).

One rule set answers both the detail view (``permitted_actions`` /
``action_refusals``) and the command handler, so the workbench never infers
eligibility on its own and a command the view did not offer is refused for
the reason the view showed.
"""

from __future__ import annotations

import secrets
from typing import Any

from app.research.golden_search.models import COMMANDS, RUNNING_STATES, STAGE_STATES, CommandName, StageName, StudyRow
from app.research.persistence.lifecycle import presented_status_for

LIVE_PRESENTATIONS = frozenset({"queued", "running"})
STOPPED_PRESENTATIONS = frozenset({"failed", "cancelled", "interrupted"})
_RETAIN_STATES = frozenset({"awaiting_validation", "awaiting_candidate", "candidate_locked", "awaiting_review", "qualification_failed"})
_CLOSE_STATES = frozenset({"locked", "awaiting_validation", "awaiting_candidate", "candidate_locked", "awaiting_review"})
#: Commands that authorize a stage, and the states they start one from.
STAGE_COMMANDS: tuple[CommandName, ...] = ("continue", "run_research", "open_exam", "approve")
#: The stages Run research carries a study through before it pauses at Compare (#2811).
RESEARCH_STATES = frozenset({"locked", "awaiting_validation"})
RESEARCH_RUNNING_STATES = frozenset({"search_running", "validation_running"})
STAGE_START_STATES = frozenset({"locked", "awaiting_validation", "candidate_locked", "awaiting_review", "qualification_failed"})


def authorize(stage: StageName) -> tuple[dict[str, Any], str]:
    """The columns that authorize ``stage`` for one worker, and the token that worker must present to claim it."""
    token = secrets.token_hex(16)
    return (
        {
            "state": STAGE_STATES[stage],
            "status": "queued",
            "pending_stage": stage,
            "stage_token": token,
            "job_id": None,
            "failure_reason": None,
            "incomplete": False,
            "finished_at_ms": None,
        },
        token,
    )


def unclaimed(row: StudyRow) -> bool:
    """A stage a command authorized that no worker has bound yet."""
    return row.status == "queued" and row.job_id is None


def presented_status(row: StudyRow, *, live: bool | None) -> str:
    """The stage run as the owner sees it; an authorized stage waiting for its first worker is ``queued``, not interrupted."""
    if unclaimed(row):
        return "queued"
    return presented_status_for(row.status, live=live)


def action_refusals(
    row: StudyRow, *, presented: str, resume_refusal: str | None, stage_refusal: str | None = None
) -> dict[CommandName, str | None]:
    """``None`` for a permitted command, else why it is not available now.

    ``stage_refusal`` is why no new stage may run for this study (its code
    identity moved since lock); it refuses each command that would start one.
    """
    running = row.state in RUNNING_STATES
    live = running and presented in LIVE_PRESENTATIONS
    stopped = running and presented in STOPPED_PRESENTATIONS
    has_evidence = "evidence" in row.results
    reasons: dict[CommandName, str | None] = dict.fromkeys(COMMANDS)
    if row.state not in RESEARCH_STATES:
        reasons["continue"] = "Continue starts the next stage of a study that is waiting for it."
    # A stopped research stage resumes; one no worker claimed is authorized afresh, withdrawing the
    # token its lost dispatch carried (#2814 review: a job that never started must be restartable).
    resumes_research = (stopped or (running and unclaimed(row))) and row.state in RESEARCH_RUNNING_STATES
    if row.state not in RESEARCH_STATES and not resumes_research:
        reasons["run_research"] = "Run research starts or resumes Search and Test over time; this study is past them, or a stage is running."
    elif resumes_research and resume_refusal is not None:
        reasons["run_research"] = resume_refusal
    if not (row.state == "awaiting_candidate" or (row.state == "candidate_locked" and not row.exam_locked)) or not has_evidence:
        reasons["select_candidate"] = "A candidate is chosen after the candidate evidence is ready and before the final test opens."
    if row.state != "candidate_locked" or row.exam_locked:
        reasons["open_exam"] = "Lock a candidate first; the final test opens once per study."
    if row.state not in ("awaiting_review", "qualification_failed"):
        reasons["approve"] = "Approval follows the final test."
    # A stopped approval published nothing (its publish moves the study in the same transaction), so the
    # owner may still decide to keep the current settings instead of finishing it.
    if row.state not in _RETAIN_STATES and not (stopped and row.state == "qualification_pending"):
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
    if stage_refusal is not None:
        for command in STAGE_COMMANDS:
            if reasons[command] is None:
                reasons[command] = stage_refusal
    return reasons


def permitted(refusals: dict[CommandName, str | None]) -> list[CommandName]:
    return [command for command in COMMANDS if refusals[command] is None]
