"""Small post-mutation receipts for retained legacy runner boundaries.

The deprecated IBKR operator-surface projection used to be rebuilt after every
Start and Stop solely to derive the next blockage-ladder rung.  The canonical
Alpaca Broker V2 panel now owns follow-up guidance, so these compatibility
boundaries report only what they can prove locally: the mutation was accepted.
"""

from __future__ import annotations

from app.operator.notices.schema import OperatorNoticeAction
from app.schemas.live_runs import MutationRungReceipt

_MUTATION_LABELS = {
    "start": "Start",
    "stop": "Stop",
}


def accepted_mutation_receipts(
    *,
    mutation_key: str,
    occurred_at_ms: int,
) -> tuple[MutationRungReceipt, list[MutationRungReceipt]]:
    """Return an evidence-bounded receipt without rebuilding a control panel."""

    mutation_label = _MUTATION_LABELS[mutation_key]
    return (
        MutationRungReceipt(
            code="mutation.scoped_all_clear",
            tier="info",
            title=f"{mutation_label} accepted.",
            message=(
                "The runner boundary accepted the mutation. Use the Alpaca Broker V2 "
                "panel for current readiness and recovery guidance."
            ),
            rung_id=None,
            actionability="self_resolving",
            resolution="No further work is required at this compatibility boundary.",
            action=OperatorNoticeAction(kind="none"),
            occurred_at_ms=occurred_at_ms,
        ),
        [],
    )
