"""Option quotes through the Clerk's IBKR supervisor: receipts owned by callbacks, lines kept to a budget."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from eventkit import Event
from ib_async import ContractDetails, Option, OptionChain, Stock, Ticker
from ib_async.objects import OptionComputation, TickData

from app.broker.ibkr.market_liveness import IbkrMarketStatusSource
from app.broker.ibkr.market_subscription import SUBSCRIPTION_TIMEOUT_MS
from app.broker.ibkr.option_chain import listed_contracts, listed_expiries
from app.broker.ibkr.option_quotes import CEILING_PROBE_MS, OptionQuoteLines, WatchRequest
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteFault, QuoteSide, quote_fault
from app.options.resolver import UnderlyingObservation, resolve_contracts
from app.options.watch import LineClass, NoLine

# The resolver's own scenes: SPY with its bar closed at 585, a dollar-wide ladder, selected on a Tuesday at 10:31:02.
from tests.options import test_resolver as scenes

NOW = scenes._at(scenes._TUESDAY, 10, 31, 2)
FRIDAY = scenes._FRIDAY
MONEY = Decimal("585.00")
_AT_THE_MONEY = {"selector": "atm"}


def _call(strike: int, expiry: date = FRIDAY) -> OptionContract:
    return scenes._contract("call", strike, expiry)


class Gateway:
    """One IBKR connection as the supervisor sees it: a chain's listing, quote lines up to an allowance, and
    the callbacks a test delivers."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        strikes: Sequence[int] = range(560, 611),
        expiries: Sequence[date] = scenes._EXPIRIES,
        allowance: int | None = None,
    ) -> None:
        self.now = NOW
        self.allowance = allowance
        self.strikes, self.expiries = list(strikes), list(expiries)
        self._stocks: dict[str, Stock] = {}
        # Every line open now, by the OCC symbol (or the ticker) it was asked for under.
        self.lines: dict[str, Ticker] = {}
        self.asked: list[str] = []
        self.cancelled: list[str] = []
        self._request_ids: dict[Ticker, int] = {}
        self.client = Mock(
            connection_generation=1, connectivity_lost_count=0, connection_lost=False,
            connection_state="connected", last_event_ms=NOW, last_ibkr_code=None,
        )
        self.client.is_connected.return_value = True
        self.client.ib.errorEvent = Event()
        self.client.ib.wrapper.ticker2ReqId = {"mktData": self._request_ids}
        self.client.ib.reqMktData.side_effect = self._request
        self.client.ib.cancelMktData.side_effect = self._cancel
        self.client.ib.reqSecDefOptParamsAsync = AsyncMock(side_effect=self._chains)
        self.client.ib.reqContractDetailsAsync = AsyncMock(side_effect=self._details)
        for module in ("market_liveness", "option_chain"):
            monkeypatch.setattr(f"app.broker.ibkr.{module}.qualify_underlying", self._qualify)

    # ---- what IBKR answers ----------------------------------------------------

    async def _qualify(self, _client: Any, symbol: str) -> Stock:
        return self._stocks.setdefault(symbol, Stock(symbol, "SMART", "USD", conId=756733 + len(self._stocks)))

    async def _chains(self, *, underlyingSymbol: str = "SPY", **_: Any) -> list[OptionChain]:
        yyyymmdd = [f"{day:%Y%m%d}" for day in self.expiries]
        strikes = [float(strike) for strike in self.strikes]
        return [
            OptionChain("SMART", "756733", underlyingSymbol, "100", yyyymmdd, strikes),
            # The same chain as another exchange lists it, and an adjusted class with a date of its own.
            OptionChain("CBOE", "756733", underlyingSymbol, "100", yyyymmdd, strikes),
            OptionChain("SMART", "756733", f"{underlyingSymbol}1", "100", ["20270115"], strikes),
        ]

    def option(self, kind: str, at: int | str, expiry: date, *, symbol: str = "SPY", **changes: Any) -> Option:
        """The contract IBKR describes for a call or a put, a strike and an expiry, with any field changed."""
        contract = OptionContract.of(root=symbol, expiry_date=expiry, right=kind, strike=at)
        described = Option(
            symbol=symbol, lastTradeDateOrContractMonth=f"{expiry:%Y%m%d}", strike=float(at), right=kind[0].upper(),
            exchange="SMART", currency="USD", multiplier="100", tradingClass=symbol, conId=contract.strike_thousandths,
            localSymbol=f"{symbol:<6}{contract.occ_symbol[len(symbol):]}",
        )
        for name, value in changes.items():
            setattr(described, name, value)
        return described

    async def _details(self, asked: Option) -> list[ContractDetails]:
        expiry = datetime.strptime(asked.lastTradeDateOrContractMonth, "%Y%m%d").date()
        return [
            ContractDetails(contract=self.option(right, strike, expiry, symbol=asked.symbol))
            for strike in self.strikes
            for right in ("call", "put")
        ]

    def _request(self, contract: Any, *_: Any) -> Ticker:
        name = contract.symbol if contract.secType == "STK" else "".join(contract.localSymbol.split())
        ticker = Ticker(contract=contract, marketDataType=1)
        self._request_ids[ticker] = len(self._request_ids) + 1
        self.asked.append(name)
        if contract.secType == "OPT" and self.allowance is not None and self._option_lines() >= self.allowance:
            # IBKR answers a request past the allowance with error 101, a moment later.
            asyncio.get_running_loop().call_soon(self.refuse, ticker, 101)
        else:
            self.lines[name] = ticker
        return ticker

    def _option_lines(self) -> int:
        return sum(ticker.contract.secType == "OPT" for ticker in self.lines.values())

    def _cancel(self, contract: Any) -> None:
        name = next((name for name, ticker in self.lines.items() if ticker.contract is contract), None)
        if name is not None:
            del self.lines[name]
            self.cancelled.append(name)

    def refuse(self, ticker: Ticker, code: int) -> None:
        self.client.ib.errorEvent.emit(self._request_ids[ticker], code, "refused", ticker.contract)

    # ---- what a test delivers -------------------------------------------------

    def tick(self, name: str, *, bid: float | None = None, ask: float | None = None, sizes: bool = False) -> None:
        """One callback for a line: a price tick for each side given, or with ``sizes`` a tick of each side's size
        at the price it already has."""
        ticker = self.lines[name]
        ticker.time = datetime.fromtimestamp(self.now / 1000, tz=UTC)
        delayed = ticker.marketDataType in (3, 4)
        ticker.ticks = []
        if bid is not None:
            ticker.bid = bid
            ticker.ticks.append(TickData(ticker.time, 66 if delayed else 1, bid, 10))
        if ask is not None:
            ticker.ask = ask
            ticker.ticks.append(TickData(ticker.time, 67 if delayed else 2, ask, 10))
        if sizes:
            ticker.ticks += [TickData(ticker.time, 0, ticker.bid, 25), TickData(ticker.time, 3, ticker.ask, 25)]
        ticker.updateEvent.emit(ticker)

    def keep_alive(self, *, but: str | None = None) -> None:
        """A callback now for the underlying and every option line but ``but``, so none reads as stalled."""
        self.tick("SPY", bid=585.10, ask=585.12)
        for name in self.open_option_lines() - {but}:
            self.tick(name, bid=1.00, ask=1.02)

    def options_asked(self) -> list[str]:
        return [name for name in self.asked if name != "SPY"]

    def reconnect(self) -> None:
        """The socket dropped and came back: IBKR forgot every request."""
        self.lines.clear()
        self.client.connection_generation += 1

    def open_option_lines(self) -> set[str]:
        return {name for name, ticker in self.lines.items() if ticker.contract.secType == "OPT"}


