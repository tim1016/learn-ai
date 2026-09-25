"""An operator's recovery reduction is shaped from the current instant (#2007).

Regular session: the market DAY leg every EXIT has always been. The broker's
declared PRE/POST window: an extended-hours DAY limit the operator confirms
against IBKR's live bid and ask, inside a band of twice the sealed exit
allowance through the book. Anything else: a typed refusal naming when the
next session opens -- never a market order the vendor queues to 09:30.
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
    ConfirmedRecoveryShape,
    ExtendedLimitProposal,
    RegularSessionReduction,
    evaluate_proposed_limit,
    price_automatic_recovery_reduction,
    price_recovery_reduction,
    quote_spread_bps,
    realized_slippage_bps,
    realized_slippage_cost,
    recovery_reduction_shape,
    reducing_leg_session_end_ms,
    reducing_leg_verdict,
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

    # suggested: floor_tick(100.00 × (1 − 20 / 10⁴)) = 99.80
    # band:      floor_tick(100.00 × (1 − 2 × 20 / 10⁴)) = 99.60
    assert pricing == ExtendedLimitProposal(
        phase="PRE",
        side=OrderSide.SELL,
        quote=quote,
        exit_allowance_bps=Decimal("20"),
        suggested_limit_price=Decimal("99.80"),
        band_limit_price=Decimal("99.60"),
    )


def test_after_hours_buy_to_cover_suggests_the_ask_plus_the_allowance() -> None:
    now = _at(17)
    quote = _quote(bid=50.00, ask=50.01, observed_at_ms=now)

    pricing = price_recovery_reduction(side=OrderSide.BUY, now_ms=now, policy=_POLICY, quote=quote)

    # suggested: ceil_tick(50.01 × (1 + 20 / 10⁴)) = ceil_tick(50.11002) = 50.12
    # band:      ceil_tick(50.01 × (1 + 40 / 10⁴)) = ceil_tick(50.21004) = 50.22
    assert isinstance(pricing, ExtendedLimitProposal)
    assert (pricing.phase, pricing.suggested_limit_price, pricing.band_limit_price) == (
        "POST",
        Decimal("50.12"),
        Decimal("50.22"),
    )


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


@pytest.mark.parametrize(
    ("now_ms", "reason_code", "opens_at_ms"),
    [
        # 07:00 and 16:01: the calendar's pre-market / after-hours is open, so
        # "no session is open" would be false (#2440 review) — what is
        # missing is a window to price an extended-hours limit in.
        (_at(7), "EXTENDED_HOURS_PRICING_UNAVAILABLE", _at(9, 30)),
        (_at(16, 1), "EXTENDED_HOURS_PRICING_UNAVAILABLE", _at(9, 30, day=date(2026, 9, 3))),
        # 21:00: nothing is scheduled, so no session is open.
        (_at(21), "NO_SESSION_OPEN", _at(9, 30, day=date(2026, 9, 3))),
    ],
)
def test_an_authority_with_no_extended_window_waits_for_the_regular_open(
    now_ms: int, reason_code: str, opens_at_ms: int
) -> None:
    with pytest.raises(ProgramLegRefused) as excinfo:
        price_recovery_reduction(side=OrderSide.SELL, now_ms=now_ms, policy=ProgramLegPolicy.regular_only(), quote=None)

    assert _refusal(excinfo) == reason_code
    assert excinfo.value.refusal.available_at_ms == opens_at_ms


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


def _shape(
    now_ms: int,
    confirmed: ConfirmedRecoveryLimit | None,
    *,
    side: OrderSide = OrderSide.SELL,
    quote: TopOfBookQuote | None = None,
    no_live_quote: bool = False,
    policy: ProgramLegPolicy = _POLICY,
) -> ConfirmedRecoveryShape | None:
    """The shape at ``now_ms``; the Clerk's live quote defaults to one read at ``now_ms``."""
    current = None if no_live_quote else quote or _quote(observed_at_ms=now_ms)
    return recovery_reduction_shape(
        side=side,
        symbol="SPY",
        quantity=10.0,
        now_ms=now_ms,
        policy=policy,
        confirmed=confirmed,
        current_quote=current,
    )


def _limit(price: str, *, observed_at_ms: int) -> ConfirmedRecoveryLimit:
    return ConfirmedRecoveryLimit(limit_price=Decimal(price), quote_observed_at_ms=observed_at_ms)


