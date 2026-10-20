"""Phase 5g.3 — cross-engine comparator unit tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.lean_sidecar.cross_reconciler import (
    CrossReconciliationTolerances,
    compare_cross_engine,
)
from app.lean_sidecar.cross_runner import CrossRunOrderEvent
from app.lean_sidecar.normalized_parser import NormalizedOrderEvent
from app.research.parity.qc_reconciler import DivergenceCategory


def _lean_fill(
    *,
    ms_utc: int = 1_736_174_400_000,  # 2025-01-06 14:00:00 UTC (09:00 ET)
    direction: str = "Buy",
    fill_quantity: float = 100.0,
    fill_price: float = 580.50,
    order_fee_amount: float | None = 1.00,
    order_event_id: int = 1,
    order_id: int = 100,
    status: str = "Filled",
) -> NormalizedOrderEvent:
    return NormalizedOrderEvent.model_validate(
        {
            "order_event_id": order_event_id,
            "order_id": order_id,
            "algorithm_id": "MyAlgorithm",
            "symbol": "SPY",
            "symbol_value": "SPY",
            "ms_utc": ms_utc,
            "status": status,
            "direction": direction,
            "quantity": fill_quantity,
            "fill_price": fill_price,
            "fill_price_currency": "USD",
            "fill_quantity": fill_quantity,
            "is_assignment": False,
            "order_fee_amount": order_fee_amount,
            "order_fee_currency": "USD",
            "message": None,
        }
    )


def _engine_fill(
    *,
    ms_utc: int = 1_736_174_400_000,
    direction: str = "Buy",
    fill_quantity: int = 100,
    fill_price: Decimal = Decimal("580.50"),
    fee: Decimal = Decimal("1.00"),
    order_event_id: int = 0,
    order_id: int = 100,
    symbol: str = "SPY",
) -> CrossRunOrderEvent:
    return CrossRunOrderEvent(
        order_event_id=order_event_id,
        order_id=order_id,
        symbol=symbol,
        ms_utc=ms_utc,
        direction=direction,  # type: ignore[arg-type]
        fill_quantity=fill_quantity,
        fill_price=fill_price,
        fee=fee,
    )


class TestP2_5DateContractAlignment:
    """P2.5 — the cross-reconciler already derives trading dates from
    each fill's ``ms_utc`` via NY-local conversion. Under the new
    contract, the manifest's ``requested_window_ms`` (now half-open
    session-boundary ms) is directly comparable to the
    NY-trading-date set the reconciler produces — no logic change
    needed, but the invariant is worth pinning so a future refactor
    doesn't accidentally re-introduce a midnight-UTC dependence.
    """

    def test_fill_ms_utc_derives_correct_ny_trading_date(self) -> None:
        """A LEAN fill at 14:00 UTC on 2025-01-06 = 09:00 ET — the NY
        trading date is 2025-01-06 (and matches the manifest's
        ``requested_window_ms.start_ms`` derived from
        ``session_open_ms_utc(2025-01-06) = 14:30 UTC``)."""
        from app.lean_sidecar.trading_calendar import session_open_ms_utc

        # 14:00 UTC = 09:00 ET on 2025-01-06.
        out = compare_cross_engine(
            [_lean_fill(ms_utc=1_736_174_400_000)],
            [_engine_fill(ms_utc=1_736_174_400_000)],
        )
        # Clean path → no divergence rows; assert the fills pair on
        # the NY-derived trading date by introducing a quantity diff
        # so the reconciler emits a divergence we can read trading_date
        # off of.
        out_div = compare_cross_engine(
            [_lean_fill(ms_utc=1_736_174_400_000, fill_quantity=100.0)],
            [_engine_fill(ms_utc=1_736_174_400_000, fill_quantity=200)],
        )
        assert out_div.divergences[0].trading_date == date(2025, 1, 6)
        # Round-trip: session-open ms of 2025-01-06 lands at the
        # manifest's start_ms_utc value under the P2.5 contract.
        assert session_open_ms_utc(date(2025, 1, 6)) == 1_736_173_800_000
        # The clean-path comparison also passes (sanity).
        assert out.passed is True

    def test_dst_boundary_fill_derives_correct_ny_date(self) -> None:
        """Fill at 13:35 UTC on 2026-03-09 (EDT) = 09:35 ET. The NY
        trading date stays 2026-03-09 despite the DST flip — fixed
        offsets would have produced 2026-03-08 or 2026-03-10."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _Zone

        ms_utc = int(_dt(2026, 3, 9, 9, 35, tzinfo=_Zone("America/New_York")).timestamp() * 1000)
        out = compare_cross_engine(
            [_lean_fill(ms_utc=ms_utc, fill_quantity=100.0)],
            [_engine_fill(ms_utc=ms_utc, fill_quantity=200)],
        )
        assert out.divergences[0].trading_date == date(2026, 3, 9)


