"""Golden Search study records as the service reads them: states, stages, rows and refusals (#2696, ADR 0074).

A study row is the mutable projection of one locked protocol: its lifecycle
``state``, the fence ``status`` of the stage run in flight, the stage a
guarded command authorized (``pending_stage`` + ``stage_token``), and the
stage outputs in ``results``. Everything a later review relies on — trials,
exposures, evaluations — is its own append-only or keyed record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

StudyState = Literal[
    "locked",
    "search_running",
    "awaiting_validation",
    "validation_running",
    "awaiting_candidate",
    "candidate_locked",
    "exam_running",
    "awaiting_review",
    "qualification_pending",
    "approved",
    "qualification_failed",
    "retained",
    "closed",
]
StageName = Literal["search", "validation", "exam", "qualification"]
FenceStatus = Literal["idle", "queued", "running", "completed", "failed", "cancelled"]
CommandName = Literal[
    "continue", "select_candidate", "open_exam", "approve", "retain", "close", "cancel", "finish", "revise"
]
CandidateKey = Literal["incumbent", "all_period", "recent"]
RetainKind = Literal["keep_current", "wait_for_fresh_data", "retain_exploration"]
#: How the HTTP layer answers a refusal: 400, 409 or 404.
RefusalKind = Literal["invalid", "conflict", "not_found"]

COMMANDS: tuple[CommandName, ...] = (
    "continue", "select_candidate", "open_exam", "approve", "retain", "close", "cancel", "finish", "revise"
)
RETAIN_KINDS: tuple[RetainKind, ...] = ("keep_current", "wait_for_fresh_data", "retain_exploration")
CANDIDATE_KEYS: tuple[CandidateKey, ...] = ("incumbent", "all_period", "recent")

#: The state a stage runs in, by stage; a stage run owns a worker while its study is in this state.
STAGE_STATES: dict[StageName, StudyState] = {
    "search": "search_running",
    "validation": "validation_running",
    "exam": "exam_running",
    "qualification": "qualification_pending",
}
RUNNING_STATES: frozenset[str] = frozenset(STAGE_STATES.values())
TERMINAL_STATES: frozenset[str] = frozenset({"approved", "retained", "closed"})
# Evaluation rows name the step that asked for them; a stage may run more than one step.
STAGE_STEPS: dict[StageName, tuple[str, ...]] = {
    "search": ("search", "pair_audit", "recent"),
    "validation": ("validation", "evidence"),
    "exam": ("exam",),
    "qualification": ("proof",),
}
# The estimate rows (``budget.estimate``) that bound each stage's work.
STAGE_ESTIMATES: dict[StageName, tuple[str, ...]] = {
    "search": ("search", "recent"),
    "validation": ("validation", "evidence"),
    "exam": ("exam",),
    "qualification": ("proof",),
}


class GoldenSearchRefusal(ValueError):
    """A request the owner can act on was refused: ``code`` names it, ``kind`` how HTTP answers it.

    ``study`` carries the current row when the refusal is about it (a stale
    revision answers with the study as it now stands).
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        kind: RefusalKind = "invalid",
        field: str | None = None,
        study: StudyRow | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind
        self.field = field
        self.study = study


@dataclass(frozen=True, slots=True)
class StudyRow:
    """One ``research_golden_search_studies`` row; satisfies ``lifecycle.FencedRecord``."""

    id: str
    parent_study_id: str | None
    strategy_key: str
    symbol: str
    state: StudyState
    revision: int
    status: FenceStatus
    attempt: int
    job_id: str | None
    pending_stage: StageName | None
    stage_token: str | None
    created_at_ms: int
    updated_at_ms: int
    finished_at_ms: int | None
    protocol: dict[str, Any]
    protocol_hash: str
    receipt: dict[str, Any]
    results: dict[str, Any]
    candidate_key: CandidateKey | None
    exam_locked: bool
    decision: dict[str, Any] | None
    budget_cap: int
    consumed_evaluations: int
    cache_hits: int
    invalid_points: int
    incomplete: bool
    failure_reason: str | None
    hidden: bool


@dataclass(frozen=True, slots=True)
class NewStudy:
    """A locked plan ready to insert: protocol, receipt and the lock request's identity."""

    id: str
    parent_study_id: str | None
    strategy_key: str
    symbol: str
    protocol: dict[str, Any]
    protocol_hash: str
    receipt: dict[str, Any]
    budget_cap: int
    request_sha256: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """A recorded command: the request's identity and the response it produced."""

    study_id: str
    idempotency_key: str
    command: str
    request_sha256: str
    response: dict[str, Any]
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class EvaluationRecord:
    """One ``research_golden_search_evaluations`` row."""

    study_id: str
    evaluation_key: str
    point_hash: str
    point: dict[str, Any]
    window_start_ms: int
    window_end_ms: int
    scenario: str
    detail: bool
    stage: str
    fold_index: int | None
    status: Literal["pending", "completed", "failed"]
    attempt: int
    retries: int
    total_trades: int | None
    net_profit: float | None
    total_return_pct: float | None
    sharpe_ratio: float | None
    max_drawdown_pct: float | None
    win_rate: float | None
    error: str | None
    detail_json: dict[str, Any] | None
    created_at_ms: int
    completed_at_ms: int | None

    def as_dict(self) -> dict[str, Any]:
        """The evaluations page row (``detail_json`` is read through the candidate view, not here)."""
        return {
            "evaluation_key": self.evaluation_key,
            "point_hash": self.point_hash,
            "point": dict(self.point),
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "scenario": self.scenario,
            "detail": self.detail,
            "stage": self.stage,
            "fold_index": self.fold_index,
            "status": self.status,
            "attempt": self.attempt,
            "retries": self.retries,
            "total_trades": self.total_trades,
            "net_profit": self.net_profit,
            "total_return_pct": self.total_return_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown_pct": self.max_drawdown_pct,
            "win_rate": self.win_rate,
            "error": self.error,
            "created_at_ms": self.created_at_ms,
            "completed_at_ms": self.completed_at_ms,
        }


@dataclass(frozen=True, slots=True)
class EvaluationPage:
    total: int
    page: int
    page_size: int
    rows: tuple[EvaluationRecord, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"total": self.total, "page": self.page, "page_size": self.page_size, "rows": [row.as_dict() for row in self.rows]}


def require_mapping(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GoldenSearchRefusal(f"{what} must be an object.", code="PAYLOAD_INVALID", field=what)
    return value