def _refused(now_ms: int, confirmed: ConfirmedRecoveryLimit | None, **kwargs: object) -> ProgramLegRefused:
    with pytest.raises(ProgramLegRefused) as excinfo:
        _shape(now_ms, confirmed, **kwargs)  # type: ignore[arg-type]
    return excinfo.value


def test_regular_session_confirmation_is_the_market_day_default() -> None:
    assert _shape(_at(10), None) is None


def test_a_pre_market_limit_is_valid_until_the_regular_open_and_keeps_its_reference_quote() -> None:
    now = _at(7)
    current = _quote(observed_at_ms=now)

    confirmed = _shape(now, _limit("99.95", observed_at_ms=now - 4_000), quote=current)

    assert confirmed is not None
    leg = confirmed.shape.apply(symbol="SPY", quantity=10.0)
    assert (leg.side, leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderSide.SELL,
        OrderType.LIMIT,
        TimeInForce.DAY,
        99.95,
        True,
    )
    assert confirmed.valid_until_ms == _at(9, 30)
    assert confirmed.reference_quote == current


def test_an_after_hours_limit_is_valid_until_the_declared_close() -> None:
    now = _at(17)

    confirmed = _shape(now, _limit("99.95", observed_at_ms=now))

    assert confirmed is not None and confirmed.valid_until_ms == _at(20)


@pytest.mark.parametrize("age_ms", [RECOVERY_QUOTE_MAX_AGE_MS + 1, -1])
def test_a_confirmation_against_a_stale_or_future_quote_asks_for_a_refresh(age_ms: int) -> None:
    now = _at(7)

    refused = _refused(now, _limit("99.95", observed_at_ms=now - age_ms))

    assert refused.reason_code == "RECOVERY_QUOTE_STALE"


def test_a_confirmed_quote_newer_than_any_the_clerk_holds_is_refused() -> None:
    """The client cannot vouch for freshness the server cannot see."""
    now = _at(7)

    refused = _refused(
        now,
        _limit("99.95", observed_at_ms=now - 1_000),
        quote=_quote(observed_at_ms=now - 2_000),
    )

    assert refused.reason_code == "RECOVERY_QUOTE_STALE"


def test_a_quote_exactly_at_the_bound_is_still_confirmable() -> None:
    now = _at(7)

    assert _shape(now, _limit("99.95", observed_at_ms=now - RECOVERY_QUOTE_MAX_AGE_MS)) is not None


def test_sending_needs_a_live_quote_now() -> None:
    now = _at(7)

    refused = _refused(now, _limit("99.95", observed_at_ms=now), no_live_quote=True)

    assert refused.reason_code == "RECOVERY_QUOTE_UNAVAILABLE"


def test_extended_session_without_a_confirmed_limit_refuses() -> None:
    assert _refused(_at(7), None).reason_code == "RECOVERY_LIMIT_REQUIRED"


def test_extended_session_without_allowances_cannot_bound_a_confirmed_limit() -> None:
    now = _at(7)

    refused = _refused(
        now,
        _limit("99.95", observed_at_ms=now),
        policy=ProgramLegPolicy(window=_WINDOW, allowances=None),
    )

    assert refused.reason_code == "EXTENDED_HOURS_ALLOWANCE_UNSET"


def test_a_limit_confirmed_before_the_open_is_refused_once_the_regular_session_starts() -> None:
    now = _at(9, 30)

    refused = _refused(now, _limit("99.95", observed_at_ms=now - 1_000))

    assert refused.reason_code == "RECOVERY_SESSION_CHANGED"


def test_confirming_while_no_session_is_open_refuses_with_the_next_open() -> None:
    now = _at(20)

    refused = _refused(now, _limit("99.95", observed_at_ms=now - 1_000))

    assert refused.reason_code == "NO_SESSION_OPEN"
    assert refused.refusal.available_at_ms == _at(4, day=date(2026, 9, 3))


@pytest.mark.parametrize("price", ["99.955", "0"])
def test_a_price_alpaca_would_reject_is_refused_before_the_exit_is_accepted(price: str) -> None:
    now = _at(7)

    refused = _refused(now, _limit(price, observed_at_ms=now))

    assert refused.reason_code == "RECOVERY_LIMIT_PRICE_INVALID"


# --- the band: how far through the book a confirmed limit may go -----------


