"""What getting out of a held option position costs, through ``exit_estimate`` alone (#3100).

Every figure is worked by hand from the rule: a sold leg is bought back at its
ask and a bought leg sold at its bid; the net close is the first less the
second, positive paid; the gross buy-back is the sold legs' asks alone. A
quote's age is ``now_ms`` less the side's receipt, and the limit is the one
a held contract's mark is allowed (``HELD_MARK_MAX_AGE_MS``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from app.broker.alpaca.clerk.exit_estimate import (
    BUY_BACK_ALONE_LINE,
    CLOSE_UNREAD_LINE,
    CUTOFF_LINE,
    FEES_LINE,
    ExitEstimate,
    OwedClose,
    exit_estimate,
)
from app.broker.alpaca.clerk.kept_option_quotes import HELD_MARK_MAX_AGE_MS
from app.options.deadlines import CloseKind
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.requirement import ContractAtMultiplier
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.sqlite.conftest import NOON

# A $5-wide put spread: the 505 put is sold and the 500 put bought.
SOLD = "SPY261016P00505000"
BOUGHT = "SPY261016P00500000"
FROM = "Quotes kept for this Dry Run"
NONE_HELD = "no quote has been kept for it yet"


def _quote(
    bid: str | None, ask: str | None, *, bid_age: int = 1_000, ask_age: int | None = None,
    data_type: QuoteDataType = QuoteDataType.LIVE,
) -> HeldQuote:
    """A quote whose sides were received ``bid_age`` and ``ask_age`` milliseconds before ``NOON``."""
    ask_age = bid_age if ask_age is None else ask_age
    return HeldQuote(
        bid=None if bid is None else QuoteSide(D(bid), NOON - bid_age),
        ask=None if ask is None else QuoteSide(D(ask), NOON - ask_age),
        data_type=data_type,
        generation=1,
    )


def _estimate(
    legs: dict[ContractAtMultiplier, int], quotes: dict[str, HeldQuote | None], **changes: object,
) -> ExitEstimate:
    arguments: dict[str, object] = {"now_ms": NOON, "quotes_from": FROM, "none_held": NONE_HELD}
    arguments.update(changes)
    return exit_estimate(legs, quotes, **arguments)  # type: ignore[arg-type]


def _terms(figure: object) -> list[tuple[str, int]]:
    return [(term.label, term.amount_cents) for term in figure.terms]  # type: ignore[attr-defined]


SPREAD: dict[ContractAtMultiplier, int] = {(SOLD, 100): -1, (BOUGHT, 100): 1}
# Deep in the money: the sold 505 put is worth 100.00 and the bought 500 put 95.00.
DEEP_IN_THE_MONEY = {SOLD: _quote("99.90", "100.00"), BOUGHT: _quote("95.00", "95.10")}


# ---- the two figures --------------------------------------------------------------------------


def test_a_five_wide_spread_deep_in_the_money_closes_whole_for_500_and_needs_10000_to_buy_its_sold_leg_back() -> None:
    """The sold put is bought back at its ask, 100.00 a share: $10,000.00 for one contract of 100 shares.
    The bought put is sold at its bid, 95.00: $9,500.00 comes back. The whole close pays $500.00."""
    estimate = _estimate(SPREAD, DEEP_IN_THE_MONEY)

    assert (estimate.net_close.state, estimate.net_close.amount_cents) == ("known", 50_000)
    assert _terms(estimate.net_close) == [
        (f"Sell 1 {BOUGHT} at the bid", -950_000),
        (f"Buy back 1 {SOLD} at the ask", 1_000_000),
    ]
    assert (estimate.gross_buy_back.state, estimate.gross_buy_back.amount_cents) == ("known", 1_000_000)
    assert _terms(estimate.gross_buy_back) == [(f"Buy back 1 {SOLD} at the ask", 1_000_000)]
    assert estimate.gross_buy_back.amount_cents == 20 * estimate.net_close.amount_cents
    assert estimate.net_close.source == estimate.gross_buy_back.source == FROM


def test_each_figure_is_for_every_contract_held_at_each_contracts_own_multiplier() -> None:
    """Two structures: 2 × 100 × 100.00 bought back and 2 × 100 × 95.00 sold. A contract of 10 shares counts 10."""
    two = _estimate({(SOLD, 100): -2, (BOUGHT, 100): 2}, DEEP_IN_THE_MONEY)
    assert (two.net_close.amount_cents, two.gross_buy_back.amount_cents) == (100_000, 2_000_000)

    odd = _estimate({(SOLD, 10): -3, (BOUGHT, 100): 1}, {SOLD: _quote("2.40", "2.50"), BOUGHT: _quote("1.00", "1.10")})
    # 3 contracts × 10 shares × 2.50 = $75.00 paid; 1 × 100 × 1.00 = $100.00 received.
    assert (odd.net_close.amount_cents, odd.gross_buy_back.amount_cents) == (-2_500, 7_500)


def test_a_figure_is_as_old_as_the_oldest_side_it_used() -> None:
    quotes = {
        SOLD: _quote("99.90", "100.00", bid_age=500, ask_age=4_000),
        BOUGHT: _quote("95.00", "95.10", bid_age=9_000, ask_age=100),
    }
    estimate = _estimate(SPREAD, quotes)

    # The net close used the sold leg's ask (4 s) and the bought leg's bid (9 s); the buy-back only the ask.
    assert (estimate.net_close.as_of_ms, estimate.gross_buy_back.as_of_ms) == (NOON - 9_000, NOON - 4_000)


def test_a_bought_leg_nothing_is_bid_for_sells_for_nothing_and_the_close_is_known() -> None:
    """A far out-of-the-money bought put is bid at zero: that is its price, not a missing one."""
    estimate = _estimate({(BOUGHT, 100): 2}, {BOUGHT: _quote("0", "0.05")})

    assert (estimate.net_close.state, estimate.net_close.amount_cents) == ("known", 0)
    assert _terms(estimate.net_close) == [(f"Sell 2 {BOUGHT} at the bid", 0)]


# ---- rule 2: a missing side ---------------------------------------------------------------------


def test_a_sold_leg_with_no_ask_makes_both_figures_unknown() -> None:
    estimate = _estimate(SPREAD, {SOLD: _quote("99.90", None), BOUGHT: _quote("95.00", "95.10")})

    for figure in (estimate.net_close, estimate.gross_buy_back):
        assert (figure.state, figure.amount_cents, figure.terms) == ("unknown", None, ())
        assert f"{SOLD}: no ask has arrived" in figure.source
    leg = next(quality for quality in estimate.quotes if quality.symbol == SOLD)
    assert (leg.state, leg.missing, leg.gap) == ("unknown", ("ask",), None)


def test_a_bought_leg_with_no_bid_makes_the_net_close_unknown_and_leaves_the_buy_back_known() -> None:
    estimate = _estimate(SPREAD, {SOLD: _quote("99.90", "100.00"), BOUGHT: _quote(None, "95.10")})

    assert (estimate.net_close.state, estimate.net_close.amount_cents) == ("unknown", None)
    assert f"{BOUGHT}: no bid has arrived" in estimate.net_close.source
    assert (estimate.gross_buy_back.state, estimate.gross_buy_back.amount_cents) == ("known", 1_000_000)


def test_a_side_a_close_does_not_use_missing_is_said_and_does_not_make_a_figure_unknown() -> None:
    """The bought leg's ask is not what a sale of it fills at: its bid is there, so the close is known."""
    estimate = _estimate(SPREAD, {SOLD: _quote("99.90", "100.00"), BOUGHT: _quote("95.00", None)})

    assert estimate.net_close.state == "known"
    bought = next(quality for quality in estimate.quotes if quality.symbol == BOUGHT)
    assert (bought.state, bought.missing, bought.gap) == ("known", ("ask",), None)


