"""Phase 5a — unit tests for the LEAN Lab self-reconciler.

The reconciler is a pure function over ``NormalizedOrderEvent`` lists,
so these tests construct the events directly without spinning up a
LEAN container or even a router. The IBKR commission math itself is
already pinned by ``tests/research/parity/test_ibkr_commission.py``;
this file tests the reconciliation-layer logic (filtering, aggregation,
categorization, tolerance semantics).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.lean_sidecar.normalized_parser import NormalizedOrderEvent
from app.lean_sidecar.reconciler import (
    DEFAULT_COMMISSION_ATOL,
    FeeDivergenceCategory,
    reconcile_against_ibkr,
)
from app.research.parity.ibkr_commission import IbkrEquityCommissionModel


def _filled_event(
    *,
    order_event_id: int = 1,
    order_id: int = 100,
    symbol: str = "SPY",
    ms_utc: int = 1_736_121_600_000,
    fill_quantity: float = 100.0,
    fill_price: float = 580.50,
    fee: float | None = 1.00,
    status: str = "Filled",
) -> NormalizedOrderEvent:
    """Build a minimal filled event. Defaults yield IBKR-clean fee of $1.00
    (100 shares × $0.005 = $0.50 → floor $1.00, well under the 0.5% cap)."""
    return NormalizedOrderEvent.model_validate(
        {
            "order_event_id": order_event_id,
            "order_id": order_id,
            "algorithm_id": "MyAlgorithm",
            "symbol": symbol,
            "symbol_value": symbol,
            "ms_utc": ms_utc,
            "status": status,
            "direction": "Buy",
            "quantity": fill_quantity,
            "fill_price": fill_price,
            "fill_price_currency": "USD",
            "fill_quantity": fill_quantity,
            "is_assignment": False,
            "order_fee_amount": fee,
            "order_fee_currency": "USD" if fee is not None else None,
            "message": None,
        }
    )


class TestReconcileAgainstIbkr:
    def test_empty_event_list_returns_zero_counts(self) -> None:
        report = reconcile_against_ibkr("run_x", [])
        assert report.total_fill_events == 0
        assert report.matched_count == 0
        assert report.divergent_count == 0
        assert report.divergences == ()
        assert report.total_recorded_fees == Decimal("0.00")
        assert report.total_expected_ibkr_fees == Decimal("0.00")

    def test_non_filled_events_are_excluded(self) -> None:
        """Submitted / Cancelled events have no fee — must not appear in
        the fill-events count or skew the totals."""
        events = [
            _filled_event(order_event_id=1, status="Submitted", fee=None),
            _filled_event(order_event_id=2, status="Filled", fee=1.00),
            _filled_event(order_event_id=3, status="Canceled", fee=None),
        ]
        report = reconcile_against_ibkr("run_x", events)
        assert report.total_fill_events == 1
        assert report.matched_count == 1
        assert report.divergent_count == 0

    def test_filled_status_is_case_insensitive(self) -> None:
        """LEAN has been inconsistent across versions ('Filled' vs 'filled').
        Both must count toward the report."""
        events = [
            _filled_event(order_event_id=1, status="Filled", fee=1.00),
            _filled_event(order_event_id=2, status="filled", fee=1.00),
        ]
        report = reconcile_against_ibkr("run_x", events)
        assert report.total_fill_events == 2
        assert report.matched_count == 2

    def test_clean_run_under_tolerance(self) -> None:
        """100 shares × $580.50 → IBKR expected $1.00 (floor). LEAN-recorded
        $1.00 → clean."""
        events = [_filled_event(fill_quantity=100, fill_price=580.50, fee=1.00)]
        report = reconcile_against_ibkr("run_x", events)
        assert report.matched_count == 1
        assert report.divergent_count == 0
        assert report.total_recorded_fees == Decimal("1.00")
        assert report.total_expected_ibkr_fees == Decimal("1.00")

    def test_commission_drift_when_recorded_differs_by_more_than_atol(self) -> None:
        """LEAN default brokerage often produces fees that don't match
        the IBKR tier — that's the whole point of this reconciliation."""
        events = [_filled_event(fill_quantity=100, fill_price=580.50, fee=5.00)]
        report = reconcile_against_ibkr("run_x", events)
        assert report.divergent_count == 1
        d = report.divergences[0]
        assert d.category == FeeDivergenceCategory.COMMISSION_DRIFT
        assert d.recorded_fee == Decimal("5.00")
        assert d.expected_ibkr_fee == Decimal("1.00")
        assert d.delta == Decimal("4.00")

    def test_no_recorded_fee_classified_separately(self) -> None:
        """A filled event missing ``orderFeeAmount`` is its own category —
        we have nothing to compare, so it isn't a 'drift' per se but
        the operator should still see it."""
        events = [_filled_event(fill_quantity=100, fill_price=580.50, fee=None)]
        report = reconcile_against_ibkr("run_x", events)
        assert report.divergent_count == 1
        d = report.divergences[0]
        assert d.category == FeeDivergenceCategory.NO_RECORDED_FEE
        assert d.recorded_fee is None
        assert d.delta is None
        assert d.expected_ibkr_fee == Decimal("1.00")

    def test_tolerance_at_boundary_is_clean(self) -> None:
        """Exactly $0.01 delta from IBKR is within the default tolerance —
        documents the boundary so a regression doesn't tighten it silently."""
        events = [_filled_event(fill_quantity=100, fill_price=580.50, fee=1.01)]
        report = reconcile_against_ibkr("run_x", events)
        assert report.matched_count == 1
        assert report.divergent_count == 0

    def test_aggregate_totals_sum_recorded_and_expected_independently(self) -> None:
        events = [
            _filled_event(order_event_id=1, fill_quantity=100, fill_price=580.50, fee=1.00),
            _filled_event(order_event_id=2, fill_quantity=200, fill_price=580.50, fee=1.00),
        ]
        report = reconcile_against_ibkr("run_x", events)
        assert report.total_recorded_fees == Decimal("2.00")
        # 200 shares × $0.005 = $1.00, but the per-order minimum is $1.00, so
        # both orders charge $1.00 → total $2.00.
        assert report.total_expected_ibkr_fees == Decimal("2.00")

    def test_negative_quantity_handled_as_sell(self) -> None:
        """The IBKR model uses abs(qty); a sell with quantity=-100 must
        produce the same fee as a buy of 100. The reconciler stores the
        rounded int qty in the divergence for human inspection."""
        events = [_filled_event(fill_quantity=-100, fill_price=580.50, fee=5.00)]
        report = reconcile_against_ibkr("run_x", events)
        d = report.divergences[0]
        assert d.fill_quantity == -100
        assert d.expected_ibkr_fee == Decimal("1.00")

    def test_custom_atol_widens_tolerance(self) -> None:
        events = [_filled_event(fill_quantity=100, fill_price=580.50, fee=5.00)]
        report = reconcile_against_ibkr(
            "run_x",
            events,
            commission_atol=Decimal("10.00"),
        )
        assert report.matched_count == 1
        assert report.divergent_count == 0
        assert report.commission_atol == Decimal("10.00")

    def test_custom_model_is_used(self) -> None:
        """Operators can pass a different commission model (e.g., a hypothetical
        IBKR Pro tier) to compare against. The default reconciliation uses the
        equity-tier model; this proves the injection seam works."""
        cheap_model = IbkrEquityCommissionModel(
            per_share=Decimal("0.0001"),
            min_per_order=Decimal("0.50"),
            max_pct_of_value=Decimal("0.005"),
        )
        events = [_filled_event(fill_quantity=100, fill_price=580.50, fee=0.50)]
        report = reconcile_against_ibkr("run_x", events, model=cheap_model)
        assert report.matched_count == 1
        assert report.total_expected_ibkr_fees == Decimal("0.50")

    def test_default_atol_matches_numerical_rigor_constant(self) -> None:
        """If the project-wide commission_atol moves in numerical-rigor.md,
        this test fails and forces the documentation to stay in sync with
        the reconciler default."""
        assert Decimal("0.01") == DEFAULT_COMMISSION_ATOL