def test_a_sell_limit_at_the_band_is_accepted_and_one_tick_past_it_refused() -> None:
    """Band = 2 × 20 bps below the 100.00 bid = 99.60 (owner decision 2026-09-19)."""
    now = _at(7)

    assert _shape(now, _limit("99.60", observed_at_ms=now)) is not None
    assert _refused(now, _limit("99.59", observed_at_ms=now)).reason_code == "RECOVERY_LIMIT_OUTSIDE_BAND"


def test_a_fat_fingered_sell_cannot_sweep_the_book() -> None:
    now = _at(7)

    assert _refused(now, _limit("0.01", observed_at_ms=now)).reason_code == "RECOVERY_LIMIT_OUTSIDE_BAND"


def test_a_cover_above_the_band_over_the_ask_is_refused() -> None:
    """Band = 2 × 20 bps above the 100.05 ask = ceil_tick(100.4502) = 100.46."""
    now = _at(7)

    assert _shape(now, _limit("100.46", observed_at_ms=now), side=OrderSide.BUY) is not None
    refused = _refused(now, _limit("100.47", observed_at_ms=now), side=OrderSide.BUY)
    assert refused.reason_code == "RECOVERY_LIMIT_OUTSIDE_BAND"


def test_a_sell_limit_above_the_bid_is_not_a_slippage_risk_and_is_accepted() -> None:
    now = _at(7)

    assert _shape(now, _limit("101.00", observed_at_ms=now)) is not None


# --- the one send-now rule every recovery leg passes -----------------------


@pytest.mark.parametrize(
    ("now_ms", "verdict"),
    [(_at(10), "send"), (_at(7), "wait"), (_at(17), "wait"), (_at(21), "wait")],
)
def test_a_market_recovery_leg_goes_out_only_inside_the_regular_session(now_ms: int, verdict: str) -> None:
    assert reducing_leg_verdict(extended_hours=False, valid_until_ms=None, now_ms=now_ms) == verdict


@pytest.mark.parametrize(
    ("now_ms", "valid_until_ms", "verdict"),
    [
        (_at(9, 29), _at(9, 30), "send"),
        (_at(9, 30), _at(9, 30), "expired"),
        (_at(21), _at(20), "expired"),
        (_at(7), None, "expired"),
    ],
)
def test_a_confirmed_limit_is_never_sent_past_the_session_it_was_priced_in(
    now_ms: int, valid_until_ms: int | None, verdict: str
) -> None:
    assert reducing_leg_verdict(extended_hours=True, valid_until_ms=valid_until_ms, now_ms=now_ms) == verdict


_HALF_DAY = date(2026, 11, 27)  # the day after Thanksgiving: the regular close is 13:00
_SATURDAY = date(2026, 9, 5)


@pytest.mark.parametrize(
    ("extended_hours", "valid_until_ms", "sent_at_ms", "ends_at_ms"),
    [
        pytest.param(False, None, _at(9, 29) + 56_000, _at(16), id="market-sent-in-the-guard-band-before-the-open"),
        pytest.param(False, None, _at(10, day=_HALF_DAY), _at(13, day=_HALF_DAY), id="market-on-a-half-day"),
        pytest.param(False, None, _at(10, day=_SATURDAY), None, id="market-on-a-day-with-no-session"),
        pytest.param(True, _at(9, 30), _at(7), _at(20), id="pre-market-limit-works-to-the-after-hours-close"),
        pytest.param(True, _at(20), _at(10, day=_SATURDAY), _at(20), id="limit-on-a-day-with-no-session"),
    ],
)
def test_a_sent_reducing_leg_ends_when_the_broker_stops_working_it(
    extended_hours: bool, valid_until_ms: int | None, sent_at_ms: int, ends_at_ms: int | None
) -> None:
    """#2440 review: a market leg sent at 09:29:56 reaches the broker in the regular session.

    It is sent on purpose inside the 5 s guard band before the open, so the
    instant it was sent is pre-market; its end is still that day's regular
    close, read from the calendar day rather than the send instant's phase.
    """
    assert (
        reducing_leg_session_end_ms(
            extended_hours=extended_hours, valid_until_ms=valid_until_ms, sent_at_ms=sent_at_ms
        )
        == ends_at_ms
    )


# --- realized slippage ------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "reference", "fill", "expected_bps"),
    [
        (OrderSide.SELL, 100.0, 99.9, 10.0),
        (OrderSide.SELL, 100.0, 100.1, -10.0),
        (OrderSide.BUY, 50.0, 50.05, 10.0),
        (OrderSide.BUY, 50.0, 49.95, -10.0),
    ],
)
def test_realized_slippage_is_positive_when_the_fill_is_worse_than_the_reference(
    side: OrderSide, reference: float, fill: float, expected_bps: float
) -> None:
    assert realized_slippage_bps(side=side, reference_price=reference, fill_price=fill) == pytest.approx(
        expected_bps, abs=1e-9, rel=0
    )