def test_a_sold_leg_nothing_is_offered_for_cannot_be_bought_back_at_any_price() -> None:
    estimate = _estimate(SPREAD, {SOLD: _quote("0", "0"), BOUGHT: _quote("95.00", "95.10")})

    assert (estimate.net_close.state, estimate.gross_buy_back.state) == ("unknown", "unknown")
    assert f"{SOLD}: nothing is offered" in estimate.gross_buy_back.source


def test_a_contract_with_no_quote_held_is_unknown_and_the_reason_is_the_callers() -> None:
    estimate = _estimate(
        SPREAD, {BOUGHT: _quote("95.00", "95.10")},
        none_held="real accounts keep no option quotes, and the bot's run is not asking for any",
    )

    assert (estimate.net_close.state, estimate.gross_buy_back.state) == ("unknown", "unknown")
    assert estimate.gross_buy_back.source == (
        f"{FROM}, which cannot price this close: "
        f"{SOLD}: real accounts keep no option quotes, and the bot's run is not asking for any"
    )
    leg = next(quality for quality in estimate.quotes if quality.symbol == SOLD)
    assert (leg.state, leg.age_ms, leg.missing, leg.gap) == ("unknown", None, ("bid", "ask"), None)


def test_delayed_data_is_no_price_of_this_market() -> None:
    estimate = _estimate(
        {(BOUGHT, 100): 1}, {BOUGHT: _quote("95.00", "95.10", data_type=QuoteDataType.DELAYED_FROZEN)},
    )

    assert estimate.net_close.state == "unknown"
    assert f"{BOUGHT}: its quote is delayed frozen data, not live" in estimate.net_close.source