Watching = tuple[IbkrMarketStatusSource, OptionQuoteLines, Gateway, list[WatchRequest]]


def _watching(
    monkeypatch: pytest.MonkeyPatch, *requests: WatchRequest, budget: int = 40, reach: int = 2, **gateway: Any
) -> Watching:
    ibkr = Gateway(monkeypatch, **gateway)
    wanted = list(requests)
    lines = OptionQuoteLines(
        requests=lambda: tuple(wanted), budget=budget, strikes_each_side=reach, clock=lambda: ibkr.now
    )
    status = IbkrMarketStatusSource(
        symbols=lambda: (), client=lambda: ibkr.client, clock=lambda: ibkr.now, options=lines
    )
    return status, lines, ibkr, wanted


async def _settle(status: IbkrMarketStatusSource, passes: int = 4) -> None:
    """Run the supervisor's pass until the listings it started reading have been read and acted on."""
    for _ in range(passes):
        await status()
        for _ in range(12):
            await asyncio.sleep(0)


def _bot(plan_strike: dict = _AT_THE_MONEY, *, held: tuple[OptionContract, ...] = (), **request: Any) -> WatchRequest:
    return WatchRequest(
        underlying="SPY",
        line_class=request.pop("line_class", LineClass.CANDIDATE),
        plan=scenes._one("call", plan_strike),
        money=request.pop("money", MONEY),
        held=held,
        **request,
    )


