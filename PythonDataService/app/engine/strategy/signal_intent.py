"""Instrument-free decisions emitted by signal-only strategies.

Strategies own *when* to enter or exit. The execution boundary owns *what*
instrument is traded and how it is sized. ``SignalIntent`` is the narrow
contract between those concerns: it intentionally contains neither a symbol
nor a quantity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class SignalIntentKind(StrEnum):
    """The two long-only lifecycle decisions supported by the stock runtime."""

    ENTER = "ENTER"
    EXIT = "EXIT"


@dataclass(frozen=True)
class SignalIntent:
    """A strategy decision at a consolidated-bar close.

    ``bar_close_ms`` is UTC epoch milliseconds at the strategy/execution
    boundary. ``intended_price`` is observability for the signal stream; it
    must not be used to price a separately selected traded instrument.
    """

    kind: SignalIntentKind
    bar_close_ms: int
    intended_price: Decimal
