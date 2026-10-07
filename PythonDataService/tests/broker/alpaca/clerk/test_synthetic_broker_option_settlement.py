"""The simulation settles a contract it holds past the contract's last session (#2913).

No broker exists to end a simulated position, so the simulation does it, by
a rule of its own: it books the sale Alpaca says it makes of an expiring
position, in cash, at the contract's exercise value at the close of its last
session. It never exercises into shares. Nothing here says what a real
account would have got.

Settling is one explicit step of the simulation's trade port. Its read of
option events settles nothing and reports none.

The scene: one SPY call, or put, struck at 585 and expiring Friday
2026-09-18, bought at noon on Tuesday 2026-09-08.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.simulated_option_settlement import (
    SETTLEMENT_BAR_MAX_AGE_MS,
    SETTLEMENT_GRACE_MS,
    OwnersWord,
    SimulatedOptionSettlements,
    SimulatedOutcome,
)
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import OptionPositionIntent, SingleOptionOrderRequest
from app.marketdata.feed import MarketDataBar
from app.options.contract import OptionContract
from app.options.deadlines import last_session_close_ms
from app.options.event_allocation import OptionEventKind
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import RECOVERY_QUOTE_PROVIDER, RUN_END_CLOSE_PROVIDER, SourceBarLedger
from app.utils.session_anchors import et_midnight_ms

ACCOUNT = "sim:settles-a-call"
EXPIRY = date(2026, 9, 18)
NOON_OF_THE_ENTRY = et_minute_of_day_ms(date(2026, 9, 8), 12 * 60)
LAST_SESSION_OPEN = et_minute_of_day_ms(EXPIRY, 9 * 60 + 30)
LAST_SESSION_CLOSE = et_minute_of_day_ms(EXPIRY, 16 * 60)
SATURDAY = et_minute_of_day_ms(date(2026, 9, 19), 12 * 60)
CALL = OptionContract.from_occ_symbol("SPY260918C00585000")
PUT = OptionContract.from_occ_symbol("SPY260918P00585000")
WINDOW = et_midnight_ms(date(2026, 9, 8))
MINUTE = 60_000


@pytest.fixture
def clock() -> list[int]:
    return [NOON_OF_THE_ENTRY]


@pytest.fixture
def bars(tmp_path: Path) -> Iterator[SourceBarLedger]:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    try:
        yield ledger
    finally:
        ledger.close()


@pytest.fixture
def simulation(bars: SourceBarLedger, clock: list[int]) -> SyntheticBroker:
    return SyntheticBroker(account_id=ACCOUNT, source_bars=bars, clock=lambda: clock[0])


async def _buys(simulation: SyntheticBroker, contract: OptionContract, clock: list[int], *, contracts: int = 1) -> None:
    received = clock[0] - 300
    quote = HeldQuote(
        bid=QuoteSide(Decimal("1.90"), received), ask=QuoteSide(Decimal("1.94"), received),
        data_type=QuoteDataType.LIVE, generation=4,
    )
    ref = f"buys-{contract.occ_symbol}"
    simulation.bind_option_entry_quote(ref, symbol=contract.occ_symbol, quote=quote, multiplier=100)
    await simulation.submit_option_order(
        SingleOptionOrderRequest(
            contract=contract, side=OrderSide.BUY, intent=OptionPositionIntent.OPEN, quantity=contracts,
            order_type=OrderType.LIMIT, limit_price=Decimal("1.94"),
        ),
        client_order_id=ref,
    )


def _spy_bar(bars: SourceBarLedger, *, close: str, end_ms: int, **prices: str) -> None:
    """The simulation keeps one minute of SPY that ended at ``end_ms``, as the market delivered it."""
    price = Decimal(close)
    said = {"open": price, "high": price, "low": price, **{name: Decimal(value) for name, value in prices.items()}}
    bars.append(
        MarketDataBar(
            feed_id="ibkr", symbol="SPY", start_ms=end_ms - MINUTE, end_ms=end_ms, close=price, volume=100,
            fetched_at_ms=end_ms, session_phase="RTH", **said,
        ),
        run_id="run-1",
    )


def _settle(simulation: SyntheticBroker, *, window: int = WINDOW) -> SimulatedOptionSettlements:
    """The step the Clerk's poll calls: settle what is held past expiry, and hand back what was settled."""
    return simulation.settle_expired_options(after_ms=window)