def test_realized_slippage_needs_a_positive_reference() -> None:
    with pytest.raises(ValueError, match="reference_price"):
        realized_slippage_bps(side=OrderSide.SELL, reference_price=0.0, fill_price=1.0)


def test_realized_slippage_cost_is_the_same_difference_in_dollars() -> None:
    cost = realized_slippage_cost(
        side=OrderSide.SELL, reference_price=100.0, fill_price=99.9, quantity=10.0
    )
    assert cost == pytest.approx(1.0, abs=1e-9, rel=0)
    # A fill better than the reference gives the operator money back.
    assert realized_slippage_cost(
        side=OrderSide.BUY, reference_price=50.0, fill_price=49.95, quantity=20.0
    ) == pytest.approx(-1.0, abs=1e-9, rel=0)


# --- what a proposed price does against the quote ---------------------------


def _proposal(side: OrderSide = OrderSide.SELL, **quote_fields) -> ExtendedLimitProposal:
    return ExtendedLimitProposal(
        phase="PRE",
        side=side,
        quote=TopOfBookQuote(
            symbol="SPY",
            bid=quote_fields.pop("bid", 100.00),
            ask=quote_fields.pop("ask", 100.05),
            source="ibkr.market_data.status",
            observed_at_ms=_at(7),
            **quote_fields,
        ),
        exit_allowance_bps=Decimal("20"),
        suggested_limit_price=Decimal("99.80") if side is OrderSide.SELL else Decimal("100.25"),
        band_limit_price=Decimal("99.60") if side is OrderSide.SELL else Decimal("100.45"),
    )


def test_quote_spread_is_measured_in_bps_of_the_mid() -> None:
    assert quote_spread_bps(_proposal().quote) == pytest.approx(4.99875, abs=1e-5, rel=0)


def test_a_price_through_the_touch_costs_at_most_the_whole_reach() -> None:
    evaluation = evaluate_proposed_limit(
        proposal=_proposal(), limit_price=Decimal("99.70"), quantity=10
    )

    # $0.30 below a $100.00 bid, on ten shares.
    assert evaluation.through_book_bps == pytest.approx(30.0, abs=1e-9, rel=0)
    assert evaluation.worst_case_cost == pytest.approx(3.0, abs=1e-9, rel=0)
    assert (evaluation.resting, evaluation.outside_band) == (False, False)


def test_a_price_behind_the_touch_rests_and_costs_nothing_to_reach() -> None:
    evaluation = evaluate_proposed_limit(
        proposal=_proposal(), limit_price=Decimal("100.20"), quantity=10
    )

    assert evaluation.through_book_bps == pytest.approx(-20.0, abs=1e-9, rel=0)
    assert evaluation.resting is True
    # Nothing is given up reaching through a book this price never reaches.
    assert evaluation.worst_case_cost == 0.0


def test_a_price_past_the_band_and_a_book_thinner_than_the_order_are_flagged() -> None:
    evaluation = evaluate_proposed_limit(
        proposal=_proposal(bid_size=4), limit_price=Decimal("99.50"), quantity=10
    )

    assert (evaluation.outside_band, evaluation.thin_book) == (True, True)


def test_a_cover_is_measured_against_the_ask() -> None:
    evaluation = evaluate_proposed_limit(
        proposal=_proposal(OrderSide.BUY, ask_size=50), limit_price=Decimal("100.35"), quantity=10
    )

    # $0.30 above a $100.05 ask, and the book is deeper than the order.
    assert evaluation.through_book_bps == pytest.approx(29.985, abs=1e-3, rel=0)
    assert evaluation.worst_case_cost == pytest.approx(3.0, abs=1e-9, rel=0)
    assert (evaluation.outside_band, evaluation.thin_book) == (False, False)


# --- automatic pricing: the watchdog's extended-hours re-drive (#2229) -------