def test_a_price_that_is_not_whole_cents_for_the_contract_is_no_price_and_is_never_rounded() -> None:
    """100.00001 a share is $10,000.001 for a contract of 100 shares: a tenth of a cent."""
    estimate = _estimate(SPREAD, {SOLD: _quote("99.90", "100.00001"), BOUGHT: _quote("95.00", "95.10")})

    assert estimate.gross_buy_back.state == "unknown"
    assert "is not a whole number of cents at 100 shares a contract" in estimate.gross_buy_back.source


# ---- rule 3: staleness --------------------------------------------------------------------------


def test_a_quote_one_millisecond_past_the_marks_limit_makes_its_figure_stale() -> None:
    at_the_limit = _estimate(SPREAD, {SOLD: _quote("99.90", "100.00", bid_age=HELD_MARK_MAX_AGE_MS), BOUGHT: _quote("95.00", "95.10")})
    past = _estimate(SPREAD, {SOLD: _quote("99.90", "100.00", bid_age=HELD_MARK_MAX_AGE_MS + 1), BOUGHT: _quote("95.00", "95.10")})

    # Both sides of the sold leg were received together, as a quote that has not ticked reads; its buy-back
    # uses the ask, so the ask's age is the one that counts.
    assert (at_the_limit.net_close.state, at_the_limit.gross_buy_back.state) == ("known", "known")
    assert (past.net_close.state, past.gross_buy_back.state) == ("stale", "stale")
    # A stale figure states the amount last worked out, with its terms, and is not an unknown.
    assert (past.net_close.amount_cents, past.gross_buy_back.amount_cents) == (50_000, 1_000_000)
    assert _terms(past.gross_buy_back) == [(f"Buy back 1 {SOLD} at the ask", 1_000_000)]


def test_the_limit_is_the_one_a_held_contracts_mark_is_allowed() -> None:
    """80 seconds: a share bar and the delivery allowance. Nothing here writes a second one."""
    assert HELD_MARK_MAX_AGE_MS == 80_000


def test_a_stale_bought_leg_makes_the_net_close_stale_and_leaves_the_buy_back_known() -> None:
    quotes = {
        SOLD: _quote("99.90", "100.00", bid_age=200),
        BOUGHT: _quote("95.00", "95.10", bid_age=HELD_MARK_MAX_AGE_MS + 1, ask_age=200),
    }
    estimate = _estimate(SPREAD, quotes)

    assert (estimate.net_close.state, estimate.gross_buy_back.state) == ("stale", "known")
    states = {quality.symbol: (quality.state, quality.age_ms) for quality in estimate.quotes}
    assert states == {SOLD: ("known", 200), BOUGHT: ("stale", HELD_MARK_MAX_AGE_MS + 1)}


