"""Golden Search qualified versions: their records, their status, and the coverage they grant (#2696).

A qualification is an immutable approved tuple — program key and version,
stock, canonical parameters, executable digests and proof — in
``research_golden_qualifications``. It never changes in place: re-proofs and
revocations are appended to ``research_golden_qualification_events``, and
the per-(program, stock) default Deploy offers is a separate pointer moved
only by compare-and-set, with every move kept in
``research_golden_default_history``.

Two questions are answered here and kept apart, because one resolver mode
cannot safely answer both:

* :func:`qualification_status` — is this record ready, stale or revoked for
  the code running now? Pure.
* :func:`resolve_coverage` — does a sealed configuration have corpus
  coverage? The registry's validated point covers without a database read;
  otherwise only the newest *ready* qualification for the exact program
  version, stock and parameter hash does. An unreadable store answers
  UNCOVERED with a "cannot verify" explanation, never an exception and never
  a claim that no approval exists.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import asyncpg

from app.research.persistence.db import with_connection
from app.schemas.run_admission import (
    QUALIFICATION_ABSENT,
    QUALIFICATION_COVERED,
    QUALIFICATION_REVOKED,
    QUALIFICATION_STALE,
    QUALIFICATION_UNVERIFIABLE,
    REGISTRY_POINT_COVERED,
)
from app.schemas.signal_program_seal import semantic_payload_hash

if TYPE_CHECKING:
    from app.engine.strategy.registry import SignalProgramContract

logger = logging.getLogger(__name__)

QualificationStatus = Literal["ready", "stale", "revoked"]
EventKind = Literal["reproved", "revoked"]
CoverageState = Literal["COVERED", "UNCOVERED"]


class DefaultChangedError(RuntimeError):
    """The default pointer no longer names the qualification the caller reviewed against."""

    def __init__(self, *, program_key: str, symbol: str, expected: str | None, current: str | None) -> None:
        super().__init__(
            f"the Golden Search default for {program_key} on {symbol} is {current or 'unset'}, "
            f"not {expected or 'unset'}; review again"
        )
        self.program_key = program_key
        self.symbol = symbol
        self.expected = expected
        self.current = current


class QualificationEventConflictError(RuntimeError):
    """A command id already recorded a different qualification event."""


@dataclass(frozen=True, slots=True)
class QualificationRow:
    id: str
    program_key: str
    program_version: str
    parameter_schema_version: str
    symbol: str
    params: dict[str, Any]
    params_sha256: str
    artifact_digest: str
    wiring_digest: str
    study_id: str
    golden_run_id: int
    golden_review_id: int
    proof: dict[str, Any]
    proof_sha256: str
    research: dict[str, Any]
    note: str
    approved_by: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class QualificationEvent:
    id: int
    qualification_id: str
    kind: EventKind
    artifact_digest: str | None
    wiring_digest: str | None
    proof: dict[str, Any] | None
    reason: str | None
    actor: str
    command_id: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class GoldenDefault:
    program_key: str
    symbol: str
    qualification_id: str | None
    revision: int
    updated_at_ms: int


def params_sha256(canonical_params: Mapping[str, Any]) -> str:
    """Identity of a canonical point: sha256 of its sorted, compact JSON, ``symbol`` included.

    The seal family's own hash primitive, so a qualification and the seal
    that later pins it hash parameters one way.
    """
    if "symbol" not in canonical_params:
        raise ValueError("a qualification's parameter identity includes its symbol")
    return semantic_payload_hash(dict(canonical_params))


def registry_point_matches(contract: SignalProgramContract, effective: Mapping[str, Any]) -> bool:
    """Whether ``effective`` (canonical, ``symbol`` included) is the program's registered validated point.

    A canonical dump omits a parameter at its identity-neutral default
    (#2696), and every validated setting a dump can omit is that default, so
    an absent name reads as the validated value.
    """
    return str(effective.get("symbol", "")).upper() in contract.validated_symbols and all(
        effective.get(name, value) == value for name, value in contract.validated_settings.items()
    )


# --------------------------------------------------------------------------
# Repository
# --------------------------------------------------------------------------
_QUALIFICATION_COLUMNS = """
    id, program_key, program_version, parameter_schema_version, symbol,
    params_json::text AS params_json, params_sha256, artifact_digest, wiring_digest,
    study_id, golden_run_id, golden_review_id, proof_json::text AS proof_json,
    proof_sha256, research_json::text AS research_json, note, approved_by, created_at_ms
"""
_EVENT_COLUMNS = """
    id, qualification_id, kind, artifact_digest, wiring_digest,
    proof_json::text AS proof_json, reason, actor, command_id, created_at_ms
"""
_DEFAULT_COLUMNS = "program_key, symbol, qualification_id, revision, updated_at_ms"


def _qualification(row: asyncpg.Record) -> QualificationRow:
    return QualificationRow(
        id=row["id"],
        program_key=row["program_key"],
        program_version=row["program_version"],
        parameter_schema_version=row["parameter_schema_version"],
        symbol=row["symbol"],
        params=json.loads(row["params_json"]),
        params_sha256=row["params_sha256"],
        artifact_digest=row["artifact_digest"],
        wiring_digest=row["wiring_digest"],
        study_id=row["study_id"],
        golden_run_id=row["golden_run_id"],
        golden_review_id=row["golden_review_id"],
        proof=json.loads(row["proof_json"]),
        proof_sha256=row["proof_sha256"],
        research=json.loads(row["research_json"]),
        note=row["note"],
        approved_by=row["approved_by"],
        created_at_ms=row["created_at_ms"],
    )


def _event(row: asyncpg.Record) -> QualificationEvent:
    return QualificationEvent(
        id=row["id"],
        qualification_id=row["qualification_id"],
        kind=row["kind"],
        artifact_digest=row["artifact_digest"],
        wiring_digest=row["wiring_digest"],
        proof=None if row["proof_json"] is None else json.loads(row["proof_json"]),
        reason=row["reason"],
        actor=row["actor"],
        command_id=row["command_id"],
        created_at_ms=row["created_at_ms"],
    )


async def insert_qualification(
    conn: asyncpg.Connection,
    *,
    qualification_id: str,
    program_key: str,
    program_version: str,
    parameter_schema_version: str,
    symbol: str,
    params: Mapping[str, Any],
    artifact_digest: str,
    wiring_digest: str,
    study_id: str,
    golden_run_id: int,
    golden_review_id: int,
    proof: Mapping[str, Any],
    research: Mapping[str, Any],
    note: str,
    approved_by: str,
    created_at_ms: int,
) -> QualificationRow:
    """Append one qualified version. Its parameter and proof hashes are derived here, never supplied.

    Refused, and nothing written, when the stored JSON no longer hashes to
    those digests: JSONB rewrites some floats (``-0.0`` reads back as
    ``0.0``), and a point whose stored form hashes differently could never
    be found by its own identity. Runs in its own transaction, a savepoint
    when the caller already holds one.
    """
    if params.get("symbol") != symbol:
        raise ValueError(f"the qualified parameters name symbol {params.get('symbol')!r}, not {symbol!r}")
    async with conn.transaction():
        row = await conn.fetchrow(
            f"""
            INSERT INTO research_golden_qualifications (
                id, program_key, program_version, parameter_schema_version, symbol,
                params_json, params_sha256, artifact_digest, wiring_digest, study_id,
                golden_run_id, golden_review_id, proof_json, proof_sha256, research_json,
                note, approved_by, created_at_ms
            ) VALUES (
                $1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12, $13::jsonb, $14, $15::jsonb, $16, $17, $18
            )
            RETURNING {_QUALIFICATION_COLUMNS}
            """,
            qualification_id,
            program_key,
            program_version,
            parameter_schema_version,
            symbol,
            json.dumps(dict(params)),
            params_sha256(params),
            artifact_digest,
            wiring_digest,
            study_id,
            golden_run_id,
            golden_review_id,
            json.dumps(dict(proof)),
            semantic_payload_hash(dict(proof)),
            json.dumps(dict(research)),
            note,
            approved_by,
            created_at_ms,
        )
        if row is None:
            raise RuntimeError(f"qualification {qualification_id!r} was not returned by its own insert")
        stored = _qualification(row)
        if params_sha256(stored.params) != stored.params_sha256 or (
            semantic_payload_hash(stored.proof) != stored.proof_sha256
        ):
            raise ValueError(
                f"qualification {qualification_id!r} does not survive storage: its stored parameters or proof "
                "hash differently from what was approved"
            )
    return stored


async def get_qualification(conn: asyncpg.Connection, qualification_id: str) -> QualificationRow | None:
    row = await conn.fetchrow(
        f"SELECT {_QUALIFICATION_COLUMNS} FROM research_golden_qualifications WHERE id = $1",
        qualification_id,
    )
    return None if row is None else _qualification(row)


async def get_qualification_by_study(conn: asyncpg.Connection, study_id: str) -> QualificationRow | None:
    """The qualified version a study published, if any: at most one, by the table's unique key."""
    row = await conn.fetchrow(
        f"SELECT {_QUALIFICATION_COLUMNS} FROM research_golden_qualifications WHERE study_id = $1",
        study_id,
    )
    return None if row is None else _qualification(row)


async def find_qualifications(
    conn: asyncpg.Connection,
    *,
    program_key: str,
    symbol: str,
    params_sha256: str,
    program_version: str | None = None,
) -> list[QualificationRow]:
    """Every qualification of this exact tuple, newest first."""
    rows = await conn.fetch(
        f"""
        SELECT {_QUALIFICATION_COLUMNS}
          FROM research_golden_qualifications
         WHERE program_key = $1 AND symbol = $2 AND params_sha256 = $3
           AND ($4::text IS NULL OR program_version = $4)
         ORDER BY created_at_ms DESC, id DESC
        """,
        program_key,
        symbol,
        params_sha256,
        program_version,
    )
    return [_qualification(row) for row in rows]


async def list_qualifications(
    conn: asyncpg.Connection,
    *,
    program_key: str | None = None,
    symbol: str | None = None,
    limit: int = 200,
) -> list[QualificationRow]:
    """Qualifications, newest first, optionally narrowed to one program and/or stock."""
    rows = await conn.fetch(
        f"""
        SELECT {_QUALIFICATION_COLUMNS}
          FROM research_golden_qualifications
         WHERE ($1::text IS NULL OR program_key = $1)
           AND ($2::text IS NULL OR symbol = $2)
         ORDER BY created_at_ms DESC, id DESC
         LIMIT $3
        """,
        program_key,
        symbol,
        limit,
    )
    return [_qualification(row) for row in rows]


async def events_for(conn: asyncpg.Connection, qualification_ids: Sequence[str]) -> dict[str, list[QualificationEvent]]:
    """Each qualification's events in append order; every requested id is present, possibly empty."""
    events: dict[str, list[QualificationEvent]] = {qualification_id: [] for qualification_id in qualification_ids}
    if not events:
        return events
    rows = await conn.fetch(
        f"""
        SELECT {_EVENT_COLUMNS}
          FROM research_golden_qualification_events
         WHERE qualification_id = ANY($1::text[])
         ORDER BY id
        """,
        list(events),
    )
    for row in rows:
        events[row["qualification_id"]].append(_event(row))
    return events


def _event_semantics(
    *,
    qualification_id: str,
    kind: str,
    artifact_digest: str | None,
    wiring_digest: str | None,
    proof: Mapping[str, Any] | None,
    reason: str | None,
) -> tuple[object, ...]:
    return (qualification_id, kind, artifact_digest, wiring_digest, None if proof is None else dict(proof), reason)


async def append_event(
    conn: asyncpg.Connection,
    *,
    qualification_id: str,
    kind: EventKind,
    actor: str,
    command_id: str,
    created_at_ms: int,
    artifact_digest: str | None = None,
    wiring_digest: str | None = None,
    proof: Mapping[str, Any] | None = None,
    reason: str | None = None,
) -> QualificationEvent:
    """Append a re-proof or revocation, idempotently per ``command_id``.

    Replaying a command returns the event it recorded. Reusing a command id
    for a different event is refused rather than silently ignored.
    """
    if kind == "reproved" and (artifact_digest is None or wiring_digest is None or proof is None):
        raise ValueError("a re-proof records its artifact digest, wiring digest and proof")
    if kind == "revoked" and (reason is None or not reason.strip()):
        raise ValueError("a revocation records its reason")
    row = await conn.fetchrow(
        f"""
        INSERT INTO research_golden_qualification_events (
            qualification_id, kind, artifact_digest, wiring_digest, proof_json, reason,
            actor, command_id, created_at_ms
        ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
        ON CONFLICT (command_id) DO NOTHING
        RETURNING {_EVENT_COLUMNS}
        """,
        qualification_id,
        kind,
        artifact_digest,
        wiring_digest,
        None if proof is None else json.dumps(dict(proof)),
        reason,
        actor,
        command_id,
        created_at_ms,
    )
    if row is not None:
        return _event(row)
    prior_row = await conn.fetchrow(
        f"SELECT {_EVENT_COLUMNS} FROM research_golden_qualification_events WHERE command_id = $1",
        command_id,
    )
    if prior_row is None:
        raise QualificationEventConflictError(f"command {command_id!r} conflicted but its event is not readable")
    prior = _event(prior_row)
    requested = _event_semantics(
        qualification_id=qualification_id,
        kind=kind,
        artifact_digest=artifact_digest,
        wiring_digest=wiring_digest,
        proof=proof,
        reason=reason,
    )
    recorded = _event_semantics(
        qualification_id=prior.qualification_id,
        kind=prior.kind,
        artifact_digest=prior.artifact_digest,
        wiring_digest=prior.wiring_digest,
        proof=prior.proof,
        reason=prior.reason,
    )
    if requested != recorded:
        raise QualificationEventConflictError(f"command {command_id!r} already recorded a different event")
    return prior


async def get_event_by_command(conn: asyncpg.Connection, command_id: str) -> QualificationEvent | None:
    """The event a command already recorded, so a replayed command answers without redoing its work."""
    row = await conn.fetchrow(
        f"SELECT {_EVENT_COLUMNS} FROM research_golden_qualification_events WHERE command_id = $1",
        command_id,
    )
    return None if row is None else _event(row)


async def _lock_default_pointer(conn: asyncpg.Connection, program_key: str, symbol: str) -> None:
    """Serialize every writer of one (program, stock) default until the caller's transaction ends.

    A transaction-scoped advisory lock rather than only the pointer's row
    lock: before the first pointer exists there is no row to lock, and a
    revocation racing the first default would otherwise slip between the
    default's revocation check and its insert.
    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"golden_default:{program_key}\x1f{symbol}",
    )


async def get_default(conn: asyncpg.Connection, program_key: str, symbol: str) -> GoldenDefault | None:
    row = await conn.fetchrow(
        f"SELECT {_DEFAULT_COLUMNS} FROM research_golden_defaults WHERE program_key = $1 AND symbol = $2",
        program_key,
        symbol,
    )
    return None if row is None else GoldenDefault(**row)


async def set_default_cas(
    conn: asyncpg.Connection,
    *,
    program_key: str,
    symbol: str,
    qualification_id: str | None,
    expected_qualification_id: str | None,
    reason: str,
    actor: str,
    now_ms: int,
) -> int:
    """Move the (program, stock) default from ``expected_qualification_id`` to ``qualification_id``.

    Compare-and-set under the pointer's row lock (or, for the first pointer,
    its primary key): a pointer that no longer names the expected
    qualification raises :class:`DefaultChangedError` and changes nothing.
    ``qualification_id=None`` clears the pointer. Every move appends a
    history row. Returns the pointer's new revision. Runs in its own
    transaction, a savepoint when the caller already holds one.
    """
    if not reason.strip():
        raise ValueError("a default change records its reason")
    async with conn.transaction():
        await _lock_default_pointer(conn, program_key, symbol)
        current = await conn.fetchrow(
            """
            SELECT qualification_id, revision FROM research_golden_defaults
             WHERE program_key = $1 AND symbol = $2
               FOR UPDATE
            """,
            program_key,
            symbol,
        )
        # Checked after the pointer lock, so a revocation that committed while
        # this waited on it is seen.
        if qualification_id is not None:
            target = await conn.fetchrow(
                """
                SELECT q.program_key, q.symbol,
                       EXISTS (
                           SELECT 1 FROM research_golden_qualification_events e
                            WHERE e.qualification_id = q.id AND e.kind = 'revoked'
                       ) AS revoked
                  FROM research_golden_qualifications q
                 WHERE q.id = $1
                """,
                qualification_id,
            )
            if target is None:
                raise ValueError(f"qualification {qualification_id!r} does not exist")
            if (target["program_key"], target["symbol"]) != (program_key, symbol):
                raise ValueError(
                    f"qualification {qualification_id!r} is for {target['program_key']} on {target['symbol']}, "
                    f"not {program_key} on {symbol}"
                )
            if target["revoked"]:
                raise ValueError(f"qualification {qualification_id!r} is revoked and cannot become the default")
        if current is None:
            revision = 1
            previous = None
            if expected_qualification_id is not None:
                raise DefaultChangedError(
                    program_key=program_key, symbol=symbol, expected=expected_qualification_id, current=None
                )
            inserted = await conn.fetchval(
                """
                INSERT INTO research_golden_defaults (program_key, symbol, qualification_id, revision, updated_at_ms)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (program_key, symbol) DO NOTHING
                RETURNING revision
                """,
                program_key,
                symbol,
                qualification_id,
                revision,
                now_ms,
            )
            if inserted is None:
                # A concurrent first pointer committed while this insert waited on its key.
                winner = await conn.fetchval(
                    "SELECT qualification_id FROM research_golden_defaults WHERE program_key = $1 AND symbol = $2",
                    program_key,
                    symbol,
                )
                raise DefaultChangedError(
                    program_key=program_key, symbol=symbol, expected=expected_qualification_id, current=winner
                )
        else:
            previous = current["qualification_id"]
            if previous != expected_qualification_id:
                raise DefaultChangedError(
                    program_key=program_key, symbol=symbol, expected=expected_qualification_id, current=previous
                )
            revision = current["revision"] + 1
            await conn.execute(
                """
                UPDATE research_golden_defaults
                   SET qualification_id = $3, revision = $4, updated_at_ms = $5
                 WHERE program_key = $1 AND symbol = $2
                """,
                program_key,
                symbol,
                qualification_id,
                revision,
                now_ms,
            )
        await conn.execute(
            """
            INSERT INTO research_golden_default_history (
                program_key, symbol, previous_qualification_id, qualification_id, revision, reason, actor, created_at_ms
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            program_key,
            symbol,
            previous,
            qualification_id,
            revision,
            reason,
            actor,
            now_ms,
        )
    return revision


