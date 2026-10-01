"""Approve a Golden Search study: prove it, record its run, publish its qualified version (#2696, ADR 0074).

Approval is the one step that turns research into something Deploy can
offer. It runs on the study's job worker thread, in four steps, each
resumable from the checkpoint the caller persists between them:

0. If the study already published a qualification, answer with it: a retry
   after a lost response publishes nothing twice.
1. Name the running build. A process whose code on disk differs from the
   code it imported cannot name what it runs, and refuses (RESTART_NEEDED).
2. Build the proof over the study's final interval — staged inputs, a lake
   replay and a replay restored from the blobs alone, one trace root — and
   checkpoint it. A checkpointed proof that names another build, or lost a
   staged input, is rebuilt under the running build rather than reused.
3. Persist one Python-only full backtest of the candidate over the same
   interval, bound to the study's data receipt, as the Golden Validation
   source run; checkpoint its id.
4. In ONE database transaction: designate that run as a Golden Validation
   case, accept it (a Python-only case is a Manual override, with the
   owner's note), insert the qualified version, move the (program, stock)
   default by compare-and-set from the version the owner reviewed against,
   and let the caller mark the study approved — all or nothing.

Every failure before that commit publishes nothing and leaves the prior
default where it was. A research override never reaches this module: proof
mismatch, missing data, a changed program, a moved default or a refused
review are technical failures, each with its own code. ``cancel_check`` runs
before each step that does work and again before the publish, so a cancel
requested while a step ran publishes nothing; cancellation (``JobCancelled``)
propagates for the caller's lifecycle to record.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import asyncpg
from fastapi import HTTPException
from pydantic import ValidationError

from app.data_lake.catalog_client import CatalogUnavailableError
from app.engine.engine import EvaluationBoundaryError
from app.engine.strategy.registry import _STRATEGY_REGISTRY, SignalProgramContract, StrategyRegistration
from app.jobs.progress import JobCancelled
from app.research.golden_search import qualifications
from app.research.golden_search.proof import (
    BlobStore,
    ProofMismatchError,
    ProofRecord,
    ProofWindow,
    build_proof,
    default_blob_store,
)
from app.research.golden_search.protocol import ExecutionAssumptions
from app.research.golden_search.zoom import BudgetExhausted
from app.research.golden_validation import service as golden_validation
from app.research.persistence.db import run_sync, with_connection
from app.research.sweep.snapshot import DataSnapshot
from app.schemas.engine_backtest import EngineBacktestRequest
from app.services.engine_backtest_service import execute_engine_backtest
from app.services.signal_program_admission import RestartNeededError, running_build_digests
from app.utils.background_loop import CallerStoppedWaitingError, run_on_background_loop
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: Evaluations each step draws from the study's reserved proof budget: the
#: lake replay and the restored replay, then the persisted run.
PROOF_EVALUATIONS = 2
RUN_EVALUATIONS = 1

ApprovalFailureCode = Literal[
    "APPROVAL_ERROR",
    "BUDGET_EXHAUSTED",
    "DATA_UNAVAILABLE",
    "DEFAULT_CHANGED",
    "GOLDEN_VALIDATION_REFUSED",
    "NOT_QUALIFIABLE",
    "PARAMETERS_CHANGED",
    "PROOF_MISMATCH",
    "PUBLISH_REFUSED",
    "REQUEST_INVALID",
    "RESTART_NEEDED",
    "RUN_NOT_SAVED",
    "STORE_UNAVAILABLE",
    "WARMUP_TOO_SHORT",
]

_UNCHANGED = " Nothing was published and the current default is unchanged."
_STORE_ERRORS = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    CatalogUnavailableError,
    OSError,
    TimeoutError,
    CallerStoppedWaitingError,
)


@dataclass(frozen=True)
class ApprovalRequest:
    """Everything approval needs, frozen from the study when the owner approved it."""

    study_id: str
    strategy_key: str
    symbol: str  # upper-case
    candidate_point: dict[str, Any]  # canonical point, "symbol" included (declarations.canonical_point)
    proof_window: ProofWindow  # the final interval [start, end) primed from the study's run-up
    snapshot: DataSnapshot  # the study's data receipt
    roots: tuple[Path, ...]
    execution: ExecutionAssumptions
    research: dict[str, Any]  # stored verbatim as the qualification's research_json
    note: str
    expected_default_qualification_id: str | None
    actor: str = "owner"


@dataclass(frozen=True)
class ApprovalCheckpoint:
    """What a resumed approval reuses rather than redoes; the caller persists it between steps.

    ``proof_reserved`` / ``run_reserved`` record that a step's reserved
    evaluations were already drawn, so a worker that dies mid-step resumes
    without drawing them twice.
    """

    run_id: int | None = None
    proof: dict[str, Any] | None = None
    proof_reserved: bool = False
    run_reserved: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "proof": None if self.proof is None else dict(self.proof),
            "proof_reserved": self.proof_reserved,
            "run_reserved": self.run_reserved,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> ApprovalCheckpoint:
        if not payload:
            return cls()
        run_id = payload.get("run_id")
        proof = payload.get("proof")
        if run_id is not None and (isinstance(run_id, bool) or not isinstance(run_id, int)):
            raise ValueError("an approval checkpoint's run_id is an integer")
        if proof is not None and not isinstance(proof, Mapping):
            raise ValueError("an approval checkpoint's proof is an object")
        return cls(
            run_id=run_id,
            proof=None if proof is None else dict(proof),
            proof_reserved=payload.get("proof_reserved") is True,
            run_reserved=payload.get("run_reserved") is True,
        )


@dataclass(frozen=True)
class ApprovalOutcome:
    status: Literal["approved", "failed"]
    qualification_id: str | None
    failure_code: str | None
    failure_reason: str | None  # owner-facing sentence


class _ApprovalFailure(Exception):
    def __init__(self, code: ApprovalFailureCode, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def qualification_id_for(study_id: str) -> str:
    """The id a study's qualified version gets: derived from the study, so a retried publish names the same one."""
    return "gq-" + hashlib.sha256(f"golden-search:{study_id}:qualification".encode()).hexdigest()[:32]