def test_a_quote_received_after_now_is_stale_never_fresh() -> None:
    estimate = _estimate(SPREAD, {SOLD: _quote("99.90", "100.00", bid_age=-5, ask_age=-5), BOUGHT: _quote("95.00", "95.10")})

    assert (estimate.net_close.state, estimate.gross_buy_back.state) == ("stale", "stale")


def test_an_unknown_figure_is_unknown_even_when_another_leg_is_stale() -> None:
    quotes = {
        SOLD: _quote("99.90", None),
        BOUGHT: _quote("95.00", "95.10", bid_age=HELD_MARK_MAX_AGE_MS + 1),
    }
    estimate = _estimate(SPREAD, quotes)

    assert estimate.net_close.state == "unknown"


# ---- rule 4: no sold leg --------------------------------------------------------------------------


def test_a_position_with_no_sold_leg_has_a_buy_back_of_zero_known_and_no_note_about_one() -> None:
    estimate = _estimate({(BOUGHT, 100): 1}, {BOUGHT: _quote("95.00", "95.10")})

    assert (estimate.gross_buy_back.state, estimate.gross_buy_back.amount_cents, estimate.gross_buy_back.terms) == (
        "known", 0, (),
    )
    assert estimate.gross_buy_back.as_of_ms == NOON
    assert (estimate.net_close.state, estimate.net_close.amount_cents) == ("known", -950_000)
    assert BUY_BACK_ALONE_LINE not in estimate.conditions


def test_a_buy_back_of_zero_is_known_even_when_the_bought_leg_has_no_quote() -> None:
    estimate = _estimate({(BOUGHT, 100): 1}, {})

    assert (estimate.net_close.state, estimate.gross_buy_back.state, estimate.gross_buy_back.amount_cents) == (
        "unknown", "known", 0,
    )


# ---- conditions -----------------------------------------------------------------------------------


def test_the_conditions_say_fees_are_not_in_it_and_that_alpacas_own_cutoff_is_not_verified() -> None:
    estimate = _estimate({(BOUGHT, 100): 1}, {BOUGHT: _quote("95.00", "95.10")})

    assert estimate.conditions == (FEES_LINE, CUTOFF_LINE)
    assert FEES_LINE == "Amounts are before fees. A positive amount is paid and a negative one comes back."
    assert CUTOFF_LINE == "Alpaca's own expiry-day cutoff has not been verified."


def test_a_sold_leg_adds_the_note_that_a_buy_back_alone_is_not_funded_by_the_close() -> None:
    estimate = _estimate(SPREAD, DEEP_IN_THE_MONEY)

    assert estimate.conditions == (FEES_LINE, BUY_BACK_ALONE_LINE, CUTOFF_LINE)


def test_the_close_the_clerk_owes_is_stated_in_its_own_time_before_the_cutoff_note() -> None:
    due = et_minute_of_day_ms(date(2026, 9, 8), 15 * 60 + 50)
    estimate = _estimate(SPREAD, DEEP_IN_THE_MONEY, owed_closes=(OwedClose(CloseKind.SOLD_LEG, due),))

    assert estimate.conditions == (
        FEES_LINE,
        BUY_BACK_ALONE_LINE,
        "This system closes the position itself at Tue Sep 8, 15:50 ET: a sold option is not held overnight.",
        CUTOFF_LINE,
    )


def test_a_close_that_was_due_says_so_and_each_close_is_stated_once_in_the_order_it_falls_due() -> None:
    earlier = et_minute_of_day_ms(date(2026, 9, 8), 11 * 60)
    later = et_minute_of_day_ms(date(2026, 9, 8), 15 * 60 + 15)
    estimate = _estimate(
        {(BOUGHT, 100): 1}, {BOUGHT: _quote("95.00", "95.10")},
        owed_closes=(
            OwedClose(CloseKind.EXPIRY_DAY, later), OwedClose(CloseKind.SOLD_LEG, earlier),
            OwedClose(CloseKind.EXPIRY_DAY, later),
        ),
    )

    assert estimate.conditions == (
        FEES_LINE,
        "This system's own close of the position was due at Tue Sep 8, 11:00 ET: a sold option is not held overnight.",
        "This system closes the position itself at Tue Sep 8, 15:15 ET: a contract it holds expires that day.",
        CUTOFF_LINE,
    )


