"""``SpecAlgorithm`` — runs a validated ``StrategySpec`` through the engine.

Single-symbol equity-long evaluator. Reads a ``StrategySpec``, constructs
the declared indicators, compiles the entry / survival / exit logic
trees, registers a consolidator at the configured resolution, and on
every consolidated bar:

  1. Updates every declared indicator with the bar's source value
     (single-price indicators) or the full bar (BarIndicator subclasses
     like ADX and Supertrend).
  2. Builds an ``EvalContext`` from current state (position flag, bar
     count, bar close time, bar close price, entry price).
  3. Picks the lifecycle block based on the strategy's position flag at
     the start of the bar:
       - Flat → evaluate the entry block.
       - In position → walk survival rules in declaration order
         (first-match-wins) and run the matching action; if no rule
         fires, evaluate the exit block. Survival actions take
         precedence over the signal-flip exit on the same bar.
  4. If the chosen block fires, snapshots indicator values for
     diagnostics and submits a market order via ``set_holdings`` /
     ``liquidate``.
  5. Calls ``observe_bar`` on **every** compiled primitive — entry,
     exit, and every survival rule's predicate — regardless of which
     block evaluated this bar. Stateful primitives like ``FreshCross``
     and ``DrawdownFromPeak`` rely on this to seed and re-seed their
     internal state across position transitions, matching the reference
     hand-coded algorithms.

Trade log management mirrors the hand-coded references: signal time
captures indicator snapshots into ``_pending_entry``; the entry fill in
``on_order_event`` pairs that snapshot with fill price/time to start an
``_OpenTrade``; the exit fill closes the trade, appends a
``LoggedTrade``, and resets the strategy's lifecycle flags so external
flatten paths (force-flat at session close, manual liquidate, bracket
TP/SL) leave the strategy in sync with the actual portfolio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.research.ml.loader import PredictionSet

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.indicators.base import BarIndicator, Indicator
from app.engine.strategy.base import LoggedTrade, Strategy
from app.engine.strategy.spec import schema as S
from app.engine.strategy.spec.indicators import build_indicator, is_bar_indicator
from app.engine.strategy.spec.primitives import (
    CompiledBlock,
    EvalContext,
    Primitive,
)
from app.utils.timestamps import to_ms_utc


@dataclass
class _PendingEntry:
    """Indicator snapshot captured when entry signal fires."""

    snapshot: dict[str, Decimal]


@dataclass
class _OpenTrade:
    """An entry that has filled but not yet exited."""

    entry_time: datetime
    entry_price: Decimal
    snapshot: dict[str, Decimal] = field(default_factory=dict)


def _bar_source_value(bar: TradeBar, source: str) -> Decimal:
    """Resolve the spec's indicator source to a Decimal price from the bar."""
    if source == "close":
        return bar.close
    if source == "open":
        return bar.open
    if source == "high":
        return bar.high
    if source == "low":
        return bar.low
    if source == "hlc3":
        return (bar.high + bar.low + bar.close) / Decimal(3)
    if source == "ohlc4":
        return (bar.open + bar.high + bar.low + bar.close) / Decimal(4)
    raise ValueError(f"unknown indicator source: {source!r}")