def test_an_automatic_after_hours_sell_prices_the_bid_less_the_allowance() -> None:
    """The Clerk prices the re-drive itself: exactly one sealed exit allowance
    through the live touch, durable with its reference quote and session end."""
    now_ms = _at(17, 5)
    quote = _quote(observed_at_ms=now_ms)
    shape = price_automatic_recovery_reduction(
        side=OrderSide.SELL,
        symbol="SPY",
        quantity=10,
        now_ms=now_ms,
        policy=_POLICY,
        quote=quote,
    )
    assert shape.shape.order_type is OrderType.LIMIT
    assert shape.shape.time_in_force is TimeInForce.DAY
    assert shape.shape.extended_hours is True
    assert shape.shape.side is OrderSide.SELL
    # floor_tick(100.00 × (1 − 20bps)) = floor_tick(99.80).
    assert shape.shape.limit_price == 99.8
    assert shape.valid_until_ms == _at(20)
    assert shape.reference_quote == quote
    assert shape.quantity == 10
    assert shape.priced_by == "clerk"


def test_an_automatic_pre_market_cover_prices_the_ask_plus_the_allowance() -> None:
    now_ms = _at(7, 30)
    shape = price_automatic_recovery_reduction(
        side=OrderSide.BUY,
        symbol="SPY",
        quantity=-10,
        now_ms=now_ms,
        policy=_POLICY,
        quote=_quote(observed_at_ms=now_ms),
    )
    # ceil_tick(100.05 × (1 + 20bps)) = ceil_tick(100.2501) = 100.26.
    assert shape.shape.limit_price == 100.26
    assert shape.shape.side is OrderSide.BUY
    assert shape.valid_until_ms == _at(9, 30)
    assert shape.quantity == 10


def test_an_automatic_price_answers_none_inside_the_regular_session() -> None:
    """Inside 09:30–16:00 the re-drive is the market DAY leg a shapeless
    recovery EXIT already builds — ``None`` is that same fact, so the two
    session notions cannot disagree into a bogus refusal."""
    assert (
        price_automatic_recovery_reduction(
            side=OrderSide.SELL,
            symbol="SPY",
            quantity=10,
            now_ms=_at(10),
            policy=_POLICY,
            quote=_quote(observed_at_ms=_at(10)),
        )
        is None
    )


def test_an_automatic_price_refuses_without_a_live_quote() -> None:
    with pytest.raises(ProgramLegRefused) as excinfo:
        price_automatic_recovery_reduction(
            side=OrderSide.SELL,
            symbol="SPY",
            quantity=10,
            now_ms=_at(17),
            policy=_POLICY,
            quote=None,
        )
    assert _refusal(excinfo) == "RECOVERY_QUOTE_UNAVAILABLE"


def test_an_automatic_price_refuses_when_the_spread_is_past_the_cap() -> None:
    """#2229: a broken book is not priced against. The gate is automatic-path
    only — the operator's ticket shows the wide spread and can override."""
    now_ms = _at(17)
    # 100.00 / 100.60 ask: (0.60 / 100.30) × 10⁴ ≈ 59.8 bps, past the default 50.
    wide = _quote(bid=100.00, ask=100.60, observed_at_ms=now_ms)
    with pytest.raises(ProgramLegRefused) as excinfo:
        price_automatic_recovery_reduction(
            side=OrderSide.SELL,
            symbol="SPY",
            quantity=10,
            now_ms=now_ms,
            policy=_POLICY,
            quote=wide,
        )
    assert _refusal(excinfo) == "RECOVERY_SPREAD_TOO_WIDE"
    # The same book is priceable when the cap is widened past it.
    wide_cap = ProgramLegPolicy(
        window=_WINDOW,
        allowances=ExtendedHoursAllowances(
            entry_bps=Decimal("10"),
            exit_bps=Decimal("20"),
            exit_spread_cap_bps=Decimal("100"),
        ),
    )
    assert (
        price_automatic_recovery_reduction(
            side=OrderSide.SELL,
            symbol="SPY",
            quantity=10,
            now_ms=now_ms,
            policy=wide_cap,
            quote=wide,
        )
        is not None
    )


def test_an_automatic_price_refuses_without_allowances() -> None:
    with pytest.raises(ProgramLegRefused) as excinfo:
        price_automatic_recovery_reduction(
            side=OrderSide.SELL,
            symbol="SPY",
            quantity=10,
            now_ms=_at(17),
            policy=ProgramLegPolicy(window=_WINDOW, allowances=None),
            quote=_quote(observed_at_ms=_at(17)),
        )
    assert _refusal(excinfo) == "EXTENDED_HOURS_ALLOWANCE_UNSET"


