"""Pessimistic intrabar bracket resolver.

Resolves take-profit / stop-loss outcomes against a consolidated bar's
``[low, high]`` range. When BOTH TP and SL fall inside a single bar's
range we have a genuine ambiguity — without sub-bar path data we cannot
know which fired first.

Following institutional worst-case convention, the adverse leg (SL)
wins the tie. If a strategy's claimed edge survives pessimistic
intrabar resolution, its win-rate claim is robust to the path we
couldn't see. Without this rule TradingView-style backtests silently
assume the favorable leg fires first and inflate win rates.

The bar magnifier (PR 5) will replay 1-minute data to resolve this
ambiguity non-pessimistically. Until then, pessimistic is the default
and only mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction


class IntrabarOutcome(Enum):
    NONE = "none"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"


@dataclass(frozen=True)
class BracketResolution:
    outcome: IntrabarOutcome
    fill_price: Decimal | None = None


def resolve_bracket_pessimistic(
    bar: TradeBar,
    position_direction: Direction,
    take_profit_price: Decimal | None,
    stop_loss_price: Decimal | None,
) -> BracketResolution:
    """Decide whether ``bar``'s range triggered TP, SL, or neither.

    For a LONG position: TP is above the entry (triggers when
    ``bar.high >= tp``); SL is below (triggers when ``bar.low <= sl``).
    For a SHORT position the signs flip: TP is below the entry
    (``bar.low <= tp``), SL is above (``bar.high >= sl``).

    When both trigger on the same bar, SL wins — pessimistic.

    A flat position or a bracket with neither level set returns
    ``NONE`` and leaves ``fill_price`` at ``None``.
    """
    if position_direction is Direction.FLAT:
        return BracketResolution(IntrabarOutcome.NONE)
    if take_profit_price is None and stop_loss_price is None:
        return BracketResolution(IntrabarOutcome.NONE)

    if position_direction is Direction.LONG:
        tp_hit = take_profit_price is not None and bar.high >= take_profit_price
        sl_hit = stop_loss_price is not None and bar.low <= stop_loss_price
    else:
        tp_hit = take_profit_price is not None and bar.low <= take_profit_price
        sl_hit = stop_loss_price is not None and bar.high >= stop_loss_price

    if sl_hit:
        return BracketResolution(IntrabarOutcome.STOP_LOSS, stop_loss_price)
    if tp_hit:
        return BracketResolution(IntrabarOutcome.TAKE_PROFIT, take_profit_price)
    return BracketResolution(IntrabarOutcome.NONE)