def _symbols(*strikes: int) -> set[str]:
    return {_call(strike).occ_symbol for strike in strikes}


def _held(lines: OptionQuoteLines, strike: int) -> HeldQuote | None:
    chain = lines.chain("SPY")
    assert chain is not None
    return chain.watched[_call(strike).occ_symbol].quote


# ---- What a callback leaves behind --------------------------------------------


async def test_each_side_of_a_quote_keeps_the_time_its_own_callback_delivered_it(monkeypatch: pytest.MonkeyPatch) -> None:
    status, lines, ibkr, _ = _watching(monkeypatch, _bot())
    await _settle(status)
    name = _call(585).occ_symbol

    ibkr.tick(name, bid=4.10)
    one_side = _held(lines, 585)
    ibkr.now += 700
    ibkr.tick(name, ask=4.14)
    ibkr.now += 3_000
    await _settle(status, passes=1)

    assert one_side == HeldQuote(QuoteSide(Decimal("4.1"), NOW), None, QuoteDataType.LIVE, lines.generation)
    # Three seconds and a supervisor pass later the receipts are what they were: reading renews nothing.
    assert _held(lines, 585) == HeldQuote(
        QuoteSide(Decimal("4.1"), NOW), QuoteSide(Decimal("4.14"), NOW + 700), QuoteDataType.LIVE, lines.generation
    )


async def test_a_tick_of_a_sides_size_is_a_receipt_for_that_side_and_a_callback_with_neither_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quiet contract's price does not tick. Its sizes do, and each says the price still stands. IBKR's own
    model figures arrive in callbacks that carry no side of the quote at all."""
    status, lines, ibkr, _ = _watching(monkeypatch, _bot())
    await _settle(status)
    name = _call(585).occ_symbol
    ibkr.tick(name, bid=4.10, ask=4.14)

    ibkr.now += 6_000
    ibkr.lines[name].modelGreeks = OptionComputation(0, impliedVol=0.2, delta=0.52)
    ibkr.tick(name)
    model_only = _held(lines, 585)
    ibkr.tick(name, sizes=True)
    sizes_ticked = _held(lines, 585)

    def fault(quote: HeldQuote | None) -> QuoteFault | None:
        assert quote is not None
        return quote_fault(quote, at_ms=ibkr.now, generation=lines.generation, max_age_ms=5_000, needs_a_bid=True)

    assert (model_only.bid.received_ms, fault(model_only)) == (NOW, QuoteFault.STALE)
    assert (sizes_ticked.bid.received_ms, sizes_ticked.ask.received_ms, fault(sizes_ticked)) == (NOW + 6_000, NOW + 6_000, None)
    assert sizes_ticked.bid.price == Decimal("4.1")
    assert lines.chain("SPY").watched[name].vendor_delta == 0.52


async def test_delayed_data_is_kept_as_delayed_and_a_switch_of_kind_leaves_nothing_received(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, lines, ibkr, _ = _watching(monkeypatch, _bot())
    await _settle(status)
    name = _call(585).occ_symbol
    ibkr.tick(name, bid=4.10, ask=4.14)

    ibkr.lines[name].marketDataType = 3
    ibkr.tick(name, bid=4.00)
    switched = _held(lines, 585)
    ibkr.tick(name, ask=4.20)
    delayed = _held(lines, 585)

    # The live ask does not stand beside a delayed bid.
    assert (switched.bid.price, switched.ask, switched.data_type) == (Decimal("4"), None, QuoteDataType.DELAYED)
    assert quote_fault(
        delayed, at_ms=ibkr.now, generation=lines.generation, max_age_ms=5_000, needs_a_bid=False
    ) is QuoteFault.NOT_LIVE


async def test_a_side_ibkr_delivers_as_nothing_there_is_held_as_zero_not_as_a_side_still_to_arrive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IBKR sends -1 for a side with nothing on it. A far call with no bid can still be bought at its ask; one
    with no ask cannot."""
    status, lines, ibkr, _ = _watching(monkeypatch, _bot())
    await _settle(status)
    ibkr.tick(_call(587).occ_symbol, bid=-1.0, ask=0.02)
    ibkr.tick(_call(586).occ_symbol, bid=0.01, ask=-1.0)

    def fault(strike: int, *, to_sell: bool) -> QuoteFault | None:
        return quote_fault(
            _held(lines, strike), at_ms=ibkr.now, generation=lines.generation, max_age_ms=5_000, needs_a_bid=to_sell
        )

    assert _held(lines, 587).bid == QuoteSide(Decimal(0), NOW)
    assert (fault(587, to_sell=False), fault(587, to_sell=True)) == (None, QuoteFault.BID_NOT_ABOVE_ZERO)
    assert fault(586, to_sell=False) is QuoteFault.ASK_NOT_ABOVE_ZERO


