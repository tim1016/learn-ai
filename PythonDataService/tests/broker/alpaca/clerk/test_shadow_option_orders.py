"""How a Live Shadow's order book answers an option order of one leg: from a quote it was handed, or not at all (#2913).

The fill itself is the one a Dry Run's simulation makes, and every rule of it
is met in ``test_synthetic_broker_option_orders.py``. Here is what is the
shadow world's own: the fill lands in the shadow book under the shadow's
execution identity, the book's stock lookups and positions show it, its
quotes are kept in the shadow account's own folder, and nothing reaches the
live account but a read.

The contract is one SPY call, bid $1.90 and offered at $1.94.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.broker import ALPACA_LIVE_CAPABILITIES
from app.broker.alpaca.clerk.kept_option_quotes import KEPT_OPTION_QUOTES_FILENAME, KeptOptionQuotes
from app.broker.alpaca.clerk.shadow_broker import ShadowPorts, compose_shadow_ports
from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_ports
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.synthesized_orders import SYNTHESIZED_ORDER_LEDGER_FILENAME
from app.broker.contract.errors import BrokerOrderRejected, BrokerUnavailable
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import OptionPositionIntent, SingleOptionOrderRequest
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.simulated_fill import SimulatedFillModel

LIVE_ACCOUNT = "9LIVE0001"
SHADOW_ACCOUNT = f"shadow:{LIVE_ACCOUNT}"
NOW = 1_788_883_200_000
GENERATION = 4
CALL = OptionContract.from_occ_symbol("SPY260918C00585000")
SYMBOL = CALL.occ_symbol
# A program order's own reference: the bot's namespace and its intent.
REF = "learn-ai/buys-a-call/v1:enter-1"
CLOSE_REF = "learn-ai/buys-a-call/v1:exit-1"


class _LiveRead:
    """The live account a shadow world reads. It has no trade method at all: nothing here could send."""

    broker_id = "alpaca"

    def __init__(self) -> None:
        self.read: list[str] = []

    def capabilities(self) -> Any:
        return ALPACA_LIVE_CAPABILITIES

    def __getattr__(self, name: str) -> Any:
        async def noted(*_args: Any, **_kwargs: Any) -> list[Any]:
            self.read.append(name)
            return []

        return noted


def _quote(bid: str | None, ask: str | None, *, age_ms: int = 300) -> HeldQuote:
    return HeldQuote(
        bid=None if bid is None else QuoteSide(Decimal(bid), NOW - age_ms),
        ask=None if ask is None else QuoteSide(Decimal(ask), NOW - age_ms),
        data_type=QuoteDataType.LIVE,
        generation=GENERATION,
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
def live() -> _LiveRead:
    return _LiveRead()


@pytest.fixture
def shadow(tmp_path: Path, live: _LiveRead) -> ShadowPorts:
    return compose_shadow_ports(
        live_read=live, live_account_id=LIVE_ACCOUNT, artifacts_root=tmp_path, clock=lambda: NOW
    )


def _bind_entry(shadow: ShadowPorts, client_order_id: str = REF, *, multiplier: int = 100) -> None:
    """What the Clerk hands the book with an opening order: the quote it was priced from and the multiplier."""
    shadow.trade.bind_option_entry_quote(
        client_order_id, symbol=SYMBOL, quote=_quote("1.90", "1.94"), multiplier=multiplier
    )


async def _holds_two(shadow: ShadowPorts) -> None:
    _bind_entry(shadow)
    await shadow.trade.submit_option_order(_buy(quantity=2), client_order_id=REF)


async def test_an_opening_order_is_filled_in_full_at_the_ask_of_the_quote_bound_to_it(shadow: ShadowPorts) -> None:
    _bind_entry(shadow)

    answer = await shadow.trade.submit_option_order(_buy(quantity=2), client_order_id=REF)

    assert (answer.status, answer.filled_quantity, answer.filled_avg_price) == ("filled", 2, Decimal("1.94"))
    assert (answer.broker, answer.order_id) == ("shadow", f"shadow-order:{REF}")
    recorded = shadow.trade.synthesized_option_fills()[REF]
    assert (recorded.fill_model, recorded.fill_price, recorded.quote_age_ms, recorded.filled_at_ms) == (
        SimulatedFillModel.NATURAL_PRICE, Decimal("1.94"), 300, NOW
    )
    assert (recorded.request.limit_price, recorded.quote.bid, recorded.quote.ask, recorded.multiplier) == (
        Decimal("1.94"), Decimal("1.90"), Decimal("1.94"), 100
    )


async def test_the_fill_is_in_the_shadow_book_under_the_contracts_symbol_and_the_shadows_own_execution_identity(
    shadow: ShadowPorts,
) -> None:
    """What the Clerk folds is the book's own record: its stock lookup, its orders and its positions show it."""
    await _holds_two(shadow)

    order = await shadow.trade.get_order_by_client_order_id(REF)
    [listed] = await shadow.read.list_orders(status="closed")
    [position] = await shadow.read.list_positions()

    assert order is not None and listed == order
    assert (order.symbol, order.asset_class, order.status, order.filled_quantity, order.filled_avg_price) == (
        SYMBOL, "us_option", "filled", 2.0, 1.94
    )
    assert [event.execution_id for event in order.events] == [f"shadow-execution:{REF}"]
    # The price stays a share's, as a broker states it. The cost is the contracts': 2 x $1.94 x 100.
    assert (position.symbol, position.asset_class, position.quantity) == (SYMBOL, "us_option", 2.0)
    assert position.average_entry_price == pytest.approx(1.94, abs=1e-9, rel=0)
    assert (position.cost_basis, position.market_value) == (388.0, 388.0)
    assert await shadow.read.list_orders(status="open") == []
    looked_up = await shadow.trade.get_option_order_by_broker_order_id(order.order_id)
    assert looked_up is not None and looked_up.client_order_id == REF


