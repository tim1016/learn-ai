"""Golden Search qualified versions as Deploy and the owner see them (#2696, ADR 0074).

Status is always judged against the build this process is running: a
version is ``ready`` only while its latest proof names the running artifact
digest, ``stale`` once the code moved, ``revoked`` forever after a
revocation, and ``unverifiable`` when this process cannot name its own
running build (its code on disk is not the code it imported). Nothing here
treats an unreadable or unjudgeable version as ready.

Also here: the Deploy offer for one version, the stock defaults Deploy
prefers, revocation, and re-proof — the restored proof replayed under the
code running now, appended as evidence, never reviving a revoked version.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

from app.data_lake.catalog_client import CatalogUnavailableError
from app.engine.engine import EvaluationBoundaryError
from app.engine.strategy.registry import _STRATEGY_REGISTRY, SignalProgramContract
from app.research.golden_search import qualifications
from app.research.golden_search.proof import BlobStore, ProofMismatchError, ProofRecord, default_blob_store, reprove
from app.research.golden_search.qualifications import GoldenDefault, QualificationEvent, QualificationRow
from app.research.persistence.db import with_connection
from app.schemas.run_admission import QUALIFICATION_REVOKED, QUALIFICATION_STALE, QUALIFICATION_UNJUDGEABLE
from app.services.signal_program_admission import RestartNeededError, running_build_digests
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

StatusName = Literal["ready", "stale", "revoked", "unverifiable"]


class QualificationRefusal(Exception):
    """An owner-facing refusal with a stable code and the HTTP status it maps to."""

    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class JudgedQualification:
    """One qualified version, its events, and what it is worth to the build running now."""

    qualification: QualificationRow
    events: tuple[QualificationEvent, ...]
    status: StatusName
    is_default: bool

    @property
    def explanation(self) -> str:
        return status_explanation(self.qualification, self.status, is_default=self.is_default)


def _contract(program_key: str) -> SignalProgramContract | None:
    registration = _STRATEGY_REGISTRY.get(program_key)
    return registration.signal_program_contract if registration is not None else None


def running_artifact_digest_for(program_key: str) -> str | None:
    """The running build's artifact digest for ``program_key``; ``None`` when it cannot be named."""
    contract = _contract(program_key)
    if contract is None:
        return None
    try:
        return running_build_digests(contract)[0]
    except (RestartNeededError, OSError, ValueError):
        logger.warning(
            "The running Signal Program build cannot be named; Golden Search qualifications are not judged ready",
            extra={"action": "golden_qualification_build_unidentified", "program_key": program_key},
            exc_info=True,
        )
        return None


def judge(
    row: QualificationRow, events: Sequence[QualificationEvent], running_artifact_digest: str | None
) -> StatusName:
    """Revoked first (no build can revive it); otherwise ready or stale for the running digest, if known."""
    if any(event.kind == "revoked" for event in events):
        return "revoked"
    if running_artifact_digest is None:
        return "unverifiable"
    return qualifications.qualification_status(row, events, running_artifact_digest)


def status_explanation(row: QualificationRow, status: StatusName, *, is_default: bool) -> str:
    """The owner-facing sentence for one judged version, naming its study and approval date."""
    if status == "revoked":
        return QUALIFICATION_REVOKED
    if status == "stale":
        return QUALIFICATION_STALE
    if status == "unverifiable":
        return QUALIFICATION_UNJUDGEABLE
    origin = (
        f"Golden configuration {row.id[:11]} from Golden Search study {row.study_id[:8]}, "
        f"approved {et_date_at_ms(row.created_at_ms).isoformat()}"
    )
    standing = "the stock's current default" if is_default else "not the stock's current default"
    return (
        f"{origin}, is ready for the running build and is {standing}. Using it changes the Deploy form only; "
        "current evidence, account access, budget and safety checks still apply."
    )


class _DigestCache:
    """One running-digest read per program per request."""

    def __init__(self) -> None:
        self._digests: dict[str, str | None] = {}

    def __call__(self, program_key: str) -> str | None:
        if program_key not in self._digests:
            self._digests[program_key] = running_artifact_digest_for(program_key)
        return self._digests[program_key]