async def test_a_line_with_no_callback_yet_holds_no_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    status, lines, _, _ = _watching(monkeypatch, _bot())
    await _settle(status)

    chain = lines.chain("SPY")

    assert set(chain.watched) == _symbols(583, 584, 585, 586, 587)
    assert all(watched.quote is None for watched in chain.watched.values())
    assert chain.expiries == scenes._EXPIRIES


# ---- Which contracts hold a line ----------------------------------------------


async def test_a_bots_window_is_watched_around_its_bars_close_and_follows_it(monkeypatch: pytest.MonkeyPatch) -> None:
    status, _, ibkr, wanted = _watching(monkeypatch, _bot())
    await _settle(status)
    first = ibkr.open_option_lines()

    wanted[0] = _bot(money=Decimal("586.20"))
    await _settle(status, passes=1)

    assert first == _symbols(583, 584, 585, 586, 587)
    assert ibkr.open_option_lines() == _symbols(584, 585, 586, 587, 588)
    # The four strikes both windows hold were left as they were: one asked for, one given up.
    assert ibkr.asked.count(_call(585).occ_symbol) == 1
    assert ibkr.cancelled == [_call(583).occ_symbol]


async def test_with_no_bar_close_given_a_window_is_centred_on_the_underlyings_live_mid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, _, ibkr, _ = _watching(monkeypatch, _bot(money=None))
    await _settle(status)
    nothing_yet = ibkr.open_option_lines()

    ibkr.tick("SPY", bid=590.10, ask=590.12)
    await _settle(status, passes=1)

    # Until the underlying has a quote a decision could use there is nowhere to centre a window.
    assert nothing_yet == set()
    assert ibkr.open_option_lines() == _symbols(588, 589, 590, 591, 592)


