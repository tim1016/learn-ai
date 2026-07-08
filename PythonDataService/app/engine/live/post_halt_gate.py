"""Post-halt reconciliation gate (PR 2 Task 5).

Reads ``<run_dir>/operator_incidents/*.json`` via ``IncidentStore`` and
returns a blocking ``OperatorIncident`` if any unresolved watchdog incident
with an uncertain outcome is present.

The gate is called by ``cmd_start`` before entering the bar loop; if it
returns non-None, the engine refuses to trade until the operator clears the
incident via the Reconcile-now button.

Uncertain outcome codes that trigger the gate (per plan §5):
  - ``watchdog.flatten_timed_out``
  - ``watchdog.flatten_failed``
  - ``watchdog.broker_disconnected_before_flatten``

Safe outcome codes that do NOT trigger the gate (broker is in a clean state):
  - ``watchdog.flatten_completed``
  - ``watchdog.flatten_not_needed``
"""

from __future__ import annotations

from pathlib import Path

from app.operator.incidents.store import IncidentStore
from app.operator.notices.schema import (
    OperatorIncident,
    OperatorNotice,
    OperatorNoticeAction,
)

# Uncertain-outcome notice codes that require reconciliation before restarting.
_UNCERTAIN_CODES: frozenset[str] = frozenset(
    {
        "watchdog.flatten_timed_out",
        "watchdog.flatten_failed",
        "watchdog.broker_disconnected_before_flatten",
    }
)


def check_post_halt_gate(run_dir: Path, *, now_ms: int) -> OperatorIncident | None:
    """Return a blocking incident if an unresolved uncertain-outcome watchdog halt exists.

    Scans ``<run_dir>/operator_incidents/`` for any incident that satisfies
    ALL of:
      1. ``category == "watchdog"``
      2. ``resolved_at_ms is None``
      3. ``notice.code`` is one of the three uncertain-outcome codes.

    If such an incident is found, returns a NEW ``OperatorIncident`` whose
    notice code is ``reconciliation.required_after_uncertain_flatten``.  The
    prior incident's ``incident_id`` is embedded in the new incident's evidence
    so the cockpit can link to the original event.

    Returns ``None`` if no blocking condition exists (directory absent, no
    unresolved incidents, or all unresolved incidents have safe outcome codes).
    """
    store = IncidentStore(run_dir)
    unresolved = store.list_unresolved()
    for incident in unresolved:
        if incident.category != "watchdog":
            continue
        if incident.notice.code not in _UNCERTAIN_CODES:
            continue
        # Found a blocking incident — wrap it in a reconciliation-required notice.
        return _make_reconciliation_required(incident, now_ms=now_ms)
    return None


def _make_reconciliation_required(
    prior: OperatorIncident,
    *,
    now_ms: int,
) -> OperatorIncident:
    """Build the blocking incident referencing the prior watchdog incident."""
    incident_id = f"gate-{now_ms}-{prior.incident_id}"
    notice = OperatorNotice(
        code="reconciliation.required_after_uncertain_flatten",
        tier="critical",
        title="Reconciliation required before restarting",
        message=(
            "A previous watchdog halt left broker state uncertain "
            f"(prior incident: {prior.incident_id}, notice: {prior.notice.code}). "
            "Run the Reconcile-now procedure in the cockpit and verify positions "
            "at IBKR before the engine will accept new orders."
        ),
        actionability="actuatable",
        resolution="Clears when runtime reconciliation records clean or adopted broker evidence for this run.",
        action=OperatorNoticeAction(
            kind="focus_cockpit_action",
            label="Run reconciliation",
            target="reconcile_now",
        ),
        runbook_slug="watchdog-halt",
        occurred_at_ms=now_ms,
    )
    return OperatorIncident(
        incident_id=incident_id,
        category="reconciliation",
        notice=notice,
        started_at_ms=now_ms,
        evidence={
            "prior_incident_id": prior.incident_id,
            "prior_notice_code": prior.notice.code,
            "prior_started_at_ms": prior.started_at_ms,
        },
    )


__all__ = ["check_post_halt_gate"]
