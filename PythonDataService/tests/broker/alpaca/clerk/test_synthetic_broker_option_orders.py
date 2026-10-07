"""How a Dry Run's simulation answers an option order of one leg: from a quote it was handed, or not at all (#2913).

The contract is one SPY call, bid $1.90 and offered at $1.94.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.broker.alpaca.clerk.synthesized_orders import SynthesizedOrderRecord
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.errors import BrokerOptionsNotSupported, BrokerOrderRejected, BrokerUnavailable
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType
from app.broker.contract.options import (
    MultiLegOptionOrderRequest,
    OptionLegRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
)
from app.marketdata.feed import MarketDataBar
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.simulated_fill import SimulatedFillModel
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import SourceBarLedger

ACCOUNT = "sim:buys-a-call"
NOW = 1_788_883_200_000
GENERATION = 4
CALL = OptionContract.from_occ_symbol("SPY260918C00585000")
SYMBOL = CALL.occ_symbol
OTHER = OptionContract.from_occ_symbol("SPY260918C00590000")
# The call's last session: Friday 2026-09-18, which closes at four o'clock New York time.
LAST_SESSION_CLOSE = et_minute_of_day_ms(date(2026, 9, 18), 16 * 60)


def _quote(bid: str | None, ask: str | None, *, age_ms: int = 300, generation: int = GENERATION) -> HeldQuote:
    return HeldQuote(
        bid=None if bid is None else QuoteSide(Decimal(bid), NOW - age_ms),
        ask=None if ask is None else QuoteSide(Decimal(ask), NOW - age_ms),
        data_type=QuoteDataType.LIVE,
        generation=generation,
    )


def _buy(limit: str = "1.94", quantity: int = 1) -> SingleOptionOrderRequest:
    return SingleOptionOrderRequest(
        contract=CALL, side=OrderSide.BUY, intent=OptionPositionIntent.OPEN, quantity=quantity,
        order_type=OrderType.LIMIT, limit_price=Decimal(limit),
    )


def _sell_to_close(quantity: int = 1) -> SingleOptionOrderRequest:
    return SingleOptionOrderRequest(
        contract=CALL, side=OrderSide.SELL, intent=OptionPositionIntent.CLOSE, quantity=quantity,
        order_type=OrderType.MARKET,
    )


def _bind_entry(
    simulation: SyntheticBroker, client_order_id: str, *, symbol: str = SYMBOL, age_ms: int = 300, multiplier: int = 100
) -> None:
    """What the Clerk hands the simulation with an opening order: the quote it was priced from, bid $1.90 and
    offered at $1.94, and the contract's multiplier as the books record it."""
    simulation.bind_option_entry_quote(
        client_order_id, symbol=symbol, quote=_quote("1.90", "1.94", age_ms=age_ms), multiplier=multiplier
    )


@pytest.fixture
def hand_over(tmp_path: Path) -> Callable[[HeldQuote], None]:
    """State a quote as the newest held for the call at ``NOW``, as the Clerk's own keeping does: straight
    into the account's kept quotes, which the simulation prices a close from."""
    kept = KeptOptionQuotes.of_account(artifacts_root=tmp_path, account_id=ACCOUNT)
    return lambda quote: kept.keep_all({SYMBOL: quote}, kept_at_ms=NOW)


@pytest.fixture
def simulation(tmp_path: Path) -> Iterator[SyntheticBroker]:
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    try:
        yield SyntheticBroker(account_id=ACCOUNT, source_bars=bars, clock=lambda: NOW)
    finally:
        bars.close()


async def test_an_opening_order_is_filled_in_full_at_the_ask_of_the_quote_bound_to_it(simulation: SyntheticBroker) -> None:
    _bind_entry(simulation, "ref-1")

    answer = await simulation.submit_option_order(_buy(quantity=2), client_order_id="ref-1")

    assert (answer.status, answer.filled_quantity, answer.filled_avg_price) == ("filled", 2, Decimal("1.94"))
    assert (answer.contract, answer.side, answer.intent) == (CALL, OrderSide.BUY, OptionPositionIntent.OPEN)
    recorded = simulation.synthesized_option_fills()["ref-1"]
    assert (recorded.fill_model, recorded.fill_price, recorded.quote_age_ms) == (
        SimulatedFillModel.NATURAL_PRICE, Decimal("1.94"), 300
    )
    assert (recorded.request.limit_price, recorded.quote.bid, recorded.quote.ask) == (
        Decimal("1.94"), Decimal("1.90"), Decimal("1.94")
    )