def approve_study(
    request: ApprovalRequest,
    *,
    checkpoint: ApprovalCheckpoint,
    save_checkpoint: Callable[[ApprovalCheckpoint], None],
    consume_reserved: Callable[[int], None],
    on_commit: Callable[[asyncpg.Connection, str], Awaitable[None]],
    cancel_check: Callable[[], object] = lambda: None,
    blob_store: BlobStore | None = None,
) -> ApprovalOutcome:
    """Prove, record and publish one study's candidate; synchronous, for the job worker thread.

    ``consume_reserved(n)`` draws from the study's proof reservation,
    ``save_checkpoint`` persists progress between steps, and ``on_commit``
    is the caller's own fenced study update, awaited inside the publish
    transaction so the study reads approved exactly when the version is
    published.
    """
    try:
        qualification_id = _approve(
            request,
            checkpoint=checkpoint,
            save_checkpoint=save_checkpoint,
            consume_reserved=consume_reserved,
            on_commit=on_commit,
            cancel_check=cancel_check,
            blob_store=blob_store or default_blob_store(),
        )
    except JobCancelled:
        raise
    except _ApprovalFailure as failure:
        logger.warning(
            "Golden Search approval failed; nothing was published",
            extra={"action": "golden_approval_failed", "study_id": request.study_id, "failure_code": failure.code},
        )
        return ApprovalOutcome(
            status="failed", qualification_id=None, failure_code=failure.code, failure_reason=failure.reason
        )
    except Exception:
        # The contract is that nothing escapes as a crash: every failure
        # before the commit is a failed approval the owner can retry.
        logger.exception(
            "Golden Search approval stopped on an unexpected error; nothing was published",
            extra={"action": "golden_approval_error", "study_id": request.study_id},
        )
        return ApprovalOutcome(
            status="failed",
            qualification_id=None,
            failure_code="APPROVAL_ERROR",
            failure_reason="Approval stopped on an unexpected error; the service log has the cause." + _UNCHANGED,
        )
    logger.info(
        "Golden Search study approved",
        extra={
            "action": "golden_approval_published",
            "study_id": request.study_id,
            "qualification_id": qualification_id,
        },
    )
    return ApprovalOutcome(status="approved", qualification_id=qualification_id, failure_code=None, failure_reason=None)


