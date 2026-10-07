"""The simulation settles a contract it holds past the contract's last session (#2913).

No broker exists to exercise or expire a simulated position, so the
simulation applies Alpaca's stated rule to its own book, from the last price
of the underlying it kept from the contract's last session. This is a
simulation's rule: nothing here says what a real account would do.

Settling is one explicit step of the simulation's trade port. Its read of
option events settles nothing and reports none.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.simulated_option_settlement import SimulatedOptionSettlements
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.option_events import BrokerOptionEventActivity, BrokerOptionShareTradeActivity
from app.broker.contract.options import OptionPositionIntent, SingleOptionOrderRequest
from app.marketdata.feed import MarketDataBar
from app.options.contract import OptionContract
from app.options.event_allocation import OptionEventKind
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import SourceBarLedger
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


def _spy_bar(bars: SourceBarLedger, *, close: str, end_ms: int) -> None:
    """The simulation keeps one minute of SPY that ended at ``end_ms``."""
    price = Decimal(close)
    bars.append(
        MarketDataBar(
            feed_id="ibkr", symbol="SPY", start_ms=end_ms - 60_000, end_ms=end_ms, open=price, high=price, low=price,
            close=price, volume=100, fetched_at_ms=end_ms, session_phase="RTH",
        ),
        run_id="run-1",
    )


def _settle(simulation: SyntheticBroker) -> SimulatedOptionSettlements:
    """The step the Clerk's poll calls: settle what is held past expiry, and hand back what was settled."""
    return simulation.settle_expired_options(after_ms=WINDOW)


def _events(settled: SimulatedOptionSettlements) -> list[BrokerOptionEventActivity]:
    return [settlement.event for settlement in settled.settled]


def _share_trades(settled: SimulatedOptionSettlements) -> list[BrokerOptionShareTradeActivity]:
    return [settlement.share_trade for settlement in settled.settled if settlement.share_trade is not None]


async def _held(simulation: SyntheticBroker) -> dict[str, float]:
    return {
        position.symbol: abs(position.quantity) * (1 if position.side == "long" else -1)
        for position in await simulation.list_positions()
    }


async def test_nothing_is_settled_while_the_contract_still_trades(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590", end_ms=LAST_SESSION_CLOSE - 60_000)
    clock[0] = LAST_SESSION_CLOSE - 1

    settled = _settle(simulation)

    assert settled.settled == () and settled.not_settled == {}
    assert await _held(simulation) == {CALL.occ_symbol: 1.0}


async def test_a_call_out_of_the_money_at_the_last_price_expires_and_the_simulation_holds_nothing(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="584.99", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY

    settled = _settle(simulation)

    [event] = _events(settled)
    assert (event.kind, event.contract, event.position_change) == (OptionEventKind.EXPIRED, CALL, -1)
    assert event.activity_date_ms == et_midnight_ms(EXPIRY)
    assert _share_trades(settled) == [] and settled.not_settled == {}
    assert await _held(simulation) == {}


@pytest.mark.parametrize(
    ("contract", "last_price", "exercised"),
    [
        pytest.param(CALL, "585.01", True, id="a call a cent in the money"),
        pytest.param(CALL, "585.00", False, id="a call at the money"),
        pytest.param(CALL, "585.009", False, id="a call less than a cent in the money"),
        pytest.param(PUT, "584.99", True, id="a put a cent in the money"),
        pytest.param(PUT, "585.00", False, id="a put at the money"),
    ],
)
async def test_a_contract_is_exercised_when_it_is_in_the_money_by_a_cent_or_more_and_expires_otherwise(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int], contract: OptionContract, last_price: str,
    exercised: bool,
) -> None:
    await _buys(simulation, contract, clock)
    _spy_bar(bars, close=last_price, end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY

    settled = _settle(simulation)

    [event] = _events(settled)
    assert event.kind is (OptionEventKind.EXERCISED if exercised else OptionEventKind.EXPIRED)
    assert len(_share_trades(settled)) == (1 if exercised else 0)


async def test_an_exercised_call_leaves_the_simulation_holding_the_shares_at_the_strike(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    """Two calls struck at 585 with SPY last at 590.25: 200 shares bought at $585.00, for $117,000.00."""
    await _buys(simulation, CALL, clock, contracts=2)
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY

    [trade] = _share_trades(_settle(simulation))
    assert (trade.underlying_symbol, trade.shares, trade.price, trade.net_amount) == (
        "SPY", 200, Decimal(585), Decimal(-117000)
    )
    [shares] = await simulation.list_positions()
    assert (shares.symbol, shares.quantity, shares.side, shares.average_entry_price) == ("SPY", 200.0, "long", 585.0)


async def test_an_exercised_put_leaves_the_simulation_short_the_shares_at_the_strike(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    await _buys(simulation, PUT, clock)
    _spy_bar(bars, close="580", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY

    _settle(simulation)

    assert await _held(simulation) == {"SPY": -100.0}


async def test_the_price_read_is_the_last_one_kept_from_the_last_session_and_the_record_says_which(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    """A later bar, from after the close, is not the contract's last price. The record names the bar used."""
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590", end_ms=LAST_SESSION_CLOSE - 120_000)
    _spy_bar(bars, close="584", end_ms=LAST_SESSION_CLOSE - 60_000)
    _spy_bar(bars, close="599", end_ms=LAST_SESSION_CLOSE + 60_000)
    clock[0] = SATURDAY

    [settlement] = _settle(simulation).settled

    assert settlement.event.kind is OptionEventKind.EXPIRED
    note = settlement.note
    assert note["underlying_price"] == "584"
    assert note["bar_end_ms"] == LAST_SESSION_CLOSE - 60_000
    assert note["last_session_close_ms"] == LAST_SESSION_CLOSE
    assert "simulation" in note["rule"].lower()


@pytest.mark.parametrize(
    "kept_at",
    [
        pytest.param(None, id="no price of the underlying at all"),
        pytest.param(LAST_SESSION_OPEN, id="only a price from before the last session opened"),
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

    assert settled.settled == ()
    assert await _held(simulation) == {CALL.occ_symbol: 1.0}
    [why] = settled.not_settled.values()
    assert "SPY" in why and "last session" in why


async def test_a_contract_is_settled_once_however_often_the_events_are_read(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY

    first = _settle(simulation)
    clock[0] = SATURDAY + 60_000
    second = _settle(simulation)

    assert [event.activity_id for event in _events(second)] == [event.activity_id for event in _events(first)]
    assert len(_events(first)) == 1
    assert await _held(simulation) == {"SPY": 100.0}


async def test_shares_are_never_settled_and_a_settlement_is_no_open_order(
    simulation: SyntheticBroker, bars: SourceBarLedger, clock: list[int]
) -> None:
    await _buys(simulation, CALL, clock)
    _spy_bar(bars, close="590.25", end_ms=LAST_SESSION_CLOSE)
    clock[0] = SATURDAY
    _settle(simulation)
    clock[0] = SATURDAY + 86_400_000 * 30

    again = _settle(simulation)

    assert len(again.settled) == 1 and await _held(simulation) == {"SPY": 100.0}
    assert await simulation.list_orders(status="open") == []


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