async def test_a_window_is_neither_moved_nor_given_up_when_its_underlyings_quote_cannot_be_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Centred on a live mid of 590.11. The underlying's next quote is crossed, and then goes five seconds
    without a tick: no decision could use either, so neither moves the window, and the lines stay open."""
    status, _, ibkr, _ = _watching(monkeypatch, _bot(money=None))
    await _settle(status)
    ibkr.tick("SPY", bid=590.10, ask=590.12)
    await _settle(status, passes=1)
    centred = ibkr.open_option_lines()

    ibkr.tick("SPY", bid=600.30, ask=600.10)
    await _settle(status, passes=1)
    while_crossed = ibkr.open_option_lines()
    ibkr.tick("SPY", bid=600.10, ask=600.30)
    ibkr.now += 5_001
    await _settle(status, passes=1)

    assert centred == while_crossed == ibkr.open_option_lines() == _symbols(588, 589, 590, 591, 592)
    assert ibkr.cancelled == []


async def test_each_underlying_has_its_own_chain_and_its_own_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    leg = scenes._leg("only", "call", _AT_THE_MONEY)
    leg["instrument"]["underlying"] = "QQQ"
    on_qqq = WatchRequest("QQQ", LineClass.CANDIDATE, scenes._plan(leg), MONEY)
    status, lines, ibkr, _ = _watching(monkeypatch, _bot(), on_qqq)
    await _settle(status)
    ibkr.tick(_call(585).occ_symbol, bid=4.10, ask=4.14)

    spy, qqq = lines.chain("SPY"), lines.chain("QQQ")

    assert set(spy.watched) == _symbols(583, 584, 585, 586, 587)
    assert sorted(qqq.watched) == [f"QQQ261009C00{strike}000" for strike in (583, 584, 585, 586, 587)]
    assert {listing.contract.root for listing in spy.listings} == {"SPY"}
    assert {listing.contract.root for listing in qqq.listings} == {"QQQ"}
    assert qqq.watched["QQQ261009C00585000"].quote is None
    assert lines.chain("IWM") is None


async def test_a_held_contract_keeps_its_line_while_candidates_give_way_and_the_window_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget of three and a held 570 call, far outside the window: the held contract and the two strikes
    nearest the window's centre have lines. However the window moves, the held one is never given up."""
    held = (_call(570),)
    status, lines, ibkr, wanted = _watching(monkeypatch, _bot(held=held), budget=3)
    await _settle(status)
    before = ibkr.open_option_lines()

    for money in ("588.00", "592.00", "561.00"):
        wanted[0] = _bot(held=held, money=Decimal(money))
        await _settle(status, passes=1)

    assert before == _symbols(570, 585, 584)
    assert ibkr.open_option_lines() == _symbols(570, 561, 560)
    assert _call(570).occ_symbol not in ibkr.cancelled
    assert lines.line_use().open == 3
    assert set(lines.line_use().without.values()) == {NoLine.OVER_BUDGET}


async def test_a_preview_is_refused_a_line_when_none_is_spare_and_displaces_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    page = WatchRequest(
        underlying="SPY", line_class=LineClass.PREVIEW, plan=scenes._one("put", _AT_THE_MONEY), money=MONEY
    )
    status, lines, ibkr, _ = _watching(monkeypatch, _bot(), page, budget=5)
    await _settle(status)

    use = lines.line_use()

    assert ibkr.open_option_lines() == _symbols(583, 584, 585, 586, 587)
    assert len(use.without) == 5
    assert all(symbol[9] == "P" and reason == NoLine.OVER_BUDGET for symbol, reason in use.without.items())
    assert (use.budget, use.open, use.ceiling) == (5, 5, None)


async def test_one_contract_two_bots_want_is_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    status, _, ibkr, _ = _watching(monkeypatch, _bot(), _bot({"selector": "atm_offset", "offset": 1}))
    await _settle(status)

    assert ibkr.open_option_lines() == _symbols(583, 584, 585, 586, 587, 588)
    assert len(ibkr.asked) == len(set(ibkr.asked))


async def test_with_no_budget_nothing_is_watched_but_what_a_bot_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    status, lines, ibkr, _ = _watching(monkeypatch, _bot(held=(_call(570),)), budget=0)
    await _settle(status)

    assert ibkr.open_option_lines() == _symbols(570)
    assert len(lines.line_use().without) == 5


async def test_a_held_contract_ibkr_does_not_list_is_named_as_that_and_never_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot holds a 999 call the chain does not list for its expiry, as after an adjustment. The Clerk
    waits for the expiry's listing before saying so: a contract is not unlisted because nothing is read yet."""
    status, lines, ibkr, _ = _watching(monkeypatch, _bot(held=(_call(999), _call(570))))
    await _settle(status)

    assert ibkr.open_option_lines() == _symbols(570, 583, 584, 585, 586, 587)
    assert "does not list" in lines.line_use().without[_call(999).occ_symbol]


# ---- When IBKR refuses a line ---------------------------------------------------


async def test_a_line_ibkr_refuses_for_capacity_is_marked_as_that_and_not_asked_for_again_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Clerk's own budget allows five lines and the account has three left. The three nearest the window's
    centre are quoted. The other two are without a line for want of capacity, which is not the Clerk's budget."""
    status, lines, ibkr, _ = _watching(monkeypatch, _bot(), allowance=3)
    await _settle(status)
    asked_once = ibkr.options_asked()
    await _settle(status, passes=3)

    use = lines.line_use()

    assert ibkr.open_option_lines() == _symbols(585, 584, 586)
    assert use.ceiling == 3
    assert dict(use.without) == {symbol: NoLine.NO_CAPACITY for symbol in _symbols(583, 587)}
    assert ibkr.options_asked() == asked_once, "a refused line is not asked for on every pass"
    # The contract has a place in the chain and no quote: an entry that needs it is a search not yet complete.
    assert _call(587).occ_symbol not in lines.chain("SPY").watched


