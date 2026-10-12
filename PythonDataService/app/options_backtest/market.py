"""What a replay asks of one past session's market data (#3030).

A minute bar is named by its close: the bar closing at ``t`` holds the trades
in ``[t - 60 s, t)``, and its ``open`` is the first of them. A minute with no
trade has no bar. Nothing here interpolates or carries a price forward.

Every price is above zero. Whoever implements ``SessionMarket`` has made sure the
session's data is there to read before a replay starts, so ``None`` only ever means
that nothing traded in that minute, and never that the data is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.options.contract import OptionContract
from app.options_backtest.setup import LegSetup

# The only contract a backtest trades: 100 shares a contract.
STANDARD_MULTIPLIER = 100


@dataclass(frozen=True)
class MinuteBar:
    """One minute of observed trades in one instrument."""

    close_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


class PickRefusal(StrEnum):
    """Why a leg's rules came to no contract."""

    CHAIN_INCOMPLETE = "chain_incomplete"
    EXPIRY_NOT_LISTED = "expiry_not_listed"
    EXPIRY_NOT_A_SESSION = "expiry_not_a_session"
    STRIKE_NOT_LISTED = "strike_not_listed"
    NOT_STANDARD = "not_standard"


@dataclass(frozen=True)
class Picked:
    """The contract a leg's rules came to."""

    contract: OptionContract
    multiplier: int = STANDARD_MULTIPLIER


@dataclass(frozen=True)
class PickRefused:
    """A leg's rules came to no contract, and why, in words an owner can read."""

    reason: PickRefusal
    detail: str


class SessionMarket(Protocol):
    """One session's admitted observations, as the replay reads them."""

    @property
    def session_open_ms(self) -> int: ...

    @property
    def session_close_ms(self) -> int: ...

    def underlying_close(self, close_ms: int) -> Decimal | None:
        """The close of the underlying's raw bar closing at ``close_ms``, or None when it did not trade.

        ``close_ms`` is a whole minute after the session's open and no later than its close.
        """
        ...

    def pick(self, leg: LegSetup, *, at_ms: int, underlying_price: Decimal) -> Picked | PickRefused:
        """The contract ``leg``'s rules come to with the underlying at ``underlying_price``.

        ``at_ms`` is the minute close the pick is decided at. A pick depends on the session's
        trading date and on the price, so the same leg and price give the same contract at any
        minute of one session.
        """
        ...

    def option_bar(self, contract: OptionContract, close_ms: int) -> MinuteBar | None:
        """The contract's bar closing at ``close_ms``, or None when it did not trade in that minute.

        ``close_ms`` is a whole minute after the session's open and no later than its close.
        """
        ...
