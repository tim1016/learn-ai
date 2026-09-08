"""Predicted-vs-observed session fee reconciliation (ADR 0059 D6)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionUnavailable,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerActivity, OrderSide
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.services.alpaca_fee_reconciliation import (
    FEE_POSTING_GRACE_MS,
    SessionFill,
    reconcile_session_fees,
    session_fee_reconciliation,
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


def _older(activity_id: str = "older", occurred_at_ms: int = DAY_START_MS - 1) -> BrokerActivity:
    """One non-FEE activity dated before the window: proof the read reached back."""
    return BrokerActivity(
        broker="alpaca",
        activity_id=activity_id,
        activity_type="CSD",
        category="non_trade_activity",
        symbol=None,
        side=None,
        quantity=None,
        price=None,
        net_amount=1_000.0,
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
    result = _reconcile(
        activities=(_older(), _fee("f1", -14.95), _fee("f2", -29.39), _fee("f3", -0.55))
    )

    assert result.verdict == "within_tolerance"
    assert result.observed_total_usd == 44.89
    assert result.delta_usd == 0.06
    assert result.observed_activity_count == 3


def test_observed_drift_beyond_tolerance() -> None:
    result = _reconcile(activities=(_older(), _fee("f1", -45.10)))

    assert result.verdict == "drift"
    assert result.delta_usd == 0.27
    assert "external orders" in result.why


def test_when_the_tolerance_band_swallows_the_charge_the_why_says_so() -> None:
    """100 sells of a fractional-cent fill: tolerance dwarfs the predicted charge."""
    fills = tuple(
        SessionFill(side=OrderSide.SELL, quantity=D("1"), fill_price=D("0.01")) for _ in range(100)
    )

    result = _reconcile(fills=fills, activities=(_older(), _fee("f1", -1.00)))

    assert result.verdict == "within_tolerance"
    assert "tolerance band" in result.why
    assert "validate on low-sell-count sessions" in result.why


def test_a_read_that_never_reached_the_windows_start_cannot_be_compared() -> None:
    """A bounded newest-first activity read may have truncated the day's FEE rows."""
    result = _reconcile(activities=(_fee("f1", -14.95), _fee("f2", -29.88)))

    assert result.verdict == "unobserved"
    assert "cannot be shown to be complete" in result.why
    assert result.delta_usd is None
    assert result.observed_total_usd is None


def test_the_same_rows_compare_once_an_older_activity_proves_coverage() -> None:
    result = _reconcile(activities=(_older(), _fee("f1", -14.95), _fee("f2", -29.88)))

    assert result.verdict == "within_tolerance"
    assert result.observed_total_usd == 44.83


def test_observation_below_the_model_is_bounded_by_end_of_day_rounding_alone() -> None:
    """Per-trade rounding can only add cents, so only 3c of slack exists below."""
    five_below = _reconcile(activities=(_older(), _fee("f1", -44.78)))
    three_below = _reconcile(activities=(_older(), _fee("f1", -44.80)))

    assert (five_below.verdict, five_below.delta_usd) == ("drift", -0.05)
    assert (three_below.verdict, three_below.delta_usd) == ("within_tolerance", -0.03)


def test_only_fee_rows_dated_this_trade_date_are_summed() -> None:
    result = _reconcile(
        activities=(_older(), _fee("today", -44.83), _fee("tomorrow", -100.00, DAY_END_MS))
    )

    assert result.observed_activity_count == 1
    assert result.observed_total_usd == 44.83
    assert result.verdict == "within_tolerance"


def test_fee_activities_from_other_days_are_ignored() -> None:
    result = _reconcile(activities=(_fee("yesterday", -44.83, DAY_START_MS - 1), _fee("no-date", -44.83, None)))

    assert result.observed_activity_count == 0
    assert result.verdict == "pending"  # nothing observed for this date; still inside the posting grace


def test_uncovered_read_is_unobserved_even_inside_the_grace_period() -> None:
    """An incomplete read cannot prove "not posted yet" any more than it can prove "no fills"."""
    result = _reconcile(now_ms=DAY_END_MS + FEE_POSTING_GRACE_MS - 1)

    assert result.verdict == "unobserved"
    assert result.observed_total_usd is None


def test_pending_inside_the_grace_period_with_coverage() -> None:
    result = _reconcile(activities=(_older(),), now_ms=DAY_END_MS + FEE_POSTING_GRACE_MS - 1)

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


def test_no_fills_and_no_activities_at_all_is_unobserved_not_no_fills() -> None:
    """An incomplete read cannot prove "no fills posted" either; a young account also lands here."""
    result = _reconcile(fills=())

    assert result.verdict == "unobserved"
    assert result.predicted is not None
    assert result.predicted.total_usd == 0.0