async def test_a_held_contract_ibkr_refuses_gets_a_line_freed_from_the_lowest_class_and_is_asked_for_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, lines, ibkr, wanted = _watching(monkeypatch, _bot(), allowance=5)
    await _settle(status)
    assert ibkr.open_option_lines() == _symbols(583, 584, 585, 586, 587)

    # The bot's entry filled: it now holds the 595 call, and the account has no line left for it.
    wanted[0] = _bot(held=(_call(595),))
    await _settle(status, passes=1)
    refused = ibkr.open_option_lines()
    await _settle(status, passes=2)
    asked_within_the_second = ibkr.asked.count(_call(595).occ_symbol)
    ibkr.now += 1_000
    await _settle(status, passes=2)

    assert _call(595).occ_symbol not in refused
    assert (asked_within_the_second, ibkr.asked.count(_call(595).occ_symbol)) == (1, 2)
    # One of the two candidates farthest from the centre gave its line up, and the held contract has it.
    assert ibkr.open_option_lines() == _symbols(595, 584, 585, 586, 583)
    assert lines.line_use().ceiling == 5


async def test_once_a_minute_one_more_line_is_tried_in_case_another_session_gave_some_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, lines, ibkr, _ = _watching(monkeypatch, _bot(), allowance=3)
    await _settle(status)
    asked_before = len(ibkr.options_asked())

    ibkr.now += CEILING_PROBE_MS - 1
    ibkr.keep_alive()
    await _settle(status, passes=2)
    too_soon = len(ibkr.options_asked())
    ibkr.allowance = 4
    ibkr.now += 1
    await _settle(status, passes=2)

    assert too_soon == asked_before
    assert len(ibkr.options_asked()) == asked_before + 1
    assert ibkr.open_option_lines() == _symbols(585, 584, 586, 583)
    assert lines.line_use().ceiling == 4


async def test_a_line_ibkr_refuses_for_another_reason_stays_refused_until_the_connection_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error 354: this account is not subscribed to the contract's market data. Asking again will not help."""
    status, lines, ibkr, _ = _watching(monkeypatch, _bot())
    await _settle(status)
    name = _call(585).occ_symbol

    ibkr.refuse(ibkr.lines[name], 354)
    ibkr.now += 120_000
    ibkr.keep_alive()
    await _settle(status, passes=3)

    assert ibkr.asked.count(name) == 1
    assert "code 354" in lines.line_use().without[name]
    assert name not in lines.chain("SPY").watched

    ibkr.reconnect()
    await _settle(status)
    assert ibkr.asked.count(name) == 2


# ---- Reconnects and repairs ---------------------------------------------------


async def test_a_reconnect_forgets_what_ibkr_would_not_grant_before_it(monkeypatch: pytest.MonkeyPatch) -> None:
    status, lines, ibkr, _ = _watching(monkeypatch, _bot(), allowance=3)
    await _settle(status)
    assert lines.line_use().ceiling == 3

    ibkr.allowance = None
    ibkr.reconnect()
    await _settle(status)

    use = lines.line_use()
    assert ibkr.open_option_lines() == _symbols(583, 584, 585, 586, 587)
    assert (use.ceiling, dict(use.without)) == (None, {})


async def test_a_reconnect_makes_every_older_quote_unusable_and_asks_for_the_lines_again_held_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, lines, ibkr, _ = _watching(monkeypatch, _bot(held=(_call(570),)))
    await _settle(status)
    for name in ibkr.open_option_lines():
        ibkr.tick(name, bid=4.10, ask=4.14)
    before = lines.generation
    picked_on = _held(lines, 585)

    ibkr.reconnect()
    ibkr.asked.clear()
    await _settle(status)

    assert lines.generation == before + 1
    assert status.option_feed().generation == lines.generation
    # A quote kept from before the reconnect fails as old; the book itself holds nothing from before it.
    assert quote_fault(
        picked_on, at_ms=ibkr.now, generation=lines.generation, max_age_ms=5_000, needs_a_bid=False
    ) is QuoteFault.OLD_GENERATION
    assert all(watched.quote is None for watched in lines.chain("SPY").watched.values())
    options_asked = [name for name in ibkr.asked if name != "SPY"]
    assert options_asked[0] == _call(570).occ_symbol
    assert set(options_asked) == _symbols(570, 583, 584, 585, 586, 587)