class QualificationNotFoundError(LookupError):
    """No qualified version has this id."""


@dataclass(frozen=True, slots=True)
class Revocation:
    """A recorded revocation and whether it cleared the (program, stock) default."""

    event: QualificationEvent
    cleared_default: bool


async def revoke_qualification(
    conn: asyncpg.Connection,
    *,
    qualification_id: str,
    reason: str,
    actor: str,
    command_id: str,
    now_ms: int,
) -> Revocation:
    """Revoke one qualified version; when it is its stock's default, clear that pointer too.

    One transaction under the default's lock (the advisory lock and the
    pointer row ``FOR UPDATE``), so a concurrent approval either commits
    before this and is cleared by it, or waits and then refuses the revoked
    version. Idempotent per ``command_id``: a replay returns the recorded
    revocation and changes nothing else. A revocation is never undone; a
    re-proof does not revive it.
    """
    async with conn.transaction():
        qualification = await get_qualification(conn, qualification_id)
        if qualification is None:
            raise QualificationNotFoundError(f"Golden Search qualification {qualification_id!r} was not found")
        prior = await get_event_by_command(conn, command_id)
        if prior is not None:
            event = await append_event(
                conn,
                qualification_id=qualification_id,
                kind="revoked",
                actor=actor,
                command_id=command_id,
                created_at_ms=now_ms,
                reason=reason,
            )
            return Revocation(event=event, cleared_default=False)
        await _lock_default_pointer(conn, qualification.program_key, qualification.symbol)
        pointer = await conn.fetchrow(
            """
            SELECT qualification_id FROM research_golden_defaults
             WHERE program_key = $1 AND symbol = $2
               FOR UPDATE
            """,
            qualification.program_key,
            qualification.symbol,
        )
        event = await append_event(
            conn,
            qualification_id=qualification_id,
            kind="revoked",
            actor=actor,
            command_id=command_id,
            created_at_ms=now_ms,
            reason=reason,
        )
        cleared = pointer is not None and pointer["qualification_id"] == qualification_id
        if cleared:
            await set_default_cas(
                conn,
                program_key=qualification.program_key,
                symbol=qualification.symbol,
                qualification_id=None,
                expected_qualification_id=qualification_id,
                reason=f"Qualification {qualification_id} revoked: {reason}",
                actor=actor,
                now_ms=now_ms,
            )
    return Revocation(event=event, cleared_default=cleared)


