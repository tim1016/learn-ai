"""Predicted-vs-observed session fee reconciliation (ADR 0059 D6)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.broker.contract.models import BrokerActivity, OrderSide
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.services.alpaca_fee_reconciliation import (
    FEE_POSTING_GRACE_MS,
    SessionFill,
    reconcile_session_fees,
)
from app.utils.session_anchors import et_midnight_ms

D = Decimal
TRADE_DATE = date(2026, 9, 8)
SESSION_OPEN_MS = session_open_ms_utc(TRADE_DATE)
DAY_START_MS = et_midnight_ms(TRADE_DATE)
DAY_END_MS = et_midnight_ms(TRADE_DATE + timedelta(days=1))

# The six FEE-001 session fills: predicted sec 14.95 + taf 29.39 + cat 0.49 = 44.83
SESSION_FILLS = (
    SessionFill(side=OrderSide.SELL, quantity=D("100"), fill_price=D("250.00")),
    SessionFill(side=OrderSide.BUY, quantity=D("100"), fill_price=D("250.00")),
    SessionFill(side=OrderSide.SELL, quantity=D("60000"), fill_price=D("10.00")),
    SessionFill(side=OrderSide.SELL, quantity=D("50205"), fill_price=D("1.00")),
    SessionFill(side=OrderSide.SELL, quantity=D("50206"), fill_price=D("1.00")),
    SessionFill(side=OrderSide.BUY, quantity=D("0.5"), fill_price=D("400.00")),
)


def _fee(activity_id: str, net_amount: float | None, occurred_at_ms: int | None = DAY_START_MS) -> BrokerActivity:
    return BrokerActivity(
        broker="alpaca",
        activity_id=activity_id,
        activity_type="FEE",
        category="non_trade_activity",
        symbol=None,
        side=None,
        quantity=None,
        price=None,
        net_amount=net_amount,
        occurred_at_ms=occurred_at_ms,
        observed_at_ms=DAY_END_MS,
    )


def _reconcile(fills=SESSION_FILLS, activities=(), now_ms: int = DAY_END_MS + 1):
    return reconcile_session_fees(
        broker="alpaca",
        account_id="123456789",
        session_open_ms=SESSION_OPEN_MS,
        fills=fills,
        fee_activities=activities,
        now_ms=now_ms,
    )


def test_prediction_and_window_are_reported() -> None:
    result = _reconcile()

    assert result.predicted is not None
    assert (result.predicted.sec_usd, result.predicted.taf_usd, result.predicted.cat_usd) == (14.95, 29.39, 0.49)
    assert result.predicted.total_usd == 44.83
    assert (result.fill_count, result.sell_fill_count) == (6, 4)
    assert (result.fill_window_start_ms, result.fill_window_end_ms) == (DAY_START_MS, DAY_END_MS)
    assert result.tolerance_usd == 0.11  # 0.01 × (3 + 2 × 4)


def test_observed_within_tolerance() -> None:
    result = _reconcile(activities=(_fee("f1", -14.95), _fee("f2", -29.39), _fee("f3", -0.55)))

    assert result.verdict == "within_tolerance"
    assert result.observed_total_usd == 44.89
    assert result.delta_usd == 0.06
    assert result.observed_activity_count == 3


def test_observed_drift_beyond_tolerance() -> None:
    result = _reconcile(activities=(_fee("f1", -45.10),))

    assert result.verdict == "drift"
    assert result.delta_usd == 0.27


def test_fee_activities_from_other_days_are_ignored() -> None:
    result = _reconcile(activities=(_fee("yesterday", -44.83, DAY_START_MS - 1), _fee("no-date", -44.83, None)))

    assert result.observed_activity_count == 0
    assert result.verdict == "pending"  # nothing observed for this date; still inside the posting grace


def test_pending_inside_the_posting_grace_period() -> None:
    result = _reconcile(now_ms=DAY_END_MS + FEE_POSTING_GRACE_MS - 1)

    assert result.verdict == "pending"
    assert result.observed_total_usd is None


def test_unobserved_after_the_grace_period() -> None:
    result = _reconcile(now_ms=DAY_END_MS + FEE_POSTING_GRACE_MS)

    assert result.verdict == "unobserved"


def test_fee_row_without_net_amount_is_unobserved_not_zero() -> None:
    result = _reconcile(activities=(_fee("f1", -14.95), _fee("f2", None)))

    assert result.verdict == "unobserved"
    assert result.observed_total_usd is None
    assert result.observed_activity_count == 2


def test_no_fills_and_no_fees() -> None:
    result = _reconcile(fills=())

    assert result.verdict == "no_fills"
    assert result.predicted is not None
    assert result.predicted.total_usd == 0.0


def test_fees_observed_with_no_fills_is_drift() -> None:
    result = _reconcile(fills=(), activities=(_fee("f1", -1.00),))

    assert result.verdict == "drift"
    assert result.delta_usd == 1.0


def test_rate_unpinned_session_has_no_prediction() -> None:
    open_2025 = session_open_ms_utc(date(2025, 6, 2))

    result = reconcile_session_fees(
        broker="alpaca",
        account_id="123456789",
        session_open_ms=open_2025,
        fills=(SessionFill(side=OrderSide.SELL, quantity=D("1"), fill_price=D("1")),),
        fee_activities=(),
        now_ms=open_2025,
    )

    assert result.verdict == "rate_unpinned"
    assert result.predicted is None
    assert result.unpinned_components == ["cat"]
    assert result.tolerance_usd is None