def _judged(
    rows: Sequence[QualificationRow],
    events: Mapping[str, Sequence[QualificationEvent]],
    default_ids: set[str],
    digests: _DigestCache,
) -> list[JudgedQualification]:
    return [
        JudgedQualification(
            qualification=row,
            events=tuple(events[row.id]),
            status=judge(row, events[row.id], digests(row.program_key)),
            is_default=row.id in default_ids,
        )
        for row in rows
    ]


async def list_judged(
    conn: asyncpg.Connection, *, program_key: str | None, symbol: str | None, limit: int
) -> list[JudgedQualification]:
    """Qualified versions, newest first, each judged against the running build."""
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        rows = await qualifications.list_qualifications(conn, program_key=program_key, symbol=symbol, limit=limit)
        events = await qualifications.events_for(conn, [row.id for row in rows])
        default_ids = await qualifications.default_qualification_ids(conn)
    return _judged(rows, events, default_ids, _DigestCache())


async def get_judged(conn: asyncpg.Connection, qualification_id: str) -> JudgedQualification | None:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        row = await qualifications.get_qualification(conn, qualification_id)
        if row is None:
            return None
        events = await qualifications.events_for(conn, [row.id])
        default_ids = await qualifications.default_qualification_ids(conn)
    return _judged([row], events, default_ids, _DigestCache())[0]


@dataclass(frozen=True)
class JudgedDefault:
    """One set (program, stock) default pointer and the version it names, judged."""

    default: GoldenDefault
    judged: JudgedQualification


async def judged_defaults(
    conn: asyncpg.Connection, *, program_key: str | None = None, symbol: str | None = None
) -> list[JudgedDefault]:
    """Every set (program, stock) default, its version judged against the running build."""
    defaults = await qualifications.read_default_qualifications(conn, program_key=program_key, symbol=symbol)
    digests = _DigestCache()
    return [
        JudgedDefault(
            default=item.default,
            judged=JudgedQualification(
                qualification=item.qualification,
                events=item.events,
                status=judge(item.qualification, item.events, digests(item.qualification.program_key)),
                is_default=True,
            ),
        )
        for item in defaults
    ]


async def ready_defaults() -> dict[tuple[str, str], JudgedQualification]:
    """The READY default per (program, stock) for Deploy; read failures propagate for the caller to log."""
    return {
        (item.default.program_key, item.default.symbol): item.judged
        for item in await with_connection(judged_defaults)
        if item.judged.status == "ready"
    }


def public_parameters(row: QualificationRow) -> dict[str, object]:
    """The canonical parameters as the Deploy form takes them: the stock is chosen beside them, not inside."""
    return {name: value for name, value in row.params.items() if name != "symbol"}


async def revoke(*, qualification_id: str, reason: str, idempotency_key: str, actor: str) -> JudgedQualification:
    command_id = f"golden-qualification:{qualification_id}:revoke:{idempotency_key}"
    try:
        await with_connection(
            qualifications.revoke_qualification,
            qualification_id=qualification_id,
            reason=reason,
            actor=actor,
            command_id=command_id,
            now_ms=now_ms_utc(),
        )
    except qualifications.QualificationNotFoundError as exc:
        raise QualificationRefusal("QUALIFICATION_NOT_FOUND", str(exc), status_code=404) from exc
    except qualifications.QualificationAlreadyRevokedError as exc:
        raise QualificationRefusal("QUALIFICATION_REVOKED", str(exc), status_code=409) from exc
    except qualifications.QualificationEventConflictError as exc:
        raise QualificationRefusal(
            "IDEMPOTENCY_CONFLICT", "That idempotency key already recorded a different command.", status_code=409
        ) from exc
    return await _require_judged(qualification_id)


async def _require_judged(qualification_id: str) -> JudgedQualification:
    judged = await with_connection(get_judged, qualification_id)
    if judged is None:
        raise QualificationRefusal(
            "QUALIFICATION_NOT_FOUND",
            f"Golden Search qualification {qualification_id!r} was not found",
            status_code=404,
        )
    return judged


async def _append_reproof(conn: asyncpg.Connection, *, program_key: str, symbol: str, **event: Any) -> None:
    """Append a re-proof under the stock's default lock: approval judges the default's readiness under it too."""
    async with conn.transaction():
        await qualifications.lock_default_pointer(conn, program_key, symbol)
        await qualifications.append_event(conn, **event)


