"""How a Dry Run's simulation answers an option order of one leg: from a quote it was handed, or not at all (#2913).

The contract is one SPY call, bid $1.90 and offered at $1.94.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.synthesized_orders import SynthesizedOrderRecord
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.errors import BrokerOptionsNotSupported, BrokerOrderRejected, BrokerUnavailable
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import (
    MultiLegOptionOrderRequest,
    OptionLegRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
)
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


@pytest.fixture
def simulation(tmp_path: Path) -> Iterator[SyntheticBroker]:
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT)
    try:
        yield SyntheticBroker(account_id=ACCOUNT, source_bars=bars, clock=lambda: NOW)
    finally:
        bars.close()


async def test_an_opening_order_is_filled_in_full_at_the_ask_of_the_quote_bound_to_it(simulation: SyntheticBroker) -> None:
    simulation.bind_option_entry_quote("ref-1", symbol=SYMBOL, quote=_quote("1.90", "1.94"))

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
    simulation.bind_option_entry_quote("ref-1", symbol=SYMBOL, quote=_quote("1.90", "1.94"))
    await simulation.submit_option_order(_buy(), client_order_id="ref-1")

    order = await simulation.get_order_by_client_order_id("ref-1")
    [position] = await simulation.list_positions()

    assert order is not None
    assert (order.symbol, order.asset_class, order.filled_quantity, order.filled_avg_price) == (SYMBOL, "us_option", 1.0, 1.94)
    assert [event.execution_id for event in order.events] == ["sim-execution:ref-1"]
    assert (position.symbol, position.asset_class, position.quantity) == (SYMBOL, "us_option", 1.0)
    looked_up = await simulation.get_option_order_by_client_order_id("ref-1")
    assert looked_up is not None and looked_up.order_id == order.order_id


async def test_an_order_with_no_quote_bound_is_refused_and_no_fill_is_invented(simulation: SyntheticBroker) -> None:
    with pytest.raises(BrokerOrderRejected, match="invents no fill") as refused:
        await simulation.submit_option_order(_buy(), client_order_id="ref-1")

    # Definitive: nothing exists, so no lookup has an outcome to settle.
    assert not isinstance(refused.value, BrokerUnavailable)
    assert await simulation.list_orders() == [] and simulation.synthesized_option_fills() == {}


async def test_a_quote_bound_for_another_contract_fills_nothing(simulation: SyntheticBroker) -> None:
    simulation.bind_option_entry_quote("ref-1", symbol=OTHER.occ_symbol, quote=_quote("1.90", "1.94"))

    with pytest.raises(BrokerOrderRejected):
        await simulation.submit_option_order(_buy(), client_order_id="ref-1")

    assert await simulation.list_orders() == []


async def test_a_limit_the_quote_does_not_reach_ends_unfilled_on_the_spot(simulation: SyntheticBroker) -> None:
    """The simulation cannot rest an order. Offered at $1.94 against a limit of $1.90, it ends with nothing bought."""
    simulation.bind_option_entry_quote("ref-1", symbol=SYMBOL, quote=_quote("1.90", "1.94"))

    answer = await simulation.submit_option_order(_buy(limit="1.90"), client_order_id="ref-1")

    assert (answer.status, answer.filled_quantity, answer.filled_avg_price) == ("canceled", 0, None)
    assert await simulation.list_positions() == []
    assert simulation.synthesized_option_fills()["ref-1"].fill_model is None


async def test_the_same_order_sent_again_is_answered_from_its_record_and_filled_once(simulation: SyntheticBroker) -> None:
    simulation.bind_option_entry_quote("ref-1", symbol=SYMBOL, quote=_quote("1.90", "1.94"))
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
    simulation.bind_option_entry_quote("ref-1", symbol=SYMBOL, quote=_quote("1.90", "1.94", age_ms=60_000))
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


async def test_a_newer_quote_with_no_bid_does_not_hide_an_older_one_that_has(simulation: SyntheticBroker) -> None:
    """An ask alone says nothing of what the contract could be sold for."""
    await _holding_one(simulation)
    simulation.keep_option_quotes({SYMBOL: _quote(None, "2.60", age_ms=100)})
    simulation.bind_option_close_quotes("ref-2", symbol=SYMBOL, side="sell", quotes=[], usable_generation=None)

    await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-2")

    assert simulation.synthesized_option_fills()["ref-2"].fill_price == Decimal("1.90")


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

    with pytest.raises(BrokerOrderRejected, match="never opens a position") as refused:
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

    with pytest.raises(BrokerOrderRejected, match="never opens a position"):
        await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-3")

    assert await simulation.list_positions() == []


async def test_a_sale_to_close_a_contract_that_is_not_held_is_refused_even_with_a_quote_to_fill_it_from(
    simulation: SyntheticBroker,
) -> None:
    simulation.keep_option_quotes({SYMBOL: _quote("2.40", "2.46")})
    assert simulation.bind_option_close_quotes("ref-2", symbol=SYMBOL, side="sell", quotes=[], usable_generation=None)

    with pytest.raises(BrokerOrderRejected, match="never opens a position"):
        await simulation.submit_option_order(_sell_to_close(), client_order_id="ref-2")

    assert await simulation.list_positions() == []


async def test_a_close_that_sells_exactly_what_is_held_is_filled(simulation: SyntheticBroker) -> None:
    simulation.bind_option_entry_quote("ref-1", symbol=SYMBOL, quote=_quote("1.90", "1.94", age_ms=60_000))
    await simulation.submit_option_order(_buy(quantity=3), client_order_id="ref-1")
    simulation.bind_option_close_quotes(
        "ref-2", symbol=SYMBOL, side="sell", quotes=[_quote("2.40", "2.46")], usable_generation=GENERATION
    )

    answer = await simulation.submit_option_order(_sell_to_close(quantity=3), client_order_id="ref-2")

    assert (answer.status, answer.filled_quantity) == ("filled", 3)
    assert await simulation.list_positions() == []


# ── a quote from the future, and a contract past its last session ────────


async def test_a_quote_stamped_later_than_the_simulations_clock_prices_no_close(simulation: SyntheticBroker) -> None:
    """It is not a price the close could have met. The newest quote received by now is the one used."""
    await _holding_one(simulation)
    simulation.keep_option_quotes({SYMBOL: _quote("9.00", "9.06", age_ms=-60_000)})
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
    simulation.bind_option_entry_quote("ref-1", symbol=SYMBOL, quote=_quote("1.90", "1.94"))

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
    simulation_with_no_stock_feed: SyntheticBroker, moving_clock: list[int]
) -> None:
    simulation = simulation_with_no_stock_feed

    assert not simulation.recovery_price_available(SYMBOL), "nothing was ever kept for it"

    simulation.keep_option_quotes({SYMBOL: _quote("2.00", "2.06", age_ms=9_000)})
    assert simulation.recovery_price_available(SYMBOL)

    # The newest quote says nothing is bid. The older bid of $2.00 no longer prices a sale.
    simulation.keep_option_quotes({SYMBOL: _quote("0", "0.05", age_ms=300)})
    assert not simulation.recovery_price_available(SYMBOL)
    # It is still offered, so a purchase to close could be priced.
    assert simulation.recovery_price_available(SYMBOL, side="buy")

    moving_clock[0] = NOW + 60_000
    simulation.keep_option_quotes({SYMBOL: HeldQuote(
        bid=QuoteSide(Decimal("0.80"), NOW + 59_000), ask=QuoteSide(Decimal("0.86"), NOW + 59_000),
        data_type=QuoteDataType.LIVE, generation=GENERATION,
    )})
    assert simulation.recovery_price_available(SYMBOL)

    moving_clock[0] = LAST_SESSION_CLOSE
    assert not simulation.recovery_price_available(SYMBOL), "its last session is over"


def test_a_recovery_of_a_contract_is_never_bound_to_a_stock_quote(
    simulation_with_no_stock_feed: SyntheticBroker,
) -> None:
    simulation_with_no_stock_feed.keep_option_quotes({SYMBOL: _quote("2.00", "2.06")})

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