async def test_the_fill_is_on_the_books_the_clerk_reads_under_the_contracts_own_symbol(
    simulation: SyntheticBroker,
) -> None:
    _bind_entry(simulation, "ref-1")
    await simulation.submit_option_order(_buy(), client_order_id="ref-1")

    order = await simulation.get_order_by_client_order_id("ref-1")
    [position] = await simulation.list_positions()

    assert order is not None
    assert (order.symbol, order.asset_class, order.filled_quantity, order.filled_avg_price) == (SYMBOL, "us_option", 1.0, 1.94)
    assert [event.execution_id for event in order.events] == ["sim-execution:ref-1"]
    assert (position.symbol, position.asset_class, position.quantity) == (SYMBOL, "us_option", 1.0)
    looked_up = await simulation.get_option_order_by_client_order_id("ref-1")
    assert looked_up is not None and looked_up.order_id == order.order_id


# ── what a position in a contract is worth in dollars ────────────────────


@pytest.mark.parametrize(
    ("contracts", "multiplier", "dollars"),
    [
        pytest.param(1, 100, 194.0, id="one contract of a hundred shares at $1.94 a share: $194.00"),
        pytest.param(3, 100, 582.0, id="three of them: $582.00"),
        pytest.param(1, 10, 19.4, id="a contract handed over as ten shares: $19.40, whatever its symbol looks like"),
    ],
)
async def test_a_position_in_a_contract_is_counted_in_dollars_by_the_multiplier_handed_over_with_its_order(
    simulation: SyntheticBroker, contracts: int, multiplier: int, dollars: float
) -> None:
    """The price stays a share's, as a broker states it. The cost and the value are the contract's own."""
    _bind_entry(simulation, "ref-1", multiplier=multiplier)
    await simulation.submit_option_order(_buy(quantity=contracts), client_order_id="ref-1")

    [position] = await simulation.list_positions()

    assert (position.asset_class, position.quantity) == ("us_option", contracts)
    # The average is the fold's own float division, as it is for shares.
    assert position.average_entry_price == pytest.approx(1.94, abs=1e-9, rel=0)
    # Exact: the multiplier is applied in decimals to what the fold counted. The simulation states no
    # current price, so the value is the cost.
    assert (position.cost_basis, position.market_value) == (dollars, dollars)
    assert (position.current_price, position.unrealized_pl) == (None, 0.0)


async def test_shares_held_beside_a_contract_are_stated_exactly_as_they_were_before_any_contract_was(
    tmp_path: Path,
) -> None:
    """Ten shares bought at $501.25: $5,012.50. Buying a contract beside them changes no figure of theirs."""
    as_it_always_was = (
        '{"broker":"synthetic","symbol":"SPY","asset_id":null,"asset_class":"us_equity","quantity":10.0,'
        '"side":"long","average_entry_price":501.25,"market_value":5012.5,"cost_basis":5012.5,'
        '"current_price":null,"unrealized_pl":0.0,"unrealized_plpc":null,"observed_at_ms":1788883200000,'
        '"prior_close_price":null}'
    )
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    try:
        simulation = SyntheticBroker(account_id=ACCOUNT, source_bars=bars, clock=lambda: NOW)
        bars.append(
            MarketDataBar(
                symbol="SPY", start_ms=NOW - 120_000, end_ms=NOW - 60_000, open=Decimal("500"), high=Decimal("502"),
                low=Decimal("499"), close=Decimal("501.25"), volume=42, fetched_at_ms=NOW - 59_000,
                feed_id="polygon-minute", session_phase="RTH",
            ),
            run_id="run-a",
        )
        await simulation.submit(BrokerOrderLeg(symbol="SPY", side="buy", quantity=10), client_order_id="shares-1")
        [alone] = await simulation.list_positions()
        _bind_entry(simulation, "ref-1")
        await simulation.submit_option_order(_buy(), client_order_id="ref-1")

        held = {position.symbol: position for position in await simulation.list_positions()}
    finally:
        bars.close()

    assert alone.model_dump_json() == as_it_always_was
    assert held["SPY"].model_dump_json() == as_it_always_was
    assert (held[SYMBOL].cost_basis, held[SYMBOL].market_value) == (194.0, 194.0)


