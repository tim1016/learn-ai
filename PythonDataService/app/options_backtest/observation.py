"""Immutable post-decision minute evidence for Options research (#3332, #3333).

Formula: equity = cash + Σ(signed contracts × multiplier × contemporaneous close).
  Collateral restricts available cash and is not subtracted from equity.
Reference: owner-defined observation and fixed-capital conventions in #3332.
Canonical implementation: this file for minute valuation; replay.py owns fills and cash.
Validated against: tests/options_backtest/test_replay_observation.py.

An absent held mark makes equity unavailable. Last-trade marks are never substituted.
The session open is an initial-capital observation, followed by every scheduled minute close.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from pydantic import TypeAdapter

from app.options.contract import OptionContract
from app.options_backtest import fills
from app.utils.session_anchors import MAX_TIMESTAMP_MS

MARK_RULE_VERSION = "options-held-minute-close-v1"


@dataclass(frozen=True)
class HeldMark:
    leg_id: str
    contract: OptionContract
    signed_contracts: int
    multiplier: int
    close: Decimal | None
    close_pending: bool

    def __post_init__(self) -> None:
        if type(self.signed_contracts) is not int or self.signed_contracts == 0:
            raise ValueError("a held mark needs a nonzero signed integer quantity")
        if type(self.multiplier) is not int or self.multiplier <= 0:
            raise ValueError("a held mark needs a positive integer multiplier")
        if self.close is not None and (not self.close.is_finite() or self.close <= 0):
            raise ValueError("a present held close must be finite and positive")


@dataclass(frozen=True)
class MinuteObservation:
    at_ms: int
    cash_usd: Decimal
    collateral_usd: Decimal
    held: tuple[HeldMark, ...] = ()

    def __post_init__(self) -> None:
        if type(self.at_ms) is not int or not 0 <= self.at_ms <= MAX_TIMESTAMP_MS:
            raise ValueError("an observation timestamp must be bounded int64 UTC milliseconds")
        if not self.cash_usd.is_finite() or not self.collateral_usd.is_finite() or self.collateral_usd < 0:
            raise ValueError("observation cash must be finite and collateral finite and nonnegative")
        if len({mark.leg_id for mark in self.held}) != len(self.held):
            raise ValueError("a minute observation names each held leg once")

    @property
    def missing_leg_ids(self) -> tuple[str, ...]:
        return tuple(mark.leg_id for mark in self.held if mark.close is None)

    @property
    def equity_usd(self) -> Decimal | None:
        """Exact value of the observed holdings, unavailable if a required mark is missing."""
        if self.missing_leg_ids:
            return None
        with localcontext(fills.EXACT):
            return self.cash_usd + sum(
                (mark.signed_contracts * mark.multiplier * mark.close for mark in self.held if mark.close is not None),
                start=Decimal(0),
            )


MINUTE_OBSERVATION_ADAPTER = TypeAdapter(MinuteObservation)
