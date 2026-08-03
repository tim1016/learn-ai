"""Pure account-scoped Clerk↔broker custody-diagnosis fold tests (task 1.1),
plus the clerk-method + endpoint seam tests (task 1.2)."""

from __future__ import annotations

from app.broker.alpaca.clerk import diagnosis
from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.alpaca.clerk.models import OrderJournalEntry
from tests.broker.alpaca.clerk.test_clerk_reconciliation import (
    _FakeBroker,
    _fixed_clock,
    _position,
)


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


# ── AlpacaClerk.custody_diagnosis() (Style A: direct clerk) ─────────────────


async def test_custody_diagnosis_reports_missing_intent_mismatch() -> None:
    broker = _FakeBroker(orders=[], positions=[_position(symbol="SPY", quantity=1, side="long")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    result = await clerk.custody_diagnosis()

    assert result.in_sync is False
    assert result.resolvable is True
    assert [s.action_id for s in result.resolution_plan] == [
        "reconcile_now",
        "record_inventory_baseline",
    ]
    assert result.divergences[0].kind == "exposure_attribution_mismatch"
    assert result.divergences[0].position_deltas[0].broker_observed_qty == 1.0
    assert result.snapshot_version  # non-empty guard token


async def test_custody_diagnosis_flat_account_is_in_sync() -> None:
    broker = _FakeBroker(orders=[], positions=[])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    result = await clerk.custody_diagnosis()

    assert result.in_sync is True
    assert result.divergences == ()
    assert result.resolution_plan == ()