class TestCleanPath:
    def test_matched_fills_pass_with_zero_divergences(self) -> None:
        out = compare_cross_engine([_lean_fill()], [_engine_fill()])

        assert out.lean_total_fills == 1
        assert out.engine_total_fills == 1
        assert out.matched_count == 1
        assert out.divergent_count == 0
        assert out.gating_divergent_count == 0
        assert out.passed is True
        assert out.counts_by_category == {}
        assert out.divergences == []

    def test_empty_inputs_pass(self) -> None:
        """Zero fills on both sides is a trivial PASS — no decisions
        made, no decisions to disagree on."""
        out = compare_cross_engine([], [])
        assert out.passed is True
        assert out.matched_count == 0
        assert out.lean_total_fills == 0
        assert out.engine_total_fills == 0


class TestDecisionMismatch:
    def test_a_fill_on_only_one_side_is_a_decision_mismatch(self) -> None:
        only_engine = compare_cross_engine([], [_engine_fill()])
        assert only_engine.divergent_count == 1, "only the engine has a fill: one divergence"
        assert only_engine.gating_divergent_count == 1, "only the engine has a fill: the divergence gates"
        assert only_engine.passed is False, "only the engine has a fill: the report fails"
        d = only_engine.divergences[0]
        assert d.category is DivergenceCategory.DECISION_MISMATCH, "only the engine has a fill: decision mismatch"
        assert d.lean_fill is None, "only the engine has a fill: the row has no LEAN fill"
        assert d.engine_fill is not None, "only the engine has a fill: the row carries the engine fill"

        only_lean = compare_cross_engine([_lean_fill()], [])
        assert only_lean.divergent_count == 1, "only LEAN has a fill: one divergence"
        assert only_lean.divergences[0].category is DivergenceCategory.DECISION_MISMATCH, (
            "only LEAN has a fill: decision mismatch"
        )
        assert only_lean.divergences[0].lean_fill is not None, "only LEAN has a fill: the row carries the LEAN fill"
        assert only_lean.divergences[0].engine_fill is None, "only LEAN has a fill: the row has no engine fill"


class TestQuantityMismatch:
    @pytest.mark.parametrize(
        ("lean_quantity", "engine_quantity"),
        [
            (100.0, 200),
            # Review-fix (P2.8): int-truncating LEAN's float quantity hides
            # fractional drift. Both sides should compare on Decimal-typed
            # quantities. LEAN reports 100.5 shares, Engine reports 100. With
            # int truncation on both sides, the comparator would emit zero
            # divergences (both → 100). With Decimal preservation, the
            # comparator surfaces a quantity_mismatch row.
            (100.5, 100),
        ],
        ids=[
            "different-quantities-emit-quantity-mismatch",
            "fractional-lean-qty-does-not-collapse-to-int",
        ],
    )
    def test_quantities_that_differ_emit_quantity_mismatch(
        self, lean_quantity: float, engine_quantity: int
    ) -> None:
        out = compare_cross_engine(
            [_lean_fill(fill_quantity=lean_quantity)],
            [_engine_fill(fill_quantity=engine_quantity)],
        )
        cats = [d.category for d in out.divergences]
        assert DivergenceCategory.QUANTITY_MISMATCH in cats, "the quantity difference is reported"
        assert out.passed is False, "a quantity mismatch fails the report"


class TestFillPriceDrift:
    @pytest.mark.parametrize(
        ("engine_price", "drift_expected"),
        [
            ("580.55", True),
            # The boundary case: 1 cent difference is exactly atol; the
            # condition is ``>``, so this should NOT emit a divergence.
            ("580.51", False),
        ],
        ids=[
            "price-diff-above-atol-emits-fill-price-drift",
            "price-diff-at-atol-does-not-emit",
        ],
    )
    def test_fill_price_drift_is_emitted_only_above_atol(self, engine_price: str, drift_expected: bool) -> None:
        out = compare_cross_engine(
            [_lean_fill(fill_price=580.50)],
            [_engine_fill(fill_price=Decimal(engine_price))],
        )
        cats = [d.category for d in out.divergences]
        assert (DivergenceCategory.FILL_PRICE_DRIFT in cats) is drift_expected, (
            "fill_price_drift is reported exactly when the price diff is above atol"
        )
        assert out.passed is not drift_expected, "the report fails exactly when fill_price_drift is reported"