class SpecAlgorithm(Strategy):
    """Strategy driver for declarative ``StrategySpec`` instances.

    Construct with a validated ``StrategySpec``. ``initialize`` walks the
    spec, instantiates engine indicators and compiled logic blocks, and
    registers a single consolidator for the symbol at the configured
    resolution.
    """

    def __init__(
        self,
        spec: S.StrategySpec,
        *,
        prediction_set: PredictionSet | None = None,
    ) -> None:
        super().__init__()
        self._spec = spec
        self._prediction_set = prediction_set

        # Predictions sanity: a spec that declares predictions must be
        # paired with a loaded PredictionSet at construction time.
        if spec.predictions and prediction_set is None:
            raise ValueError(
                f"spec {spec.name!r} declares predictions "
                f"({[p.id for p in spec.predictions]}) but no prediction_set "
                f"was provided to SpecAlgorithm"
            )
        if prediction_set is not None and spec.predictions:
            prediction_set.assert_pairs_with(spec)

        # Runtime guards for forward-compatible spec features that the
        # current evaluator does not yet support. These run at construction
        # time so a user can't accidentally start a backtest against a
        # later-phase spec and get silently-wrong results.
        if not isinstance(spec.position, S.EquityLongPosition):
            raise NotImplementedError(
                f"evaluator supports EQUITY_LONG positions only "
                f"(spec uses {type(spec.position).__name__})"
            )
        # Survival rule actions: Phase 2.1 ships CLOSE_ALL only.
        for rule in spec.survival:
            if not isinstance(rule.action, S.CloseAllAction):
                raise NotImplementedError(
                    f"survival rule {rule.name!r}: action {type(rule.action).__name__} "
                    f"not supported in Phase 2.1 (only CLOSE_ALL)"
                )
        if spec.entry.pyramiding != 1:
            raise NotImplementedError(
                f"evaluator supports pyramiding=1 only (spec uses {spec.entry.pyramiding})"
            )

        self._symbol_name = spec.symbols[0].upper()
        self._symbol: str = ""

        # Engine indicators keyed by spec id. Populated in ``initialize``.
        # Mixed Indicator (single-price) and BarIndicator (full-bar);
        # the evaluator dispatches updates accordingly.
        self._indicators: dict[str, Indicator | BarIndicator] = {}
        # Per-indicator source field (one for each block). Single-price
        # indicators read this; bar indicators ignore it.
        self._sources: dict[str, str] = {}

        # Compiled entry/exit blocks plus a flat list of every primitive
        # instance — the evaluator calls ``observe_bar`` on every entry
        # in this list at the end of each bar.
        self._entry_block: CompiledBlock | None = None
        self._exit_block: CompiledBlock | None = None
        # Survival rules in declaration order; first-match-wins per bar.
        # Each tuple is (rule_name, compiled_when_block, action).
        self._survival_rules: list[tuple[str, CompiledBlock, S.SurvivalAction]] = []
        self._all_primitives: list[Primitive] = []

        # Lifecycle state.
        self._in_position: bool = False
        self._bar_count: int = 0
        self._entry_bar_count: int | None = None
        self._pending_entry: _PendingEntry | None = None
        self._open_trade: _OpenTrade | None = None

        self.trade_log: list[LoggedTrade] = []

    # ------------------------------------------------------------------
    # Lifecycle.
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        # Sensible defaults — the router (or a parity test wrapper) will
        # override start/end dates and cash via the standard Strategy API
        # before the engine runs.
        self.set_start_date(2024, 3, 28)
        self.set_end_date(2026, 3, 27)
        self.set_cash(100000)

        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol_name)

        # Build engine indicator instances.
        for block in self._spec.indicators:
            self._indicators[block.id] = build_indicator(block)
            self._sources[block.id] = block.source

        # Compile entry / exit / survival logic trees and collect every
        # primitive. observe_bar is called on every primitive every bar
        # regardless of which block evaluated.
        self._all_primitives = []
        self._entry_block = CompiledBlock(
            self._spec.entry.logic, self._spec.entry.conditions, self._all_primitives
        )
        self._exit_block = CompiledBlock(
            self._spec.exit.logic, self._spec.exit.conditions, self._all_primitives
        )
        self._survival_rules = []
        for rule in self._spec.survival:
            compiled_when = CompiledBlock(
                rule.when.logic, rule.when.conditions, self._all_primitives
            )
            self._survival_rules.append((rule.name, compiled_when, rule.action))

        # Reset lifecycle state (in case initialize is called more than once
        # in a single instance — shouldn't happen, but be safe).
        self._in_position = False
        self._bar_count = 0
        self._entry_bar_count = None
        self._pending_entry = None
        self._open_trade = None

        # Single consolidator at the configured resolution.
        self.ctx.register_consolidator(
            self._symbol,
            timedelta(minutes=self._spec.resolution.period_minutes),
            self._on_consolidated_bar,
        )

    # ------------------------------------------------------------------
    # Bar handler — the per-bar loop described in the module docstring.
    # ------------------------------------------------------------------
    def _on_consolidated_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        assert self._entry_block is not None
        assert self._exit_block is not None

        # 1. Update every declared indicator. Bar indicators (ADX,
        # SUPERTREND) consume the full TradeBar; single-price indicators
        # (SMA, EMA, RSI, MACD) consume the configured source field.
        for ind_id, ind in self._indicators.items():
            if is_bar_indicator(ind):
                ind.update(bar)  # type: ignore[arg-type]
            else:
                source = self._sources[ind_id]
                ind.update(bar.end_time, _bar_source_value(bar, source))  # type: ignore[arg-type]

        # 2. Bar count increments BEFORE evaluate so that BarsSinceEntry
        # reads the entry bar as 0 — entry fires on the bar where
        # current_bar_count == _entry_bar_count, set inside the entry
        # branch below.
        self._bar_count += 1

        # 3. Build evaluator context — captures position state at the
        # START of this bar (before any entry/exit decision flips it).
        # ``entry_price`` is None on the entry bar (the fill hasn't
        # happened yet) and on every bar while flat; PnL primitives
        # gate on this.
        entry_price = self._open_trade.entry_price if self._open_trade is not None else None

        # Build the predictions snapshot for this bar. Empty when no
        # PredictionSet is wired (prediction-free specs).
        predictions: dict[str, Decimal] = {}
        if self._prediction_set is not None and self._spec.predictions:
            ts_ms = to_ms_utc(bar.end_time)
            row = self._prediction_set.index[ts_ms]  # KeyError == coverage-check bug
            for ref in self._spec.predictions:
                predictions[ref.id] = Decimal(str(row[ref.field]))

        ctx = EvalContext(
            indicators=self._indicators,
            current_bar_count=self._bar_count,
            bar_close_time=bar.end_time,
            bar_close_price=bar.close,
            current_bar=bar,
            in_position=self._in_position,
            entry_bar_count=self._entry_bar_count,
            entry_price=entry_price,
            predictions=predictions,
        )

        # 4. Choose the lifecycle block based on position state at the
        # start of the bar. While in position, survival rules are
        # checked first (top-to-bottom, first match wins) and act as
        # a higher-priority "manage" layer than the signal-flip exit
        # block. While flat, entry runs.
        if self._in_position:
            survival_fired = False
            for rule_name, compiled_when, action in self._survival_rules:
                if compiled_when.evaluate(ctx):
                    self._apply_survival_action(rule_name, action, bar)
                    survival_fired = True
                    break
            if not survival_fired and self._exit_block.evaluate(ctx):
                self.ctx.liquidate(self._symbol)
                self.ctx.log(
                    f"EXIT SIGNAL: {bar.end_time.strftime('%Y-%m-%d %H:%M')} "
                    f"Close={bar.close:.2f}"
                )
                self._in_position = False
                self._entry_bar_count = None
        else:
            if self._entry_block.evaluate(ctx):
                # Capture diagnostics snapshot at signal time — describes the
                # decision that triggered the entry.
                snapshot = self._snapshot_indicators()
                self._pending_entry = _PendingEntry(snapshot=snapshot)
                self._submit_entry()
                self._in_position = True
                self._entry_bar_count = self._bar_count
                self.ctx.log(
                    f"ENTRY SIGNAL: {bar.end_time.strftime('%Y-%m-%d %H:%M')} "
                    f"Close={bar.close:.2f} "
                    f"{self._format_snapshot(snapshot)}"
                )

        # 5. End-of-bar state update for every primitive — entry AND exit
        # primitives, regardless of which block evaluated. This is the
        # parity-critical step: stateful primitives like FreshCross must
        # see every eligible bar to keep ``_prev_above`` aligned with the
        # hand-coded references' state-update-at-bottom-of-handler pattern.
        for primitive in self._all_primitives:
            primitive.observe_bar(ctx)

    # ------------------------------------------------------------------
    # Helpers.
    # ------------------------------------------------------------------
    def _snapshot_indicators(self) -> dict[str, Decimal]:
        """Capture a name→value bag of indicator readings for diagnostics.

        Snapshot every indicator declared in the spec — that gives the
        trade log a reproducible record of the decision context at signal
        time. The spec's ``diagnostics.snapshot_at_entry`` could narrow
        this in Phase 2; for Phase 1 we capture everything declared so
        parity tests against hand-coded twins can compare on the full set.
        """
        snap: dict[str, Decimal] = {}
        for ind_id, ind in self._indicators.items():
            v = ind.current_value
            if v is not None:
                snap[ind_id] = v
        return snap

    @staticmethod
    def _format_snapshot(snapshot: dict[str, Decimal]) -> str:
        return " ".join(f"{k}={v}" for k, v in snapshot.items())

    def _apply_survival_action(
        self, rule_name: str, action: S.SurvivalAction, bar: TradeBar
    ) -> None:
        """Execute a survival rule's action.

        Phase 2.1 supports CLOSE_ALL only — liquidate the entire position.
        The ``__init__`` guard rejects other actions, so this is a small
        match. Logged with the firing rule name so the trade log is
        attributable to the specific rule.
        """
        assert self.ctx is not None
        if isinstance(action, S.CloseAllAction):
            self.ctx.liquidate(self._symbol)
            self.ctx.log(
                f"MANAGE FIRE: {rule_name!r} at {bar.end_time.strftime('%Y-%m-%d %H:%M')} "
                f"Close={bar.close:.2f} → CLOSE_ALL"
            )
            self._in_position = False
            self._entry_bar_count = None
            return
        # __init__ guard prevents this; defensive only.
        raise NotImplementedError(f"survival action {type(action).__name__} not supported")

    def _submit_entry(self) -> None:
        """Submit the entry order according to the spec's size rule."""
        assert self.ctx is not None
        size = self._spec.entry.size
        if isinstance(size, S.SetHoldings):
            self.ctx.set_holdings(self._symbol, Decimal(str(size.fraction)))
            return
        if isinstance(size, S.FixedContracts):
            # Phase 1 is equity-only; FixedContracts is reserved for Phase 2
            # options sizing.
            raise NotImplementedError(
                "FixedContracts sizing is reserved for Phase 2 (options) — "
                "Phase 1 equity specs use SetHoldings"
            )
        raise TypeError(f"unknown size rule type: {type(size).__name__}")

    # ------------------------------------------------------------------
    # Fill-driven trade bookkeeping. Same shape as the hand-coded
    # references so the trade log is comparable trade-by-trade.
    # ------------------------------------------------------------------
    def on_order_event(self, event: OrderEvent) -> None:
        if event.direction == Direction.LONG:
            if self._pending_entry is None:
                if self.ctx is not None:
                    self.ctx.log(f"WARN: LONG fill at {event.time} with no pending entry")
                return
            self._open_trade = _OpenTrade(
                entry_time=event.time,
                entry_price=event.fill_price,
                snapshot=dict(self._pending_entry.snapshot),
            )
            self._pending_entry = None
            if self.ctx is not None:
                self.ctx.log(
                    f"ENTRY: {event.time.strftime('%Y-%m-%d %H:%M')} "
                    f"Price={event.fill_price:.2f} "
                    f"{self._format_snapshot(self._open_trade.snapshot)}"
                )
            return

        # SHORT/FLAT fill → exit.
        #
        # Reset the strategy's lifecycle flags BEFORE the early return so
        # external flatten paths (force-flat at session close, manual
        # liquidate, bracket TP/SL) leave the strategy in sync with the
        # actual portfolio. Otherwise ``_in_position`` stays True after
        # the position is gone and the next bar would evaluate exit/
        # survival rules against a phantom position. Mirrors the
        # hand-coded references' ``on_force_flat`` semantics.
        self._in_position = False
        self._entry_bar_count = None
        self._pending_entry = None
        if self._open_trade is None:
            return
        entry = self._open_trade
        pnl_pts = event.fill_price - entry.entry_price
        pnl_pct = pnl_pts / entry.entry_price
        result = "WIN" if pnl_pts >= 0 else "LOSS"
        self.trade_log.append(
            LoggedTrade(
                entry_time=entry.entry_time,
                entry_price=entry.entry_price,
                exit_time=event.time,
                exit_price=event.fill_price,
                pnl_pts=pnl_pts,
                pnl_pct=pnl_pct,
                result=result,
                indicators=dict(entry.snapshot),
                signal_reason=self._spec.name,
            )
        )
        if self.ctx is not None:
            self.ctx.log(
                f"EXIT: {event.time.strftime('%Y-%m-%d %H:%M')} "
                f"Price={event.fill_price:.2f} PnL={pnl_pts:.2f} "
                f"({pnl_pct * 100:.2f}%) {result}"
            )
        self._open_trade = None

    def on_end_of_algorithm(self) -> None:
        if self._in_position:
            assert self.ctx is not None
            self.ctx.liquidate(self._symbol)
            self._in_position = False
