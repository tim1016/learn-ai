"""WatchdogHaltExecutor — typed 5-step halt with per-step timeouts.

Orchestrates the PRD §7 5-step shutdown sequence on lease loss, delegating
each step to a ``WatchdogShutdownController``.  Every step fails closed and
continues — a failing ``flatten_now`` does NOT skip ``disconnect_broker`` or
``request_engine_exit``.  Exceptions never propagate out of ``execute``.

Constants (pinned per plan):
    LEASE_LOSS_GRACE_MS    = 5_000   (handled by SUSPECTED_LOSS in child_watchdog)
    FLATTEN_TIMEOUT_MS     = 20_000
    DISCONNECT_TIMEOUT_MS  = 3_000

Terminal outcome map (notice code → tier):
    flatten completed + disconnect completed → watchdog.flatten_completed (info)
    flatten not_needed (no open positions)  → watchdog.flatten_not_needed  (info)
    flatten timed_out                       → watchdog.flatten_timed_out   (critical)
    flatten failed (exception)              → watchdog.flatten_failed       (critical)
    broker disconnected before flatten ran  → watchdog.broker_disconnected_before_flatten (critical)

Incident persistence design (Finding 1 / Finding 2):
  - Incident writes are best-effort.  If the artifacts directory is
    unwritable, the halt sequence STILL runs — we log CRITICAL with the
    full incident payload so the operator can recover from logs.
  - Only SAFE terminal outcomes (flatten_completed, flatten_not_needed) are
    auto-resolved by the executor.  Critical outcomes remain unresolved so
    ``check_post_halt_gate`` blocks restart until the reconciliation
    orchestrator explicitly clears them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from app.operator.incidents.store import IncidentStore
from app.operator.incidents.watchdog_notices import (
    broker_disconnected_before_flatten_notice,
    flatten_completed_notice,
    flatten_failed_notice,
    flatten_not_needed_notice,
    flatten_timed_out_notice,
    watchdog_incident,
)
from app.operator.notices.schema import OperatorIncident

logger = logging.getLogger(__name__)

# Pinned constants (plan §3 §22).
FLATTEN_TIMEOUT_MS: int = 20_000
DISCONNECT_TIMEOUT_MS: int = 3_000

# Notice codes that indicate the broker is in a clean state.  Only these are
# auto-resolved by the executor.  All other (critical) terminal codes stay
# unresolved so the post-halt gate blocks restart until reconciliation runs.
_SAFE_OUTCOME_CODES: frozenset[str] = frozenset(
    {
        "watchdog.flatten_completed",
        "watchdog.flatten_not_needed",
    }
)

LeaseLossReason = Literal["LEASE_EXPIRED", "BOOT_ID_CHANGED"]

FlattenOutcome = Literal[
    "completed",
    "not_needed",
    "timed_out",
    "failed",
    "broker_disconnected_before_flatten",
]
BrokerDisconnectOutcome = Literal["completed", "timed_out", "failed"]


class WatchdogShutdownController(Protocol):
    """Interface for the 5 shutdown primitives the engine exposes to the executor."""

    async def block_submissions(self) -> None: ...

    async def persist_paused(self, reason: LeaseLossReason) -> None: ...

    async def flatten_now(self, reason: LeaseLossReason) -> FlattenOutcome: ...

    async def disconnect_broker(self) -> BrokerDisconnectOutcome: ...

    async def request_engine_exit(self) -> None: ...


@dataclass(frozen=True)
class WatchdogTimeouts:
    """Per-step timeout budget in milliseconds."""

    flatten_timeout_ms: int = FLATTEN_TIMEOUT_MS
    disconnect_timeout_ms: int = DISCONNECT_TIMEOUT_MS


@dataclass
class _StepEvidence:
    """Evidence accumulated across the 5 steps."""

    block_submissions_ok: bool = False
    persist_paused_ok: bool = False
    flatten_outcome: str | None = None
    flatten_ms: int | None = None
    flatten_error: str | None = None
    disconnect_outcome: str | None = None
    disconnect_ms: int | None = None
    disconnect_error: str | None = None
    per_step_errors: list[str] = field(default_factory=list)


def _watchdog_gate_status(flatten_outcome: str | None) -> str:
    if flatten_outcome in {"completed", "not_needed"}:
        return "pass"
    if flatten_outcome in {"failed", "timed_out", "broker_disconnected_before_flatten"}:
        return "freeze"
    return "unknown"


def _watchdog_gate_next_step(flatten_outcome: str | None) -> str:
    if flatten_outcome in {"completed", "not_needed"}:
        return "GATE_PASSING"
    return "CHECK_IBKR"


def _incident_evidence(
    reason: LeaseLossReason,
    ev: _StepEvidence,
    *,
    terminal_notice_code: str,
    evidence_at_ms: int,
) -> dict[str, object]:
    return {
        "reason": reason,
        "block_submissions_ok": ev.block_submissions_ok,
        "persist_paused_ok": ev.persist_paused_ok,
        "flatten_outcome": ev.flatten_outcome,
        "flatten_ms": ev.flatten_ms,
        "flatten_error": ev.flatten_error,
        "disconnect_outcome": ev.disconnect_outcome,
        "disconnect_ms": ev.disconnect_ms,
        "disconnect_error": ev.disconnect_error,
        "per_step_errors": ev.per_step_errors,
        "gate_results": [
            {
                "gate_id": "watchdog.lease_loss",
                "status": _watchdog_gate_status(ev.flatten_outcome),
                "source": "watchdog_halt_executor",
                "operator_reason": terminal_notice_code,
                "operator_next_step": _watchdog_gate_next_step(ev.flatten_outcome),
                "evidence_at_ms": evidence_at_ms,
            }
        ],
    }


class WatchdogHaltExecutor:
    """Runs the 5-step halt sequence and writes a typed ``OperatorIncident``.

    Args:
        controller:     The engine's shutdown primitives.
        incident_store: Per-run store for durable incident records.
        timeouts:       Step timeout budgets.
        clock_ms:       Injectable clock (default: ``time.time_ns() // 1_000_000``).
        log:            Logger to use (default: module logger).
    """

    def __init__(
        self,
        controller: WatchdogShutdownController,
        incident_store: IncidentStore,
        *,
        timeouts: WatchdogTimeouts | None = None,
        clock_ms: Callable[[], int] | None = None,
        log: logging.Logger | None = None,
        artifacts_root: Path | None = None,
        account_id: str | None = None,
    ) -> None:
        self._controller = controller
        self._store = incident_store
        self._timeouts = timeouts or WatchdogTimeouts()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._log = log or logger
        self._artifacts_root = artifacts_root
        self._account_id = account_id

    async def execute(self, reason: LeaseLossReason) -> OperatorIncident:
        """Run the 5 steps in order; persist a typed incident.

        Returns the final ``OperatorIncident`` (resolved or unresolved depending
        on the terminal outcome).  Never raises — every step exception is caught,
        logged, and recorded in the incident evidence.

        Incident persistence is best-effort (Finding 1): if the artifacts
        directory is unwritable the halt sequence STILL runs.  Failures are
        logged at CRITICAL with the full incident payload.

        Auto-resolve policy (Finding 2): only safe outcomes
        (flatten_completed, flatten_not_needed) are resolved by this executor.
        Critical outcomes stay unresolved so the post-halt gate blocks restart
        until the reconciliation orchestrator clears them.
        """
        started_at_ms = self._clock_ms()
        ev = _StepEvidence()

        # Build + persist the initial incident *before* executing steps so a
        # crash mid-sequence still leaves a durable (pessimistic) record.
        # Best-effort: a write failure must NOT abort the halt sequence.
        initial = watchdog_incident(reason=reason, started_at_ms=started_at_ms)
        try:
            self._store.append(initial)
        except Exception as exc:
            self._log.critical(
                "[WATCHDOG] failed to persist initial halt incident; continuing halt sequence",
                extra={
                    "incident_id": initial.incident_id,
                    "incident_payload": initial.model_dump_json(),
                    "exception": repr(exc),
                },
            )

        # Step 1 — block submissions IMMEDIATELY (no timeout; synchronous intent).
        try:
            await self._controller.block_submissions()
            ev.block_submissions_ok = True
            self._log.info("[WATCHDOG] step 1 block_submissions completed")
        except Exception as exc:
            msg = f"step1 block_submissions: {exc!r}"
            ev.per_step_errors.append(msg)
            self._log.critical("[WATCHDOG] %s", msg, exc_info=True)

        # Step 2 — durable PAUSED (no timeout; side effect must commit before step 3).
        try:
            await self._controller.persist_paused(reason)
            ev.persist_paused_ok = True
            self._log.info("[WATCHDOG] step 2 persist_paused completed")
        except Exception as exc:
            msg = f"step2 persist_paused: {exc!r}"
            ev.per_step_errors.append(msg)
            self._log.critical("[WATCHDOG] %s", msg, exc_info=True)

        # Step 3 — flatten, with timeout.
        flatten_outcome = await self._run_flatten(reason, ev)

        # Persist flatten proof / unresolved exposure evidence BEFORE broker
        # disconnect. If the process dies during broker teardown, the operator
        # must still see the terminal flatten outcome, not only the pessimistic
        # in-progress scaffold.
        flatten_recorded_at_ms = self._clock_ms()
        terminal_notice = _select_terminal_notice(
            flatten_outcome=flatten_outcome,
            flatten_ms=ev.flatten_ms,
            flatten_timeout_ms=self._timeouts.flatten_timeout_ms,
            flatten_error=ev.flatten_error,
            occurred_at_ms=flatten_recorded_at_ms,
        )
        is_safe = terminal_notice.code in _SAFE_OUTCOME_CODES
        pre_disconnect_incident = initial.model_copy(
            update={
                "notice": terminal_notice,
                "evidence": _incident_evidence(
                    reason,
                    ev,
                    terminal_notice_code=terminal_notice.code,
                    evidence_at_ms=flatten_recorded_at_ms,
                ),
                "resolved_at_ms": flatten_recorded_at_ms if is_safe else None,
            }
        )
        try:
            self._store.append(pre_disconnect_incident)
        except Exception as exc:
            self._log.critical(
                "[WATCHDOG] failed to persist pre-disconnect flatten evidence",
                extra={
                    "incident_id": pre_disconnect_incident.incident_id,
                    "incident_payload": pre_disconnect_incident.model_dump_json(),
                    "exception": repr(exc),
                },
            )
        if not is_safe:
            self._write_account_freeze_for_unsafe_outcome(
                reason=terminal_notice.code,
                recorded_at_ms=flatten_recorded_at_ms,
            )

        # Step 4 — disconnect broker, with timeout.
        await self._run_disconnect(ev)

        # Step 5 — request engine exit (fire and forget).
        try:
            await self._controller.request_engine_exit()
            self._log.info("[WATCHDOG] step 5 request_engine_exit completed")
        except Exception as exc:
            msg = f"step5 request_engine_exit: {exc!r}"
            ev.per_step_errors.append(msg)
            self._log.critical("[WATCHDOG] %s", msg, exc_info=True)

        completed_at_ms = self._clock_ms()

        # Decide whether this is a safe outcome that can be auto-resolved.
        resolved_at_ms = completed_at_ms if is_safe else None

        # Amend the incident with the terminal notice + evidence.
        final_incident = initial.model_copy(
            update={
                "notice": terminal_notice,
                "evidence": _incident_evidence(
                    reason,
                    ev,
                    terminal_notice_code=terminal_notice.code,
                    evidence_at_ms=completed_at_ms,
                ),
                "resolved_at_ms": resolved_at_ms,
            }
        )
        # Best-effort: persist the terminal incident.  A write failure is logged
        # at CRITICAL but does not prevent returning the in-memory incident.
        try:
            self._store.append(final_incident)
        except Exception as exc:
            self._log.critical(
                "[WATCHDOG] failed to persist terminal halt incident",
                extra={
                    "incident_id": final_incident.incident_id,
                    "incident_payload": final_incident.model_dump_json(),
                    "exception": repr(exc),
                },
            )

        # Only auto-resolve safe outcomes.  Critical outcomes stay unresolved
        # so the post-halt gate blocks restart until reconciliation clears them.
        if is_safe:
            try:
                self._store.resolve(initial.incident_id, resolved_at_ms=completed_at_ms)
            except Exception as exc:
                self._log.critical(
                    "[WATCHDOG] failed to resolve safe incident",
                    extra={"incident_id": initial.incident_id, "exception": repr(exc)},
                )

        self._log.info(
            "[WATCHDOG] halt complete notice=%s tier=%s resolved=%s",
            terminal_notice.code,
            terminal_notice.tier,
            is_safe,
        )
        return final_incident

    def _write_account_freeze_for_unsafe_outcome(self, *, reason: str, recorded_at_ms: int) -> None:
        if self._artifacts_root is None or not self._account_id:
            return
        try:
            from app.engine.live.account_artifacts import AccountFreezeEvidence, write_account_freeze

            write_account_freeze(
                self._artifacts_root,
                AccountFreezeEvidence(
                    account_id=self._account_id,
                    freeze_kind="exposure",
                    reason=reason,
                    source="watchdog_halt_executor",
                    recorded_at_ms=recorded_at_ms,
                    operator_next_step="CHECK_IBKR",
                ),
            )
        except Exception as exc:
            self._log.critical(
                "[WATCHDOG] failed to persist account freeze for unsafe halt outcome",
                extra={
                    "account_id": self._account_id,
                    "reason": reason,
                    "exception": repr(exc),
                },
            )

    # ------------------------------------------------------------------
    # Step helpers
    # ------------------------------------------------------------------

    async def _run_flatten(self, reason: LeaseLossReason, ev: _StepEvidence) -> FlattenOutcome:
        """Step 3 — flatten with timeout.  Sets ev.flatten_outcome/ms/error.

        Returns the FlattenOutcome string so the caller can build the terminal notice.
        """
        t0 = self._clock_ms()
        timeout_s = self._timeouts.flatten_timeout_ms / 1000.0
        try:
            outcome: FlattenOutcome = await asyncio.wait_for(
                self._controller.flatten_now(reason), timeout=timeout_s
            )
            ev.flatten_outcome = outcome
            ev.flatten_ms = self._clock_ms() - t0
            self._log.info("[WATCHDOG] step 3 flatten_now outcome=%s ms=%s", outcome, ev.flatten_ms)
            return outcome
        except TimeoutError:
            ev.flatten_outcome = "timed_out"
            ev.flatten_ms = self._clock_ms() - t0
            self._log.critical(
                "[WATCHDOG] step 3 flatten_now timed out after %sms", self._timeouts.flatten_timeout_ms
            )
            return "timed_out"
        except Exception as exc:
            ev.flatten_outcome = "failed"
            ev.flatten_ms = self._clock_ms() - t0
            ev.flatten_error = repr(exc)
            msg = f"step3 flatten_now: {exc!r}"
            ev.per_step_errors.append(msg)
            self._log.critical("[WATCHDOG] %s", msg, exc_info=True)
            return "failed"

    async def _run_disconnect(self, ev: _StepEvidence) -> None:
        """Step 4 — broker disconnect with timeout.  Sets ev.disconnect_outcome/ms/error."""
        t0 = self._clock_ms()
        timeout_s = self._timeouts.disconnect_timeout_ms / 1000.0
        try:
            outcome: BrokerDisconnectOutcome = await asyncio.wait_for(
                self._controller.disconnect_broker(), timeout=timeout_s
            )
            ev.disconnect_outcome = outcome
            ev.disconnect_ms = self._clock_ms() - t0
            self._log.info(
                "[WATCHDOG] step 4 disconnect_broker outcome=%s ms=%s", outcome, ev.disconnect_ms
            )
        except TimeoutError:
            ev.disconnect_outcome = "timed_out"
            ev.disconnect_ms = self._clock_ms() - t0
            self._log.critical(
                "[WATCHDOG] step 4 disconnect_broker timed out after %sms",
                self._timeouts.disconnect_timeout_ms,
            )
        except Exception as exc:
            ev.disconnect_outcome = "failed"
            ev.disconnect_ms = self._clock_ms() - t0
            ev.disconnect_error = repr(exc)
            msg = f"step4 disconnect_broker: {exc!r}"
            ev.per_step_errors.append(msg)
            self._log.critical("[WATCHDOG] %s", msg, exc_info=True)


# ---------------------------------------------------------------------------
# Terminal notice selector
# ---------------------------------------------------------------------------


def _select_terminal_notice(
    *,
    flatten_outcome: FlattenOutcome,
    flatten_ms: int | None,
    flatten_timeout_ms: int,
    flatten_error: str | None,
    occurred_at_ms: int,
) -> object:
    """Map flatten outcome to the correct terminal notice."""
    if flatten_outcome == "not_needed":
        return flatten_not_needed_notice(occurred_at_ms=occurred_at_ms)
    if flatten_outcome == "completed":
        return flatten_completed_notice(flatten_ms=flatten_ms, occurred_at_ms=occurred_at_ms)
    if flatten_outcome == "timed_out":
        return flatten_timed_out_notice(
            flatten_timeout_ms=flatten_timeout_ms, occurred_at_ms=occurred_at_ms
        )
    if flatten_outcome == "broker_disconnected_before_flatten":
        return broker_disconnected_before_flatten_notice(occurred_at_ms=occurred_at_ms)
    # "failed" (and any unexpected value)
    return flatten_failed_notice(error_summary=flatten_error, occurred_at_ms=occurred_at_ms)


__all__ = [
    "BrokerDisconnectOutcome",
    "FlattenOutcome",
    "LeaseLossReason",
    "WatchdogHaltExecutor",
    "WatchdogShutdownController",
    "WatchdogTimeouts",
]
