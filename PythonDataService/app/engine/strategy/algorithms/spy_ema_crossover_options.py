"""SpyEmaCrossoverOptionsAlgorithm V1 — Options spread overlay.

Formula: EMA(5)/EMA(10) crossover + RSI(14) range filter → bull call or put spread entry; same signal logic as SpyEmaCrossoverAlgorithm with option-leg execution overlay.
Reference: Internal — no external port reference; signal logic mirrors SpyEmaCrossoverAlgorithm (LEAN-ported, see spy_ema_crossover.py provenance block). Option pricing via app/engine/options/pricer.py (QuantLib).
Canonical implementation: app/engine/strategy/algorithms/spy_ema_crossover_options.py
Validated against: app/engine/tests/ (indirect); signal-for-signal parity with spy_ema_crossover.py asserted by design (same indicator params, same entry/exit conditions).

Uses the same 15-minute EMA(5)/EMA(10) crossover + RSI(14) signal engine
as ``SpyEmaCrossoverAlgorithm``, but instead of buying the underlying
equity, enters a bull call spread or bull put spread on the underlying's
options chain.

Key design decisions:
  * Reuses the exact same signal logic (entry/exit conditions, indicator
    params) as the equity strategy, ensuring signal-for-signal parity.
  * Spread lifecycle is managed internally via ``OpenSpread`` — the engine's
    equity-oriented Portfolio/Order system is not used for option legs.
  * Option pricing and Greeks come from the ``ChainResolver`` which supports
    three modes: QUANTLIB_ONLY, MARKET_PREFERRED, MARKET_REQUIRED.
  * PnL is logged per-spread using the ``LoggedTrade`` format, with leg
    details packed into the ``indicators`` bag for dynamic rendering.
  * The strategy manages its own cash accounting (entry debit/credit,
    exit debit/credit, multiplier).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import OrderEvent
from app.engine.indicators.ema import ExponentialMovingAverage
from app.engine.indicators.rsi import RelativeStrengthIndex
from app.engine.options.chain_resolver import (
    ChainResolver,
    passes_liquidity_filter,
    select_by_delta,
    select_expiration,
)
from app.engine.options.pricer import (
    PricedContract,
    PricingMode,
    SpreadType,
    price_contract,
)
from app.engine.strategy.base import LoggedTrade, Strategy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spread state model (from V1 spec §11)
# ---------------------------------------------------------------------------


@dataclass
class OpenSpread:
    """State of an active spread position."""

    entry_time: datetime
    spread_type: SpreadType
    expiration: date

    # Long leg
    long_leg: PricedContract
    long_entry_price: float

    # Short leg
    short_leg: PricedContract
    short_entry_price: float

    # Net entry cost and countdown
    entry_net: float  # positive = debit, negative = credit
    bars_remaining: int

    # Underlying price at entry (for context logging)
    underlying_entry_price: float

    # Indicator snapshot at signal time
    ema5: Decimal
    ema10: Decimal
    rsi: Decimal

    @property
    def spread_width(self) -> float:
        return abs(float(self.short_leg.strike) - float(self.long_leg.strike))

    @property
    def max_loss(self) -> float:
        """Maximum loss for the spread."""
        if self.spread_type == SpreadType.BULL_CALL:
            return self.entry_net  # debit paid
        else:
            # Bull put: spread width - credit received
            return self.spread_width - abs(self.entry_net)

    @property
    def max_profit(self) -> float:
        """Maximum profit for the spread."""
        if self.spread_type == SpreadType.BULL_CALL:
            return self.spread_width - self.entry_net
        else:
            return abs(self.entry_net)  # credit received


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class SpyEmaCrossoverOptionsAlgorithm(Strategy):
    """Options spread overlay on the EMA crossover signal engine."""

    def __init__(
        self,
        # Signal parameters (same as equity strategy)
        symbol: str = "SPY",
        ema_fast_period: int = 5,
        ema_slow_period: int = 10,
        rsi_period: int = 14,
        ema_gap_min: float = 0.20,
        rsi_min: float = 50.0,
        rsi_max: float = 70.0,
        timeframe_minutes: int = 15,
        bars_to_hold: int = 5,
        # Options parameters
        spread_type: str = "BULL_CALL",
        min_dte: int = 7,
        max_dte: int = 30,
        long_call_delta_target: float = 0.60,
        short_call_delta_target: float = 0.30,
        short_put_delta_target: float = -0.30,
        long_put_delta_target: float = -0.15,
        min_open_interest: int = 100,
        min_volume: int = 10,
        max_bid_ask_spread_pct: float = 0.20,
        contracts_per_trade: int = 1,
        max_positions: int = 1,
        contract_multiplier: int = 100,
        # Pricing parameters
        pricing_mode: str = "quantlib_only",
        pricing_engine: str = "analytic_bs",
        risk_free_rate: float = 0.05,
        dividend_yield: float = 0.0,
        default_iv: float = 0.20,
        half_spread_pct: float = 0.01,
    ) -> None:
        super().__init__()

        # Signal config
        self._symbol_name = symbol.upper()
        self._symbol: str = ""
        self._ema_fast_period = ema_fast_period
        self._ema_slow_period = ema_slow_period
        self._rsi_period = rsi_period
        self._ema_gap_min = Decimal(str(ema_gap_min))
        self._rsi_min = Decimal(str(rsi_min))
        self._rsi_max = Decimal(str(rsi_max))
        self._timeframe_minutes = timeframe_minutes
        self._bars_to_hold = bars_to_hold

        # Options config
        self._spread_type = SpreadType(spread_type)
        self._min_dte = min_dte
        self._max_dte = max_dte
        self._long_call_delta = long_call_delta_target
        self._short_call_delta = short_call_delta_target
        self._short_put_delta = short_put_delta_target
        self._long_put_delta = long_put_delta_target
        self._min_oi = min_open_interest
        self._min_vol = min_volume
        self._max_spread_pct = max_bid_ask_spread_pct
        self._contracts_per_trade = contracts_per_trade
        self._max_positions = max_positions
        self._multiplier = contract_multiplier

        # Pricing config
        self._pricing_mode = PricingMode(pricing_mode)
        self._pricing_engine = pricing_engine

        # Chain resolver
        self._chain_resolver = ChainResolver(
            pricing_mode=self._pricing_mode,
            pricing_engine=self._pricing_engine,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            default_iv=default_iv,
            half_spread_pct=half_spread_pct,
        )
        self._half_spread_pct = half_spread_pct

        # Indicators (created in initialize)
        self._ema_fast: ExponentialMovingAverage | None = None
        self._ema_slow: ExponentialMovingAverage | None = None
        self._rsi: RelativeStrengthIndex | None = None

        # Signal state — mirrors the equity strategy exactly so that
        # entry/exit bar timing is identical regardless of whether the
        # options chain resolution succeeds.
        self._prev_ema_fast_above_slow: bool = False
        self._in_position: bool = False
        self._bars_until_exit: int = 0

        # Spread state (set only when chain resolution succeeds)
        self._open_spread: OpenSpread | None = None
        self._signal_count: int = 0
        self._skip_count: int = 0

        # Trade log
        self.trade_log: list[LoggedTrade] = []

    def initialize(self) -> None:
        self.set_start_date(2024, 3, 28)
        self.set_end_date(2026, 3, 27)
        self.set_cash(100000)

        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol_name)

        self._ema_fast = ExponentialMovingAverage(
            f"EMA{self._ema_fast_period}",
            self._ema_fast_period,
        )
        self._ema_slow = ExponentialMovingAverage(
            f"EMA{self._ema_slow_period}",
            self._ema_slow_period,
        )
        self._rsi = RelativeStrengthIndex(
            f"RSI{self._rsi_period}",
            self._rsi_period,
        )

        self._prev_ema_fast_above_slow = False
        self._in_position = False
        self._open_spread = None

        self.ctx.register_consolidator(
            self._symbol,
            timedelta(minutes=self._timeframe_minutes),
            self._on_consolidated_bar,
        )

    # ------------------------------------------------------------------
    # Consolidated bar handler
    # ------------------------------------------------------------------

    def _on_consolidated_bar(self, bar: TradeBar) -> None:
        assert self._ema_fast is not None
        assert self._ema_slow is not None
        assert self._rsi is not None
        assert self.ctx is not None

        # Update indicators
        self._ema_fast.update(bar.end_time, bar.close)
        self._ema_slow.update(bar.end_time, bar.close)
        self._rsi.update(bar.end_time, bar.close)

        # Warmup guard
        if not (self._ema_fast.is_ready and self._ema_slow.is_ready and self._rsi.is_ready):
            if self._ema_fast.is_ready and self._ema_slow.is_ready:
                assert self._ema_fast.current_value is not None
                assert self._ema_slow.current_value is not None
                self._prev_ema_fast_above_slow = self._ema_fast.current_value > self._ema_slow.current_value
            else:
                self._prev_ema_fast_above_slow = False
            return

        assert self._ema_fast.current_value is not None
        assert self._ema_slow.current_value is not None
        assert self._rsi.current_value is not None

        ema_fast_val = self._ema_fast.current_value
        ema_slow_val = self._ema_slow.current_value
        rsi_val = self._rsi.current_value

        current_above = ema_fast_val > ema_slow_val
        ema_gap = ema_fast_val - ema_slow_val

        if self._in_position:
            # Countdown to exit — identical to equity strategy.
            self._bars_until_exit -= 1
            if self._bars_until_exit <= 0:
                if self._open_spread is not None:
                    self._exit_spread(bar)
                else:
                    # Signal fired but chain resolution failed — nothing
                    # to close, just release the position lock.
                    self.ctx.log(f"POSITION RELEASED (no spread): {bar.end_time.strftime('%Y-%m-%d %H:%M')}")
                self._in_position = False
        else:
            # Entry check — same logic as equity strategy
            fresh_crossover = current_above and not self._prev_ema_fast_above_slow
            gap_ok = ema_gap >= self._ema_gap_min
            rsi_ok = self._rsi_min <= rsi_val <= self._rsi_max

            if fresh_crossover and gap_ok and rsi_ok:
                self._signal_count += 1
                # Lock the position BEFORE attempting entry — mirrors
                # the equity strategy's unconditional _in_position=True.
                # This ensures the same 5-bar lockout window regardless
                # of whether chain resolution succeeds.
                self._in_position = True
                self._bars_until_exit = self._bars_to_hold
                self._attempt_entry(bar, ema_fast_val, ema_slow_val, rsi_val)

        self._prev_ema_fast_above_slow = current_above

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def _attempt_entry(
        self,
        bar: TradeBar,
        ema_fast: Decimal,
        ema_slow: Decimal,
        rsi: Decimal,
    ) -> None:
        """Attempt to enter a spread on the entry signal."""
        assert self.ctx is not None
        underlying_price = float(bar.close)
        eval_date = bar.end_time.date()

        self.ctx.log(
            f"ENTRY SIGNAL #{self._signal_count}: "
            f"{bar.end_time.strftime('%Y-%m-%d %H:%M')} "
            f"Close={bar.close:.2f} EMA_fast={ema_fast:.4f} "
            f"EMA_slow={ema_slow:.4f} RSI={rsi:.2f}"
        )

        # Resolve the option chain
        chain = self._chain_resolver.resolve(
            underlying=self._symbol_name,
            underlying_price=underlying_price,
            evaluation_date=eval_date,
            min_dte=self._min_dte,
            max_dte=self._max_dte,
        )

        if chain is None or len(chain.contracts) == 0:
            self._skip_count += 1
            self.ctx.log(f"  NO TRADE: chain resolution failed (mode={self._pricing_mode.value})")
            return

        # Select expiration
        expiration = select_expiration(chain, self._min_dte, self._max_dte)
        if expiration is None:
            self._skip_count += 1
            self.ctx.log(f"  NO TRADE: no valid expiration in [{self._min_dte}, {self._max_dte}] DTE")
            return

        # Filter to chosen expiration
        exp_contracts = chain.filter_by_expiration(expiration)

        # Apply liquidity filters
        liquid_contracts = [
            c for c in exp_contracts if passes_liquidity_filter(c, self._min_oi, self._min_vol, self._max_spread_pct)
        ]

        if len(liquid_contracts) < 2:
            self._skip_count += 1
            self.ctx.log(f"  NO TRADE: insufficient liquid contracts ({len(liquid_contracts)} passed filters)")
            return

        # Select legs based on spread type
        long_leg: PricedContract | None = None
        short_leg: PricedContract | None = None

        if self._spread_type == SpreadType.BULL_CALL:
            calls = [c for c in liquid_contracts if c.option_type == "call"]
            long_leg = select_by_delta(calls, self._long_call_delta)
            short_leg = select_by_delta(calls, self._short_call_delta)

            # Validate: K_long < K_short for bull call spread
            if long_leg and short_leg and long_leg.strike >= short_leg.strike:
                self._skip_count += 1
                self.ctx.log(
                    f"  NO TRADE: invalid strike relationship (long={long_leg.strike} >= short={short_leg.strike})"
                )
                return

        elif self._spread_type == SpreadType.BULL_PUT:
            puts = [c for c in liquid_contracts if c.option_type == "put"]
            short_leg = select_by_delta(puts, self._short_put_delta)
            long_leg = select_by_delta(puts, self._long_put_delta)

            # Validate: K_long < K_short for bull put spread
            if long_leg and short_leg and long_leg.strike >= short_leg.strike:
                self._skip_count += 1
                self.ctx.log(
                    f"  NO TRADE: invalid strike relationship (long={long_leg.strike} >= short={short_leg.strike})"
                )
                return

        if long_leg is None or short_leg is None:
            self._skip_count += 1
            self.ctx.log("  NO TRADE: could not find both legs for delta targeting")
            return

        # Ensure legs are different contracts
        if long_leg.symbol == short_leg.symbol:
            self._skip_count += 1
            self.ctx.log("  NO TRADE: both legs selected the same contract")
            return

        # Compute fill prices
        long_fill = long_leg.fill_price("buy", self._half_spread_pct)
        short_fill = short_leg.fill_price("sell", self._half_spread_pct)

        # Net entry cost
        if self._spread_type == SpreadType.BULL_CALL:
            entry_net = long_fill - short_fill  # debit (positive)
        else:
            entry_net = short_fill - long_fill  # credit (negative = received)
            entry_net = -entry_net  # store as negative for credit

        # Create the open spread
        self._open_spread = OpenSpread(
            entry_time=bar.end_time,
            spread_type=self._spread_type,
            expiration=expiration,
            long_leg=long_leg,
            long_entry_price=long_fill,
            short_leg=short_leg,
            short_entry_price=short_fill,
            entry_net=entry_net,
            bars_remaining=self._bars_to_hold,
            underlying_entry_price=underlying_price,
            ema5=ema_fast,
            ema10=ema_slow,
            rsi=rsi,
        )

        # Update Portfolio cash to reflect the capital deployed.
        # Bull call: debit paid (entry_net > 0 → cash decreases)
        # Bull put: credit received (entry_net < 0 → cash increases)
        capital_deployed = Decimal(str(round(entry_net * self._multiplier * self._contracts_per_trade, 2)))
        self.ctx.portfolio.cash -= capital_deployed

        self.ctx.log(
            f"  SPREAD ENTERED: {self._spread_type.value} "
            f"exp={expiration} "
            f"long={long_leg.strike}@{long_fill:.2f} (Δ={long_leg.greeks.delta:.3f}) "
            f"short={short_leg.strike}@{short_fill:.2f} (Δ={short_leg.greeks.delta:.3f}) "
            f"net={'debit' if entry_net > 0 else 'credit'}={abs(entry_net):.2f} "
            f"width={self._open_spread.spread_width:.0f} "
            f"source={chain.source}"
        )

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def _exit_spread(self, bar: TradeBar) -> None:
        """Close the open spread and log the trade."""
        assert self.ctx is not None
        assert self._open_spread is not None

        spread = self._open_spread
        underlying_price = float(bar.close)
        eval_date = bar.end_time.date()

        # Re-price both legs at exit
        dte = (spread.expiration - eval_date).days
        iv = spread.long_leg.implied_volatility  # use entry IV for consistency

        if dte <= 0:
            # At or past expiration — use intrinsic values
            if spread.long_leg.option_type == "call":
                long_exit = max(underlying_price - float(spread.long_leg.strike), 0)
                short_exit = max(underlying_price - float(spread.short_leg.strike), 0)
            else:
                long_exit = max(float(spread.long_leg.strike) - underlying_price, 0)
                short_exit = max(float(spread.short_leg.strike) - underlying_price, 0)
        else:
            # Re-price via QuantLib (or the chain resolver for market mode)
            long_repriced = price_contract(
                underlying_price=underlying_price,
                strike=float(spread.long_leg.strike),
                expiration=spread.expiration,
                option_type=spread.long_leg.option_type,
                volatility=iv,
                evaluation_date=eval_date,
                risk_free_rate=self._chain_resolver.risk_free_rate,
                dividend_yield=self._chain_resolver.dividend_yield,
                engine=self._chain_resolver.pricing_engine,
            )
            short_repriced = price_contract(
                underlying_price=underlying_price,
                strike=float(spread.short_leg.strike),
                expiration=spread.expiration,
                option_type=spread.short_leg.option_type,
                volatility=iv,
                evaluation_date=eval_date,
                risk_free_rate=self._chain_resolver.risk_free_rate,
                dividend_yield=self._chain_resolver.dividend_yield,
                engine=self._chain_resolver.pricing_engine,
            )
            long_exit = long_repriced.fill_price("sell", self._half_spread_pct)
            short_exit = short_repriced.fill_price("buy", self._half_spread_pct)

        # Compute exit net
        if spread.spread_type == SpreadType.BULL_CALL:
            exit_net = long_exit - short_exit  # credit received (should be positive if profitable)
            pnl_per_contract = exit_net - spread.entry_net
        else:
            exit_net = short_exit - long_exit  # debit paid to close
            pnl_per_contract = abs(spread.entry_net) - exit_net  # credit - debit_to_close

        # Dollar PnL
        dollar_pnl = pnl_per_contract * self._multiplier * self._contracts_per_trade

        # PnL % relative to capital at risk (max loss)
        spread.max_loss * self._multiplier * self._contracts_per_trade
        pnl_pct = Decimal(str(pnl_per_contract)) / Decimal(str(spread.max_loss)) if spread.max_loss > 0 else Decimal(0)

        result = "WIN" if pnl_per_contract >= 0 else "LOSS"

        # Return capital + PnL to Portfolio cash.
        # On entry we deducted entry_net * multiplier * contracts.
        # On exit we receive exit_net * multiplier * contracts back.
        exit_proceeds = Decimal(str(round(exit_net * self._multiplier * self._contracts_per_trade, 2)))
        self.ctx.portfolio.cash += exit_proceeds

        # Log the trade
        self.trade_log.append(
            LoggedTrade(
                entry_time=spread.entry_time,
                entry_price=Decimal(str(round(spread.entry_net, 4))),
                exit_time=bar.end_time,
                exit_price=Decimal(str(round(exit_net, 4))),
                pnl_pts=Decimal(str(round(pnl_per_contract, 4))),
                pnl_pct=pnl_pct,
                result=result,
                indicators={
                    # Signal indicators
                    "ema5": spread.ema5,
                    "ema10": spread.ema10,
                    "rsi": spread.rsi,
                    # Spread metadata
                    "spread_type": Decimal(1 if spread.spread_type == SpreadType.BULL_CALL else 2),
                    "expiration_dte": Decimal(str((spread.expiration - spread.entry_time.date()).days)),
                    "spread_width": Decimal(str(spread.spread_width)),
                    # Long leg
                    "long_strike": spread.long_leg.strike,
                    "long_entry": Decimal(str(round(spread.long_entry_price, 4))),
                    "long_exit": Decimal(str(round(long_exit, 4))),
                    "long_delta": Decimal(str(round(spread.long_leg.greeks.delta, 4))),
                    # Short leg
                    "short_strike": spread.short_leg.strike,
                    "short_entry": Decimal(str(round(spread.short_entry_price, 4))),
                    "short_exit": Decimal(str(round(short_exit, 4))),
                    "short_delta": Decimal(str(round(spread.short_leg.greeks.delta, 4))),
                    # Underlying
                    "underlying_entry": Decimal(str(round(spread.underlying_entry_price, 2))),
                    "underlying_exit": Decimal(str(round(underlying_price, 2))),
                    # PnL detail
                    "dollar_pnl": Decimal(str(round(dollar_pnl, 2))),
                    "max_profit": Decimal(str(round(spread.max_profit, 4))),
                    "max_loss": Decimal(str(round(spread.max_loss, 4))),
                    # Data source
                    "pricing_mode": Decimal(
                        1
                        if self._pricing_mode == PricingMode.QUANTLIB_ONLY
                        else 2
                        if self._pricing_mode == PricingMode.MARKET_PREFERRED
                        else 3
                    ),
                },
                signal_reason=(
                    f"{spread.spread_type.value} "
                    f"{spread.long_leg.strike}/{spread.short_leg.strike} "
                    f"exp={spread.expiration}"
                ),
            )
        )

        self.ctx.log(
            f"EXIT SPREAD: {bar.end_time.strftime('%Y-%m-%d %H:%M')} "
            f"underlying={underlying_price:.2f} "
            f"long_exit={long_exit:.2f} short_exit={short_exit:.2f} "
            f"net_exit={exit_net:.2f} "
            f"PnL/contract={pnl_per_contract:.2f} "
            f"$PnL={dollar_pnl:.2f} ({float(pnl_pct) * 100:.2f}%) {result}"
        )

        self._open_spread = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_order_event(self, event: OrderEvent) -> None:
        """Options strategy does not use the equity order system.

        All spread lifecycle is managed internally via OpenSpread state.
        This handler is a no-op.
        """
        pass

    def on_end_of_algorithm(self) -> None:
        """Force-close any open spread at end of backtest."""
        if self._in_position and self._open_spread is not None and self.ctx is not None:
            # Create a synthetic bar for the exit
            self.ctx.log(
                f"END OF ALGORITHM: force-closing open spread "
                f"({self._open_spread.spread_type.value} "
                f"{self._open_spread.long_leg.strike}/{self._open_spread.short_leg.strike})"
            )
            # Use the last reference price
            last_price = self.ctx.portfolio.reference_price.get(self._symbol_name, Decimal(0))
            if last_price > 0:
                from app.engine.data.trade_bar import TradeBar as TB

                synthetic_bar = TB(
                    symbol=self._symbol_name,
                    time=self.ctx.current_time or datetime.now(UTC),
                    open=last_price,
                    high=last_price,
                    low=last_price,
                    close=last_price,
                    volume=Decimal(0),
                )
                self._exit_spread(synthetic_bar)