async def test_a_recovery_that_kept_the_requests_alive_keeps_the_lines_and_nothing_they_delivered_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IBKR's 1100 then 1102: the data farm was lost and came back with every request maintained."""
    status, lines, ibkr, _ = _watching(monkeypatch, _bot())
    await _settle(status)
    name = _call(585).occ_symbol
    ibkr.tick(name, bid=4.10, ask=4.14)
    asked = list(ibkr.asked)
    before = lines.generation

    ibkr.client.connection_state = "soft_lost"
    await _settle(status, passes=1)
    while_lost = status.option_feed()
    ibkr.tick(name, bid=9.99, ask=10.01)
    not_taken = _held(lines, 585)
    ibkr.client.connection_state = "connected"
    ibkr.client.connectivity_lost_count += 1
    ibkr.client.last_ibkr_code = 1102
    await _settle(status, passes=1)
    restored = _held(lines, 585)
    ibkr.tick(name, bid=4.20)

    assert while_lost.unhealthy is not None
    # A callback that arrives while IBKR says the data is lost changes nothing held.
    assert (not_taken.bid.price, not_taken.ask.price) == (Decimal("4.1"), Decimal("4.14"))
    assert (ibkr.asked, ibkr.cancelled) == (asked, [])
    assert lines.generation == before + 1
    assert restored is None
    assert _held(lines, 585) == HeldQuote(QuoteSide(Decimal("4.2"), NOW), None, QuoteDataType.LIVE, before + 1)


