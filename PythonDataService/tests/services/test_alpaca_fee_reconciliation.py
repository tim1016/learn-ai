"""Predicted-vs-observed session fee reconciliation (ADR 0059 D6)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.broker.alpaca.clerk.active_authority import set_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.economic_projection_models import ExecutionPage, ExecutionRow
from app.broker.contract.models import BrokerActivity, OrderSide
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.services.alpaca_fee_reconciliation import (
    FEE_POSTING_GRACE_MS,
    SessionFill,
    reconcile_session_fees,
    session_fee_reconciliation,
    session_fills,
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


def _row(fill_id: str, filled_at_ms: int, side: OrderSide, quantity: float, price: float) -> ExecutionRow:
    return ExecutionRow(
        fill_id=fill_id,
        execution_id=None,
        order_ref=f"order-{fill_id}",
        strategy_instance_id=None,
        origin="strategy",
        state="effective",
        event_kind="fill",
        symbol="AAPL",
        side=side,
        quantity=quantity,
        price=price,
        fee=None,
        fee_fidelity="not_reported",
        filled_at_ms=filled_at_ms,
        recorded_at_ms=filled_at_ms,
    )


def _page(rows: tuple[ExecutionRow, ...], next_cursor: str | None) -> ExecutionPage:
    return ExecutionPage(
        account_id="123456789",
        authority_generation=1,
        control_revision=1,
        executions=rows,
        next_cursor=next_cursor,
    )


class _Pager:
    """Newest-first pages keyed by cursor, recording every call."""

    def __init__(self, pages: dict[str | None, ExecutionPage]) -> None:
        self.pages = pages
        self.calls: list[tuple[str | None, int, str | None]] = []

    def account_executions(self, *, cursor: str | None, limit: int, state: str | None) -> ExecutionPage:
        self.calls.append((cursor, limit, state))
        return self.pages[cursor]


def test_session_fills_keeps_only_the_window_and_stops_paging_before_it() -> None:
    pager = _Pager(
        {
            None: _page(
                (
                    _row("after", DAY_END_MS, OrderSide.SELL, 1.0, 1.0),
                    _row("late", DAY_END_MS - 1, OrderSide.SELL, 100.0, 250.0),
                    _row("early", DAY_START_MS, OrderSide.BUY, 0.5, 400.0),
                ),
                "page-2",
            ),
            "page-2": _page((_row("before", DAY_START_MS - 1, OrderSide.SELL, 7.0, 7.0),), "page-3"),
            "page-3": _page((), None),
        }
    )

    fills = session_fills(pager, window_start_ms=DAY_START_MS, window_end_ms=DAY_END_MS)

    assert fills == [
        SessionFill(side=OrderSide.SELL, quantity=D("100"), fill_price=D("250")),
        SessionFill(side=OrderSide.BUY, quantity=D("0.5"), fill_price=D("400")),
    ]
    assert pager.calls == [(None, 100, "effective"), ("page-2", 100, "effective")]


def test_session_fills_stops_when_pages_run_out() -> None:
    pager = _Pager({None: _page((_row("only", SESSION_OPEN_MS, OrderSide.SELL, 2.0, 3.0),), None)})

    fills = session_fills(pager, window_start_ms=DAY_START_MS, window_end_ms=DAY_END_MS)

    assert fills == [SessionFill(side=OrderSide.SELL, quantity=D("2"), fill_price=D("3"))]


class _Port:
    def __init__(self) -> None:
        self.calls: list[tuple[int | None, int]] = []

    async def list_activities(self, *, after_ms: int | None = None, limit: int = 100) -> list[BrokerActivity]:
        self.calls.append((after_ms, limit))
        return []


async def test_facade_is_unavailable_without_an_active_sqlite_clerk() -> None:
    set_active_clerk_runtime(None)
    port = _Port()

    result = await session_fee_reconciliation(broker="alpaca", port=port, session_open_ms=SESSION_OPEN_MS, now_ms=DAY_END_MS)

    assert result.verdict == "unavailable"
    assert result.account_id is None
    assert result.predicted is None
    assert (result.fill_window_start_ms, result.fill_window_end_ms) == (DAY_START_MS, DAY_END_MS)
    assert port.calls == []