async def test_what_is_left_of_a_position_is_still_in_dollars_after_a_sale_and_after_the_simulation_is_opened_again(
    tmp_path: Path,
) -> None:
    """Three contracts bought, one sold. The two left cost $388.00, and still do to a simulation opened afresh:
    the multiplier is kept with the order, not held in memory."""
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    try:
        simulation = SyntheticBroker(account_id=ACCOUNT, source_bars=bars, clock=lambda: NOW)
        _bind_entry(simulation, "ref-1", age_ms=60_000)
        await simulation.submit_option_order(_buy(quantity=3), client_order_id="ref-1")
        simulation.bind_option_close_quotes(
            "ref-2", symbol=SYMBOL, side="sell", quotes=[_quote("2.40", "2.46")], usable_generation=GENERATION
        )
        await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-2")

        [left] = await simulation.list_positions()
        [opened_again] = await SyntheticBroker(account_id=ACCOUNT, source_bars=bars, clock=lambda: NOW).list_positions()
    finally:
        bars.close()

    # After a sale the fold restates the cost from its float average, as it does for shares: hence the tolerance.
    assert left.quantity == 2.0
    assert left.average_entry_price == pytest.approx(1.94, abs=1e-9, rel=0)
    assert left.cost_basis == pytest.approx(388.0, abs=1e-9, rel=0)
    assert left.market_value == left.cost_basis
    assert opened_again == left


async def test_an_opening_order_that_no_multiplier_came_with_is_not_filled(
    simulation: SyntheticBroker, hand_over: Callable[[HeldQuote], None]
) -> None:
    """Bound as a close is bound, with a quote and no multiplier. Filled, its position could only be counted as
    shares: a hundred times too small. The simulation fills no such order."""
    hand_over(_quote("1.90", "1.94"))
    assert simulation.bind_option_close_quotes("ref-1", symbol=SYMBOL, side="sell", quotes=[], usable_generation=None)

    with pytest.raises(BrokerOrderRejected) as refused:
        await simulation.submit_option_order(_buy(), client_order_id="ref-1")

    assert not isinstance(refused.value, BrokerUnavailable)
    assert await simulation.list_orders() == [] and await simulation.list_positions() == []


async def test_an_order_with_no_quote_bound_is_refused_and_no_fill_is_invented(simulation: SyntheticBroker) -> None:
    with pytest.raises(BrokerOrderRejected, match="invents no fill") as refused:
        await simulation.submit_option_order(_buy(), client_order_id="ref-1")

    # Definitive: nothing exists, so no lookup has an outcome to settle.
    assert not isinstance(refused.value, BrokerUnavailable)
    assert await simulation.list_orders() == [] and simulation.synthesized_option_fills() == {}


async def test_a_quote_bound_for_another_contract_fills_nothing(simulation: SyntheticBroker) -> None:
    _bind_entry(simulation, "ref-1", symbol=OTHER.occ_symbol)

    with pytest.raises(BrokerOrderRejected):
        await simulation.submit_option_order(_buy(), client_order_id="ref-1")

    assert await simulation.list_orders() == []


async def test_a_limit_the_quote_does_not_reach_ends_unfilled_on_the_spot(simulation: SyntheticBroker) -> None:
    """The simulation cannot rest an order. Offered at $1.94 against a limit of $1.90, it ends with nothing bought."""
    _bind_entry(simulation, "ref-1")

    answer = await simulation.submit_option_order(_buy(limit="1.90"), client_order_id="ref-1")

    assert (answer.status, answer.filled_quantity, answer.filled_avg_price) == ("canceled", 0, None)
    assert await simulation.list_positions() == []
    assert simulation.synthesized_option_fills()["ref-1"].fill_model is None


