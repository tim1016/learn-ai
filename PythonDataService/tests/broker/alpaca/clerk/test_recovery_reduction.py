"""An operator's recovery reduction is shaped from the current instant (#2007).

Regular session: the market DAY leg every EXIT has always been. The broker's
declared PRE/POST window: an extended-hours DAY limit the operator confirms
against IBKR's live bid and ask. Anything else: a typed refusal naming when
the next session opens -- never a market order the vendor queues to 09:30.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy, ProgramLegRefused
from app.broker.alpaca.clerk.recovery_reduction import (
    RECOVERY_QUOTE_MAX_AGE_MS,
    ConfirmedRecoveryLimit,
    ExtendedLimitProposal,
    RegularSessionReduction,
    price_recovery_reduction,
    recovery_reduction_shape,
)
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import OrderSide, OrderType, TimeInForce
from app.schemas.market_liveness import TopOfBookQuote
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")
_WEDNESDAY = date(2026, 9, 2)
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_POLICY = ProgramLegPolicy(
    window=_WINDOW,
    allowances=ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20")),
)


def _at(hour: int, minute: int = 0, *, day: date = _WEDNESDAY) -> int:
    return to_ms_utc(datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET))


def _quote(*, bid: float = 100.00, ask: float = 100.05, observed_at_ms: int) -> TopOfBookQuote:
    return TopOfBookQuote(
        symbol="SPY",
        bid=bid,
        ask=ask,
        source="ibkr.market_data.status",
        observed_at_ms=observed_at_ms,
    )


def _refusal(excinfo: pytest.ExceptionInfo[ProgramLegRefused]) -> str:
    return excinfo.value.reason_code


# --- presentation: what the operator is shown before confirming -------------


def test_regular_session_needs_no_quote_and_reduces_market_day() -> None:
    pricing = price_recovery_reduction(side=OrderSide.SELL, now_ms=_at(10), policy=_POLICY, quote=None)

    assert pricing == RegularSessionReduction()


def test_pre_market_sell_suggests_the_bid_less_the_sealed_exit_allowance() -> None:
    now = _at(7)
    quote = _quote(observed_at_ms=now)

    pricing = price_recovery_reduction(side=OrderSide.SELL, now_ms=now, policy=_POLICY, quote=quote)

    # floor_tick(100.00 × (1 − 20 / 10⁴)) = 99.80
    assert pricing == ExtendedLimitProposal(
        phase="PRE",
        side=OrderSide.SELL,
        quote=quote,
        exit_allowance_bps=Decimal("20"),
        suggested_limit_price=Decimal("99.80"),
    )


def test_after_hours_buy_to_cover_suggests_the_ask_plus_the_allowance() -> None:
    now = _at(17)
    quote = _quote(bid=50.00, ask=50.01, observed_at_ms=now)

    pricing = price_recovery_reduction(side=OrderSide.BUY, now_ms=now, policy=_POLICY, quote=quote)

    # ceil_tick(50.01 × (1 + 20 / 10⁴)) = ceil_tick(50.11002) = 50.12
    assert isinstance(pricing, ExtendedLimitProposal)
    assert (pricing.phase, pricing.suggested_limit_price) == ("POST", Decimal("50.12"))


@pytest.mark.parametrize(
    ("now_ms", "opens_at_ms"),
    [
        # 21:00 Wednesday: the next session is Thursday's pre-market.
        (_at(21), _at(4, day=date(2026, 9, 3))),
        # 02:00 Thursday: that same morning's pre-market.
        (_at(2, day=date(2026, 9, 3)), _at(4, day=date(2026, 9, 3))),
        # Saturday: Monday's pre-market (2026-09-07 is Labor Day, so Tuesday).
        (_at(12, day=date(2026, 9, 5)), _at(4, day=date(2026, 9, 8))),
    ],
)
def test_no_open_session_refuses_and_names_the_next_open(now_ms: int, opens_at_ms: int) -> None:
    with pytest.raises(ProgramLegRefused) as excinfo:
        price_recovery_reduction(side=OrderSide.SELL, now_ms=now_ms, policy=_POLICY, quote=None)

    assert _refusal(excinfo) == "NO_SESSION_OPEN"
    assert excinfo.value.refusal.available_at_ms == opens_at_ms


def test_an_authority_with_no_extended_window_waits_for_the_regular_open() -> None:
    with pytest.raises(ProgramLegRefused) as excinfo:
        price_recovery_reduction(side=OrderSide.SELL, now_ms=_at(7), policy=ProgramLegPolicy.regular_only(), quote=None)

    assert _refusal(excinfo) == "NO_SESSION_OPEN"
    assert excinfo.value.refusal.available_at_ms == _at(9, 30)


def test_extended_session_without_a_live_quote_refuses_rather_than_guessing() -> None:
    with pytest.raises(ProgramLegRefused) as excinfo:
        price_recovery_reduction(side=OrderSide.SELL, now_ms=_at(7), policy=_POLICY, quote=None)

    assert _refusal(excinfo) == "RECOVERY_QUOTE_UNAVAILABLE"


def test_extended_session_without_allowances_refuses() -> None:
    now = _at(7)
    policy = ProgramLegPolicy(window=_WINDOW, allowances=None)

    with pytest.raises(ProgramLegRefused) as excinfo:
        price_recovery_reduction(side=OrderSide.SELL, now_ms=now, policy=policy, quote=_quote(observed_at_ms=now))

    assert _refusal(excinfo) == "EXTENDED_HOURS_ALLOWANCE_UNSET"


def test_a_sub_penny_bid_that_prices_to_zero_is_a_typed_refusal() -> None:
    now = _at(7)

    with pytest.raises(ProgramLegRefused) as excinfo:
        price_recovery_reduction(
            side=OrderSide.SELL,
            now_ms=now,
            policy=_POLICY,
            quote=_quote(bid=0.0001, ask=0.0002, observed_at_ms=now),
        )

    assert _refusal(excinfo) == "RECOVERY_QUOTE_UNPRICEABLE"


# --- execution: what the operator's confirmation turns into -----------------


def _shape(now_ms: int, confirmed: ConfirmedRecoveryLimit | None, *, policy: ProgramLegPolicy = _POLICY):
    return recovery_reduction_shape(
        side=OrderSide.SELL,
        symbol="SPY",
        quantity=10.0,
        now_ms=now_ms,
        policy=policy,
        confirmed=confirmed,
    )


def test_regular_session_confirmation_is_the_market_day_default() -> None:
    assert _shape(_at(10), None) is None


def test_extended_session_confirmation_is_an_extended_day_limit_at_the_operators_price() -> None:
    now = _at(7)
    confirmed = ConfirmedRecoveryLimit(limit_price=Decimal("99.95"), quote_observed_at_ms=now - 4_000)

    shape = _shape(now, confirmed)

    assert shape is not None
    leg = shape.apply(symbol="SPY", quantity=10.0)
    assert (leg.side, leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderSide.SELL,
        OrderType.LIMIT,
        TimeInForce.DAY,
        99.95,
        True,
    )


@pytest.mark.parametrize("age_ms", [RECOVERY_QUOTE_MAX_AGE_MS + 1, -1])
def test_a_confirmation_against_a_stale_or_future_quote_asks_for_a_refresh(age_ms: int) -> None:
    now = _at(7)
    confirmed = ConfirmedRecoveryLimit(limit_price=Decimal("99.95"), quote_observed_at_ms=now - age_ms)

    with pytest.raises(ProgramLegRefused) as excinfo:
        _shape(now, confirmed)

    assert _refusal(excinfo) == "RECOVERY_QUOTE_STALE"


def test_a_quote_exactly_at_the_bound_is_still_confirmable() -> None:
    now = _at(7)
    confirmed = ConfirmedRecoveryLimit(
        limit_price=Decimal("99.95"), quote_observed_at_ms=now - RECOVERY_QUOTE_MAX_AGE_MS
    )

    assert _shape(now, confirmed) is not None


def test_extended_session_without_a_confirmed_limit_refuses() -> None:
    with pytest.raises(ProgramLegRefused) as excinfo:
        _shape(_at(7), None)

    assert _refusal(excinfo) == "RECOVERY_LIMIT_REQUIRED"


def test_a_limit_confirmed_before_the_open_is_refused_once_the_regular_session_starts() -> None:
    now = _at(9, 30)
    confirmed = ConfirmedRecoveryLimit(limit_price=Decimal("99.95"), quote_observed_at_ms=now - 1_000)

    with pytest.raises(ProgramLegRefused) as excinfo:
        _shape(now, confirmed)

    assert _refusal(excinfo) == "RECOVERY_SESSION_CHANGED"


def test_confirming_while_no_session_is_open_refuses_with_the_next_open() -> None:
    now = _at(20)
    confirmed = ConfirmedRecoveryLimit(limit_price=Decimal("99.95"), quote_observed_at_ms=now - 1_000)

    with pytest.raises(ProgramLegRefused) as excinfo:
        _shape(now, confirmed)

    assert _refusal(excinfo) == "NO_SESSION_OPEN"
    assert excinfo.value.refusal.available_at_ms == _at(4, day=date(2026, 9, 3))


@pytest.mark.parametrize("price", ["99.955", "0"])
def test_a_price_alpaca_would_reject_is_refused_before_the_exit_is_accepted(price: str) -> None:
    now = _at(7)
    confirmed = ConfirmedRecoveryLimit(limit_price=Decimal(price), quote_observed_at_ms=now)

    with pytest.raises(ProgramLegRefused) as excinfo:
        _shape(now, confirmed)

    assert _refusal(excinfo) == "RECOVERY_LIMIT_PRICE_INVALID"