def _approve(
    request: ApprovalRequest,
    *,
    checkpoint: ApprovalCheckpoint,
    save_checkpoint: Callable[[ApprovalCheckpoint], None],
    consume_reserved: Callable[[int], None],
    on_commit: Callable[[asyncpg.Connection, str], Awaitable[None]],
    cancel_check: Callable[[], object],
    blob_store: BlobStore,
) -> str:
    published = _store_call(qualifications.get_qualification_by_study, request.study_id)
    if published is not None:
        return published.id
    registration, contract = _signal_program(request.strategy_key)
    canonical = _canonical_candidate(registration, request)
    try:
        artifact_digest, wiring_digest = running_build_digests(contract)
    except RestartNeededError as exc:
        raise _ApprovalFailure("RESTART_NEEDED", str(exc) + _UNCHANGED) from exc
    except (OSError, ValueError) as exc:
        raise _ApprovalFailure(
            "RESTART_NEEDED",
            f"The program's source files could not be read to name the running build ({type(exc).__name__}). "
            "Restart the service, then retry the approval." + _UNCHANGED,
        ) from exc

    proof = (
        None
        if checkpoint.proof is None
        else _reusable_proof(
            checkpoint.proof, request, canonical, contract, artifact_digest, wiring_digest, blob_store
        )
    )
    if proof is None:
        # A cancel requested while an earlier step ran must stop here, before
        # more work and long before anything is published.
        cancel_check()
        if not checkpoint.proof_reserved:
            _consume(consume_reserved, PROOF_EVALUATIONS)
            checkpoint = dataclasses.replace(checkpoint, proof_reserved=True)
            save_checkpoint(checkpoint)
        proof = _build_proof(request, canonical, blob_store, artifact_digest, wiring_digest)
        checkpoint = dataclasses.replace(checkpoint, proof=proof.as_dict())
        save_checkpoint(checkpoint)
        _require_blobs(proof, blob_store)

    run_id = checkpoint.run_id
    if run_id is None:
        cancel_check()
        if not checkpoint.run_reserved:
            _consume(consume_reserved, RUN_EVALUATIONS)
            checkpoint = dataclasses.replace(checkpoint, run_reserved=True)
            save_checkpoint(checkpoint)
        run_id = _persist_full_run(request, canonical, cancel_check=cancel_check)
        checkpoint = dataclasses.replace(checkpoint, run_id=run_id)
        save_checkpoint(checkpoint)

    cancel_check()
    try:
        return _store_call(
            _publish,
            request=request,
            canonical=canonical,
            contract=contract,
            proof=proof,
            run_id=run_id,
            on_commit=on_commit,
            wait_for_outcome=True,
        )
    except _ApprovalFailure as failure:
        if failure.code != "STORE_UNAVAILABLE":
            raise
        return _published_despite(request.study_id, failure)


def _published_despite(study_id: str, failure: _ApprovalFailure) -> str:
    """Look before reporting a store failure around the publish: its commit may have landed.

    A command timeout or a dropped connection at COMMIT leaves the outcome
    unknown, and "nothing was published" would then be a false claim about
    the default Deploy offers. The publish is all or nothing, so the study's
    qualification existing means every part of it committed.
    """
    try:
        published = _store_call(qualifications.get_qualification_by_study, study_id)
    except _ApprovalFailure as unreadable:
        raise _ApprovalFailure(
            "STORE_UNAVAILABLE",
            f"The research store failed while publishing ({type(failure.__cause__).__name__}), so whether the "
            "version was published is not known. Retry the approval: a retry answers with the version if it was "
            "published, and publishes it once if it was not.",
        ) from unreadable
    if published is None:
        raise failure
    logger.warning(
        "Golden Search publish reported a store failure but its commit landed",
        extra={"action": "golden_approval_publish_outcome_recovered", "study_id": study_id},
    )
    return published.id


def _store_call[T](fn: Callable[..., Awaitable[T]], /, *args: Any, wait_for_outcome: bool = False, **kwargs: Any) -> T:
    """Run ``fn`` on the research store from this worker thread.

    The publish waits for its own outcome however long it takes (each
    statement still has the pool's command timeout): a wait that gave up
    while the transaction went on to commit would report "nothing was
    published" about a version that was.
    """
    try:
        if wait_for_outcome:
            return run_on_background_loop(with_connection(fn, *args, **kwargs), timeout=None)
        return run_sync(with_connection(fn, *args, **kwargs))
    except _STORE_ERRORS as exc:
        raise _ApprovalFailure(
            "STORE_UNAVAILABLE",
            f"The research store could not be reached ({type(exc).__name__}). Retry the approval once it is back."
            + _UNCHANGED,
        ) from exc