async def test_the_same_order_sent_again_is_answered_from_its_record_and_filled_once(simulation: SyntheticBroker) -> None:
    _bind_entry(simulation, "ref-1")
    first = await simulation.submit_option_order(_buy(), client_order_id="ref-1")

    again = await simulation.submit_option_order(_buy(), client_order_id="ref-1")

    assert again.order_id == first.order_id
    assert [position.quantity for position in await simulation.list_positions()] == [1.0]


async def test_an_order_of_several_legs_is_not_taken(simulation: SyntheticBroker) -> None:
    spread = MultiLegOptionOrderRequest(
        legs=(
            OptionLegRequest(contract=CALL, side=OrderSide.BUY, intent=OptionPositionIntent.OPEN, ratio=1),
            OptionLegRequest(contract=OTHER, side=OrderSide.SELL, intent=OptionPositionIntent.OPEN, ratio=1),
        ),
        quantity=1, order_type=OrderType.LIMIT, net_limit_price=Decimal("1.94"),
    )

    with pytest.raises(BrokerOptionsNotSupported, match="several legs"):
        await simulation.submit_option_order(spread, client_order_id="ref-1")

    assert await simulation.list_orders() == []


async def _holding_one(simulation: SyntheticBroker) -> None:
    _bind_entry(simulation, "ref-1", age_ms=60_000)
    await simulation.submit_option_order(_buy(), client_order_id="ref-1")


async def test_a_close_is_sold_at_the_bid_of_a_quote_its_decision_could_use(simulation: SyntheticBroker) -> None:
    await _holding_one(simulation)
    bound = simulation.bind_option_close_quotes(
        "ref-2", symbol=SYMBOL, side="sell", quotes=[_quote("2.40", "2.46")], usable_generation=GENERATION
    )

    answer = await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-2")

    assert bound
    assert (answer.status, answer.filled_avg_price) == ("filled", Decimal("2.40"))
    assert simulation.synthesized_option_fills()["ref-2"].fill_model is SimulatedFillModel.NATURAL_PRICE
    assert await simulation.list_positions() == []


@pytest.mark.parametrize(
    ("handed", "usable_generation", "price", "age_ms"),
    [
        pytest.param([], None, "1.90", 60_000, id="none handed: the entry's own quote, a minute old"),
        pytest.param([("2.10", "2.16", 9_000, GENERATION)], GENERATION, "2.10", 9_000, id="one too old to use"),
        pytest.param([("2.10", "2.16", 300, GENERATION - 1)], GENERATION, "2.10", 300, id="one from before a reconnect"),
    ],
)
async def test_a_close_with_no_usable_quote_is_sold_from_the_newest_kept_and_says_so(
    simulation: SyntheticBroker, handed: list[tuple[str, str, int, int]], usable_generation: int | None, price: str,
    age_ms: int,
) -> None:
    await _holding_one(simulation)
    quotes = [_quote(bid, ask, age_ms=age, generation=generation) for bid, ask, age, generation in handed]
    simulation.bind_option_close_quotes(
        "ref-2", symbol=SYMBOL, side="sell", quotes=quotes, usable_generation=usable_generation
    )

    await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-2")

    sale = simulation.synthesized_option_fills()["ref-2"]
    assert (sale.fill_model, sale.fill_price, sale.quote_age_ms) == (
        SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal(price), age_ms
    )
    assert sale.quote_ref is not None and sale.quote_ref.startswith(f"kept-option-quote:{SYMBOL}:")


