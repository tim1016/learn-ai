"""The one write path for backtest runs, and the parity bookkeeping around it (PRD #1929).

Every producer — the engine backtest, the LEAN sidecar, the spec-strategy
runner, the LEAN backfill script — hands its persist payload to
:func:`persist_run_payload_sync` (from a worker thread) or
:func:`persist_run_payload` (from a coroutine). Persistence is best-effort
by design and must stay so: a failure logs, yields ``None`` for the run id,
and never fails the run that produced the payload. The failure modes moved
from HTTP errors to database errors; the semantics did not.

Writes go through the shared writer loop (``research.persistence.db``), so a
worker thread and the FastAPI loop never share a connection, and a coroutine
on any other loop reaches the same pool through a thread rather than
creating a pool of its own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from app.research.backtest_runs import repository as repo
from app.research.backtest_runs.engine_payload import build_engine_run_payload
from app.research.backtest_runs.parity import freeze_parity_for_lean_run
from app.research.backtest_runs.records import BacktestRunRecord, record_from_payload
from app.research.persistence.db import DB_CALL_TIMEOUT_SECONDS, run_sync, with_connection

if TYPE_CHECKING:
    from app.routers.engine import EngineBacktestResponse

logger = logging.getLogger(__name__)


def persist_run_payload_sync(payload: Mapping[str, Any]) -> int | None:
    """Write a canonical persist payload from a worker thread; ``None`` when persistence failed.

    ``parity_failure_detail`` on the payload (LEAN's failed-run shape) says the
    row being written is a companion that produced no comparable result, so its
    group settles at ``run_failed`` instead of being compared (#1977).
    """
    return _persist_sync(
        lambda: record_from_payload(payload),
        source=str(payload.get("source")),
        parity_failure_detail=payload.get("parity_failure_detail") or None,
    )


def persist_engine_response_sync(
    *,
    response: EngineBacktestResponse,
    symbol: str,
    start_date: str,
    end_date: str,
    resolution: str,
    parameters: Mapping[str, Any],
    duration_ms: int,
    commission_per_order: float = 0.0,
    compatibility_profile: Literal["us-equity-raw-ibkr-v1"] | None = None,
    requested_engine: Literal["python", "lean", "both"] = "python",
    parity_group_id: str | None = None,
) -> int | None:
    """Shape a completed engine response into its row and write it; ``None`` when persistence failed.

    A report the pure builder refuses (no producer timestamps, a ledger that
    does not reconcile) is a persistence failure like any other: it logs and
    leaves the run id null, and the completed backtest is unaffected.
    """
    return _persist_sync(
        lambda: record_from_payload(
            build_engine_run_payload(
                response=response,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                resolution=resolution,
                parameters=parameters,
                duration_ms=duration_ms,
                commission_per_order=commission_per_order,
                compatibility_profile=compatibility_profile,
                requested_engine=requested_engine,
                parity_group_id=parity_group_id,
            )
        ),
        source="engine",
    )


def _persist_sync(
    make_record: Callable[[], BacktestRunRecord], *, source: str, parity_failure_detail: str | None = None
) -> int | None:
    """The one best-effort write. A LEAN companion also settles its parity group afterwards.

    A payload the converter refuses and a write that fails mean the same thing
    to the caller — the run was not persisted — but a caller that stops waiting
    means something else again, so the timeout is reported apart from both.
    """
    try:
        record = make_record()
    except Exception:
        logger.exception("[RUNS] Payload rejected, run not persisted (source=%s)", source)
        return None
    try:
        outcome = run_sync(_insert_and_settle(record, parity_failure_detail))
    except TimeoutError:
        # ``run_sync`` does not cancel on timeout: the insert and the settle
        # chained behind it run to their own conclusion on the writer loop.
        # The caller learns nothing, which is "outcome unknown" — not "not
        # persisted" — and any parity mark it makes from that is provisional
        # until the row lands and supersedes it (#1977).
        logger.warning(
            "[RUNS] Run persistence outcome unknown after %.0fs (source=%s); the write continues on the writer loop",
            DB_CALL_TIMEOUT_SECONDS,
            source,
        )
        return None
    except Exception:
        logger.exception("[RUNS] Run not persisted (source=%s)", source)
        return None
    return outcome.run_id


async def persist_run_payload(payload: Mapping[str, Any]) -> int | None:
    """The coroutine form of :func:`persist_run_payload_sync`, safe on any event loop."""
    return await asyncio.to_thread(persist_run_payload_sync, payload)


async def _insert_and_settle(record: BacktestRunRecord, parity_failure_detail: str | None) -> repo.InsertOutcome:
    """Write the row, then settle its parity group — both on the writer loop.

    Chaining the settle here rather than making it a second hop from the
    calling thread is what makes it survive a caller that has stopped waiting:
    the coroutine is not cancelled on timeout, so a group whose companion
    landed still reaches its terminal state (#1977).
    """
    outcome = await with_connection(repo.insert_run, record)
    logger.info("[RUNS] Run persisted (id=%s, source=%s, created=%s)", outcome.run_id, record.source, outcome.created)
    if record.source == "lean-sidecar" and record.parity_group_id:
        await _settle_parity(
            parity_group_id=record.parity_group_id,
            right_run_id=outcome.run_id,
            failure_detail=parity_failure_detail,
        )
    return outcome


async def _settle_parity(*, parity_group_id: str, right_run_id: int, failure_detail: str | None) -> None:
    """Bring the group to the terminal state the landed companion row justifies.

    A companion that produced no comparable result settles the group at
    ``run_failed`` — comparing its zero-trade row against a real Python run
    would report a divergence that did not happen. Best-effort on its own, so
    the run row persists even when the settle fails.
    """
    try:
        if failure_detail is not None:
            row, transitioned = await with_connection(
                repo.mark_parity_failed, parity_group_id, status="run_failed", detail=failure_detail
            )
            _log_parity_failure_mark(
                parity_group_id, row=row, transitioned=transitioned, status="run_failed", detail=failure_detail
            )
            return
        await with_connection(freeze_parity_for_lean_run, right_run_id=right_run_id, parity_group_id=parity_group_id)
    except Exception:
        logger.exception(
            "[PARITY] Verdict computation failed for group %s (right=%s); verdict left as it stands",
            parity_group_id,
            right_run_id,
        )


def record_parity_disposition_sync(*, parity_group_id: str, left_run_id: int, status: str, verdict_json: str) -> None:
    """Record the run-time parity disposition (``pending`` or ``unavailable``); never raises."""
    try:
        run_sync(
            with_connection(
                repo.create_parity_verdict,
                parity_group_id=parity_group_id,
                left_run_id=left_run_id,
                status=status,
                verdict_json=verdict_json,
            )
        )
    except Exception:
        logger.exception("[PARITY] verdict-row create failed for %s", parity_group_id)


def mark_parity_failed_sync(parity_group_id: str, *, status: str, detail: str) -> None:
    """``pending -> run_failed | persist_failed``; a frozen verdict is never overwritten. Never raises."""
    try:
        row, transitioned = run_sync(
            with_connection(repo.mark_parity_failed, parity_group_id, status=status, detail=detail)
        )
    except Exception:
        logger.exception("[PARITY] mark-failed write failed for %s", parity_group_id)
        return
    _log_parity_failure_mark(parity_group_id, row=row, transitioned=transitioned, status=status, detail=detail)


def _log_parity_failure_mark(
    parity_group_id: str, *, row: repo.ParityVerdictRow | None, transitioned: bool, status: str, detail: str
) -> None:
    if row is None:
        logger.warning("[PARITY] mark-failed for unknown group %s", parity_group_id)
    elif transitioned:
        logger.info("[PARITY] Group %s marked %s: %s", parity_group_id, status, detail)
    else:
        logger.info("[PARITY] Group %s already %s; not overwriting", parity_group_id, row.status)
