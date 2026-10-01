"""Qualification status and the coverage resolver, with an injected lookup (#2696).

No database: every lookup here is a stand-in returning (or failing to return)
evidence, so these run in PR CI. The repository itself is exercised against
Postgres in ``test_qualifications_repository.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence

import asyncpg
import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.golden_search.qualifications import (
    QUALIFICATION_ABSENT,
    QUALIFICATION_COVERED,
    QUALIFICATION_REVOKED,
    QUALIFICATION_STALE,
    QUALIFICATION_UNVERIFIABLE,
    REGISTRY_POINT_COVERED,
    EventKind,
    QualificationEvent,
    QualificationEvidence,
    QualificationRow,
    QualificationSubject,
    params_sha256,
    qualification_status,
    registry_point_matches,
    resolve_coverage,
)

PROGRAM = "ema_crossover_signal"
CONTRACT = _STRATEGY_REGISTRY[PROGRAM].signal_program_contract
assert CONTRACT is not None
RUNNING = "1" * 64
PREVIOUS = "2" * 64
REGISTRY_POINT = {**CONTRACT.validated_settings, "symbol": "SPY"}
# A configuration only a Golden Search qualification can cover.
TUNED = {**CONTRACT.validated_settings, "rsi_min": 30.0, "symbol": "SPY"}


def _row(
    qualification_id: str = "q-1",
    *,
    params: dict | None = None,
    artifact_digest: str = RUNNING,
    created_at_ms: int = 1_000,
    program_version: str | None = None,
) -> QualificationRow:
    point = TUNED if params is None else params
    return QualificationRow(
        id=qualification_id,
        program_key=PROGRAM,
        program_version=CONTRACT.program_version if program_version is None else program_version,
        parameter_schema_version=CONTRACT.parameter_schema_version,
        symbol=str(point["symbol"]),
        params=dict(point),
        params_sha256=params_sha256(point),
        artifact_digest=artifact_digest,
        wiring_digest="w" * 64,
        study_id=f"study-{qualification_id}",
        golden_run_id=1,
        golden_review_id=1,
        proof={},
        proof_sha256="p" * 64,
        research={},
        note="Approved after the final test.",
        approved_by="local:owner",
        created_at_ms=created_at_ms,
    )


def _event(
    event_id: int, kind: EventKind, *, qualification_id: str = "q-1", artifact_digest: str | None = None
) -> QualificationEvent:
    return QualificationEvent(
        id=event_id,
        qualification_id=qualification_id,
        kind=kind,
        artifact_digest=artifact_digest,
        wiring_digest=None if artifact_digest is None else "w" * 64,
        proof=None if artifact_digest is None else {},
        reason="Parameters withdrawn." if kind == "revoked" else None,
        actor="local:owner",
        command_id=f"command-{event_id}",
        created_at_ms=2_000 + event_id,
    )


def _evidence(row: QualificationRow, *events: QualificationEvent) -> QualificationEvidence:
    return QualificationEvidence(qualification=row, events=tuple(events))


class _Lookup:
    """Returns fixed evidence and records each subject it was asked about."""

    def __init__(self, evidence: Sequence[QualificationEvidence] = ()) -> None:
        self.evidence = list(evidence)
        self.subjects: list[QualificationSubject] = []

    async def __call__(self, subject: QualificationSubject) -> list[QualificationEvidence]:
        self.subjects.append(subject)
        return self.evidence


async def _resolve(params: dict, lookup) -> object:
    return await resolve_coverage(
        program_key=PROGRAM, contract=CONTRACT, params=params, running_artifact_digest=RUNNING, lookup=lookup
    )


# ---------------------------------------------------------------------------
# params_sha256 and the registry point
# ---------------------------------------------------------------------------
def test_params_sha256_is_the_sha256_of_sorted_compact_json() -> None:
    point = {"symbol": "SPY", "gap": 0.2, "rsi_min": 30.0}

    expected = hashlib.sha256(json.dumps(point, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    assert params_sha256(point) == expected
    assert params_sha256(dict(reversed(point.items()))) == expected


def test_params_sha256_binds_the_symbol() -> None:
    assert params_sha256({**TUNED, "symbol": "QQQ"}) != params_sha256(TUNED)
    with pytest.raises(ValueError, match="includes its symbol"):
        params_sha256({"gap": 0.2})


def test_registry_point_matches_only_the_validated_point_on_a_validated_symbol() -> None:
    assert registry_point_matches(CONTRACT, REGISTRY_POINT)
    assert registry_point_matches(CONTRACT, {**REGISTRY_POINT, "symbol": "spy"})
    assert not registry_point_matches(CONTRACT, TUNED)
    assert not registry_point_matches(CONTRACT, {**REGISTRY_POINT, "symbol": "MSFT"})


# ---------------------------------------------------------------------------
# qualification_status
# ---------------------------------------------------------------------------
def test_qualification_status_is_ready_when_the_approval_digest_is_running() -> None:
    assert qualification_status(_row(), [], RUNNING) == "ready"


def test_qualification_status_is_stale_once_the_program_bytes_move() -> None:
    assert qualification_status(_row(artifact_digest=PREVIOUS), [], RUNNING) == "stale"


def test_qualification_status_follows_the_latest_reproof() -> None:
    row = _row(artifact_digest=PREVIOUS)

    assert qualification_status(row, [_event(1, "reproved", artifact_digest=RUNNING)], RUNNING) == "ready"
    assert (
        qualification_status(
            row,
            [_event(1, "reproved", artifact_digest=RUNNING), _event(2, "reproved", artifact_digest="3" * 64)],
            RUNNING,
        )
        == "stale"
    )
    # A re-proof under newer code moves the proof off the original digest: running the old bytes again is stale.
    assert qualification_status(_row(), [_event(1, "reproved", artifact_digest=PREVIOUS)], RUNNING) == "stale"


def test_qualification_status_revoked_wins_over_any_reproof() -> None:
    events = [_event(1, "revoked"), _event(2, "reproved", artifact_digest=RUNNING)]

    assert qualification_status(_row(), events, RUNNING) == "revoked"


def test_qualification_status_refuses_another_qualifications_events() -> None:
    with pytest.raises(ValueError, match="do not belong"):
        qualification_status(_row(), [_event(1, "revoked", qualification_id="q-2")], RUNNING)


# ---------------------------------------------------------------------------
# resolve_coverage
# ---------------------------------------------------------------------------
async def test_resolve_coverage_covers_the_registry_point_without_a_read() -> None:
    async def unreachable(_subject: QualificationSubject) -> list[QualificationEvidence]:
        raise AssertionError("the registry point must not read the qualification store")

    coverage = await _resolve(REGISTRY_POINT, unreachable)

    assert (coverage.state, coverage.qualification_id, coverage.explanation) == (
        "COVERED",
        None,
        REGISTRY_POINT_COVERED,
    )


async def test_resolve_coverage_asks_for_the_exact_tuple() -> None:
    lookup = _Lookup()

    await _resolve(TUNED, lookup)

    assert lookup.subjects == [
        QualificationSubject(
            program_key=PROGRAM,
            program_version=CONTRACT.program_version,
            symbol="SPY",
            params_sha256=params_sha256(TUNED),
        )
    ]


async def test_resolve_coverage_covers_with_the_newest_ready_qualification() -> None:
    older, newer = _row("q-old", created_at_ms=1_000), _row("q-new", created_at_ms=5_000)
    stale = _row("q-stale", created_at_ms=9_000, artifact_digest=PREVIOUS)

    coverage = await _resolve(TUNED, _Lookup([_evidence(older), _evidence(stale), _evidence(newer)]))

    assert (coverage.state, coverage.qualification_id, coverage.explanation) == (
        "COVERED",
        "q-new",
        QUALIFICATION_COVERED,
    )


async def test_resolve_coverage_reports_a_stale_qualification_as_needing_a_reproof() -> None:
    coverage = await _resolve(TUNED, _Lookup([_evidence(_row(artifact_digest=PREVIOUS))]))

    assert (coverage.state, coverage.qualification_id, coverage.explanation) == (
        "UNCOVERED",
        None,
        QUALIFICATION_STALE,
    )


async def test_resolve_coverage_never_covers_with_a_revoked_qualification() -> None:
    coverage = await _resolve(TUNED, _Lookup([_evidence(_row(), _event(1, "revoked"))]))

    assert (coverage.state, coverage.explanation) == ("UNCOVERED", QUALIFICATION_REVOKED)


async def test_resolve_coverage_prefers_the_actionable_stale_explanation_over_revoked() -> None:
    revoked = _evidence(_row("q-1", created_at_ms=9_000), _event(1, "revoked"))
    stale = _evidence(_row("q-2", artifact_digest=PREVIOUS))

    coverage = await _resolve(TUNED, _Lookup([revoked, stale]))

    assert (coverage.state, coverage.explanation) == ("UNCOVERED", QUALIFICATION_STALE)


async def test_resolve_coverage_without_any_qualification_is_uncovered() -> None:
    coverage = await _resolve(TUNED, _Lookup())

    assert (coverage.state, coverage.qualification_id, coverage.explanation) == (
        "UNCOVERED",
        None,
        QUALIFICATION_ABSENT,
    )


@pytest.mark.parametrize(
    "foreign",
    [
        _row(params={**TUNED, "rsi_min": 31.0}),
        _row(params={**TUNED, "symbol": "QQQ"}),
        _row(program_version="ema-crossover-signal/v0"),
    ],
    ids=["other-parameters", "other-stock", "other-program-version"],
)
async def test_resolve_coverage_never_trusts_a_lookup_row_for_another_tuple(foreign: QualificationRow) -> None:
    coverage = await _resolve(TUNED, _Lookup([_evidence(foreign)]))

    assert (coverage.state, coverage.explanation) == ("UNCOVERED", QUALIFICATION_ABSENT)


async def test_resolve_coverage_leaves_an_unrelated_stock_uncovered() -> None:
    lookup = _Lookup([_evidence(_row())])

    spy = await _resolve(TUNED, lookup)
    qqq = await _resolve({**TUNED, "symbol": "QQQ"}, lookup)

    assert spy.state == "COVERED"
    assert (qqq.state, qqq.explanation) == ("UNCOVERED", QUALIFICATION_ABSENT)


@pytest.mark.parametrize(
    "failure",
    [
        asyncpg.UndefinedTableError("relation does not exist"),
        asyncpg.InsufficientPrivilegeError("permission denied"),
        OSError("connection refused"),
        TimeoutError(),
    ],
    ids=["missing-table", "no-grant", "connection-refused", "timeout"],
)
async def test_resolve_coverage_fails_closed_when_the_store_cannot_be_read(
    failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    async def unreadable(_subject: QualificationSubject) -> list[QualificationEvidence]:
        raise failure

    with caplog.at_level(logging.WARNING, logger="app.research.golden_search.qualifications"):
        coverage = await _resolve(TUNED, unreadable)

    assert (coverage.state, coverage.qualification_id, coverage.explanation) == (
        "UNCOVERED",
        None,
        QUALIFICATION_UNVERIFIABLE,
    )
    # Fail-closed, never silent: the cause is logged with its traceback.
    (record,) = [r for r in caplog.records if getattr(r, "action", None) == "golden_qualification_unverifiable"]
    assert record.exc_info is not None and record.exc_info[1] is failure


async def test_resolve_coverage_fails_closed_on_evidence_it_cannot_interpret() -> None:
    mixed = _evidence(_row(), _event(1, "reproved", qualification_id="q-other", artifact_digest=RUNNING))

    coverage = await _resolve(TUNED, _Lookup([mixed]))

    assert (coverage.state, coverage.explanation) == ("UNCOVERED", QUALIFICATION_UNVERIFIABLE)
