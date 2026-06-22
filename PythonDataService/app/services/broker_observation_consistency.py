"""PRD #619-D4 — broker observation consistency (B′ divergence surface).

The data plane and the live-engine child each have their own
observation of the IBKR broker connection.  Per ADR-0011 (PRD
#619-A) the *child's* observation is the authority for the bound
instance — the singleton observation is advisory.  When the two
disagree, the operator must see the divergence prominently without
the child's authoritative posture being silently overwritten.

This module is the pure resolver: it takes the child's broker block
(from ``engine_runtime.json``) and the data plane's singleton
snapshot (from ``snapshot_data_plane_broker``) and returns a typed
``BrokerObservationConsistency`` verdict.

The four-way verdict mirrors the runtime-freshness shape: ``CONSISTENT``
(both verified, same account), ``CONFLICTING`` (both verified,
different accounts), ``UNKNOWN`` (one observation missing or stale),
``NOT_COMPARABLE`` (the comparison is not apples-to-apples, e.g.
the two are configured for different modes).

The cockpit (D4 / D5) renders the verdict as a card under the broker
hero — prominent on ``CONFLICTING`` — but **never** mutates the
child's authoritative ``operator_surface.broker`` projection.
"""

from __future__ import annotations

from typing import Literal

from app.broker.runtime_snapshot import BrokerRuntimeSnapshot
from app.engine.live.engine_runtime import BrokerBlock
from app.schemas.live_runs import BrokerObservationConsistency

# Closed reason-code vocabulary.  The cockpit's reason-code lookup
# (D5) covers each entry.
REASON_CODES: frozenset[str] = frozenset(
    {
        "ACCOUNTS_MATCH",
        "ACCOUNTS_DIVERGE",
        "CHILD_OBSERVATION_MISSING",
        "DATA_PLANE_OBSERVATION_MISSING",
        "DATA_PLANE_DISCONNECTED",
        "CONFIGURED_MODES_DIVERGE",
    }
)


def evaluate_broker_observation_consistency(
    *,
    child: BrokerBlock | None,
    data_plane: BrokerRuntimeSnapshot,
    child_configured_mode: Literal["paper", "live"] | None = None,
    now_ms: int,
) -> BrokerObservationConsistency:
    """Compare the child and data-plane broker observations.

    Decision tree:

    1. **NOT_COMPARABLE** — the data plane snapshot's
       ``configured_mode`` is set, the child's run spec carries a
       configured mode, and the two differ (paper vs live).  The
       comparison is not apples-to-apples; rendering one verdict
       over the other would mislead.
    2. **UNKNOWN** — either side is unavailable: no child observation
       yet (no ``engine_runtime`` or empty ``connected_account``), or
       the data plane singleton is disabled / unreachable / has no
       account.
    3. **CONSISTENT** — both report the same non-empty account.
    4. **CONFLICTING** — both report non-empty accounts that differ.

    The reason-code list is ordered most-relevant-first; the head is
    the operator's primary signal.
    """
    child_account = (
        child.connected_account if child is not None else None
    )
    data_plane_account = (
        data_plane.connected_account
        if data_plane.client_available and data_plane.connected
        else None
    )

    # Mode mismatch makes the comparison meaningless even when both
    # accounts are populated.
    if (
        child_configured_mode is not None
        and data_plane.configured_mode is not None
        and child_configured_mode != data_plane.configured_mode
    ):
        return BrokerObservationConsistency(
            verdict="NOT_COMPARABLE",
            child_account=child_account,
            data_plane_account=data_plane_account,
            reason_codes=["CONFIGURED_MODES_DIVERGE"],
            compared_at_ms=now_ms,
        )

    missing: list[str] = []
    if not child_account:
        missing.append("CHILD_OBSERVATION_MISSING")
    if not data_plane.client_available:
        missing.append("DATA_PLANE_OBSERVATION_MISSING")
    elif not data_plane.connected:
        missing.append("DATA_PLANE_DISCONNECTED")
    elif not data_plane_account:
        missing.append("DATA_PLANE_OBSERVATION_MISSING")

    if missing:
        return BrokerObservationConsistency(
            verdict="UNKNOWN",
            child_account=child_account,
            data_plane_account=data_plane_account,
            reason_codes=missing,
            compared_at_ms=now_ms,
        )

    if child_account == data_plane_account:
        return BrokerObservationConsistency(
            verdict="CONSISTENT",
            child_account=child_account,
            data_plane_account=data_plane_account,
            reason_codes=["ACCOUNTS_MATCH"],
            compared_at_ms=now_ms,
        )
    return BrokerObservationConsistency(
        verdict="CONFLICTING",
        child_account=child_account,
        data_plane_account=data_plane_account,
        reason_codes=["ACCOUNTS_DIVERGE"],
        compared_at_ms=now_ms,
    )