def test_no_fills_with_a_covering_activity_is_no_fills() -> None:
    result = _reconcile(fills=(), activities=(_older(),))

    assert result.verdict == "no_fills"


def test_fees_observed_with_no_fills_is_drift() -> None:
    result = _reconcile(fills=(), activities=(_older(), _fee("f1", -1.00)))

    assert result.verdict == "drift"
    assert result.delta_usd == 1.0


def _unpinned_2025(activities=()):
    open_2025 = session_open_ms_utc(date(2025, 6, 2))
    return reconcile_session_fees(
        broker="alpaca",
        account_id="123456789",
        session_open_ms=open_2025,
        fills=(SessionFill(side=OrderSide.SELL, quantity=D("1"), fill_price=D("1")),),
        fee_activities=activities,
        now_ms=open_2025,
    )


def test_rate_unpinned_session_has_no_prediction() -> None:
    result = _unpinned_2025()

    assert result.verdict == "rate_unpinned"
    assert result.predicted is None
    assert result.unpinned_components == ["cat"]
    assert result.tolerance_usd is None


def test_rate_unpinned_still_reports_what_alpaca_charged() -> None:
    """An unpredictable session is not an unobservable one: the charge is still fact."""
    day_start_2025 = et_midnight_ms(date(2025, 6, 2))
    charged = (
        _older(occurred_at_ms=day_start_2025 - 1),
        _fee("f1", -0.75, day_start_2025),
        _fee("f2", -0.20, day_start_2025),
    )

    result = _unpinned_2025(activities=charged)

    assert result.verdict == "rate_unpinned"
    assert result.predicted is None
    assert result.observed_total_usd == 0.95
    assert result.observed_activity_count == 2


def test_rate_unpinned_without_coverage_withholds_the_observed_total() -> None:
    """Coverage gates the rate_unpinned arm's claim too: it just can't withhold a prediction."""
    day_start_2025 = et_midnight_ms(date(2025, 6, 2))
    charged = (_fee("f1", -0.75, day_start_2025),)

    result = _unpinned_2025(activities=charged)

    assert result.verdict == "rate_unpinned"
    assert result.observed_total_usd is None
    assert "cannot be shown to be complete" in result.why


class _Port:
    def __init__(self) -> None:
        self.calls: list[int | None] = []

    async def list_activities(self, *, after_ms: int | None = None, limit: int = 100) -> list[BrokerActivity]:
        self.calls.append(after_ms)
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


async def test_facade_reads_the_account_window_and_an_activity_span_it_can_bound(
    tmp_path: Path,
) -> None:
    """The activity read must be able to reach before the window it must cover."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-FEE-RECON", artifacts_root=tmp_path)
    port = _Port()
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="sqlite",
            clerk=SqliteAlpacaClerkFacade(
                account_mode="paper",
                repo=repo,
                read=port,  # type: ignore[arg-type]
                trade=port,  # type: ignore[arg-type]
            ),
        )
    )
    try:
        result = await session_fee_reconciliation(
            broker="alpaca",
            port=port,
            session_open_ms=SESSION_OPEN_MS,
            now_ms=DAY_END_MS,
        )
    finally:
        set_active_clerk_runtime(None)
        repo.close()

    assert result.account_id == "PA-FEE-RECON"
    # The fake port returns no activities at all, so the read never demonstrates it
    # reached past the window start: "unobserved", not "no_fills" (I2 gates that
    # claim on coverage too).
    assert (result.fill_count, result.verdict) == (0, "unobserved")
    assert port.calls == [0]


async def test_facade_reports_unavailable_when_the_reader_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader refusal (row-count guard, malformed row) becomes ``unavailable``, not a 500."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-FEE-RECON", artifacts_root=tmp_path)
    port = _Port()
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="sqlite",
            clerk=SqliteAlpacaClerkFacade(
                account_mode="paper",
                repo=repo,
                read=port,  # type: ignore[arg-type]
                trade=port,  # type: ignore[arg-type]
            ),
        )
    )

    def _refuse(clerk: SqliteAlpacaClerkFacade, *, from_ms: int, to_ms: int) -> list[SessionFill]:
        raise EconomicProjectionUnavailable("SQLite session fill window limit exceeded.")

    monkeypatch.setattr("app.services.alpaca_fee_reconciliation._read_session_fills", _refuse)
    try:
        result = await session_fee_reconciliation(
            broker="alpaca",
            port=port,
            session_open_ms=SESSION_OPEN_MS,
            now_ms=DAY_END_MS,
        )
    finally:
        set_active_clerk_runtime(None)
        repo.close()

    assert result.verdict == "unavailable"
    assert result.account_id == "PA-FEE-RECON"
    assert "SQLite session fill window limit exceeded." in result.why
    assert port.calls == []
