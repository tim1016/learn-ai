"""Unit tests for the pessimistic intrabar bracket resolver.

The core invariant: when BOTH TP and SL fall inside a single bar's
``[low, high]`` range, SL wins. This is the "if a strategy survives
the pessimistic resolver, trust the edge" modeling choice that kills
the TradingView-style inflated-win-rate bias.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.intrabar_resolver import (
    BracketResolution,
    IntrabarOutcome,
    resolve_bracket_pessimistic,
)
from app.engine.execution.order import Direction


def _bar(high: str, low: str, close: str = "500.00") -> TradeBar:
    start = datetime(2024, 1, 2, 14, 45, tzinfo=UTC)
    end = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=end,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1_000_000,
    )


# ---------------------------------------------------------------------------
# LONG position
# ---------------------------------------------------------------------------


def test_long_both_in_range_stop_loss_wins():
    """Core pessimistic rule: both TP and SL inside the bar's range → SL."""
    bar = _bar(high="512", low="488")
    resolution = resolve_bracket_pessimistic(
        bar,
        Direction.LONG,
        take_profit_price=Decimal("510"),
        stop_loss_price=Decimal("490"),
    )
    assert resolution == BracketResolution(IntrabarOutcome.STOP_LOSS, Decimal("490"))


def test_long_only_take_profit_hit():
    bar = _bar(high="512", low="495")
    resolution = resolve_bracket_pessimistic(
        bar,
        Direction.LONG,
        take_profit_price=Decimal("510"),
        stop_loss_price=Decimal("490"),
    )
    assert resolution == BracketResolution(IntrabarOutcome.TAKE_PROFIT, Decimal("510"))


def test_long_only_stop_loss_hit():
    bar = _bar(high="505", low="488")
    resolution = resolve_bracket_pessimistic(
        bar,
        Direction.LONG,
        take_profit_price=Decimal("510"),
        stop_loss_price=Decimal("490"),
    )
    assert resolution == BracketResolution(IntrabarOutcome.STOP_LOSS, Decimal("490"))


def test_long_neither_hit():
    bar = _bar(high="505", low="495")
    resolution = resolve_bracket_pessimistic(
        bar,
        Direction.LONG,
        take_profit_price=Decimal("510"),
        stop_loss_price=Decimal("490"),
    )
    assert resolution == BracketResolution(IntrabarOutcome.NONE, None)


def test_long_take_profit_equal_to_high_triggers():
    """Inclusive edge: ``bar.high == tp`` counts as a touch."""
    bar = _bar(high="510", low="495")
    resolution = resolve_bracket_pessimistic(
        bar,
        Direction.LONG,
        take_profit_price=Decimal("510"),
        stop_loss_price=None,
    )
    assert resolution == BracketResolution(IntrabarOutcome.TAKE_PROFIT, Decimal("510"))


def test_long_stop_loss_equal_to_low_triggers():
    """Inclusive edge: ``bar.low == sl`` counts as a touch."""
    bar = _bar(high="510", low="490")
    resolution = resolve_bracket_pessimistic(
        bar,
        Direction.LONG,
        take_profit_price=None,
        stop_loss_price=Decimal("490"),
    )
    assert resolution == BracketResolution(IntrabarOutcome.STOP_LOSS, Decimal("490"))


# ---------------------------------------------------------------------------
# SHORT position — signs flip
# ---------------------------------------------------------------------------


def test_short_both_in_range_stop_loss_wins():
    """For a SHORT, TP is BELOW entry (bar.low) and SL is ABOVE (bar.high).
    With both in range, SL still wins."""
    bar = _bar(high="512", low="488")
    resolution = resolve_bracket_pessimistic(
        bar,
        Direction.SHORT,
        take_profit_price=Decimal("490"),
        stop_loss_price=Decimal("510"),
    )
    assert resolution == BracketResolution(IntrabarOutcome.STOP_LOSS, Decimal("510"))


def test_short_only_take_profit_hit():
    bar = _bar(high="505", low="488")
    resolution = resolve_bracket_pessimistic(
        bar,
        Direction.SHORT,
        take_profit_price=Decimal("490"),
        stop_loss_price=Decimal("510"),
    )
    assert resolution == BracketResolution(IntrabarOutcome.TAKE_PROFIT, Decimal("490"))


def test_short_only_stop_loss_hit():
    bar = _bar(high="512", low="495")
    resolution = resolve_bracket_pessimistic(
        bar,
        Direction.SHORT,
        take_profit_price=Decimal("490"),
        stop_loss_price=Decimal("510"),
    )
    assert resolution == BracketResolution(IntrabarOutcome.STOP_LOSS, Decimal("510"))


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_no_bracket_levels_returns_none(direction: Direction):
    bar = _bar(high="512", low="488")
    resolution = resolve_bracket_pessimistic(bar, direction, None, None)
    assert resolution == BracketResolution(IntrabarOutcome.NONE, None)


def test_flat_position_never_triggers():
    """A flat position can't be stopped out or taken-profit — always NONE."""
    bar = _bar(high="512", low="488")
    resolution = resolve_bracket_pessimistic(
        bar,
        Direction.FLAT,
        take_profit_price=Decimal("510"),
        stop_loss_price=Decimal("490"),
    )
    assert resolution == BracketResolution(IntrabarOutcome.NONE, None)


def test_long_with_only_take_profit_set():
    """Missing SL — only TP is watched."""
    bar = _bar(high="512", low="488")
    resolution = resolve_bracket_pessimistic(
        bar,
        Direction.LONG,
        take_profit_price=Decimal("510"),
        stop_loss_price=None,
    )
    assert resolution == BracketResolution(IntrabarOutcome.TAKE_PROFIT, Decimal("510"))


def test_long_with_only_stop_loss_set():
    """Missing TP — only SL is watched."""
    bar = _bar(high="512", low="488")
    resolution = resolve_bracket_pessimistic(
        bar,
        Direction.LONG,
        take_profit_price=None,
        stop_loss_price=Decimal("490"),
    )
    assert resolution == BracketResolution(IntrabarOutcome.STOP_LOSS, Decimal("490"))