def test_an_automatic_price_refuses_when_no_session_is_open() -> None:
    with pytest.raises(ProgramLegRefused) as excinfo:
        price_automatic_recovery_reduction(
            side=OrderSide.SELL,
            symbol="SPY",
            quantity=10,
            now_ms=_at(21),
            policy=_POLICY,
            quote=_quote(observed_at_ms=_at(21)),
        )
    assert _refusal(excinfo) == "NO_SESSION_OPEN"
    # 21:00 Wednesday: the next session is Thursday's pre-market.
    assert excinfo.value.refusal.available_at_ms == _at(4, day=date(2026, 9, 3))


def test_a_wider_deploy_band_multiple_accepts_a_price_the_default_refuses() -> None:
    """#2229: ALPACA_LIVE_XH_EXIT_BAND_MULTIPLE widens the band a confirmed
    limit must stay inside; the suggested price and the allowance are
    untouched by it."""
    now_ms = _at(17)
    quote = _quote(observed_at_ms=now_ms)
    wide = ProgramLegPolicy(
        window=_WINDOW,
        allowances=ExtendedHoursAllowances(
            entry_bps=Decimal("10"), exit_bps=Decimal("20"), exit_band_multiple=Decimal(4)
        ),
    )
    # 60 bps through a 100.00 bid: past the default 2×20 bps band...
    with pytest.raises(ProgramLegRefused) as excinfo:
        recovery_reduction_shape(
            side=OrderSide.SELL,
            symbol="SPY",
            quantity=10,
            now_ms=now_ms,
            policy=_POLICY,
            confirmed=ConfirmedRecoveryLimit(
                limit_price=Decimal("99.40"), quote_observed_at_ms=now_ms
            ),
            current_quote=quote,
        )
    assert _refusal(excinfo) == "RECOVERY_LIMIT_OUTSIDE_BAND"
    # ...and inside the 4× one.
    assert (
        recovery_reduction_shape(
            side=OrderSide.SELL,
            symbol="SPY",
            quantity=10,
            now_ms=now_ms,
            policy=wide,
            confirmed=ConfirmedRecoveryLimit(
                limit_price=Decimal("99.40"), quote_observed_at_ms=now_ms
            ),
            current_quote=quote,
        )
        is not None
    )
    # The proposal's band widens with it; the suggestion does not.
    narrow_proposal = price_recovery_reduction(
        side=OrderSide.SELL, now_ms=now_ms, policy=_POLICY, quote=quote
    )
    wide_proposal = price_recovery_reduction(
        side=OrderSide.SELL, now_ms=now_ms, policy=wide, quote=quote
    )
    assert isinstance(narrow_proposal, ExtendedLimitProposal)
    assert isinstance(wide_proposal, ExtendedLimitProposal)
    assert wide_proposal.band_limit_price < narrow_proposal.band_limit_price
    assert wide_proposal.suggested_limit_price == narrow_proposal.suggested_limit_price


def test_a_sell_band_past_one_hundred_percent_is_unbounded_not_unpriceable() -> None:
    """PR #2230 review: an accepted configuration (20 % exit allowance, 10×
    band) must not make a sell flatten unpriceable — the band floors to zero,
    which is not a price, so it is treated as having no lower bound."""
    from decimal import Decimal as _Decimal

    extreme = ProgramLegPolicy(
        window=_WINDOW,
        allowances=ExtendedHoursAllowances(
            entry_bps=_Decimal("10"),
            exit_bps=_Decimal("2000"),
            exit_band_multiple=_Decimal("10"),
        ),
    )
    now_ms = _at(17)
    quote = _quote(observed_at_ms=now_ms)
    pricing = price_recovery_reduction(
        side=OrderSide.SELL, now_ms=now_ms, policy=extreme, quote=quote
    )
    assert isinstance(pricing, ExtendedLimitProposal)
    assert pricing.suggested_limit_price == _Decimal("80.00")
    assert pricing.band_limit_price is None  # 200 % of the touch is not a price

    # Any positive limit is inside an unbounded band, and the evaluation
    # agrees.
    deep = _Decimal("5.00")
    assert (
        recovery_reduction_shape(
            side=OrderSide.SELL,
            symbol="SPY",
            quantity=10,
            now_ms=now_ms,
            policy=extreme,
            confirmed=ConfirmedRecoveryLimit(
                limit_price=deep, quote_observed_at_ms=now_ms
            ),
            current_quote=quote,
        )
        is not None
    )
    evaluation = evaluate_proposed_limit(proposal=pricing, limit_price=deep, quantity=10)
    assert evaluation.outside_band is False