async def test_the_quotes_are_kept_in_the_shadow_accounts_own_folder_beside_its_order_book(
    shadow: ShadowPorts, tmp_path: Path
) -> None:
    """Where the Clerk's keeping writes a held contract's marks and the account's reading marks from."""
    await _holds_two(shadow)

    folder = tmp_path / "accounts" / "alpaca" / SHADOW_ACCOUNT
    assert (folder / SYNTHESIZED_ORDER_LEDGER_FILENAME).is_file()
    assert (folder / KEPT_OPTION_QUOTES_FILENAME).is_file()
    kept = KeptOptionQuotes.of_account(artifacts_root=tmp_path, account_id=SHADOW_ACCOUNT)
    newest = kept.newest(SYMBOL)
    assert newest is not None and (newest.quote.bid, newest.quote.ask, newest.kept_at_ms) == (
        Decimal("1.90"), Decimal("1.94"), NOW
    )


async def test_a_close_is_sold_at_the_bid_of_the_newest_quote_kept_and_the_book_is_flat(
    shadow: ShadowPorts, tmp_path: Path
) -> None:
    await _holds_two(shadow)
    # The Clerk's keeping has since stated a newer mark for the held contract.
    KeptOptionQuotes.of_account(artifacts_root=tmp_path, account_id=SHADOW_ACCOUNT).keep_all(
        {SYMBOL: _quote("2.05", "2.09", age_ms=100)}, kept_at_ms=NOW
    )

    bound = shadow.trade.bind_option_close_quotes(
        CLOSE_REF, symbol=SYMBOL, side="sell", quotes=(), usable_generation=None
    )
    answer = await shadow.trade.submit_option_order(_sell_to_close(2), client_order_id=CLOSE_REF)

    assert bound is True
    assert (answer.status, answer.filled_quantity, answer.filled_avg_price) == ("filled", 2, Decimal("2.05"))
    sale = shadow.trade.synthesized_option_fills()[CLOSE_REF]
    # No exit decision's quote came with it, so it is priced from the newest kept and says so.
    assert sale.fill_model is SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE and sale.quote_ref is not None
    assert await shadow.read.list_positions() == []


async def test_an_order_with_no_quote_bound_is_refused_and_no_fill_is_invented(shadow: ShadowPorts) -> None:
    with pytest.raises(BrokerOrderRejected, match="invents no fill") as refused:
        await shadow.trade.submit_option_order(_buy(), client_order_id=REF)

    assert not isinstance(refused.value, BrokerUnavailable) and refused.value.broker == "shadow"
    assert shadow.book.orders() == [] and shadow.trade.synthesized_option_fills() == {}