def _signal_program(strategy_key: str) -> tuple[StrategyRegistration, SignalProgramContract]:
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    contract = registration.signal_program_contract if registration is not None else None
    if registration is None or registration.signal_program_factory is None or contract is None:
        raise _ApprovalFailure(
            "NOT_QUALIFIABLE", f"{strategy_key!r} is not a registered Signal Program, so it cannot be qualified."
        )
    return registration, contract


def _canonical_candidate(registration: StrategyRegistration, request: ApprovalRequest) -> dict[str, Any]:
    """The candidate exactly as the registered schema writes it; it must already be that form."""
    if not request.note.strip():
        raise _ApprovalFailure("REQUEST_INVALID", "Approval requires a written note." + _UNCHANGED)
    symbol = request.symbol
    if symbol != symbol.upper() or request.candidate_point.get("symbol") != symbol or request.snapshot.symbol != symbol:
        raise _ApprovalFailure(
            "REQUEST_INVALID",
            f"The candidate, the data receipt and the study must all name {symbol.upper()}." + _UNCHANGED,
        )
    try:
        canonical = registration.param_schema.model_validate({**request.candidate_point, "symbol": symbol}).model_dump(
            mode="json"
        )
    except ValidationError as exc:
        raise _ApprovalFailure(
            "PARAMETERS_CHANGED",
            f"The candidate's parameters no longer validate against the program ({exc.error_count()} error(s)); "
            "a changed program needs a new study." + _UNCHANGED,
        ) from exc
    if canonical != request.candidate_point:
        raise _ApprovalFailure(
            "PARAMETERS_CHANGED",
            "The program now writes the candidate's parameters differently; a changed program needs a new study."
            + _UNCHANGED,
        )
    return canonical


def _consume(consume_reserved: Callable[[int], None], evaluations: int) -> None:
    try:
        consume_reserved(evaluations)
    except BudgetExhausted as exc:
        raise _ApprovalFailure(
            "BUDGET_EXHAUSTED",
            "The study's reserved proof budget is used up, so the proof cannot be rebuilt." + _UNCHANGED,
        ) from exc


def _build_proof(
    request: ApprovalRequest,
    canonical: Mapping[str, Any],
    blob_store: BlobStore,
    artifact_digest: str,
    wiring_digest: str,
) -> ProofRecord:
    try:
        return build_proof(
            strategy_key=request.strategy_key,
            symbol=request.symbol,
            params=canonical,
            window=request.proof_window,
            snapshot=request.snapshot,
            roots=request.roots,
            blob_store=blob_store,
            artifact_digest=artifact_digest,
            wiring_digest=wiring_digest,
        )
    except ProofMismatchError as exc:
        raise _ApprovalFailure("PROOF_MISMATCH", f"The proof did not hold ({exc.code}): {exc}." + _UNCHANGED) from exc
    except EvaluationBoundaryError as exc:
        raise _ApprovalFailure(
            "WARMUP_TOO_SHORT",
            f"The study's run-up does not prime the program before the final interval: {exc}." + _UNCHANGED,
        ) from exc
    except CatalogUnavailableError as exc:
        raise _ApprovalFailure(
            "DATA_UNAVAILABLE",
            "The data lake's catalog could not be reached to verify the proof inputs. Retry once it is back."
            + _UNCHANGED,
        ) from exc
    except ValueError as exc:
        raise _ApprovalFailure("PROOF_MISMATCH", f"The proof inputs cannot be proven: {exc}." + _UNCHANGED) from exc