@dataclass(frozen=True, slots=True)
class DefaultQualification:
    """One (program, stock) default pointer, the qualified version it names, and that version's events."""

    default: GoldenDefault
    qualification: QualificationRow
    events: tuple[QualificationEvent, ...]


async def read_default_qualifications(
    conn: asyncpg.Connection,
    *,
    program_key: str | None = None,
    symbol: str | None = None,
) -> list[DefaultQualification]:
    """Every set default pointer with its qualified version and events, read in one snapshot.

    A cleared pointer (no qualification) is left out: it offers nothing.
    Status is the caller's to judge against the build it runs.
    """
    if conn.is_in_transaction():
        return await _read_defaults(conn, program_key=program_key, symbol=symbol)
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        return await _read_defaults(conn, program_key=program_key, symbol=symbol)


async def _read_defaults(
    conn: asyncpg.Connection, *, program_key: str | None, symbol: str | None
) -> list[DefaultQualification]:
    pointers = await conn.fetch(
        f"""
        SELECT {_DEFAULT_COLUMNS} FROM research_golden_defaults
         WHERE qualification_id IS NOT NULL
           AND ($1::text IS NULL OR program_key = $1)
           AND ($2::text IS NULL OR symbol = $2)
         ORDER BY program_key, symbol
        """,
        program_key,
        symbol,
    )
    defaults = [GoldenDefault(**row) for row in pointers]
    named = [pointer.qualification_id for pointer in defaults if pointer.qualification_id is not None]
    rows = {
        row["id"]: _qualification(row)
        for row in await conn.fetch(
            f"SELECT {_QUALIFICATION_COLUMNS} FROM research_golden_qualifications WHERE id = ANY($1::text[])",
            named,
        )
    }
    events = await events_for(conn, list(rows))
    return [
        DefaultQualification(
            default=pointer, qualification=rows[qualification_id], events=tuple(events[qualification_id])
        )
        for pointer in defaults
        if (qualification_id := pointer.qualification_id) is not None and qualification_id in rows
    ]