class TestReconcilerEdgeCases:
    def test_zero_quantity_fill_produces_zero_expected_fee(self) -> None:
        """An IBKR fill with zero shares is a no-op — fee should be $0.00,
        not the per-order minimum. (LEAN doesn't typically emit these, but
        the model is defensive.)"""
        events = [_filled_event(fill_quantity=0, fill_price=580.50, fee=0.00)]
        report = reconcile_against_ibkr("run_x", events)
        assert report.matched_count == 1
        assert report.total_expected_ibkr_fees == Decimal("0.00")

    def test_large_trade_hits_percentage_cap(self) -> None:
        """A penny-stock-sized order would otherwise charge per-share well
        beyond the 0.5% trade-value cap. The IBKR model clamps at the cap."""
        # 100,000 shares × $0.10 = $10,000 trade value. Per-share would be
        # $500. The cap = 0.5% × $10,000 = $50. So expected fee = $50.
        events = [_filled_event(fill_quantity=100_000, fill_price=0.10, fee=50.00)]
        report = reconcile_against_ibkr("run_x", events)
        assert report.matched_count == 1
        assert report.total_expected_ibkr_fees == Decimal("50.00")


@pytest.mark.parametrize(
    "recorded,expected_category",
    [
        # Values are post-quantization to cents: the reconciler rounds
        # recorded fees and delta to two-decimal-place Decimals before
        # comparing, so a raw 1.011 becomes 1.01 (boundary, clean).
        (1.005, "clean"),  # rounds to 1.01 → boundary (clean)
        (1.01, "clean"),  # exactly $0.01 → boundary inclusive (clean)
        (1.02, "commission_drift"),  # $0.02 over → drift
        (0.99, "clean"),  # exactly $0.01 below → boundary inclusive
        (0.98, "commission_drift"),  # $0.02 below → drift
    ],
)
def test_atol_boundary_classification(recorded: float, expected_category: str) -> None:
    """The reconciler's classification boundary is inclusive on |delta| ≤ atol,
    where both sides are quantized to cents first."""
    events = [_filled_event(fill_quantity=100, fill_price=580.50, fee=recorded)]
    report = reconcile_against_ibkr("run_x", events)
    if expected_category == "clean":
        assert report.matched_count == 1, f"recorded={recorded}: should be clean"
    else:
        assert report.divergent_count == 1, f"recorded={recorded}: should diverge"