async def test_a_newer_quote_with_no_bid_leaves_a_sale_unpriced_whatever_an_older_quote_had_bid(
    simulation: SyntheticBroker, hand_over: Callable[[HeldQuote], None]
) -> None:
    """Bought with $1.90 bid a minute ago. The newest quote kept has an ask and no bid.

    The $1.90 is what was bid before that quote. Nothing kept says it still
    is, so the sale is bound to nothing and nothing is sold. The next quote
    with a bid sells it, at that bid.
    """
    await _holding_one(simulation)
    hand_over(_quote(None, "2.60", age_ms=100))

    bound = simulation.bind_option_close_quotes("ref-2", symbol=SYMBOL, side="sell", quotes=[], usable_generation=None)
    with pytest.raises(BrokerOrderRejected, match="invents no fill"):
        await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-2")

    assert not bound
    assert [position.quantity for position in await simulation.list_positions()] == [1.0]
    assert "ref-2" not in simulation.synthesized_option_fills()

    hand_over(_quote("0.80", "0.86", age_ms=50))
    assert simulation.bind_option_close_quotes("ref-3", symbol=SYMBOL, side="sell", quotes=[], usable_generation=None)
    await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-3")

    assert simulation.synthesized_option_fills()["ref-3"].fill_price == Decimal("0.80")
    assert await simulation.list_positions() == []


async def test_an_exits_own_quote_with_no_bid_is_the_newest_and_the_entrys_older_bid_does_not_stand_in_for_it(
    simulation: SyntheticBroker,
) -> None:
    """The quote read at the exit decision is put back before the close is bound. It is the newest, and has no bid."""
    await _holding_one(simulation)

    bound = simulation.bind_option_close_quotes(
        "ref-2", symbol=SYMBOL, side="sell", quotes=[_quote(None, "2.60", age_ms=100)], usable_generation=GENERATION
    )

    assert not bound


async def test_a_close_of_a_contract_no_quote_was_ever_kept_for_cannot_be_bound(simulation: SyntheticBroker) -> None:
    assert not simulation.bind_option_close_quotes("ref-2", symbol=SYMBOL, side="sell", quotes=[], usable_generation=None)

    with pytest.raises(BrokerOrderRejected):
        await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-2")


async def test_a_contract_nobody_bids_for_is_not_sold_for_nothing(simulation: SyntheticBroker) -> None:
    """The books take no fill at a price of zero, so the position is left open."""
    await _holding_one(simulation)
    simulation.bind_option_close_quotes(
        "ref-2", symbol=SYMBOL, side="sell", quotes=[_quote("0", "0.05")], usable_generation=GENERATION
    )

    with pytest.raises(BrokerOrderRejected, match="sale for nothing"):
        await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-2")

    assert [position.quantity for position in await simulation.list_positions()] == [1.0]


# ── a closing order closes what is held, and nothing more ────────────────


async def test_a_closing_sale_for_more_than_is_held_is_refused_and_nothing_goes_short(
    simulation: SyntheticBroker,
) -> None:
    await _holding_one(simulation)
    simulation.bind_option_close_quotes(
        "ref-2", symbol=SYMBOL, side="sell", quotes=[_quote("2.40", "2.46")], usable_generation=GENERATION
    )

    with pytest.raises(BrokerOrderRejected) as refused:
        await simulation.submit_option_order(_sell_to_close(quantity=5), client_order_id="ref-2")

    assert not isinstance(refused.value, BrokerUnavailable)
    assert [position.quantity for position in await simulation.list_positions()] == [1.0]
    assert "ref-2" not in simulation.synthesized_option_fills()


async def test_a_second_close_of_a_position_already_sold_is_refused_under_any_order_id(
    simulation: SyntheticBroker,
) -> None:
    await _holding_one(simulation)
    quotes = [_quote("2.40", "2.46")]
    simulation.bind_option_close_quotes("ref-2", symbol=SYMBOL, side="sell", quotes=quotes, usable_generation=GENERATION)
    await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-2")
    simulation.bind_option_close_quotes("ref-3", symbol=SYMBOL, side="sell", quotes=quotes, usable_generation=GENERATION)

    with pytest.raises(BrokerOrderRejected):
        await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-3")

    assert await simulation.list_positions() == []


async def test_a_sale_to_close_a_contract_that_is_not_held_is_refused_even_with_a_quote_to_fill_it_from(
    simulation: SyntheticBroker, hand_over: Callable[[HeldQuote], None]
) -> None:
    hand_over(_quote("2.40", "2.46"))
    assert simulation.bind_option_close_quotes("ref-2", symbol=SYMBOL, side="sell", quotes=[], usable_generation=None)

    with pytest.raises(BrokerOrderRejected):
        await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-2")

    assert await simulation.list_positions() == []