async def _held(simulation: SyntheticBroker) -> dict[str, float]:
    return {
        position.symbol: abs(position.quantity) * (1 if position.side == "long" else -1)
        for position in await simulation.list_positions()
    }


def test_the_wait_for_the_closing_bar_and_the_oldest_bar_that_may_stand_for_it_are_what_was_decided() -> None:
    """Two named settings of the simulation's rule: two minutes, and fifteen. Changed only by a decision."""
    assert (SETTLEMENT_GRACE_MS, SETTLEMENT_BAR_MAX_AGE_MS) == (2 * MINUTE, 15 * MINUTE)


# ── what it settles to ───────────────────────────────────────────────────


async def test_nothing_is_settled_while_the_contract_still_trades(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590", end_ms=LAST_SESSION_CLOSE - MINUTE)
    clock[0] = LAST_SESSION_CLOSE - 1

    settled = _settle(simulation)

    assert (settled.settled, settled.not_settled, settled.waiting) == ((), {}, {})
    assert await _held(simulation) == {CALL.occ_symbol: 1.0}


async def test_a_call_in_the_money_is_sold_at_its_exercise_value_and_no_shares_appear(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    """Two calls struck at 585 with SPY closing at 590.25: 5.25 a share, 2 x 100 x 5.25 = $1,050.00 in cash.

    Exercised, they would have bought 200 shares for $117,000.00. A Dry
    Run's budget could not pay for them, so nothing is exercised.
    """
    await _buys(simulation, CALL, clock, contracts=2)
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY

    [settlement] = _settle(simulation).settled

    record = settlement.record
    assert (record.outcome, record.contracts, record.multiplier) == (SimulatedOutcome.SETTLED_IN_CASH, 2, 100)
    assert (record.exercise_value, record.booked_price, record.cash_cents) == (Decimal("5.25"), Decimal("5.25"), 105_000)
    # Nothing is held: not the contract, and no share of SPY, bought or sold.
    assert await _held(simulation) == {}


async def test_a_put_in_the_money_is_sold_at_its_exercise_value_and_leaves_no_short(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    """One put struck at 585 with SPY closing at 580.00: 5.00 a share, $500.00. Exercised, it would have sold 100 shares short."""
    await _buys(simulation, PUT, clock)
    _spy_bar(bars, close="580", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY

    [settlement] = _settle(simulation).settled

    assert (settlement.record.outcome, settlement.record.booked_price, settlement.record.cash_cents) == (
        SimulatedOutcome.SETTLED_IN_CASH, Decimal(5), 50_000
    )
    assert await _held(simulation) == {}


@pytest.mark.parametrize(
    ("contract", "close", "outcome", "booked", "cash_cents"),
    [
        pytest.param(CALL, "585.01", SimulatedOutcome.SETTLED_IN_CASH, "0.01", 100, id="a call a cent in the money"),
        pytest.param(PUT, "584.99", SimulatedOutcome.SETTLED_IN_CASH, "0.01", 100, id="a put a cent in the money"),
        pytest.param(CALL, "585.12345", SimulatedOutcome.SETTLED_IN_CASH, "0.12", 1_200, id="rounded down to the cent"),
        pytest.param(CALL, "585.009", SimulatedOutcome.EXPIRED, "0", 0, id="a call a fraction of a cent in the money"),
        pytest.param(CALL, "585.00", SimulatedOutcome.EXPIRED, "0", 0, id="a call at the money"),
        pytest.param(CALL, "584.99", SimulatedOutcome.EXPIRED, "0", 0, id="a call out of the money"),
        pytest.param(PUT, "585.00", SimulatedOutcome.EXPIRED, "0", 0, id="a put at the money"),
    ],
)
async def test_a_value_that_rounds_down_to_nothing_is_an_expiry_and_a_cent_or_more_is_a_sale(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int], contract: OptionContract, close: str,
    outcome: SimulatedOutcome, booked: str, cash_cents: int,
) -> None:
    await _buys(simulation, contract, clock)
    _spy_bar(bars, close=close, end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY

    [settlement] = _settle(simulation).settled

    record = settlement.record
    assert (record.outcome, record.booked_price, record.cash_cents) == (outcome, Decimal(booked), cash_cents)
    # An expiry is no sale and has no fee. A sale's fee is the option fee model's.
    assert (record.fee_cents is None) is (outcome is SimulatedOutcome.EXPIRED)
    assert await _held(simulation) == {}


async def test_a_settlement_travels_as_an_expiry_dated_the_expiry_date_and_states_no_cash_of_its_own(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    """The rule that chooses lots knows three kinds of event. A settlement ends a bought position and moves no
    shares, so it travels as the one that does that. Its price is in the simulation's record, not on the row."""
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY

    [settlement] = _settle(simulation).settled

    event = settlement.event
    assert (event.kind, event.contract, event.position_change) == (OptionEventKind.EXPIRED, CALL, -1)
    assert event.activity_date_ms == et_midnight_ms(EXPIRY)
    assert (event.activity_type, event.net_amount) == ("SIMULATED_CASH_SETTLEMENT", None)


async def test_the_record_says_the_rule_the_price_every_figure_and_the_fee(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    """One call sold at 5.25 a share on 2026-09-18, $525.00. Its fee, as a day of its own, each type rounded up
    to the cent: SEC 525 x 0.0000206 = 0.010815, two cents; TAF 0.00329, one; CAT 100 x 0.000003 = 0.0003,
    one; ORF 0.015, two; OCC 0.025, three. Nine cents."""
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE - MINUTE)
    clock[0] = SATURDAY

    [settlement] = _settle(simulation).settled

    said = settlement.record.note()
    assert "simulation" in said["rule"].lower() and "not a broker" in said["rule"].lower()
    assert (said["underlying"], said["underlying_price"], said["strike"], said["right"]) == ("SPY", "590.25", "585", "call")
    assert (said["exercise_value"], said["booked_price"]) == ("5.25", "5.25")
    assert (said["contracts"], said["multiplier"], said["cash_cents"]) == (1, 100, 52_500)
    assert said["bar_ref"] and said["bar_provider"] == "ibkr"
    assert (said["bar_end_ms"], said["last_session_close_ms"], said["bar_before_close_ms"]) == (
        LAST_SESSION_CLOSE - MINUTE, LAST_SESSION_CLOSE, MINUTE
    )
    assert said["settled_at_ms"] == SATURDAY
    assert said["fee_cents"] == 9 and "option fee model" in said["fees"]


# ── where the price comes from ───────────────────────────────────────────


async def test_the_price_is_the_close_of_the_bar_and_none_of_its_other_prices(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    """A bar that opened at 580, reached 599, fell to 570 and closed at 590.25. Only the close settles."""
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE, open="580", high="599", low="570")
    clock[0] = SATURDAY

    [settlement] = _settle(simulation).settled

    assert settlement.record.price.underlying_price == Decimal("590.25")
    assert settlement.record.booked_price == Decimal("5.25")


async def test_the_bar_that_ends_at_the_close_settles_at_once(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="586", end_ms=LAST_SESSION_CLOSE)
    clock[0] = LAST_SESSION_CLOSE

    settled = _settle(simulation)

    [settlement] = settled.settled
    assert settlement.record.price.bar_before_close_ms == 0 and settled.waiting == {}


async def test_without_the_closing_bar_the_simulation_waits_and_settles_from_it_when_it_lands(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    """The race at the close. The 15:59 bar closed at 584.50, out of the money. Half a second after the close
    the 16:00 bar has not landed. Settled then, the contract would expire for good. The closing bar lands a
    second later at 586.00, a dollar in the money, and that is what it settles at."""
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="584.50", end_ms=LAST_SESSION_CLOSE - MINUTE)
    clock[0] = LAST_SESSION_CLOSE + 500

    early = _settle(simulation)

    assert (early.settled, early.not_settled) == ((), {})
    assert early.waiting == {CALL.occ_symbol: LAST_SESSION_CLOSE + SETTLEMENT_GRACE_MS}
    assert await _held(simulation) == {CALL.occ_symbol: 1.0}

    _spy_bar(bars, close="586.00", end_ms=LAST_SESSION_CLOSE)
    clock[0] = LAST_SESSION_CLOSE + 1_500
    [settlement] = _settle(simulation).settled

    assert (settlement.record.outcome, settlement.record.booked_price) == (SimulatedOutcome.SETTLED_IN_CASH, Decimal(1))
    assert settlement.record.price.bar_before_close_ms == 0


@pytest.mark.parametrize(
    ("after_the_close_ms", "settles"),
    [
        pytest.param(SETTLEMENT_GRACE_MS - 1, False, id="a millisecond before the wait is over"),
        pytest.param(SETTLEMENT_GRACE_MS, True, id="when the wait is over"),
    ],
)
async def test_once_the_wait_is_over_the_newest_bar_of_the_session_settles_and_says_how_old_it_is(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int], after_the_close_ms: int, settles: bool
) -> None:
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590", end_ms=LAST_SESSION_CLOSE - 2 * MINUTE)
    _spy_bar(bars, close="586", end_ms=LAST_SESSION_CLOSE - MINUTE)
    clock[0] = LAST_SESSION_CLOSE + after_the_close_ms

    settled = _settle(simulation)

    assert len(settled.settled) == (1 if settles else 0)
    assert (CALL.occ_symbol in settled.waiting) is not settles
    if settles:
        price = settled.settled[0].record.price
        assert (price.underlying_price, price.bar_before_close_ms) == (Decimal(586), MINUTE)


@pytest.mark.parametrize(
    ("before_the_close_ms", "settles"),
    [
        pytest.param(SETTLEMENT_BAR_MAX_AGE_MS, True, id="a bar as old as the rule allows"),
        pytest.param(SETTLEMENT_BAR_MAX_AGE_MS + MINUTE, False, id="a bar a minute older than that"),
        pytest.param(6 * 60 * MINUTE + 25 * MINUTE, False, id="the 09:35 bar of a bot stopped that morning"),
    ],
)
async def test_a_bar_too_long_before_the_close_is_no_price_and_the_simulation_says_how_old_it_is(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int], before_the_close_ms: int, settles: bool
) -> None:
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE - before_the_close_ms)
    clock[0] = SATURDAY

    settled = _settle(simulation)

    assert len(settled.settled) == (1 if settles else 0)
    if settles:
        assert settled.settled[0].record.price.bar_before_close_ms == before_the_close_ms
        return
    assert await _held(simulation) == {CALL.occ_symbol: 1.0} and settled.waiting == {}
    [why] = settled.not_settled.values()
    assert "SPY" in why and f"{before_the_close_ms // MINUTE} minutes before the close" in why
    assert "15 minutes" in why


@pytest.mark.parametrize(
    "kept_at",
    [
        pytest.param(None, id="no price of the underlying at all"),
        pytest.param(LAST_SESSION_OPEN, id="only a price from before the last session opened"),
        pytest.param(LAST_SESSION_CLOSE + MINUTE, id="only a price from after the close"),
    ],
)
async def test_with_no_price_from_the_last_session_nothing_is_settled_and_the_simulation_says_why(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int], kept_at: int | None
) -> None:
    await _buys(simulation, CALL, clock)
    if kept_at is not None:
        _spy_bar(bars, close="590", end_ms=kept_at)
    clock[0] = SATURDAY

    settled = _settle(simulation)

    assert settled.settled == () and settled.waiting == {}
    assert await _held(simulation) == {CALL.occ_symbol: 1.0}
    [why] = settled.not_settled.values()
    # A bar that ended at the open is of the hours before the session. It is no old price of the session.
    assert why.startswith("This simulation kept no price of SPY from the contract's last session")
    assert "before the close" not in why
    # No promise that one will be kept: none will be, by itself.
    assert "until" not in why


async def test_a_bar_from_after_the_close_is_never_the_price(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="584", end_ms=LAST_SESSION_CLOSE - MINUTE)
    _spy_bar(bars, close="599", end_ms=LAST_SESSION_CLOSE + MINUTE)
    clock[0] = SATURDAY

    [settlement] = _settle(simulation).settled

    assert settlement.record.outcome is SimulatedOutcome.EXPIRED
    assert (settlement.record.price.underlying_price, settlement.record.price.bar_end_ms) == (
        Decimal(584), LAST_SESSION_CLOSE - MINUTE
    )


@pytest.mark.parametrize("provider", [RUN_END_CLOSE_PROVIDER, RECOVERY_QUOTE_PROVIDER])
async def test_a_bar_the_simulation_made_as_the_evidence_of_a_fill_is_never_the_price(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int], provider: str
) -> None:
    """The market's last bar closed at 584.00. A fill of SPY shares a second before the close left its own
    evidence bar at 599.00, ending at the close. That is a price the simulation wrote, not one the market
    delivered: the contract expires, on the market's bar."""
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="584", end_ms=LAST_SESSION_CLOSE - MINUTE)
    price = Decimal(599)
    evidence = MarketDataBar(
        symbol="SPY", start_ms=LAST_SESSION_CLOSE - 1, end_ms=LAST_SESSION_CLOSE, open=price, high=price,
        low=price, close=price, volume=0, fetched_at_ms=LAST_SESSION_CLOSE, feed_id=provider, session_phase="RTH",
    )
    (bars.retain_run_end_close if provider == RUN_END_CLOSE_PROVIDER else bars.retain_recovery_quote)(evidence)
    clock[0] = SATURDAY

    [settlement] = _settle(simulation).settled

    assert settlement.record.outcome is SimulatedOutcome.EXPIRED
    assert (settlement.record.price.underlying_price, settlement.record.price.bar_provider) == (Decimal(584), "ibkr")