# --------------------------------------------------------------------------
# Status and coverage
# --------------------------------------------------------------------------
def qualification_status(
    row: QualificationRow,
    events: Sequence[QualificationEvent],
    running_artifact_digest: str,
) -> QualificationStatus:
    """``revoked`` once any revocation exists; else ``ready`` iff the latest proof's digest is the running one.

    The latest proof is the newest ``reproved`` event's, or the approval's own
    when it was never re-proved. Events of another qualification are refused:
    mixing them in could revive or revoke the wrong record.
    """
    foreign = [event.id for event in events if event.qualification_id != row.id]
    if foreign:
        raise ValueError(f"events {foreign} do not belong to qualification {row.id!r}")
    if any(event.kind == "revoked" for event in events):
        return "revoked"
    reproofs = [event for event in events if event.kind == "reproved"]
    proven_digest = max(reproofs, key=lambda event: event.id).artifact_digest if reproofs else row.artifact_digest
    return "ready" if proven_digest == running_artifact_digest else "stale"


@dataclass(frozen=True, slots=True)
class QualificationSubject:
    """The exact tuple a coverage lookup asks about."""

    program_key: str
    program_version: str
    symbol: str
    params_sha256: str


@dataclass(frozen=True, slots=True)
class QualificationEvidence:
    qualification: QualificationRow
    events: tuple[QualificationEvent, ...]