async def test_a_limit_the_quote_does_not_reach_ends_unfilled_on_the_spot_and_never_rests(shadow: ShadowPorts) -> None:
    """A shadow book rests a stock limit in extended hours. An option order never rests in it."""
    _bind_entry(shadow)

    answer = await shadow.trade.submit_option_order(_buy(limit="1.93"), client_order_id=REF)

    assert (answer.status, answer.filled_quantity, answer.filled_avg_price) == ("canceled", 0, None)
    assert await shadow.read.list_orders(status="open") == []
    assert await shadow.read.list_positions() == []
    # A cancel of an order that has ended leaves its record, option part and all, as it was.
    await shadow.trade.cancel(answer.order_id)
    assert shadow.trade.synthesized_option_fills()[REF].fill_price is None


async def test_the_same_order_sent_again_is_answered_from_its_record_and_filled_once(shadow: ShadowPorts) -> None:
    _bind_entry(shadow)
    first = await shadow.trade.submit_option_order(_buy(), client_order_id=REF)
    _bind_entry(shadow)

    again = await shadow.trade.submit_option_order(_buy(), client_order_id=REF)

    assert again.order_id == first.order_id and again.filled_quantity == 1
    assert [position.quantity for position in await shadow.read.list_positions()] == [1.0]


async def test_a_closing_sale_for_more_than_the_book_holds_is_refused_and_nothing_goes_short(
    shadow: ShadowPorts,
) -> None:
    await _holds_two(shadow)
    assert shadow.trade.bind_option_close_quotes(
        CLOSE_REF, symbol=SYMBOL, side="sell", quotes=(), usable_generation=None
    )

    with pytest.raises(BrokerOrderRejected, match="never opens a position"):
        await shadow.trade.submit_option_order(_sell_to_close(3), client_order_id=CLOSE_REF)

    assert [position.quantity for position in await shadow.read.list_positions()] == [2.0]


@pytest.mark.parametrize(
    ("kept", "priced"),
    [
        pytest.param(None, False, id="nothing kept for the contract"),
        pytest.param(("1.90", "1.94"), True, id="the newest kept has something bid"),
        pytest.param(("0", "0.05"), False, id="the newest kept has nothing bid"),
        pytest.param((None, "1.94"), False, id="the newest kept has no bid at all"),
    ],
)
def test_a_flatten_of_a_contract_has_a_price_only_when_the_newest_quote_kept_has_something_bid(
    shadow: ShadowPorts, tmp_path: Path, kept: tuple[str | None, str | None] | None, priced: bool
) -> None:
    """Asked before any reduction is accepted, through the Clerk's guarded port, so an owner is refused a
    Flatten up front that the book could not price."""
    if kept is not None:
        KeptOptionQuotes.of_account(artifacts_root=tmp_path, account_id=SHADOW_ACCOUNT).restore(SYMBOL, _quote(*kept))
    _read, trade = guard_broker_ports(read=shadow.read, trade=shadow.trade, intake=ReentrantAsyncLock())

    assert trade.recovery_price_available(SYMBOL, side="sell") is priced
    assert shadow.trade.bind_option_close_quotes(
        CLOSE_REF, symbol=SYMBOL, side="sell", quotes=(), usable_generation=None
    ) is (kept is not None and kept[0] is not None)


def test_a_flatten_of_shares_is_answered_as_it_was_before_the_book_took_option_orders(shadow: ShadowPorts) -> None:
    """The book binds a recovery in shares to its bot's newest bar when it is sent, and says then when it has
    none. Asked ahead, it answers yes, as the Clerk's guard answered for it."""
    _read, trade = guard_broker_ports(read=shadow.read, trade=shadow.trade, intake=ReentrantAsyncLock())

    assert trade.recovery_price_available("SPY") is True
    assert shadow.trade.recovery_price_available("SPY", side="buy") is True


async def test_nothing_reaches_the_live_account_but_the_reads_a_shadow_always_made(
    shadow: ShadowPorts, live: _LiveRead
) -> None:
    """An entry, a mark, a close and every lookup are the book's own. The live account is not asked."""
    await _holds_two(shadow)
    assert shadow.trade.bind_option_close_quotes(
        CLOSE_REF, symbol=SYMBOL, side="sell", quotes=(_quote("1.90", "1.94"),), usable_generation=GENERATION
    )
    await shadow.trade.submit_option_order(_sell_to_close(2), client_order_id=CLOSE_REF)
    await shadow.trade.get_option_order_by_client_order_id(REF)
    await shadow.read.list_positions()
    await shadow.read.list_orders()

    assert live.read == []