async def test_a_close_that_sells_exactly_what_is_held_is_filled(simulation: SyntheticBroker) -> None:
    _bind_entry(simulation, "ref-1", age_ms=60_000)
    await simulation.submit_option_order(_buy(quantity=3), client_order_id="ref-1")
    simulation.bind_option_close_quotes(
        "ref-2", symbol=SYMBOL, side="sell", quotes=[_quote("2.40", "2.46")], usable_generation=GENERATION
    )

    answer = await simulation.submit_option_order(_sell_to_close(quantity=3), client_order_id="ref-2")

    assert (answer.status, answer.filled_quantity) == ("filled", 3)
    assert await simulation.list_positions() == []


# ── a quote from the future, and a contract past its last session ────────


async def test_a_quote_stamped_later_than_the_simulations_clock_prices_no_close(
    simulation: SyntheticBroker, hand_over: Callable[[HeldQuote], None]
) -> None:
    """It is not a price the close could have met. The newest quote received by now is the one used."""
    await _holding_one(simulation)
    hand_over(_quote("9.00", "9.06", age_ms=-60_000))
    simulation.bind_option_close_quotes("ref-2", symbol=SYMBOL, side="sell", quotes=[], usable_generation=None)

    await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-2")

    sale = simulation.synthesized_option_fills()["ref-2"]
    assert (sale.fill_price, sale.quote_age_ms) == (Decimal("1.90"), 60_000)


@pytest.fixture
def moving_clock() -> list[int]:
    return [NOW]


@pytest.fixture
def simulation_in_time(tmp_path: Path, moving_clock: list[int]) -> Iterator[SyntheticBroker]:
    """The same simulation, with a clock the test moves."""
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    try:
        yield SyntheticBroker(account_id=ACCOUNT, source_bars=bars, clock=lambda: moving_clock[0])
    finally:
        bars.close()


async def test_nothing_is_sold_in_a_contract_past_its_last_session_whatever_was_bound_before_it(
    simulation_in_time: SyntheticBroker, moving_clock: list[int]
) -> None:
    """The quote was bound while the contract still traded. The order arrives when it no longer does."""
    simulation = simulation_in_time
    await _holding_one(simulation)
    moving_clock[0] = LAST_SESSION_CLOSE - 1
    assert simulation.bind_option_close_quotes(
        "ref-2", symbol=SYMBOL, side="sell", quotes=[_quote("2.40", "2.46")], usable_generation=GENERATION
    )
    moving_clock[0] = LAST_SESSION_CLOSE

    with pytest.raises(BrokerOrderRejected, match="expired") as refused:
        await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-2")

    assert not isinstance(refused.value, BrokerUnavailable)
    assert [position.quantity for position in await simulation.list_positions()] == [1.0]
    assert "ref-2" not in simulation.synthesized_option_fills()


async def test_a_close_of_a_contract_past_its_last_session_is_not_bound_to_any_kept_quote(
    simulation_in_time: SyntheticBroker, moving_clock: list[int]
) -> None:
    simulation = simulation_in_time
    await _holding_one(simulation)
    quotes = [_quote("2.40", "2.46")]
    moving_clock[0] = LAST_SESSION_CLOSE - 1
    bound_while_it_traded = simulation.bind_option_close_quotes(
        "ref-2", symbol=SYMBOL, side="sell", quotes=quotes, usable_generation=GENERATION
    )
    moving_clock[0] = LAST_SESSION_CLOSE + 68 * 3_600_000

    bound_after = simulation.bind_option_close_quotes(
        "ref-3", symbol=SYMBOL, side="sell", quotes=quotes, usable_generation=GENERATION
    )

    assert bound_while_it_traded and not bound_after


async def test_nothing_is_bought_in_a_contract_past_its_last_session_either(
    simulation_in_time: SyntheticBroker, moving_clock: list[int]
) -> None:
    simulation = simulation_in_time
    moving_clock[0] = LAST_SESSION_CLOSE
    _bind_entry(simulation, "ref-1")

    with pytest.raises(BrokerOrderRejected, match="expired"):
        await simulation.submit_option_order(_buy(), client_order_id="ref-1")

    assert await simulation.list_orders() == []