QualificationLookup = Callable[[QualificationSubject], Awaitable[Sequence[QualificationEvidence]]]


async def read_qualification_evidence(
    conn: asyncpg.Connection, subject: QualificationSubject
) -> list[QualificationEvidence]:
    """Every qualification of ``subject`` with its events, read in one snapshot.

    On its own, both reads share one read-only repeatable-read transaction,
    so a revocation cannot land between them. Inside a caller's transaction
    they share the caller's, under its isolation.
    """
    if conn.is_in_transaction():
        return await _read_evidence(conn, subject)
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        return await _read_evidence(conn, subject)


async def _read_evidence(conn: asyncpg.Connection, subject: QualificationSubject) -> list[QualificationEvidence]:
    rows = await find_qualifications(
        conn,
        program_key=subject.program_key,
        symbol=subject.symbol,
        params_sha256=subject.params_sha256,
        program_version=subject.program_version,
    )
    events = await events_for(conn, [row.id for row in rows])
    return [QualificationEvidence(qualification=row, events=tuple(events[row.id])) for row in rows]


async def load_qualification_evidence(subject: QualificationSubject) -> list[QualificationEvidence]:
    """The production lookup: every qualification of ``subject`` with its events, from the research store."""
    return await with_connection(read_qualification_evidence, subject)


