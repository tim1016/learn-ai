"""Disposition authoring for unavailable recovery capabilities (#1778, S17).

The operator-blocker contract says `wait` renders no move: there is nothing
for the operator to do but wait. Every unavailable recovery capability was
authored `wait` regardless of cause -- including stale evidence, whose cure
*is* an operator action. The blocker therefore rendered no move at all for
the one condition an operator could actually fix.

The frontend contract is not what changed here. `wait` correctly renders no
move; the authoring was violating its own contract.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.projection_models import RecoveryCapability
from app.services.broker_v2_panel.sqlite_panel_adapter import _capability_blocker


def _capability(
    *,
    freshness: str,
    reason_code: str = "RECOVERY_EVIDENCE_STALE",
) -> RecoveryCapability:
    return RecoveryCapability(
        action_id="recover_exact_execution_evidence",
        label="Recover exact execution evidence",
        explanation="Re-read the broker's execution record for this run.",
        available=False,
        unavailable_reason_code=reason_code,
        unavailable_reason="The recorded execution evidence is no longer fresh.",
        scope="CUSTODY_SUBJECT",
        freshness=freshness,  # type: ignore[arg-type]
        evidence=(),
        reduction_plan=None,
        confirmation=None,
        next_step="Reconcile the account, then retry.",
        concurrency_token="token-1",
        execution_ref=None,
        mutation=True,
        primary=False,
    )


def test_stale_evidence_is_fixable_here_and_offers_the_reconcile_move() -> None:
    blocker = _capability_blocker(_capability(freshness="stale"))

    assert blocker.disposition == "fix_here"
    assert blocker.primary_move is not None
    # The move is backend-authored; the frontend never infers a cure from a
    # reason code.
    assert blocker.primary_move.label


def test_a_condition_the_operator_cannot_cure_still_offers_no_move() -> None:
    """Contract regression the other way: `wait` must stay move-less.

    If this ever starts rendering a move, the disposition contract has been
    broken from the authoring side again.
    """
    blocker = _capability_blocker(_capability(freshness="unavailable"))

    assert blocker.disposition == "wait"
    assert blocker.primary_move is None
