"""Condition primitives — runtime instances built from validated schema nodes.

Each primitive implements a two-phase contract:

* ``evaluate(ctx)`` — returns ``True`` iff the condition fires THIS bar,
  using current indicator values and any previously-stored state.
* ``observe_bar(ctx)`` — updates internal state for use NEXT bar. Stateless
  primitives implement this as a no-op. Must be idempotent.

Critical ordering (enforced by the evaluator): every bar runs
``evaluate`` BEFORE ``observe_bar``. ``observe_bar`` is the
"end-of-bar state update for next bar" hook, not a pre-evaluate refresh.
This matches the hand-coded reference algorithms, which compute
``current_above`` against the **previous** stored ``_prev_above``, then
update ``_prev_above`` at the bottom of the bar handler.

Statefulness lives on the primitive *instance* — one instance per
condition node in the spec — so two ``FreshCross(ema5, ema10, up)`` and
``FreshCross(sma_s, sma_l, up)`` track independent prev-above state.

The ``observe_bar`` discipline is the single most important property of
this layer; without it, a stateful primitive in the survival/exit branch
would never seed during the entry-eligible period, and FreshCross would
silently desync from its hand-coded twin. The reference for this is
``SpyEmaCrossoverAlgorithm._on_fifteen_minute_bar``: it updates
``_prev_ema5_above_ema10`` outside any ``if self._in_position:`` branch,
on every eligible bar.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.engine.strategy.spec import schema as S

if TYPE_CHECKING:
    from app.engine.data.trade_bar import TradeBar
    from app.engine.indicators.base import Indicator


# ---------------------------------------------------------------------------
# Evaluator context — a tiny bag of state passed to every primitive on
# evaluate / observe_bar. Avoids the primitives needing to know about the
# strategy or engine internals.
# ---------------------------------------------------------------------------
@dataclass
class EvalContext:
    """Per-bar state visible to primitives during evaluate / observe_bar."""

    indicators: dict[str, Indicator]  # keyed by spec indicator id
    current_bar_count: int  # consolidated bar handler invocations so far
    bar_close_time: datetime  # end_time of the current consolidated bar
    bar_close_price: Decimal  # close price of the current consolidated bar
    current_bar: TradeBar | None = None  # full OHLC bar for BarProperty primitives

    # Position lifecycle: set when entry fires, cleared when exit fires.
    in_position: bool = False
    entry_bar_count: int | None = None  # bar count at the moment entry fired
    # Entry fill price for the currently-open trade (None until the entry
    # order has filled). Used by PnL primitives in survival rules.
    entry_price: Decimal | None = None


# ---------------------------------------------------------------------------
# Operand evaluator — walks an Operand AST and returns a Decimal value
# (or None if any referenced indicator is not yet ready).
# ---------------------------------------------------------------------------
def evaluate_operand(operand, ctx: EvalContext) -> Decimal | None:
    """Recursively evaluate an Operand AST node.

    Returns ``None`` if any referenced indicator is not ready. The caller
    (a Comparison primitive) treats ``None`` as "condition cannot fire
    this bar" — same semantics as a warmup guard.
    """
    if isinstance(operand, S.IndicatorRef):
        ind = ctx.indicators[operand.indicator]
        if not ind.is_ready:
            return None
        return ind.current_value
    if isinstance(operand, S.ConstOperand):
        return Decimal(str(operand.value))
    if isinstance(operand, S.BarField):
        # Reserved for Phase 2 (price-based comparisons). Phase 1 specs
        # don't use BarField, but the schema admits it.
        raise NotImplementedError("BarField operand is reserved for Phase 2")
    if isinstance(operand, S.Subtract):
        left = evaluate_operand(operand.left, ctx)
        right = evaluate_operand(operand.right, ctx)
        if left is None or right is None:
            return None
        return left - right
    raise TypeError(f"unknown operand type: {type(operand).__name__}")


# ---------------------------------------------------------------------------
# Primitive base class.
# ---------------------------------------------------------------------------
class Primitive(ABC):
    """Runtime instance of a single condition node.

    Concrete subclasses are constructed from validated schema nodes via
    ``build_primitive``. Every primitive is exercised by the evaluator on
    every bar — even ones that live in a logic branch that won't fire,
    because their ``observe_bar`` may need to seed state for later bars.
    """

    @abstractmethod
    def evaluate(self, ctx: EvalContext) -> bool:
        """Return True iff the condition fires this bar."""

    def observe_bar(self, ctx: EvalContext) -> None:  # pragma: no cover - no-op default
        """Update internal state at the end of the bar. Stateless by default."""


# ---------------------------------------------------------------------------
# Stateless primitives.
# ---------------------------------------------------------------------------
def _compare(op: str, left: Decimal, right: Decimal) -> bool:
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == ">=":
        return left >= right
    if op == ">":
        return left > right
    raise ValueError(f"unknown comparison operator: {op!r}")


class IndicatorComparisonPrimitive(Primitive):
    def __init__(self, node: S.IndicatorComparison) -> None:
        self.node = node

    def evaluate(self, ctx: EvalContext) -> bool:
        left = evaluate_operand(self.node.left, ctx)
        right = evaluate_operand(self.node.right, ctx)
        if left is None or right is None:
            # Warmup guard: any referenced indicator not yet ready means
            # the comparison is undefined. Return False so the AND/OR
            # fold doesn't accidentally fire on a partial value.
            return False
        return _compare(self.node.op, left, right)


class IndicatorBetweenPrimitive(Primitive):
    def __init__(self, node: S.IndicatorBetween) -> None:
        self.node = node

    def evaluate(self, ctx: EvalContext) -> bool:
        ind = ctx.indicators[self.node.indicator]
        if not ind.is_ready:
            return False
        v = ind.current_value
        assert v is not None
        lo = Decimal(str(self.node.lo))
        hi = Decimal(str(self.node.hi))
        if self.node.inclusive:
            return lo <= v <= hi
        return lo < v < hi


class BarsSinceEntryPrimitive(Primitive):
    """Number of bar handler invocations since the bar on which entry fired.

    Counts so that the entry bar itself is 0, the next bar is 1, etc. This
    matches the hand-coded SPY algorithm where ``_bars_until_exit = 5``
    is set on the entry bar and decremented on each subsequent bar — the
    5th bar after entry is the exit bar (``BarsSinceEntry >= 5`` fires).
    """

    def __init__(self, node: S.BarsSinceEntry) -> None:
        self.node = node

    def evaluate(self, ctx: EvalContext) -> bool:
        if not ctx.in_position or ctx.entry_bar_count is None:
            return False
        elapsed = ctx.current_bar_count - ctx.entry_bar_count
        return _compare(self.node.op, Decimal(elapsed), Decimal(self.node.value))


class PnLPercentPrimitive(Primitive):
    """Compares unrealized PnL fraction to a threshold.

    PnL fraction = (current_close - entry_price) / entry_price.

    Returns False when no position is open OR the entry has not yet
    filled (``entry_price is None``). The latter case can happen on the
    same bar entry signals fire in NEXT_BAR_OPEN fill mode.
    """

    def __init__(self, node: S.PnLPercent) -> None:
        self.node = node

    def evaluate(self, ctx: EvalContext) -> bool:
        if not ctx.in_position or ctx.entry_price is None:
            return False
        pnl_frac = (ctx.bar_close_price - ctx.entry_price) / ctx.entry_price
        return _compare(self.node.op, pnl_frac, Decimal(str(self.node.value)))


class PnLPointsPrimitive(Primitive):
    """Compares unrealized PnL in price points to a threshold.

    Same gating as ``PnLPercent`` — returns False when no position is
    open or the entry has not yet filled.
    """

    def __init__(self, node: S.PnLPoints) -> None:
        self.node = node

    def evaluate(self, ctx: EvalContext) -> bool:
        if not ctx.in_position or ctx.entry_price is None:
            return False
        pnl_pts = ctx.bar_close_price - ctx.entry_price
        return _compare(self.node.op, pnl_pts, Decimal(str(self.node.value)))


class DrawdownFromPeakPrimitive(Primitive):
    """Trailing-stop primitive: fires when current close has retraced from
    the peak-since-entry by at least ``value``.

    Statefulness: tracks ``_peak`` since position open. On the first
    in-position bar after entry, ``_peak`` seeds to the current close.
    On every subsequent in-position bar, ``observe_bar`` updates ``_peak``
    to ``max(_peak, current_close)`` AFTER ``evaluate`` has computed the
    drawdown — so the first bar's drawdown is always 0 (no false fire on
    the entry fill bar). ``observe_bar`` resets ``_peak`` to None when
    flat, so a subsequent re-entry starts with a fresh peak rather than
    inheriting the prior trade's high-water mark.
    """

    def __init__(self, node: S.DrawdownFromPeak) -> None:
        self.node = node
        self._peak: Decimal | None = None

    def evaluate(self, ctx: EvalContext) -> bool:
        if not ctx.in_position or ctx.entry_price is None:
            return False
        if self._peak is None:
            return False  # first in-position bar: no peak yet
        drawdown = (self._peak - ctx.bar_close_price) / self._peak
        return drawdown >= Decimal(str(self.node.value))

    def observe_bar(self, ctx: EvalContext) -> None:
        if not ctx.in_position:
            self._peak = None
            return
        if ctx.entry_price is None:
            return  # entry not filled yet — defer seeding
        if self._peak is None:
            self._peak = ctx.bar_close_price
            return
        if ctx.bar_close_price > self._peak:
            self._peak = ctx.bar_close_price


class BarPropertyPrimitive(Primitive):
    """Compares a bar-derived property (range, body, %-of-close) to a threshold.

    Stateless — reads only the current bar. Phase 2.1 schema sets the
    bar fields (``open``, ``high``, ``low``, ``close``) on ``EvalContext``
    via ``bar_close_price`` only; this primitive needs the full bar so
    we expose it through ``ctx.current_bar`` (added alongside).
    """

    def __init__(self, node: S.BarProperty) -> None:
        self.node = node

    def evaluate(self, ctx: EvalContext) -> bool:
        bar = ctx.current_bar
        if bar is None:
            return False  # defensive — should always be set during evaluate
        prop = self.node.property
        if prop == "range":
            v = bar.high - bar.low
        elif prop == "body":
            v = abs(bar.close - bar.open)
        elif prop == "range_pct":
            v = (bar.high - bar.low) / bar.close if bar.close > 0 else Decimal(0)
        elif prop == "body_pct":
            v = abs(bar.close - bar.open) / bar.close if bar.close > 0 else Decimal(0)
        else:  # pragma: no cover — schema guards
            raise ValueError(f"unknown bar property: {prop!r}")
        return _compare(self.node.op, v, Decimal(str(self.node.value)))


class TimeOfDayPrimitive(Primitive):
    def __init__(self, node: S.TimeOfDay) -> None:
        self.node = node
        self._tz = ZoneInfo(node.tz)
        self._after = self._parse(node.after)
        self._before = self._parse(node.before)

    @staticmethod
    def _parse(s: str | None) -> time | None:
        if s is None:
            return None
        h, m = s.split(":")
        return time(int(h), int(m))

    def evaluate(self, ctx: EvalContext) -> bool:
        local = ctx.bar_close_time.astimezone(self._tz).time()
        if self._after is not None and local < self._after:
            return False
        return not (self._before is not None and local > self._before)


# ---------------------------------------------------------------------------
# Stateful primitive: FreshCross.
# ---------------------------------------------------------------------------
class FreshCrossPrimitive(Primitive):
    """Detects a freshly-occurred crossover between two indicators.

    Semantics (matches the hand-coded reference algorithms):

    * Internal state ``_prev_above`` is ``None`` until the first bar on
      which both referenced indicators are ready.
    * On that first eligible bar, ``evaluate`` returns False (seed-only).
      ``observe_bar`` then sets ``_prev_above`` to the current sign.
    * On subsequent eligible bars, ``evaluate`` returns True iff the sign
      flipped in the requested direction (``up`` or ``down``).
    * ``observe_bar`` updates ``_prev_above`` to the current sign at the
      end of every eligible bar — including bars where the position is
      open. This is critical for parity: without it, the first death-cross
      after a position closes would see stale state and fire wrong.

    The seed-without-firing-on-first-eligible-bar contract mirrors
    ``SmaCrossoverAlgorithm``'s ``_prev_short_above_long is None`` branch.
    The "observe-on-every-eligible-bar regardless of position state"
    contract mirrors ``SpyEmaCrossoverAlgorithm``'s state update outside
    the ``if self._in_position:`` block.
    """

    def __init__(self, node: S.FreshCross) -> None:
        self.node = node
        self._prev_above: bool | None = None

    def _both_ready(self, ctx: EvalContext) -> bool:
        left = ctx.indicators[self.node.left]
        right = ctx.indicators[self.node.right]
        return left.is_ready and right.is_ready

    def _current_above(self, ctx: EvalContext) -> bool:
        left = ctx.indicators[self.node.left]
        right = ctx.indicators[self.node.right]
        return left.current_value > right.current_value  # type: ignore[operator]

    def evaluate(self, ctx: EvalContext) -> bool:
        if not self._both_ready(ctx):
            return False
        if self._prev_above is None:
            return False  # seed-only on the first eligible bar
        cur_above = self._current_above(ctx)
        if self.node.direction == "up":
            return cur_above and not self._prev_above
        return (not cur_above) and self._prev_above

    def observe_bar(self, ctx: EvalContext) -> None:
        if not self._both_ready(ctx):
            return
        self._prev_above = self._current_above(ctx)


# ---------------------------------------------------------------------------
# Logic-tree evaluator.
# ---------------------------------------------------------------------------
@dataclass
class _LogicGroup:
    """Compiled form of a LogicNode — a list of primitives plus a fold op."""

    op: str  # "AND" or "OR"
    children: list  # list[Primitive | _LogicGroup]


def _compile_node(node, primitives_out: list[Primitive]) -> Primitive | _LogicGroup:
    """Compile a Condition or LogicNode, appending all leaves to primitives_out.

    The primitives_out list is the flat set of primitive instances the
    evaluator must observe on every bar, regardless of which logic branch
    they sit under.
    """
    if isinstance(node, S.LogicNode):
        children = [_compile_node(c, primitives_out) for c in node.conditions]
        return _LogicGroup(op=node.logic, children=children)

    primitive = _build_leaf(node)
    primitives_out.append(primitive)
    return primitive


def _build_leaf(node) -> Primitive:
    if isinstance(node, S.IndicatorComparison):
        return IndicatorComparisonPrimitive(node)
    if isinstance(node, S.IndicatorBetween):
        return IndicatorBetweenPrimitive(node)
    if isinstance(node, S.FreshCross):
        return FreshCrossPrimitive(node)
    if isinstance(node, S.BarsSinceEntry):
        return BarsSinceEntryPrimitive(node)
    if isinstance(node, S.TimeOfDay):
        return TimeOfDayPrimitive(node)
    if isinstance(node, S.PnLPercent):
        return PnLPercentPrimitive(node)
    if isinstance(node, S.PnLPoints):
        return PnLPointsPrimitive(node)
    if isinstance(node, S.DrawdownFromPeak):
        return DrawdownFromPeakPrimitive(node)
    if isinstance(node, S.BarProperty):
        return BarPropertyPrimitive(node)
    raise NotImplementedError(f"primitive kind {type(node).__name__} not supported")


def _evaluate_compiled(node, ctx: EvalContext) -> bool:
    if isinstance(node, _LogicGroup):
        if node.op == "AND":
            return all(_evaluate_compiled(c, ctx) for c in node.children)
        return any(_evaluate_compiled(c, ctx) for c in node.children)
    return node.evaluate(ctx)


class CompiledBlock:
    """Compiled lifecycle block — knows how to evaluate the logic tree."""

    def __init__(self, logic: str, conditions: list, all_primitives_out: list[Primitive]) -> None:
        # Build a synthetic root LogicNode-equivalent so the evaluator
        # uniformly walks AND/OR groups.
        children = [_compile_node(c, all_primitives_out) for c in conditions]
        self._root = _LogicGroup(op=logic, children=children)

    def evaluate(self, ctx: EvalContext) -> bool:
        if not self._root.children:
            return False  # an empty block never fires
        return _evaluate_compiled(self._root, ctx)