async def test_a_line_that_never_delivers_is_asked_for_again_and_one_gone_quiet_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 585 call's line delivers nothing at all: it is broken. The 584's delivered once and then had nothing
    new to say, as a quiet contract does: its quote is old, and still the last thing known of it."""
    status, lines, ibkr, _ = _watching(monkeypatch, _bot())
    await _settle(status)
    silent, quiet = _call(585).occ_symbol, _call(584).occ_symbol
    ibkr.keep_alive(but=silent)

    ibkr.now += SUBSCRIPTION_TIMEOUT_MS + 1
    ibkr.tick("SPY", bid=585.10, ask=585.12)
    await _settle(status, passes=1)
    given_up = silent not in ibkr.lines
    ibkr.now += 1_000
    await _settle(status, passes=1)

    assert given_up
    assert (ibkr.asked.count(silent), ibkr.cancelled) == (2, [silent])
    assert _held(lines, 585) is None
    assert ibkr.asked.count(quiet) == 1
    assert _held(lines, 584).bid == QuoteSide(Decimal("1"), NOW)


async def test_a_dropped_connection_gives_every_line_up_and_says_the_feed_is_not_up(monkeypatch: pytest.MonkeyPatch) -> None:
    status, lines, ibkr, _ = _watching(monkeypatch, _bot())
    await _settle(status)

    ibkr.client.is_connected.return_value = False
    await _settle(status, passes=1)

    assert status.option_feed().unhealthy is not None
    assert lines.line_use().open == 0
    assert status.underlying_quote("SPY") is None


# ---- The listing ---------------------------------------------------------------


async def test_the_chains_expiries_are_those_of_the_underlyings_own_class(monkeypatch: pytest.MonkeyPatch) -> None:
    ibkr = Gateway(monkeypatch)

    assert await listed_expiries(ibkr.client, "SPY") == scenes._EXPIRIES


async def test_a_listed_contract_is_the_one_its_own_symbol_names_with_the_multiplier_ibkr_gives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ibkr = Gateway(monkeypatch, strikes=[585])
    described = [
        ibkr.option("call", 585, FRIDAY),
        ibkr.option("put", "585.5", FRIDAY, multiplier="10"),
        # An adjusted contract, a symbol that is not one, and a contract whose fields disagree with its symbol.
        ibkr.option("call", 590, FRIDAY, tradingClass="SPY1", localSymbol="SPY1  261009C00590000"),
        ibkr.option("call", 591, FRIDAY, localSymbol="SPY 591 C"),
        ibkr.option("call", 592, FRIDAY, strike=593.0),
        ibkr.option("put", 594, FRIDAY, right="C"),
        ibkr.option("call", 595, FRIDAY, tradingClass="SPYW"),
    ]
    ibkr.client.ib.reqContractDetailsAsync = AsyncMock(
        return_value=[ContractDetails(contract=contract) for contract in described]
    )

    listed = await listed_contracts(ibkr.client, "SPY", FRIDAY)

    assert set(listed) == {"SPY261009C00585000", "SPY261009P00585500"}
    standard, mini = listed["SPY261009C00585000"], listed["SPY261009P00585500"]
    assert (standard.listing.multiplier, standard.listing.is_standard, standard.ibkr_contract) == (100, True, described[0])
    assert (mini.listing.multiplier, mini.listing.is_standard) == (10, False)
    assert ibkr.client.ib.reqContractDetailsAsync.await_args.args[0].lastTradeDateOrContractMonth == "20261009"


async def test_only_the_expiries_a_plan_comes_to_have_their_contracts_read(monkeypatch: pytest.MonkeyPatch) -> None:
    status, lines, ibkr, _ = _watching(monkeypatch, _bot())
    await _settle(status)

    chain = lines.chain("SPY")

    assert {listing.contract.expiry_date for listing in chain.listings} == {FRIDAY}
    assert chain.expiries == scenes._EXPIRIES
    assert ibkr.client.ib.reqContractDetailsAsync.await_count == 1
    assert ibkr.client.ib.reqSecDefOptParamsAsync.await_count == 1


async def test_a_listing_that_cannot_be_read_is_tried_again_and_watches_nothing_meanwhile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, lines, ibkr, _ = _watching(monkeypatch, _bot())
    reads = ibkr.client.ib.reqSecDefOptParamsAsync
    reads.side_effect = [TimeoutError("no answer"), *([await ibkr._chains()] * 3)]

    await _settle(status, passes=2)
    unread = lines.chain("SPY")
    ibkr.now += 1_000
    await _settle(status)

    assert (unread, reads.await_count) == (None, 2)
    assert ibkr.open_option_lines() == _symbols(583, 584, 585, 586, 587)


# ---- The whole path ------------------------------------------------------------


async def test_the_quotes_the_supervisor_holds_are_what_a_resolution_picks_from(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolver's own scene, delivered tick by tick through the supervisor. The rule comes to the contract
    it comes to when the same quotes are handed over directly."""
    scene = scenes._scene()
    plan = scenes._one("call", {"selector": "delta", "target": 0.30})
    status, lines, ibkr, _ = _watching(monkeypatch, WatchRequest("SPY", LineClass.CANDIDATE, plan, MONEY), reach=12)
    await _settle(status)
    ibkr.now = scene.selection_ms - 400
    for name in ibkr.open_option_lines():
        quote = scene.chain.watched[name].quote
        ibkr.tick(name, bid=float(quote.bid.price), ask=float(quote.ask.price))
    underlying = scene.underlying.quote
    ibkr.tick("SPY", bid=float(underlying.bid.price), ask=float(underlying.ask.price))
    ibkr.now = scene.selection_ms

    resolution = resolve_contracts(
        plan,
        chain=lines.chain("SPY"),
        underlying=UnderlyingObservation(scene.underlying.pricing_bar_close, status.underlying_quote("SPY")),
        feed=status.option_feed(),
        model=scene.model,
        limits=scene.limits,
        decision_bar_close_ms=scene.decision_bar_close_ms,
        selection_ms=scene.selection_ms,
    )

    assert resolution.picked == scene.resolve(plan).picked
    assert resolution.quotes["only"] == replace(scene.quote_of(resolution.picked["only"]), generation=lines.generation)
    assert resolution.evidence.legs[0].listed_expiries_ms == tuple(
        scenes.option_expiry_anchor_ms(day) for day in scenes._EXPIRIES
    )


async def test_a_clerk_that_watches_no_option_contract_has_no_option_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    ibkr = Gateway(monkeypatch)
    status = IbkrMarketStatusSource(symbols=lambda: ("SPY",), client=lambda: ibkr.client, clock=lambda: ibkr.now)

    await _settle(status, passes=2)

    assert (status.option_feed(), status.underlying_quote("SPY")) == (None, None)
    assert ibkr.asked == ["SPY"]