# ── whether a contract can be closed now: asked before an owner's Flatten ──


@pytest.fixture
def simulation_with_no_stock_feed(tmp_path: Path, moving_clock: list[int]) -> Iterator[SyntheticBroker]:
    """A simulation whose stock quote source fails the test if it is asked anything."""

    def never_asked(symbol: str, _now_ms: int) -> None:
        pytest.fail(f"the stock quote source was asked about {symbol}")

    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    try:
        yield SyntheticBroker(
            account_id=ACCOUNT, source_bars=bars, clock=lambda: moving_clock[0], quote_source=never_asked
        )
    finally:
        bars.close()


def test_a_contract_can_be_sold_now_only_when_the_newest_quote_kept_for_it_has_something_bid(
    simulation_with_no_stock_feed: SyntheticBroker, moving_clock: list[int], hand_over: Callable[[HeldQuote], None]
) -> None:
    simulation = simulation_with_no_stock_feed

    assert not simulation.recovery_price_available(SYMBOL), "nothing was ever kept for it"

    hand_over(_quote("2.00", "2.06", age_ms=9_000))
    assert simulation.recovery_price_available(SYMBOL)

    # The newest quote has an ask and no bid at all. The older bid of $2.00 prices no sale.
    hand_over(_quote(None, "2.06", age_ms=600))
    assert not simulation.recovery_price_available(SYMBOL)
    assert simulation.recovery_price_available(SYMBOL, side="buy")

    # The newest quote says nothing is bid. The older bid of $2.00 no longer prices a sale.
    hand_over(_quote("0", "0.05", age_ms=300))
    assert not simulation.recovery_price_available(SYMBOL)
    # It is still offered, so a purchase to close could be priced.
    assert simulation.recovery_price_available(SYMBOL, side="buy")

    # A bid stamped a minute ahead of the clock is not one a sale made now could meet.
    in_a_minute = HeldQuote(
        bid=QuoteSide(Decimal("0.80"), NOW + 59_000), ask=QuoteSide(Decimal("0.86"), NOW + 59_000),
        data_type=QuoteDataType.LIVE, generation=GENERATION,
    )
    hand_over(in_a_minute)
    assert not simulation.recovery_price_available(SYMBOL)
    moving_clock[0] = NOW + 60_000
    assert simulation.recovery_price_available(SYMBOL)

    moving_clock[0] = LAST_SESSION_CLOSE
    assert not simulation.recovery_price_available(SYMBOL), "its last session is over"


def test_a_recovery_of_a_contract_is_never_bound_to_a_stock_quote(
    simulation_with_no_stock_feed: SyntheticBroker, hand_over: Callable[[HeldQuote], None]
) -> None:
    hand_over(_quote("2.00", "2.06"))

    assert not simulation_with_no_stock_feed.bind_latest_recovery_bar("ref-2", symbol=SYMBOL, side="sell")


def test_an_order_record_written_before_option_orders_reads_and_writes_back_unchanged() -> None:
    """A ledger with no option order is byte for byte what an older build wrote and can read."""
    written_before = (
        '{"seq":1,"order":{"broker":"synthetic","order_id":"sim-order:x","client_order_id":"x","symbol":"SPY",'
        '"asset_class":"us_equity","side":"buy","order_type":"market","time_in_force":"day","quantity":1.0,'
        '"filled_quantity":1.0,"limit_price":null,"stop_price":null,"extended_hours":false,"filled_avg_price":10.0,'
        '"status":"filled","submitted_at_ms":1,"created_at_ms":1,"updated_at_ms":1,"filled_at_ms":1,'
        '"canceled_at_ms":null,"expired_at_ms":null,"replaced_by":null,"replaces":null,"events":[],'
        '"observed_at_ms":1,"fill_latency_seconds":null,"unreadable_fields":[]},"leg":null,"anchor":null}'
    )

    record = SynthesizedOrderRecord.model_validate_json(written_before)

    assert record.option is None
    assert record.model_dump_json() == written_before