@dataclass(frozen=True, slots=True)
class Coverage:
    state: CoverageState
    qualification_id: str | None
    explanation: str
    # The running artifact digest a qualification's status was judged
    # against; ``None`` when no qualification was judged (the registry point,
    # or evidence that could not be read). A proof computed under another
    # digest must not reuse this verdict.
    artifact_digest: str | None = None


def _matches_subject(row: QualificationRow, subject: QualificationSubject) -> bool:
    return (
        row.program_key == subject.program_key
        and row.program_version == subject.program_version
        and row.symbol == subject.symbol
        and row.params_sha256 == subject.params_sha256
    )


def _coverage_from(
    evidence: Sequence[QualificationEvidence],
    subject: QualificationSubject,
    running_artifact_digest: str,
    *,
    pinned_qualification_id: str | None = None,
) -> Coverage:
    # The lookup is trusted for nothing: a record of another tuple never covers this one.
    statuses = [
        (item.qualification, qualification_status(item.qualification, item.events, running_artifact_digest))
        for item in evidence
        if _matches_subject(item.qualification, subject)
        and (pinned_qualification_id is None or item.qualification.id == pinned_qualification_id)
    ]
    judged = {"artifact_digest": running_artifact_digest}
    ready = [row for row, status in statuses if status == "ready"]
    if ready:
        newest = max(ready, key=lambda row: (row.created_at_ms, row.id))
        return Coverage(state="COVERED", qualification_id=newest.id, explanation=QUALIFICATION_COVERED, **judged)
    if any(status == "stale" for _row, status in statuses):
        return Coverage(state="UNCOVERED", qualification_id=None, explanation=QUALIFICATION_STALE, **judged)
    if statuses:
        return Coverage(state="UNCOVERED", qualification_id=None, explanation=QUALIFICATION_REVOKED, **judged)
    return Coverage(state="UNCOVERED", qualification_id=None, explanation=QUALIFICATION_ABSENT, **judged)


