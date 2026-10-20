"""Capture only the admitted trade minutes consumed by the session (#3368)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.options.contract import OptionContract
from app.options_backtest.market import MinuteBar, Picked, PickRefused, SessionMarket
from app.options_backtest.setup import LegSetup


@dataclass(frozen=True)
class ConsumedMinute:
    symbol: str
    close_ms: int
    bar: MinuteBar | None


@dataclass(frozen=True)
class ObservedOrder:
    order_ref: str
    attempt_id: str
    leg_id: str
    entry_number: int
    symbol: str
    kind: str
    filled_at_ms: int


class TradeMinuteMarket(SessionMarket, Protocol):
    def underlying_bar(self, close_ms: int) -> MinuteBar | None: ...


class ObservedMarket:
    """The rules' narrow market interface, with an atomic-commit observation buffer."""

    def __init__(self, market: TradeMinuteMarket, underlying: str) -> None:
        self.market, self.underlying = market, underlying
        self.minutes: list[ConsumedMinute] = []
        self.session_open_ms = market.session_open_ms
        self.session_close_ms = market.session_close_ms

    def underlying_close(self, close_ms: int) -> Decimal | None:
        price = self.market.underlying_close(close_ms)
        self.minutes.append(ConsumedMinute(self.underlying, close_ms, self.market.underlying_bar(close_ms)))
        return price

    def pick(self, leg: LegSetup, *, at_ms: int, underlying_price: Decimal) -> Picked | PickRefused:
        return self.market.pick(leg, at_ms=at_ms, underlying_price=underlying_price)

    def option_bar(self, contract: OptionContract, close_ms: int) -> MinuteBar | None:
        bar = self.market.option_bar(contract, close_ms)
        self.minutes.append(ConsumedMinute(contract.occ_symbol, close_ms, bar))
        return bar
