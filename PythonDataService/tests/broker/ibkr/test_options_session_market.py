"""IBKR completed trade bars and canonical setup picks; no quote-price substitution."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from app.broker.ibkr import options_session_market as module
from app.broker.ibkr.option_chain import ListedContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.resolver import Chain, FeedState, Listing, WatchedContract
from app.options_backtest.market import Picked
from app.options_bots.session import OpenLeg
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.services.option_watch import OptionWatchList
from app.utils.timestamps import datetime_at_ms
from tests.options_backtest._market import FULL_CLOSE_MS, OPEN_MS, D, at, contract, leg, setup

CALL = contract("call", "500")


class _Errors:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, callback: Any) -> _Errors:
        self.handlers.append(callback)
        return self

    def __isub__(self, callback: Any) -> _Errors:
        self.handlers.remove(callback)
        return self

    def emit(self, *args: Any) -> None:
        for handler in tuple(self.handlers):
            handler(*args)


class _IB:
    def __init__(self) -> None:
        self.client = self.wrapper = self
        self.errorEvent = _Errors()
        self.requests: list[tuple[Any, ...]] = []
        self.cancelled: list[int] = []
        self.waiters: dict[int, tuple[asyncio.Future[Any], Any]] = {}
        self.rows: dict[int, list[Any]] = {}
        self.error: tuple[int, str] | None = None
        self.never_finishes = False
        self._next = 0

    def getReqId(self) -> int:
        self._next += 1
        return self._next

    def startReq(self, identity: int, _contract: Any, *, container: Any) -> asyncio.Future[Any]:
        future = asyncio.get_running_loop().create_future()
        self.waiters[identity] = future, container
        return future

    def reqHistoricalData(self, identity: int, ibkr_contract: Any, *args: Any) -> None:
        self.requests.append((ibkr_contract.conId, *args))
        if self.never_finishes:
            return
        future, bars = self.waiters[identity]
        if self.error is not None:
            self.errorEvent.emit(identity, *self.error, ibkr_contract)
        bars.extend(self.rows.get(ibkr_contract.conId, []))
        future.set_result(bars)

    def cancelHistoricalData(self, identity: int) -> None:
        self.cancelled.append(identity)

    def _endReq(self, identity: int) -> None:
        self.waiters.pop(identity, None)


class _Client:
    def __init__(self) -> None:
        self.ib = _IB()
        self.connection_generation = 1
        self.data_loss_epoch = 0

    def require_connected(self) -> None:
        return


def _raw(minute: int, price: str, *, volume: int = 1) -> SimpleNamespace:
    # IBKR names this bar by its open. Rules name it by its close.
    return SimpleNamespace(
        date=datetime_at_ms(at(minute - 1)), open=price, high=price, low=price, close=price, volume=volume
    )


def _listed(option: Any, identity: int) -> ListedContract:
    return ListedContract(
        Listing(option, multiplier=100, shares_delivered=100, exercise="american"),
        SimpleNamespace(symbol="SPY", conId=identity),
    )


@pytest.fixture
async def market_inputs(monkeypatch: pytest.MonkeyPatch) -> tuple[_Client, OptionWatchList]:
    client = _Client()
    client.ib.rows = {1: [_raw(5, "500")], 2: [_raw(5, "2.00")]}

    async def underlying(_client: Any, _symbol: str) -> Any:
        return SimpleNamespace(symbol="SPY", conId=1)

    async def expiries(_client: Any, _symbol: str) -> tuple[date, ...]:
        return (date(2026, 10, 9), date(2026, 10, 16))

    async def contracts(_client: Any, _symbol: str, expiry: date) -> dict[str, ListedContract]:
        if expiry != date(2026, 10, 9):
            raise AssertionError("this-week's Friday expiry is today, not next Friday")
        return {CALL.occ_symbol: _listed(CALL, 2)}

    monkeypatch.setattr(module, "qualify_underlying", underlying)
    monkeypatch.setattr(module, "listed_expiries", expiries)
    monkeypatch.setattr(module, "listed_contracts", contracts)
    return client, OptionWatchList(clock=lambda: at(100))


def _market(written: Any, inputs: tuple[_Client, OptionWatchList], **kwargs: Any) -> module.IbkrOptionsSessionMarket:
    client, watch = inputs
    return module.IbkrOptionsSessionMarket(
        written,
        session_open_ms=OPEN_MS,
        session_close_ms=FULL_CLOSE_MS,
        client=client,
        watch=watch,
        clock=lambda: at(100),
        **kwargs,
    )


async def test_completed_option_trade_close_and_friday_expiry_use_exact_setup_rules(
    market_inputs: tuple[_Client, OptionWatchList],
) -> None:
    written_json = setup(leg()).model_dump(mode="json")
    written_json["legs"][0]["expiry"] = {"kind": "this_week"}
    written = module.OptionsBacktestSetup.model_validate(written_json)
    market = _market(written, market_inputs)

    async with market:
        await market.prepare(at(5))
        assert market.underlying_close(at(5)) == D("500")
        pick = market.pick(written.legs[0], at_ms=at(5), underlying_price=D("500"))
        assert isinstance(pick, Picked) and pick.contract == CALL
        assert market.option_bar(CALL, at(5)).close == D("2.00")
        assert len(market_inputs[1].requests()) == 1
        request = market_inputs[0].ib.requests[0]
        assert request[2:8] == ("120 S", "1 min", "TRADES", True, 2, False)
        assert request[1] == "20261009-13:35:00"

    assert market_inputs[1].requests() == ()
    assert market_inputs[0].ib.errorEvent.handlers == []


async def test_successful_empty_or_zero_volume_minute_means_no_trade_without_forward_fill(
    market_inputs: tuple[_Client, OptionWatchList],
) -> None:
    client, _watch = market_inputs
    market = _market(setup(leg()), market_inputs)
    await market.prepare(at(5))
    client.ib.rows = {1: [_raw(6, "501")], 2: [_raw(5, "2.00"), _raw(6, "2.00", volume=0)]}
    await market.prepare(at(6), contracts=(CALL,))
    assert market.underlying_close(at(6)) == D("501")
    assert market.option_bar(CALL, at(6)) is None
    with pytest.raises(module.OptionsMarketUnavailable, match="not been prepared"):
        market.option_bar(CALL, at(7))


async def test_request_permission_error_is_unavailable_and_does_not_publish_a_no_trade_frame(
    market_inputs: tuple[_Client, OptionWatchList],
) -> None:
    client, _watch = market_inputs
    market = _market(setup(leg()), market_inputs)
    client.ib.error = (162, "No market data permissions for this contract")
    with pytest.raises(module.OptionsMarketUnavailable, match="permissions"):
        await market.prepare(at(5))
    with pytest.raises(module.OptionsMarketUnavailable, match="not been prepared"):
        market.underlying_close(at(5))
    assert client.ib.errorEvent.handlers == []


async def test_timeout_cancels_its_exact_ibkr_request_instead_of_returning_empty(
    market_inputs: tuple[_Client, OptionWatchList],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _watch = market_inputs
    client.ib.never_finishes = True
    monkeypatch.setattr(module, "_HISTORY_TIMEOUT_S", 0.001)
    market = _market(setup(leg()), market_inputs)
    with pytest.raises(module.OptionsMarketUnavailable, match="did not finish"):
        await market.prepare(at(5))
    assert client.ib.cancelled == [1]
    assert client.ib.errorEvent.handlers == []
    assert client.ib.waiters == {}


async def test_repeated_same_minute_is_cached_but_reconnect_cannot_reuse_its_generation(
    market_inputs: tuple[_Client, OptionWatchList],
) -> None:
    client, _watch = market_inputs
    market = _market(setup(leg()), market_inputs)
    await market.prepare(at(5))
    await market.prepare(at(5), contracts=(CALL,), held_contracts=(CALL,))
    assert len(client.ib.requests) == 2
    client.connection_generation = 2
    await market.prepare(at(5), held_contracts=(CALL,))
    assert len(client.ib.requests) == 4
    assert any(request.held == (CALL,) for request in market_inputs[1].requests())


async def test_quote_is_only_execution_evidence_and_never_a_substitute_for_missing_trade(
    market_inputs: tuple[_Client, OptionWatchList],
) -> None:
    client, _watch = market_inputs
    now = at(100)
    quote = HeldQuote(QuoteSide(D("9.90"), now), QuoteSide(D("10.00"), now), QuoteDataType.LIVE, generation=7)
    source = SimpleNamespace(
        option_feed=lambda: FeedState(7, None),
        option_chain=lambda _symbol: Chain((), {CALL.occ_symbol: WatchedContract(quote, None)}),
    )
    market = _market(setup(leg()), market_inputs, quotes=lambda: source)
    client.ib.rows[2] = []
    await market.prepare(at(5))
    assert market.option_bar(CALL, at(5)) is None
    decision = OpenLeg("call", 0, CALL, 1, 100, at(5), "buy")
    terms = SealedOptionOrderTerms(
        gap_limit_percent="10", entry_wait_seconds=20, cost_limit_percent="10", expiry_close_lead_minutes=45
    )
    evidence = await market.entry_evidence(decision, terms, proven_by_ms=now + 50_000)
    assert evidence.quotes == {"call": quote}
    assert evidence.picked == {"call": CALL}
    assert evidence.generation == 7


async def test_malformed_option_bar_is_unavailable_instead_of_missing_or_repaired(
    market_inputs: tuple[_Client, OptionWatchList],
) -> None:
    client, _watch = market_inputs
    client.ib.rows[2] = [_raw(5, "0")]
    market = _market(setup(leg()), market_inputs)
    with pytest.raises(module.OptionsMarketUnavailable, match="not positive"):
        await market.prepare(at(5))