async def reprove_qualification(
    *,
    qualification_id: str,
    idempotency_key: str,
    actor: str,
    blob_store: BlobStore | None = None,
) -> JudgedQualification:
    """Replay a version's restored proof under the code running now and append the re-proof.

    Refused, with nothing appended: a revoked version (a re-proof never
    revives one), a stored proof that no longer hashes to its recorded
    digest, a process that cannot name its running build, and a replay
    whose trace root differs. The replay runs on a worker thread.
    """
    command_id = f"golden-qualification:{qualification_id}:reprove:{idempotency_key}"
    judged = await _require_judged(qualification_id)
    prior = await with_connection(qualifications.get_event_by_command, command_id)
    if prior is not None:
        if prior.qualification_id != qualification_id or prior.kind != "reproved":
            raise QualificationRefusal(
                "IDEMPOTENCY_CONFLICT", "That idempotency key already recorded a different command.", status_code=409
            )
        return judged
    if judged.status == "revoked":
        raise QualificationRefusal(
            "QUALIFICATION_REVOKED", "A revoked qualification cannot be re-proved back into use.", status_code=409
        )
    row = judged.qualification
    try:
        record = ProofRecord.from_dict(row.proof)
    except ValueError as exc:
        raise QualificationRefusal("PROOF_MISMATCH", f"The stored proof is unreadable: {exc}", status_code=409) from exc
    if record.sha256() != row.proof_sha256:
        raise QualificationRefusal(
            "PROOF_MISMATCH", "The stored proof no longer hashes to the digest recorded at approval.", status_code=409
        )
    contract = _contract(row.program_key)
    if contract is None:
        raise QualificationRefusal(
            "NOT_QUALIFIABLE", f"{row.program_key!r} is no longer a registered Signal Program.", status_code=409
        )
    try:
        artifact_digest, wiring_digest = running_build_digests(contract)
    except RestartNeededError as exc:
        raise QualificationRefusal("RESTART_NEEDED", str(exc), status_code=409) from exc
    except (OSError, ValueError) as exc:
        raise QualificationRefusal(
            "RESTART_NEEDED",
            f"The program's source files could not be read ({type(exc).__name__}). Restart the service.",
            status_code=409,
        ) from exc
    try:
        reproved = await asyncio.to_thread(
            reprove,
            record,
            blob_store or default_blob_store(),
            artifact_digest=artifact_digest,
            wiring_digest=wiring_digest,
        )
    except ProofMismatchError as exc:
        raise QualificationRefusal(
            "PROOF_MISMATCH", f"The re-proof did not hold ({exc.code}): {exc}", status_code=409
        ) from exc
    except EvaluationBoundaryError as exc:
        raise QualificationRefusal(
            "PROOF_MISMATCH", f"The re-proof could not prime the program: {exc}", status_code=409
        ) from exc
    except CatalogUnavailableError as exc:
        raise QualificationRefusal(
            "CATALOG_UNAVAILABLE",
            "The data lake's catalog could not be reached; retry once it is back.",
            status_code=503,
        ) from exc
    try:
        await with_connection(
            _append_reproof,
            program_key=row.program_key,
            symbol=row.symbol,
            qualification_id=qualification_id,
            kind="reproved",
            actor=actor,
            command_id=command_id,
            created_at_ms=reproved.created_at_ms,
            artifact_digest=reproved.artifact_digest,
            wiring_digest=reproved.wiring_digest,
            proof=reproved.as_dict(),
        )
    except qualifications.QualificationEventConflictError as exc:
        # A concurrent request with this key appended its own re-proof first:
        # that is this command's outcome, not a conflict.
        raced = await with_connection(qualifications.get_event_by_command, command_id)
        if raced is None or raced.qualification_id != qualification_id or raced.kind != "reproved":
            raise QualificationRefusal(
                "IDEMPOTENCY_CONFLICT", "That idempotency key already recorded a different command.", status_code=409
            ) from exc
        return await _require_judged(qualification_id)
    logger.info(
        "Golden Search qualification re-proved",
        extra={"action": "golden_qualification_reproved", "qualification_id": qualification_id},
    )
    return await _require_judged(qualification_id)


__all__ = [
    "QUALIFICATION_UNJUDGEABLE",
    "JudgedDefault",
    "JudgedQualification",
    "QualificationRefusal",
    "StatusName",
    "get_judged",
    "judge",
    "judged_defaults",
    "list_judged",
    "public_parameters",
    "ready_defaults",
    "reprove_qualification",
    "revoke",
    "running_artifact_digest_for",
    "status_explanation",
]
