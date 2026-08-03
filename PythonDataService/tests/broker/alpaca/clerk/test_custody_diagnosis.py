"""Pure account-scoped Clerk↔broker custody-diagnosis fold tests (task 1.1)."""

from __future__ import annotations

from app.broker.alpaca.clerk import diagnosis
from app.broker.alpaca.clerk.models import OrderJournalEntry
from tests.broker.alpaca.clerk.test_clerk_reconciliation import _position


def test_attribution_mismatch_reports_per_symbol_delta() -> None:
    # Journal expects nothing (no baseline, no terminal orders); broker holds 1 SPY.
    entries: list[OrderJournalEntry] = []
    positions = [_position()]

    divergences = diagnosis.diagnose_custody(
        entries, orders=[], positions=positions, namespaces=frozenset()
    )

    assert len(divergences) == 1
    d = divergences[0]
    assert d.kind == "exposure_attribution_mismatch"
    assert d.state == "resolvable_now"
    assert d.resolution_step == "record_inventory_baseline"
    assert d.position_deltas == (
        diagnosis.CustodyPositionDelta(
            symbol="SPY", clerk_attributed_qty=0.0, broker_observed_qty=1.0
        ),
    )
    assert d.possible_causes  # backend-authored, non-empty
    assert d.explanation  # backend-authored prose


def test_flat_and_reconciled_account_has_no_divergence() -> None:
    assert diagnosis.diagnose_custody([], orders=[], positions=[], namespaces=frozenset()) == ()