async def test_on_a_half_day_the_close_is_the_early_one(
    bars: SourceBarLedger, clock: list[int], simulation: SyntheticBroker
) -> None:
    """Friday 2026-11-27 closes at 13:00. Nothing is settled a millisecond before; the 13:00 bar settles at 13:00."""
    half_day = date(2026, 11, 27)
    contract = OptionContract.of(root="SPY", expiry_date=half_day, right="call", strike=585)
    close_ms = last_session_close_ms(half_day)
    assert close_ms == et_minute_of_day_ms(half_day, 13 * 60)
    await _buys(simulation, contract, clock)
    _spy_bar(bars, close="590.25", end_ms=close_ms)
    _spy_bar(bars, close="600", end_ms=close_ms + 30 * MINUTE)

    clock[0] = close_ms - 1
    assert _settle(simulation).settled == ()
    clock[0] = close_ms
    [settlement] = _settle(simulation).settled

    assert (settlement.record.booked_price, settlement.record.last_session_close_ms) == (Decimal("5.25"), close_ms)


# ── once, and only what it holds ─────────────────────────────────────────


async def test_a_contract_is_settled_once_however_often_the_step_runs_and_is_handed_over_each_time(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY

    first = _settle(simulation)
    # A price that turns up later changes nothing: the contract is no longer held.
    _spy_bar(bars, close="700", end_ms=LAST_SESSION_CLOSE + MINUTE)
    clock[0] = SATURDAY + MINUTE
    second = _settle(simulation)

    assert [item.event.activity_id for item in second.settled] == [item.event.activity_id for item in first.settled]
    assert [item.record for item in second.settled] == [item.record for item in first.settled]
    assert len(first.settled) == 1 and await _held(simulation) == {}


async def test_shares_are_never_settled_and_a_settlement_is_no_open_order(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY
    _settle(simulation)
    clock[0] = SATURDAY + 86_400_000 * 30

    again = _settle(simulation)

    assert len(again.settled) == 1 and await _held(simulation) == {}
    assert await simulation.list_orders(status="open") == []
    # The settlement is in the ledger as a sale that filled at the close of the contract's last session,
    # whenever the step that made it ran.
    [_bought, sold] = sorted(await simulation.list_orders(status="closed"), key=lambda order: order.filled_at_ms or 0)
    assert (sold.side, sold.filled_quantity, sold.filled_avg_price, sold.filled_at_ms) == (
        "sell", 1.0, 5.25, LAST_SESSION_CLOSE
    )


@pytest.mark.parametrize(
    ("window", "handed_over"),
    [
        pytest.param(et_midnight_ms(EXPIRY), True, id="a window that starts on the expiry date"),
        pytest.param(et_minute_of_day_ms(EXPIRY, 15 * 60), True, id="any instant of the expiry date"),
        pytest.param(et_midnight_ms(date(2026, 9, 19)), False, id="a window that starts the day after"),
    ],
)
async def test_a_settlement_is_handed_over_while_the_window_reaches_its_expiry_date(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int], window: int, handed_over: bool
) -> None:
    """A contract bought on its own expiry day is read for from that day on. Its settlement is dated that day."""
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY

    settled = _settle(simulation, window=window)

    assert len(settled.settled) == (1 if handed_over else 0)
    # Settled in the ledger either way: the window only says what is handed over.
    assert await _held(simulation) == {}


async def test_reading_the_accounts_option_events_settles_nothing_and_reports_none(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    """The read is a read. Only the settlement step settles, and only it hands a settlement over."""
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY

    evidence = await simulation.read_option_event_activities(after_ms=WINDOW)

    assert (evidence.events, evidence.share_trades, evidence.read_whole) == ((), (), True)
    assert await _held(simulation) == {CALL.occ_symbol: 1.0}
    # Once the step has settled it, the read still reports none: the step's own answer carries the record.
    assert len(_settle(simulation).settled) == 1
    assert (await simulation.read_option_event_activities(after_ms=WINDOW)).events == ()


async def _sells_to_open(simulation: SyntheticBroker, contract: OptionContract, clock: list[int]) -> None:
    """The simulation is handed an order that opens a sold position. No bot may send one yet; the port takes it."""
    received = clock[0] - 300
    quote = HeldQuote(
        bid=QuoteSide(Decimal("1.90"), received), ask=QuoteSide(Decimal("1.94"), received),
        data_type=QuoteDataType.LIVE, generation=4,
    )
    ref = f"sells-{contract.occ_symbol}"
    simulation.bind_option_entry_quote(ref, symbol=contract.occ_symbol, quote=quote, multiplier=100)
    await simulation.submit_option_order(
        SingleOptionOrderRequest(
            contract=contract, side=OrderSide.SELL, intent=OptionPositionIntent.OPEN, quantity=1,
            order_type=OrderType.LIMIT, limit_price=Decimal("1.90"),
        ),
        client_order_id=ref,
    )


async def test_a_contract_whose_closing_price_is_still_awaited_is_not_let_go_of_at_a_word_and_one_with_none_is(
    simulation: SyntheticBroker, clock: list[int]
) -> None:
    """While the wait lasts the closing bar may still land, so the owner's word ends nothing. Once the wait is
    over and no price was kept, the same word takes the contract out of the ledger at nothing, once."""
    await _buys(simulation, CALL, clock)
    clock[0] = LAST_SESSION_CLOSE + SETTLEMENT_GRACE_MS - 1
    word = OwnersWord(operator="the-owner", reason="The feed was down all day.", said_at_ms=clock[0])

    early = simulation.settle_expired_options(after_ms=WINDOW, at_the_owners_word={CALL.occ_symbol: word})

    assert (early.settled, early.not_settled) == ((), {})
    assert early.waiting == {CALL.occ_symbol: LAST_SESSION_CLOSE + SETTLEMENT_GRACE_MS}
    assert await _held(simulation) == {CALL.occ_symbol: 1.0}

    clock[0] = LAST_SESSION_CLOSE + SETTLEMENT_GRACE_MS
    late = simulation.settle_expired_options(after_ms=WINDOW, at_the_owners_word={CALL.occ_symbol: word})

    [ending] = late.settled
    assert (late.not_settled, late.waiting) == ({}, {})
    assert (ending.record.outcome, ending.record.booked_price, ending.record.cash_cents) == (
        SimulatedOutcome.ENDED_BY_OWNER, Decimal(0), 0
    )
    assert ending.record.owner == word and "kept no price of SPY" in ending.record.why_no_settlement
    assert await _held(simulation) == {}
    again = _settle(simulation)
    assert [settlement.record for settlement in again.settled] == [ending.record]
    assert (again.not_settled, again.waiting) == ({}, {})


async def test_a_sold_contract_is_settled_in_no_way_and_is_not_let_go_of_at_a_word(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    """A sold call that ends in the money would be assigned: shares would move. The simulation's rule is for
    what it bought. It settles a sold contract in no way, says so, and no word of the owner's ends it."""
    await _sells_to_open(simulation, CALL, clock)
    assert await _held(simulation) == {CALL.occ_symbol: -1.0}
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY
    word = OwnersWord(operator="the-owner", reason=None, said_at_ms=SATURDAY)

    settled = simulation.settle_expired_options(after_ms=WINDOW, at_the_owners_word={CALL.occ_symbol: word})

    assert settled.settled == () and settled.waiting == {}
    assert "only whole contracts it bought" in settled.not_settled[CALL.occ_symbol]
    assert await _held(simulation) == {CALL.occ_symbol: -1.0}
