"""What a run reads of the past, and the pin it keeps of each thing it read (#3030).

A run replays many sessions and asks, for each, three things of whoever stores the market's history: the
underlying's minute bars, the listing of option contracts, and the minute bars of each contract the session
could trade. This is the whole of that conversation. The run knows nothing of a lake, a vendor or a file:
another piece implements ``RunData`` over the lake, and tests implement it over dictionaries.

``None`` means not captured: the data is not there to read, and a run that needs it stops and says so. An
empty sequence means captured, and nothing traded. The two are never the same answer.

A bar is named by its close, as ``MinuteBar`` says: the bar closing at ``t`` holds the trades in
``[t - 60 s, t)``. Whoever implements ``RunData`` converts a vendor's start-of-minute times once, when it reads;
the run adds and subtracts nothing. Prices are raw, never split-adjusted, in time order.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.options.contract import OptionContract
from app.options_backtest.market import MinuteBar
from app.options_backtest.picking import Listing

_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Captured[T]:
    """A value read from storage, with the sha256 of the stored file it was read from.

    The run records the hash with each session's result, so a later replay can refuse data that has changed.
    """

    value: T
    sha256: str

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError(f"a pin is a sha256 as 64 lowercase hex digits; got {self.sha256!r}")


class RunData(Protocol):
    """Everything a run reads. Each answer is ``None`` when the data is not captured."""

    def underlying_day(self, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        """The underlying's raw minute bars for the session on ``trading_date``."""
        ...

    def listing(self, trading_date: date) -> Captured[Listing] | None:
        """The option contracts the underlying listed as of ``trading_date``."""
        ...

    def option_day(self, contract: OptionContract, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        """One contract's minute bars for the session on ``trading_date``; empty when it never traded."""
        ...