def test_a_close_the_clerk_has_stopped_making_by_itself_is_said_to_be_the_owners_and_never_the_systems() -> None:
    due = et_minute_of_day_ms(date(2026, 9, 8), 15 * 60 + 50)
    estimate = _estimate(
        {(BOUGHT, 100): 1}, {BOUGHT: _quote("95.00", "95.10")},
        owed_closes=(OwedClose(CloseKind.SOLD_LEG, due, left_to_owner=True),),
    )

    assert estimate.conditions == (
        FEES_LINE,
        "A close of the position is due at Tue Sep 8, 15:50 ET: a sold option is not held overnight. "
        "This system has stopped exiting this bot by itself, so the close is yours to make.",
        CUTOFF_LINE,
    )


def test_a_close_that_could_not_be_read_is_said_and_never_taken_for_none_owed() -> None:
    estimate = _estimate({(BOUGHT, 100): 1}, {BOUGHT: _quote("95.00", "95.10")}, owed_closes=None)

    assert estimate.conditions == (FEES_LINE, CLOSE_UNREAD_LINE, CUTOFF_LINE)


# ---- what each leg's quote says -----------------------------------------------------------------


def test_each_legs_quality_is_its_state_age_missing_sides_and_gap() -> None:
    quotes = {
        SOLD: _quote("99.90", "100.00", bid_age=700, ask_age=2_500),
        BOUGHT: _quote("95.00", "95.35", bid_age=6_000, ask_age=300),
    }
    estimate = _estimate(SPREAD, quotes)

    # In symbol order. The age is that of the side the close fills at: the ask of a sold leg, the bid of a bought.
    assert [
        (quality.symbol, quality.contracts, quality.state, quality.age_ms, quality.missing, quality.gap)
        for quality in estimate.quotes
    ] == [
        (BOUGHT, 1, "known", 6_000, (), D("0.35")),
        (SOLD, -1, "known", 2_500, (), D("0.10")),
    ]


def test_a_crossed_quote_shows_its_gap_below_zero_as_quoted() -> None:
    estimate = _estimate({(BOUGHT, 100): 1}, {BOUGHT: _quote("95.10", "95.00")})

    [quality] = estimate.quotes
    assert quality.gap == D("-0.10")


# ---- what the caller may hand it --------------------------------------------------------------------


@pytest.mark.parametrize("legs", [{}, {(SOLD, 100): 0}])
def test_a_position_that_is_not_held_has_no_estimate(legs: dict[ContractAtMultiplier, int]) -> None:
    with pytest.raises(ValueError, match="of a position that is held"):
        _estimate(legs, {})


def test_a_contract_netting_to_zero_holds_nothing_and_is_left_out() -> None:
    estimate = _estimate({(SOLD, 100): 0, (BOUGHT, 100): 1}, {BOUGHT: _quote("95.00", "95.10")})

    assert [quality.symbol for quality in estimate.quotes] == [BOUGHT]
    assert estimate.gross_buy_back.amount_cents == 0


@pytest.mark.parametrize("held", [1.5, True, "1"])
def test_contracts_are_held_whole_and_nothing_is_rounded(held: object) -> None:
    with pytest.raises(ValueError, match="whole contracts"):
        _estimate({(BOUGHT, 100): held}, {BOUGHT: _quote("95.00", "95.10")})  # type: ignore[dict-item]


@pytest.mark.parametrize("multiplier", [0, -100, 2.5])
def test_a_multiplier_is_a_whole_number_of_shares(multiplier: object) -> None:
    with pytest.raises(ValueError, match="multiplier"):
        _estimate({(BOUGHT, multiplier): 1}, {BOUGHT: _quote("95.00", "95.10")})  # type: ignore[dict-item]