async def resolve_coverage(
    *,
    program_key: str,
    contract: SignalProgramContract,
    params: Mapping[str, Any],
    running_artifact_digest: str,
    lookup: QualificationLookup = load_qualification_evidence,
    qualification_id: str | None = None,
) -> Coverage:
    """Corpus coverage for one sealed configuration (``params`` canonical, ``symbol`` included).

    The registered validated point is covered without a read. Otherwise the
    newest ready qualification of the exact (program, version, stock,
    parameter hash) covers it; a stale one is reported as needing a
    re-proof, a revoked one as revoked. ``qualification_id`` pins the answer
    to that one qualified version — a seal re-verifying the version it was
    admitted under is covered by it alone, never by whichever version is
    ready or default now. Fails closed: any failure to read or interpret the
    evidence is UNCOVERED with a "cannot verify" explanation.
    """
    if registry_point_matches(contract, params):
        return Coverage(state="COVERED", qualification_id=None, explanation=REGISTRY_POINT_COVERED)
    try:
        subject = QualificationSubject(
            program_key=program_key,
            program_version=contract.program_version,
            symbol=str(params["symbol"]),
            params_sha256=params_sha256(params),
        )
        return _coverage_from(
            await lookup(subject),
            subject,
            running_artifact_digest,
            pinned_qualification_id=qualification_id,
        )
    except Exception:
        # Admission must fail closed, not raise: an escaped error is not a refusal.
        logger.warning(
            "Golden Search qualification evidence could not be read; coverage fails closed",
            extra={"action": "golden_qualification_unverifiable", "program_key": program_key},
            exc_info=True,
        )
        return Coverage(state="UNCOVERED", qualification_id=None, explanation=QUALIFICATION_UNVERIFIABLE)


__all__ = [
    "QUALIFICATION_ABSENT",
    "QUALIFICATION_COVERED",
    "QUALIFICATION_REVOKED",
    "QUALIFICATION_STALE",
    "QUALIFICATION_UNVERIFIABLE",
    "REGISTRY_POINT_COVERED",
    "Coverage",
    "CoverageState",
    "DefaultChangedError",
    "DefaultQualification",
    "EventKind",
    "GoldenDefault",
    "QualificationEvent",
    "QualificationEventConflictError",
    "QualificationEvidence",
    "QualificationLookup",
    "QualificationNotFoundError",
    "QualificationRow",
    "QualificationStatus",
    "QualificationSubject",
    "Revocation",
    "append_event",
    "events_for",
    "find_qualifications",
    "get_default",
    "get_event_by_command",
    "get_qualification",
    "get_qualification_by_study",
    "insert_qualification",
    "list_qualifications",
    "load_qualification_evidence",
    "params_sha256",
    "qualification_status",
    "read_default_qualifications",
    "read_qualification_evidence",
    "registry_point_matches",
    "resolve_coverage",
    "revoke_qualification",
    "set_default_cas",
]