class TestCommissionDriftGatingPolicy:
    """Default gating per D3: COMMISSION_DRIFT is diagnostic. Phase-5b-
    Branch-A semantics (assert_fees=True): COMMISSION_DRIFT becomes
    gating."""

    def test_commission_drift_is_diagnostic_by_default_and_gating_with_assert_fees(self) -> None:
        by_default = compare_cross_engine(
            [_lean_fill(order_fee_amount=5.00)],
            [_engine_fill(fee=Decimal("1.00"))],
        )
        cats = [d.category for d in by_default.divergences]
        # The divergence is reported...
        assert DivergenceCategory.COMMISSION_DRIFT in cats, "by default the commission drift is reported"
        # ...but the report still PASSES because it's diagnostic-only.
        assert by_default.divergent_count == 1, "by default there is one divergence"
        assert by_default.gating_divergent_count == 0, "by default the commission drift does not gate"
        assert by_default.passed is True, "by default the report passes: drift is diagnostic-only"

        with_assert_fees = compare_cross_engine(
            [_lean_fill(order_fee_amount=5.00)],
            [_engine_fill(fee=Decimal("1.00"))],
            assert_fees=True,
        )
        assert with_assert_fees.divergent_count == 1, "with assert_fees there is one divergence"
        assert with_assert_fees.gating_divergent_count == 1, "with assert_fees the commission drift gates"
        assert with_assert_fees.passed is False, "with assert_fees the report fails"


class TestLeanNonFilledStatusIgnored:
    """LEAN emits multiple event types per order (Submitted, Filled,
    Canceled). The comparator must only count Filled — submission
    lifecycle events have no Engine-Lab counterpart and would produce
    spurious DECISION_MISMATCH rows."""

    def test_submitted_status_is_filtered_out(self) -> None:
        out = compare_cross_engine(
            [
                _lean_fill(status="Submitted", order_event_id=1),
                _lean_fill(status="Filled", order_event_id=2),
            ],
            [_engine_fill()],
        )
        assert out.lean_total_fills == 1  # Submitted dropped
        assert out.matched_count == 1
        assert out.passed is True


class TestTolerancesPropagateToOutput:
    def test_custom_tolerances_widen_acceptance(self) -> None:
        # 10-cent drift, but explicitly widened atol allows it.
        out = compare_cross_engine(
            [_lean_fill(fill_price=580.50)],
            [_engine_fill(fill_price=Decimal("580.60"))],
            tolerances=CrossReconciliationTolerances(fill_price_atol=Decimal("0.20")),
        )
        cats = [d.category for d in out.divergences]
        assert DivergenceCategory.FILL_PRICE_DRIFT not in cats


class TestPairingByTradingDate:
    """Fills on the same UTC ms but different NY trading dates (i.e.,
    extended-hours fills crossing the midnight boundary) must pair on
    NY trading date, not UTC date."""

    def test_pairing_uses_ny_trading_date(self) -> None:
        # Pick a UTC ms that is 2025-01-07 00:30 UTC = 2025-01-06 19:30 ET
        # (Monday after-hours). Both engines fire on the same NY date.
        ms_after_hours = 1_736_209_800_000  # 2025-01-07 00:30 UTC
        out = compare_cross_engine(
            [_lean_fill(ms_utc=ms_after_hours)],
            [_engine_fill(ms_utc=ms_after_hours)],
        )
        # Same NY trading date → pair, zero divergences.
        assert out.matched_count == 1
        assert out.passed is True
        # And the divergence list (if any) carries the NY-LOCAL trading
        # date, not UTC.
        out_with_drift = compare_cross_engine(
            [_lean_fill(ms_utc=ms_after_hours, fill_price=580.50)],
            [_engine_fill(ms_utc=ms_after_hours, fill_price=Decimal("580.60"))],
        )
        d = out_with_drift.divergences[0]
        assert d.trading_date == date(2025, 1, 6)