def _reusable_proof(
    payload: Mapping[str, Any],
    request: ApprovalRequest,
    canonical: Mapping[str, Any],
    contract: SignalProgramContract,
    artifact_digest: str,
    wiring_digest: str,
    blob_store: BlobStore,
) -> ProofRecord | None:
    """The proof an earlier attempt built, if it still proves this exact candidate on this build.

    ``None`` asks for a rebuild: a proof built under code that is no longer
    the running build, or whose staged inputs are no longer intact, is
    replaced by a fresh one from the receipted lake, exactly as a first
    attempt under this build would have built it. Refusing instead would
    strand the study, because a retry would meet the same checkpoint
    forever. The rebuild draws nothing new: the reservation was drawn when
    the checkpointed proof was first built.
    """
    try:
        proof = ProofRecord.from_dict(payload)
    except ValueError as exc:
        raise _ApprovalFailure("PROOF_MISMATCH", f"The saved proof is unreadable: {exc}." + _UNCHANGED) from exc
    if (
        proof.program_key != request.strategy_key
        or proof.program_version != contract.program_version
        or proof.symbol != request.symbol
        or proof.params != canonical
        or proof.window != request.proof_window
    ):
        raise _ApprovalFailure(
            "PROOF_MISMATCH", "The saved proof is for a different candidate or window than this approval." + _UNCHANGED
        )
    if (proof.artifact_digest, proof.wiring_digest) != (artifact_digest, wiring_digest):
        logger.info(
            "A saved approval proof names another build; rebuilding it under the running build",
            extra={"action": "golden_approval_proof_rebuilt", "study_id": request.study_id, "cause": "stale"},
        )
        return None
    try:
        _require_blobs(proof, blob_store)
    except _ApprovalFailure as lost:
        logger.warning(
            "A saved approval proof lost a staged input; rebuilding it from the receipted lake",
            extra={
                "action": "golden_approval_proof_rebuilt",
                "study_id": request.study_id,
                "cause": "blobs",
                "reason": lost.reason,
            },
        )
        return None
    return proof


def _require_blobs(proof: ProofRecord, blob_store: BlobStore) -> None:
    """Every staged input must still be in the blob store: a later re-proof replays from them alone."""
    try:
        for digest in proof.manifest.values():
            blob_store.get(digest)
    except ProofMismatchError as exc:
        raise _ApprovalFailure(
            "PROOF_MISMATCH", f"A staged proof input is no longer intact ({exc.code}): {exc}." + _UNCHANGED
        ) from exc


def _persist_full_run(
    request: ApprovalRequest, canonical: Mapping[str, Any], *, cancel_check: Callable[[], object]
) -> int:
    """One saved Python-only full backtest of the candidate over the proof window, bound to the data receipt."""
    window = request.proof_window
    start, end, warmup = (
        et_date_at_ms(window.start_ms),
        et_date_at_ms(window.end_ms - 1),
        et_date_at_ms(window.warmup_from_ms),
    )
    engine_request = EngineBacktestRequest(
        strategy_name=request.strategy_key,
        requested_engine="python",
        params=dict(canonical),
        from_date=start.isoformat(),
        to_date=end.isoformat(),
        warmup_from_date=warmup.isoformat() if warmup < start else None,
        fill_mode=request.execution.fill_mode,
        commission_per_order=request.execution.commission_per_order,
        slippage_per_share=request.execution.slippage_per_share,
        initial_cash=request.execution.initial_cash,
        resolution="minute",
        save_study=True,
        auto_fetch=False,
        summary_only=False,
    )
    try:
        response = execute_engine_backtest(
            request=engine_request,
            on_phase=lambda _phase: None,
            on_log=lambda _message: None,
            data_manifest=request.snapshot.artifacts,
            while_waiting=cancel_check,
        )
    except HTTPException as exc:
        raise _ApprovalFailure(
            "RUN_NOT_SAVED", f"The candidate's final-interval run was refused: {exc.detail}." + _UNCHANGED
        ) from exc
    except CatalogUnavailableError as exc:
        raise _ApprovalFailure(
            "DATA_UNAVAILABLE",
            "The data lake's catalog could not be reached for the candidate's run. Retry once it is back." + _UNCHANGED,
        ) from exc
    if not response.success:
        raise _ApprovalFailure(
            "RUN_NOT_SAVED",
            f"The candidate's final-interval run failed: {response.error or 'engine failure'}." + _UNCHANGED,
        )
    if response.save_outcome != "saved" or response.study_id is None:
        raise _ApprovalFailure(
            "RUN_NOT_SAVED",
            f"The candidate's final-interval run was not saved to history ({response.save_outcome}). "
            "Retry the approval." + _UNCHANGED,
        )
    return response.study_id


