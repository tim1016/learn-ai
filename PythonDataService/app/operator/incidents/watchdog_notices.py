"""Watchdog notice builders for the five terminal halt outcomes.

Each function produces an ``OperatorNotice`` (or an ``OperatorIncident``
scaffold) whose ``code``, ``tier``, ``title``, ``message``, and ``action``
are per PRD §6.3.  The cockpit renders ``title`` and ``message`` verbatim,
so the copy here is trader-facing.

Five terminal outcomes:
  - ``flatten_completed``              — info;    broker risk is zero.
  - ``flatten_not_needed``             — info;    no open positions to flatten.
  - ``flatten_timed_out``              — critical; flatten did not finish in time.
  - ``flatten_failed``                 — critical; flatten raised an exception.
  - ``broker_disconnected_before_flatten`` — critical; broker dropped before flatten ran.

The critical-tier notices set ``action.kind = "external_manual_check"`` so the
cockpit renders an "Check IBKR" affordance.  Info-tier notices set ``kind =
"none"``.

``watchdog_incident`` builds the initial ``OperatorIncident`` scaffold at
inception (before the outcome is known) with a placeholder notice.  The
executor amends the notice at terminal step.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from app.operator.notices.schema import (
    OperatorIncident,
    OperatorNotice,
    OperatorNoticeAction,
)

_RUNBOOK_SLUG = "watchdog-halt"

LeaseLossReason = str  # forwarded from child_watchdog; avoid circular import.


# ---------------------------------------------------------------------------
# Notice builders — five terminal outcomes
# ---------------------------------------------------------------------------


def flatten_completed_notice(
    *,
    flatten_ms: int | None = None,
    occurred_at_ms: int | None = None,
) -> OperatorNotice:
    """Broker is in a clean state: flatten ran and completed within the deadline."""
    return OperatorNotice(
        code="watchdog.flatten_completed",
        tier="info",
        title="Watchdog halt: positions flattened cleanly",
        message=(
            "The engine lost its daemon lease and triggered a controlled halt. "
            "All open positions were flattened before the broker session was disconnected. "
            "No manual action is required — the account is in a clean state."
        ),
        action=OperatorNoticeAction(kind="none"),
        runbook_slug=_RUNBOOK_SLUG,
        forensic_facts={"flatten_ms": flatten_ms},
        occurred_at_ms=occurred_at_ms,
    )


def flatten_not_needed_notice(
    *,
    occurred_at_ms: int | None = None,
) -> OperatorNotice:
    """Broker is in a clean state: no positions were open when the halt ran."""
    return OperatorNotice(
        code="watchdog.flatten_not_needed",
        tier="info",
        title="Watchdog halt: no positions to flatten",
        message=(
            "The engine lost its daemon lease and triggered a controlled halt. "
            "The account held no open positions, so no flatten was needed. "
            "No manual action is required — the account is in a clean state."
        ),
        action=OperatorNoticeAction(kind="none"),
        runbook_slug=_RUNBOOK_SLUG,
        occurred_at_ms=occurred_at_ms,
    )


def flatten_timed_out_notice(
    *,
    flatten_timeout_ms: int | None = None,
    occurred_at_ms: int | None = None,
) -> OperatorNotice:
    """Broker state is uncertain: the flatten call did not complete within the deadline."""
    return OperatorNotice(
        code="watchdog.flatten_timed_out",
        tier="critical",
        title="Watchdog halt: flatten timed out — check positions at IBKR",
        message=(
            "The engine lost its daemon lease and attempted a controlled halt, "
            "but the flatten operation did not complete within the allowed time "
            f"({flatten_timeout_ms} ms). "
            "The broker session was then forcefully disconnected. "
            "Open positions may still exist in your IBKR account. "
            "Verify and close any residual positions manually before restarting."
        ),
        action=OperatorNoticeAction(
            kind="external_manual_check",
            label="Check positions at IBKR",
        ),
        runbook_slug=_RUNBOOK_SLUG,
        forensic_facts={"flatten_timeout_ms": flatten_timeout_ms},
        occurred_at_ms=occurred_at_ms,
    )


def flatten_failed_notice(
    *,
    error_summary: str | None = None,
    occurred_at_ms: int | None = None,
) -> OperatorNotice:
    """Broker state is uncertain: flatten raised an exception."""
    return OperatorNotice(
        code="watchdog.flatten_failed",
        tier="critical",
        title="Watchdog halt: flatten failed — check positions at IBKR",
        message=(
            "The engine lost its daemon lease and attempted a controlled halt, "
            "but the flatten operation raised an error and did not complete. "
            "The broker session was then forcefully disconnected. "
            "Open positions may still exist in your IBKR account. "
            "Verify and close any residual positions manually before restarting."
        ),
        action=OperatorNoticeAction(
            kind="external_manual_check",
            label="Check positions at IBKR",
        ),
        runbook_slug=_RUNBOOK_SLUG,
        forensic_facts={"error_summary": error_summary},
        occurred_at_ms=occurred_at_ms,
    )


def broker_disconnected_before_flatten_notice(
    *,
    occurred_at_ms: int | None = None,
) -> OperatorNotice:
    """Broker state is uncertain: the broker disconnected before flatten could run."""
    return OperatorNotice(
        code="watchdog.broker_disconnected_before_flatten",
        tier="critical",
        title="Watchdog halt: broker disconnected before flatten — check positions at IBKR",
        message=(
            "The engine lost its daemon lease and attempted a controlled halt, "
            "but the broker connection was lost before the flatten operation could run. "
            "Open positions may still exist in your IBKR account. "
            "Verify and close any residual positions manually before restarting."
        ),
        action=OperatorNoticeAction(
            kind="external_manual_check",
            label="Check positions at IBKR",
        ),
        runbook_slug=_RUNBOOK_SLUG,
        occurred_at_ms=occurred_at_ms,
    )


def residual_positions_after_failed_flatten_incident(
    *,
    positions: Mapping[str, float],
    started_at_ms: int,
    error_summary: str | None = None,
) -> OperatorIncident:
    """Critical incident for a failed recovery flatten with known residuals."""
    non_zero = {symbol: qty for symbol, qty in positions.items() if qty != 0}
    parts = ", ".join(f"{symbol} {qty:+g}" for symbol, qty in sorted(non_zero.items()))
    incident_id = f"watchdog-residual-{started_at_ms}-{uuid.uuid4().hex[:8]}"
    notice = OperatorNotice(
        code="watchdog.flatten_failed",
        tier="critical",
        title="Recovery flatten failed: residual position remains",
        message=(
            "The bot exited after losing its broker/runtime path, recovery flatten failed, "
            f"and IBKR still reports residual position(s): {parts}. "
            "Verify and close these positions manually before restarting."
        ),
        action=OperatorNoticeAction(
            kind="external_manual_check",
            label="Check positions at IBKR",
            target="ibkr_positions",
        ),
        runbook_slug=_RUNBOOK_SLUG,
        forensic_facts={
            "error_summary": error_summary,
            "residual_positions": parts,
            "residual_count": len(non_zero),
        },
        occurred_at_ms=started_at_ms,
    )
    return OperatorIncident(
        incident_id=incident_id,
        category="watchdog",
        notice=notice,
        started_at_ms=started_at_ms,
        evidence={
            "residual_positions": dict(non_zero),
            "error_summary": error_summary,
        },
    )


def recovery_flatten_uncertain_incident(
    *,
    started_at_ms: int,
    error_summary: str | None = None,
) -> OperatorIncident:
    """Critical incident when recovery flatten failed and residuals are unknown."""
    incident_id = f"watchdog-recovery-uncertain-{started_at_ms}-{uuid.uuid4().hex[:8]}"
    notice = flatten_failed_notice(
        error_summary=error_summary,
        occurred_at_ms=started_at_ms,
    )
    return OperatorIncident(
        incident_id=incident_id,
        category="watchdog",
        notice=notice,
        started_at_ms=started_at_ms,
        evidence={
            "residual_positions": None,
            "error_summary": error_summary,
            "positions_fetch_failed": True,
        },
    )


# ---------------------------------------------------------------------------
# Incident scaffold builder
# ---------------------------------------------------------------------------


def watchdog_incident(
    *,
    reason: LeaseLossReason,
    started_at_ms: int,
    evidence: dict[str, object] | None = None,
) -> OperatorIncident:
    """Build the initial ``OperatorIncident`` scaffold at halt inception.

    The notice is deliberately the most pessimistic terminal outcome —
    ``flatten_failed`` — so that if the child process crashes AFTER the
    initial append but BEFORE the executor writes the terminal notice, the
    leftover incident is treated as critical by ``check_post_halt_gate`` and
    blocks any restart until the operator reconciles.

    The executor overwrites this scaffold with the real outcome (which may be
    a safe info-tier notice) at the terminal step.  On successful halt the
    scaffold is never visible to the gate in its pessimistic form.
    """
    incident_id = f"watchdog-{started_at_ms}-{uuid.uuid4().hex[:8]}"
    scaffold_notice = flatten_failed_notice(
        error_summary="Watchdog halt is in progress or did not complete — verify positions at IBKR before resuming",
        occurred_at_ms=started_at_ms,
    )
    return OperatorIncident(
        incident_id=incident_id,
        category="watchdog",
        notice=scaffold_notice,
        started_at_ms=started_at_ms,
        evidence={"reason": reason, **(evidence or {})},
    )


__all__ = [
    "broker_disconnected_before_flatten_notice",
    "flatten_completed_notice",
    "flatten_failed_notice",
    "flatten_not_needed_notice",
    "flatten_timed_out_notice",
    "recovery_flatten_uncertain_incident",
    "residual_positions_after_failed_flatten_incident",
    "watchdog_incident",
]
