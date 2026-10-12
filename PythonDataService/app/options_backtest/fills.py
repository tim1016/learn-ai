"""The price and money arithmetic of an options backtest replay (#3030).

Formula: buy fill   = ceil_to_step(seen · (1 + p/100))
    sell fill  = floor_to_step(seen · (1 − p/100)), which is never below zero
    order value = filled · multiplier · contracts
    entry cost  = order value + fee;  exit proceeds = order value − fee
    P&L = (sold − bought) · multiplier · contracts − entry fee − exit fee
    stop level  = bought · (1 − v/100) for a percent stop, bought − v for a points stop;
                  hit by a close ≤ level
    momentum level from the starting price s = s · (1 ± v/100) or s ± v;
                  reached by a close ≥ level when it is up, ≤ level when it is down
  with p the slippage in percent, v a rule's value, and ``seen`` the open of the fill bar.
Reference: the owner's rules in #3030 (behaviours 3 to 6 and 16). Nothing outside the repo is
  ported: AlgoTest's own pages name the same triggers but fill at the trigger level, and this
  does not.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_fills.py, and through the replay in
  tests/options_backtest/test_replay_*.py.

Every figure is exact. The work runs in a context that holds a hundred digits and refuses
to round (``Inexact`` is trapped), so no result depends on the caller's decimal context and a
figure that cannot be held raises in place of being rounded. A level is not rounded to the
price step: only a fill is, against the trader.
"""

from __future__ import annotations

from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, Inexact, InvalidOperation, Overflow, localcontext

from app.options_backtest.setup import Momentum, StopLoss

EXACT = Context(prec=100, Emax=MAX_EMAX, Emin=MIN_EMIN, traps=[Inexact, InvalidOperation, Overflow])

_HUNDRED = Decimal(100)


def _to_step(price: Decimal, step: Decimal, *, up: bool) -> Decimal:
    whole_steps, remainder = divmod(price, step)
    if up and remainder > 0:
        whole_steps += 1
    return whole_steps * step


def _require_not_negative(seen: Decimal) -> None:
    if seen < 0:
        raise ValueError(f"a premium is not negative; got {seen}")


def buy_fill_price(seen: Decimal, *, slippage_percent: Decimal, price_step_usd: Decimal) -> Decimal:
    """What a buy fills at: the price seen moved up by the slippage, then rounded up to the price step."""
    _require_not_negative(seen)
    with localcontext(EXACT):
        return _to_step(seen * (1 + slippage_percent / _HUNDRED), price_step_usd, up=True)


def sell_fill_price(seen: Decimal, *, slippage_percent: Decimal, price_step_usd: Decimal) -> Decimal:
    """What a sale fills at: the price seen moved down by the slippage, then rounded down to the price step.

    Never below zero: slippage is under 100% and a premium is not negative.
    """
    _require_not_negative(seen)
    with localcontext(EXACT):
        return _to_step(seen * (1 - slippage_percent / _HUNDRED), price_step_usd, up=False)


def order_value(filled: Decimal, *, multiplier: int, contracts: int) -> Decimal:
    """The dollars an order moves: its fill price for every share of every contract."""
    with localcontext(EXACT):
        return filled * multiplier * contracts


def entry_cost(filled: Decimal, *, multiplier: int, contracts: int, fee: Decimal) -> Decimal:
    """The cash a buy takes out: its value and its fee."""
    with localcontext(EXACT):
        return order_value(filled, multiplier=multiplier, contracts=contracts) + fee


def exit_proceeds(filled: Decimal, *, multiplier: int, contracts: int, fee: Decimal) -> Decimal:
    """The cash a sale brings in: its value less its fee."""
    with localcontext(EXACT):
        return order_value(filled, multiplier=multiplier, contracts=contracts) - fee


def trade_pnl(
    *, bought: Decimal, sold: Decimal, multiplier: int, contracts: int, entry_fee: Decimal, exit_fee: Decimal
) -> Decimal:
    """A round trip's profit: the price move on every share, less both fees. ``sold`` may be a mark."""
    with localcontext(EXACT):
        return (sold - bought) * multiplier * contracts - entry_fee - exit_fee


def stop_level(rule: StopLoss, *, bought: Decimal) -> Decimal:
    """The premium at or below which a bought leg is sold, from what was paid for it."""
    with localcontext(EXACT):
        if rule.kind == "percent":
            return bought * (1 - rule.value / _HUNDRED)
        return bought - rule.value


def stop_hit(level: Decimal, *, close: Decimal) -> bool:
    """Whether a minute that closed at ``close`` has reached a stop. A high or a low inside it never does."""
    return close <= level


def momentum_level(rule: Momentum, *, starting_price: Decimal) -> Decimal:
    """The premium a leg waits for: its starting price moved by the rule."""
    with localcontext(EXACT):
        match rule.kind:
            case "percent_up":
                return starting_price * (1 + rule.value / _HUNDRED)
            case "percent_down":
                return starting_price * (1 - rule.value / _HUNDRED)
            case "points_up":
                return starting_price + rule.value
            case "points_down":
                return starting_price - rule.value


def momentum_reached(rule: Momentum, *, level: Decimal, close: Decimal) -> bool:
    """Whether a minute that closed at ``close`` has reached the level, in the direction the rule moves."""
    if rule.kind in ("percent_up", "points_up"):
        return close >= level
    return close <= level