def _require_case(
    dossier: golden_validation.GoldenValidationDossier,
    request: ApprovalRequest,
    canonical: Mapping[str, Any],
    contract: SignalProgramContract,
) -> None:
    """The Golden Validation case must be exactly the tuple being qualified, or admission could not match it."""
    case = dossier.validation_case
    strategy = case.get("strategy") if isinstance(case.get("strategy"), dict) else {}
    if (
        strategy.get("name") != request.strategy_key
        or strategy.get("program_version") != contract.program_version
        or case.get("symbol") != request.symbol
        or case.get("parameters") != dict(canonical)
    ):
        raise _ApprovalFailure(
            "GOLDEN_VALIDATION_REFUSED",
            "The saved run does not record the exact program version, stock and parameters being approved."
            + _UNCHANGED,
        )


async def _publish(
    conn: asyncpg.Connection,
    *,
    request: ApprovalRequest,
    canonical: Mapping[str, Any],
    contract: SignalProgramContract,
    proof: ProofRecord,
    run_id: int,
    on_commit: Callable[[asyncpg.Connection, str], Awaitable[None]],
) -> str:
    """Golden Validation designation and review, the qualified version and the default: one transaction.

    Golden Validation's own transactions nest as savepoints, so a refusal
    at any step rolls every earlier step back with it.
    """
    qualification_id = qualification_id_for(request.study_id)
    now_ms = now_ms_utc()
    async with conn.transaction():
        published = await qualifications.get_qualification_by_study(conn, request.study_id)
        if published is not None:
            return published.id
        try:
            designated = await golden_validation.designate(
                conn,
                source_run_id=run_id,
                command_id=f"golden-search:{request.study_id}:designate",
                label=f"Golden Search study {request.study_id[:8]}",
                rationale=request.note,
                actor=request.actor,
            )
            _require_case(designated, request, canonical, contract)
            reviewed = await golden_validation.review(
                conn,
                golden_run_id=designated.golden_run.id,
                command_id=f"golden-search:{request.study_id}:review",
                expected_evidence_revision=designated.evidence.revision,
                decision="accept",
                reason=request.note,
                quantconnect_backtest_id=None,
                authorized_program_version=None,
                actor=request.actor,
            )
        except golden_validation.GoldenValidationError as exc:
            raise _ApprovalFailure(
                "GOLDEN_VALIDATION_REFUSED", f"Golden Validation refused the review: {exc}" + _UNCHANGED
            ) from exc
        review = reviewed.latest_review
        if review is None or review.decision != "accept":
            raise _ApprovalFailure(
                "GOLDEN_VALIDATION_REFUSED", "Golden Validation recorded no acceptance." + _UNCHANGED
            )
        try:
            await qualifications.insert_qualification(
                conn,
                qualification_id=qualification_id,
                program_key=request.strategy_key,
                program_version=contract.program_version,
                parameter_schema_version=contract.parameter_schema_version,
                symbol=request.symbol,
                params=canonical,
                artifact_digest=proof.artifact_digest,
                wiring_digest=proof.wiring_digest,
                study_id=request.study_id,
                golden_run_id=designated.golden_run.id,
                golden_review_id=review.id,
                proof=proof.as_dict(),
                research=request.research,
                note=request.note,
                approved_by=request.actor,
                created_at_ms=now_ms,
            )
            await qualifications.set_default_cas(
                conn,
                program_key=request.strategy_key,
                symbol=request.symbol,
                qualification_id=qualification_id,
                expected_qualification_id=request.expected_default_qualification_id,
                reason=f"Approved Golden Search study {request.study_id}",
                actor=request.actor,
                now_ms=now_ms,
            )
        except qualifications.DefaultChangedError as exc:
            raise _ApprovalFailure(
                "DEFAULT_CHANGED",
                f"The default changed since you reviewed (it is now {exc.current or 'unset'}); review again."
                + _UNCHANGED,
            ) from exc
        except ValueError as exc:
            raise _ApprovalFailure(
                "PUBLISH_REFUSED", f"The qualified version was refused: {exc}." + _UNCHANGED
            ) from exc
        await on_commit(conn, qualification_id)
    return qualification_id


__all__ = [
    "PROOF_EVALUATIONS",
    "RUN_EVALUATIONS",
    "ApprovalCheckpoint",
    "ApprovalFailureCode",
    "ApprovalOutcome",
    "ApprovalRequest",
    "approve_study",
    "qualification_id_for",
]
