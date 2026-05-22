"""SpyEmaCrossoverAlgorithm — Python port of LEAN's C# reference algorithm.

Formula: Long-only EMA(5)/EMA(10) crossover on 15-min SPY bars with RSI(14) filter. Entry: fresh EMA5 > EMA10 crossover AND (EMA5 - EMA10) >= 0.20 AND 50 <= RSI <= 70. Position: SetHoldings(SPY, 1.0). Exit: 5 consolidated bars (75 minutes) after entry.
Reference: Lean/Algorithm.CSharp/SpyEmaCrossoverAlgorithm.cs (Apr 2026 revision); TradingView Pine validation `docs/validation/SPY_EMA_Crossover_RSI.pine`; validation report `docs/validation/SPY_EMA_Crossover_Validation_Report.pdf`.
Canonical implementation: this file. Parity-pinned secondary: `app/engine/strategy/spec/evaluator.py::SpecAlgorithm` driven by `app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json` reproduces the hand-coded twin trade-by-trade (Phase 1 acceptance gate, 2026-05-04).
Validated against: PythonDataService/tests/test_strategy_engine.py (engine-level); TV parity via Pine; spec ↔ hand-coded parity at `app/engine/strategy/spec/tests/test_spec_spy_ema_parity.py`.

Line-for-line port of
``Lean/Algorithm.CSharp/SpyEmaCrossoverAlgorithm.cs`` (Apr 2026 revision).
The intent is to produce the same trades, in the same order, at the same
prices, as the LEAN reference output at
``Lean/Launcher/bin/Debug/SpyEmaCrossoverAlgorithm-log.txt``.

Strategy rules:
  * 15-minute bars consolidated from minute SPY data.
  * Long-only EMA(5)/EMA(10) crossover with RSI(14) filter (Wilders).
  * Entry: fresh EMA5 > EMA10 crossover AND (ema5 - ema10) >= 0.20
           AND 50 <= RSI <= 70.
  * Position: SetHoldings(SPY, 1.0) — all-in on the signal bar.
  * Exit: after exactly 5 consolidated bars (75 minutes), Liquidate.

Trade logging:
  Trades are logged in ``on_order_event`` using actual fill prices and
  times from the portfolio's fills — NOT from the signal bar's close.
  This makes the trade log consistent with the portfolio accounting
  regardless of fill mode. In SIGNAL_BAR_CLOSE mode the fill price
  equals the signal bar close (so the LEAN bit-exact match is
  preserved); in NEXT_BAR_OPEN mode the trade log reflects actual
  next-bar-open fills, so statistics computed from it match the
  portfolio's net profit.

  Indicator snapshots (EMA5, EMA10, RSI) are captured at signal time
  and stashed in ``_pending_entry``, because they describe the decision
  that triggered the entry — not the state at fill time.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.engine.live.indicator_state import ValidationResult

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.framework.insight import Insight, InsightDirection
from app.engine.indicators.ema import ExponentialMovingAverage
from app.engine.indicators.rsi import RelativeStrengthIndex
from app.engine.strategy.base import DecisionSnapshot, LoggedTrade, Strategy


@dataclass
class _PendingEntry:
    """Indicator snapshot captured at entry signal time.

    Populated when the strategy submits the entry order; consumed when
    the corresponding entry fill arrives in ``on_order_event``.
    """

    ema5: Decimal
    ema10: Decimal
    rsi: Decimal


@dataclass
class _OpenTrade:
    """An entry that has filled but not yet exited."""

    entry_time: datetime
    entry_price: Decimal
    quantity: int
    ema5: Decimal
    ema10: Decimal
    rsi: Decimal


class SpyEmaCrossoverAlgorithm(Strategy):
    STRATEGY_KEY = "spy_ema_crossover"
    CONSOLIDATOR_PERIOD_MIN = 15

    def __init__(self, symbol: str = "SPY", output_dir: Path | None = None) -> None:
        super().__init__()
        # The symbol is parameterized (default SPY) so the exact same
        # rule set can be re-used against other tickers like QQQ without
        # duplicating the algorithm. The SPY default keeps the LEAN
        # bit-exact parity test unchanged.
        self._symbol_name = symbol.upper()
        self._output_dir = output_dir
        self._symbol: str = ""
        self._ema5: ExponentialMovingAverage | None = None
        self._ema10: ExponentialMovingAverage | None = None
        self._rsi14: RelativeStrengthIndex | None = None

        self._prev_ema5_above_ema10: bool = False

        # Strategy state (flipped at signal time, like LEAN's C# code).
        self._in_position: bool = False
        self._bars_until_exit: int = 0

        # Two-stage trade bookkeeping:
        #   signal time     → _pending_entry (indicator snapshot)
        #   entry fill      → _open_trade    (entry price/time from fill)
        #   exit fill       → _LoggedTrade appended to trade_log
        self._pending_entry: _PendingEntry | None = None
        self._open_trade: _OpenTrade | None = None

        self.trade_log: list[LoggedTrade] = []

        # CSV emitter state — populated in initialize() if output_dir is set.
        self._observations_writer: csv.writer | None = None  # type: ignore[type-arg]
        self._observations_fp: object | None = None
        self._state_writer: csv.writer | None = None  # type: ignore[type-arg]
        self._state_fp: object | None = None

    def initialize(self) -> None:
        # LEAN-parity defaults — match the C# reference Initialize().
        # The router's ``_apply_overrides`` runs right after initialize()
        # and replaces these with the request body's start_date /
        # end_date / initial_cash whenever the caller supplies them, so
        # the Engine Lab picker still wins. The defaults only kick in
        # for the no-arg fixture path used by the bit-exact LEAN
        # validation test.
        self.set_start_date(2024, 3, 28)
        self.set_end_date(2026, 3, 27)
        self.set_cash(100000)

        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol_name)

        # Indicators (updated manually in the handler).
        self._ema5 = ExponentialMovingAverage("EMA5", 5)
        self._ema10 = ExponentialMovingAverage("EMA10", 10)
        self._rsi14 = RelativeStrengthIndex("RSI14", 14)

        self._prev_ema5_above_ema10 = False
        self._in_position = False

        # 15-minute consolidator.
        self.ctx.register_consolidator(
            self._symbol,
            timedelta(minutes=15),
            self._on_fifteen_minute_bar,
        )

        if self._output_dir is not None:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            obs_path = self._output_dir / "observations.csv"
            self._observations_fp = obs_path.open("w", encoding="utf-8", newline="")
            self._observations_writer = csv.writer(self._observations_fp)  # type: ignore[arg-type]
            self._observations_writer.writerow(["ms_utc", "open", "high", "low", "close", "volume"])
            self._observations_fp.flush()  # type: ignore[union-attr]
            state_path = self._output_dir / "state.csv"
            self._state_fp = state_path.open("w", encoding="utf-8", newline="")
            self._state_writer = csv.writer(self._state_fp)  # type: ignore[arg-type]
            self._state_writer.writerow(["ts_ms_utc", "close", "ema_fast", "ema_slow", "rsi", "cross_state", "signal"])
            self._state_fp.flush()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # on_minute_bar override — writes to observations.csv when output_dir
    # is configured.  Called by the engine for every minute bar before
    # consolidator dispatch, including the session-close bar.
    # ------------------------------------------------------------------
    def on_minute_bar(self, bar: TradeBar) -> None:
        if self._observations_writer is None:
            return
        ms_utc = int(bar.end_time.timestamp() * 1000)
        self._observations_writer.writerow(
            [
                str(ms_utc),
                str(bar.open),
                str(bar.high),
                str(bar.low),
                str(bar.close),
                str(bar.volume),
            ]
        )

    # ------------------------------------------------------------------
    # Bar handler — the line-for-line port of OnFifteenMinuteBar.
    # ------------------------------------------------------------------
    def _on_fifteen_minute_bar(self, bar: TradeBar) -> None:
        assert self._ema5 is not None
        assert self._ema10 is not None
        assert self._rsi14 is not None
        assert self.ctx is not None

        # Update indicators with consolidated bar close at EndTime.
        self._ema5.update(bar.end_time, bar.close)
        self._ema10.update(bar.end_time, bar.close)
        self._rsi14.update(bar.end_time, bar.close)

        # Warmup guard — mirrors the C# branch.
        if not (self._ema5.is_ready and self._ema10.is_ready and self._rsi14.is_ready):
            if self._ema5.is_ready and self._ema10.is_ready:
                assert self._ema5.current_value is not None
                assert self._ema10.current_value is not None
                self._prev_ema5_above_ema10 = self._ema5.current_value > self._ema10.current_value
            else:
                self._prev_ema5_above_ema10 = False
            return

        assert self._ema5.current_value is not None
        assert self._ema10.current_value is not None
        assert self._rsi14.current_value is not None

        ema5_val = self._ema5.current_value
        ema10_val = self._ema10.current_value
        rsi_val = self._rsi14.current_value

        current_above = ema5_val > ema10_val
        ema_gap = ema5_val - ema10_val

        # Per-bar action label tracked locally; published at end of
        # handler via ``last_decision_snapshot`` for the live runtime's
        # DecisionWriter (Phase C-2 observability hook). Trading logic
        # is unchanged — the variable is set inside existing branches
        # and read only at the bottom.
        bar_signal = "HOLD"

        if self._in_position:
            # Decrement bars-until-exit and liquidate when we hit zero.
            self._bars_until_exit -= 1
            if self._bars_until_exit <= 0:
                # Submit the exit order. The actual exit price and time
                # will come from the resulting fill event in
                # ``on_order_event``.
                self.ctx.liquidate(self._symbol)
                self.ctx.log(f"EXIT SIGNAL: {bar.end_time.strftime('%Y-%m-%d %H:%M')} Close={bar.close:.2f}")
                self._in_position = False
                bar_signal = "EXIT"
        else:
            # Entry check.
            fresh_crossover = current_above and not self._prev_ema5_above_ema10
            gap_ok = ema_gap >= Decimal("0.20")
            rsi_ok = Decimal(50) <= rsi_val <= Decimal(70)

            if fresh_crossover and gap_ok and rsi_ok:
                # Stash the indicator snapshot — it describes the
                # decision that triggered the entry, so it must be
                # captured here (not at fill time).
                self._pending_entry = _PendingEntry(ema5=ema5_val, ema10=ema10_val, rsi=rsi_val)
                self.ctx.set_holdings(self._symbol, Decimal(1))
                self._in_position = True
                self._bars_until_exit = 5
                bar_signal = "ENTER"

                # ── Emit Insight (Phase 1) ──
                # Dual-mode: the strategy still trades via set_holdings()
                # as before. The insight records a structured prediction
                # that the InsightManager will score after the period.
                rsi_float = float(rsi_val)
                # Confidence derived from RSI position in the 50-70 band.
                # Peak confidence at RSI=60 (center of the band).
                rsi_position = (rsi_float - 50.0) / 20.0  # 0.0 at 50, 1.0 at 70
                confidence = 0.5 + 0.3 * (1.0 - abs(rsi_position - 0.5))

                self.ctx.emit_insight(
                    Insight.price(
                        symbol=self._symbol,
                        direction=InsightDirection.UP,
                        period=timedelta(minutes=15 * 5),  # 5 bars × 15 min
                        magnitude=float(ema_gap / bar.close),
                        confidence=round(confidence, 4),
                        source_model="EmaCross_5_10_RSI14",
                        tag=(f"EMA5={ema5_val:.4f} EMA10={ema10_val:.4f} RSI={rsi_val:.2f} Gap={ema_gap:.4f}"),
                    )
                )

                self.ctx.log(
                    f"ENTRY SIGNAL: {bar.end_time.strftime('%Y-%m-%d %H:%M')} "
                    f"Close={bar.close:.2f} "
                    f"EMA5={ema5_val:.4f} EMA10={ema10_val:.4f} "
                    f"Gap={ema_gap:.4f} RSI={rsi_val:.2f}"
                )

        # Update the crossover state for the next bar.
        self._prev_ema5_above_ema10 = current_above

        # Publish the per-bar decision snapshot (observability only —
        # live engine reads this to populate decisions.parquet; backtest
        # paths and unit tests that don't observe it see no change).
        # ``bar.end_time`` is already an aware datetime in the engine's
        # exchange tz, so .timestamp() is the correct UTC seconds.
        self.last_decision_snapshot = DecisionSnapshot(
            bar_close_ms=int(bar.end_time.timestamp() * 1000),
            ema5=float(ema5_val),
            ema10=float(ema10_val),
            rsi=float(rsi_val),
            signal=bar_signal,
            intended_price=float(bar.close),
        )

        # Emit to state.csv when output_dir is configured.  This block is
        # after the early-return warmup guard above, so rows are only written
        # once all three indicators are is_ready — matching the LEAN trusted
        # sample's OnConsolidatedBar behaviour.
        if self._state_writer is not None:
            if ema5_val > ema10_val:
                cross_state = "above"
            elif ema5_val < ema10_val:
                cross_state = "below"
            else:
                cross_state = "equal"
            ms_utc = int(bar.end_time.timestamp() * 1000)
            self._state_writer.writerow(
                [
                    str(ms_utc),
                    str(bar.close),
                    str(ema5_val),
                    str(ema10_val),
                    str(rsi_val),
                    cross_state,
                    bar_signal,
                ]
            )

    # ------------------------------------------------------------------
    # Fill-driven trade bookkeeping.
    # ------------------------------------------------------------------
    def on_order_event(self, event: OrderEvent) -> None:
        """Turn fills into trade log entries.

        Entry fills pair with ``_pending_entry`` to start an open trade;
        exit fills close the open trade and append a ``_LoggedTrade``.
        The strategy ignores fills that don't correspond to its own
        entry/exit intentions — e.g., a stray liquidation on
        end-of-algorithm with no pending trade will just reset state.
        """
        if event.direction == Direction.LONG:
            # Entry fill.
            if self._pending_entry is None:
                # Defensive: a LONG fill without a pending entry means
                # state is out of sync. Log and skip.
                if self.ctx is not None:
                    self.ctx.log(f"WARN: LONG fill at {event.time} with no pending entry")
                return
            self._open_trade = _OpenTrade(
                entry_time=event.time,
                entry_price=event.fill_price,
                quantity=event.fill_quantity,
                ema5=self._pending_entry.ema5,
                ema10=self._pending_entry.ema10,
                rsi=self._pending_entry.rsi,
            )
            self._pending_entry = None
            if self.ctx is not None:
                self.ctx.log(
                    f"ENTRY: {event.time.strftime('%Y-%m-%d %H:%M')} "
                    f"Price={event.fill_price:.2f} "
                    f"EMA5={self._open_trade.ema5:.4f} "
                    f"EMA10={self._open_trade.ema10:.4f} "
                    f"RSI={self._open_trade.rsi:.2f}"
                )
        else:
            # SHORT/FLAT fill → exit.
            if self._open_trade is None:
                # A liquidate with no open trade; nothing to record.
                return
            entry = self._open_trade
            exit_price = event.fill_price
            exit_time = event.time
            pnl_pts = exit_price - entry.entry_price
            pnl_pct = pnl_pts / entry.entry_price
            result = "WIN" if pnl_pts >= 0 else "LOSS"
            self.trade_log.append(
                LoggedTrade(
                    entry_time=entry.entry_time,
                    entry_price=entry.entry_price,
                    exit_time=exit_time,
                    exit_price=exit_price,
                    quantity=entry.quantity,
                    pnl_pts=pnl_pts,
                    pnl_pct=pnl_pct,
                    result=result,
                    indicators={
                        "ema5": entry.ema5,
                        "ema10": entry.ema10,
                        "rsi": entry.rsi,
                    },
                )
            )
            if self.ctx is not None:
                self.ctx.log(
                    f"EXIT: {exit_time.strftime('%Y-%m-%d %H:%M')} "
                    f"Price={exit_price:.2f} PnL={pnl_pts:.2f} "
                    f"({pnl_pct * 100:.2f}%) {result}"
                )
            self._open_trade = None

    def on_end_of_algorithm(self) -> None:
        if self._in_position:
            assert self.ctx is not None
            self.ctx.liquidate(self._symbol)
            self._in_position = False
        if self._observations_fp is not None:
            self._observations_fp.close()  # type: ignore[union-attr]
            self._observations_fp = None
        if self._state_fp is not None:
            self._state_fp.close()  # type: ignore[union-attr]
            self._state_fp = None

    # ---- Indicator-state persistence hooks (PR1) ----

    def report_state_for_persistence(self) -> dict | None:
        """Return the strategy's persistable state, or None if not restorable.

        Returns None when any of:
          * indicators not all is_ready (the restored state would be
            sub-warmup and the validation ladder would reject it)
          * position not flat (we'd be hydrating into an open trade
            tomorrow with no way to reconcile entry context)
          * pending entry / open trade bookkeeping is mid-flight

        On the happy path returns a dict with ema5/ema10/rsi14 indicator
        states (via to_state_dict), the prev-cross flag, and a lifecycle
        block proving the strategy is flat.
        """
        if self._ema5 is None or self._ema10 is None or self._rsi14 is None:
            return None
        if not (self._ema5.is_ready and self._ema10.is_ready and self._rsi14.is_ready):
            return None
        if self._in_position:
            return None
        if self._pending_entry is not None or self._open_trade is not None:
            return None
        return {
            "ema5": self._ema5.to_state_dict(),
            "ema10": self._ema10.to_state_dict(),
            "rsi14": self._rsi14.to_state_dict(),
            "_prev_ema5_above_ema10": self._prev_ema5_above_ema10,
            "lifecycle": {
                "position_qty": 0,
                "pending_orders_count": 0,
                "open_insights": 0,
                "last_signal_kind": None,
                "last_signal_bar_end_ms": None,
            },
        }

    def restore_state_from_persistence(self, payload: dict) -> None:
        """Rehydrate indicator internals + _prev_ema5_above_ema10 from payload.

        Caller (LiveContext.hydrate_indicator_state) guarantees that
        ``validate_state_payload(payload)`` has already passed, and
        that this is called immediately after ``initialize()`` while
        indicators are fresh-constructed and unfed.
        """
        assert self._ema5 is not None
        assert self._ema10 is not None
        assert self._rsi14 is not None
        self._ema5.restore_state(payload["ema5"])
        self._ema10.restore_state(payload["ema10"])
        self._rsi14.restore_state(payload["rsi14"])
        prev_above = payload["_prev_ema5_above_ema10"]
        if not isinstance(prev_above, bool):
            raise ValueError("payload_mismatch: _prev_ema5_above_ema10 must be bool")
        self._prev_ema5_above_ema10 = prev_above

    def validate_state_payload(self, payload: dict) -> ValidationResult:
        """Shape-check the payload for this strategy. Returns a ValidationResult.

        Imports ValidationResult locally to avoid a module-level
        cycle (indicator_state -> strategy is not desirable; this
        method is rarely called in hot paths).
        """
        from app.engine.live.indicator_state import ValidationResult

        required_top = {"ema5", "ema10", "rsi14", "_prev_ema5_above_ema10", "lifecycle"}
        if not isinstance(payload, dict) or not required_top.issubset(payload.keys()):
            return ValidationResult.failed("payload_mismatch", payload_shape_ok=False)
        if not isinstance(payload["_prev_ema5_above_ema10"], bool):
            return ValidationResult.failed("payload_mismatch", payload_shape_ok=False)
        if not isinstance(payload["lifecycle"], dict):
            return ValidationResult.failed("payload_mismatch", payload_shape_ok=False)
        required_lifecycle = {"position_qty", "pending_orders_count", "open_insights"}
        if not required_lifecycle.issubset(payload["lifecycle"].keys()):
            return ValidationResult.failed("payload_mismatch", payload_shape_ok=False)
        # Lifecycle counters must be strict ints (bool is a subclass of int in Python;
        # exclude it to prevent True/False sneaking in where 0/1 is expected).
        if any(
            not isinstance(payload["lifecycle"][k], int) or isinstance(payload["lifecycle"][k], bool)
            for k in required_lifecycle
        ):
            return ValidationResult.failed("payload_mismatch", payload_shape_ok=False)
        return ValidationResult.all_passed()
