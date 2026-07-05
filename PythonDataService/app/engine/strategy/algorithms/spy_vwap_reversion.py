"""SpyVwapReversionAlgorithm — Python port of the QuantConnect VWAP-band
reversion reference (PRD-C / PR-K).

Formula: long-only intraday mean-reversion on SPY 1-min bars. Session-anchored
VWAP (typical=(H+L+C)/3); bands = vwap ± K·σ where σ is the population std
(ddof=0) of the last LOOKBACK (close−vwap) values. Enter long when close <
lower band (inside the session window, under the per-day trade cap); exit when
close ≥ vwap; force-flat five minutes before the scheduled NYSE close.
Fixed quantity per trade.

Reference: references/quantconnect/spy_vwap_reversion/main.py
(QuantConnect Cloud LEAN). Canonical implementation: this file.
Validated against: tests/integration/reconciliation/test_spy_vwap_reversion_qc.py
(trade-by-trade vs the QC orders golden fixture,
tests/fixtures/golden/spy-vwap-reversion-qc/).

Pinned parameters mirror the QC reference EXACTLY — see the table in
docs/references/spy-vwap-reversion-port.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar
from app.engine.indicators.rolling_distance_sigma import RollingDistanceSigma
from app.engine.indicators.vwap import SessionAnchoredVwap
from app.engine.strategy.base import Strategy
from app.lean_sidecar.trading_calendar import session_window_for_date

_ET = ZoneInfo("America/New_York")


class SpyVwapReversionAlgorithm(Strategy):
    STRATEGY_KEY = "spy_vwap_reversion"
    CONSOLIDATOR_PERIOD_MIN = 1

    # Pinned parameters (must match references/quantconnect/spy_vwap_reversion/main.py).
    K = 2.0
    LOOKBACK = 30
    QUANTITY = 100
    SKIP_OPEN_MIN = 5
    SKIP_CLOSE_MIN = 5
    MAX_TRADES_PER_DAY = 4

    def __init__(self, symbol: str = "SPY") -> None:
        super().__init__()
        self._symbol_name = symbol.upper()
        self._symbol: str = ""
        self._vwap = SessionAnchoredVwap()
        self._sigma = RollingDistanceSigma(self.LOOKBACK)
        self._session_date = None
        self._trades_today = 0
        self._in_position = False

    def initialize(self) -> None:
        self.set_start_date(2024, 3, 4)
        self.set_end_date(2024, 3, 8)
        self.set_cash(100_000)
        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol_name)
        self.ctx.register_consolidator(
            self._symbol, timedelta(minutes=self.CONSOLIDATOR_PERIOD_MIN), self._on_bar
        )

    def _maybe_reset_session(self, d) -> None:
        if self._session_date != d:
            self._session_date = d
            self._vwap.reset()
            self._sigma.reset()
            self._trades_today = 0

    def _in_entry_window(self, t) -> bool:
        minutes = t.hour * 60 + t.minute
        session_bounds = self._session_bounds_minutes_et(t.date())
        if session_bounds is None:
            return False
        session_open_min, session_close_min = session_bounds
        open_min = session_open_min + self.SKIP_OPEN_MIN
        last_entry_min = session_close_min - self.SKIP_CLOSE_MIN
        return open_min <= minutes < last_entry_min

    def _should_force_flat(self, t) -> bool:
        session_bounds = self._session_bounds_minutes_et(t.date())
        if session_bounds is None:
            return False
        _, session_close_min = session_bounds
        minutes = t.hour * 60 + t.minute
        return minutes >= session_close_min - self.SKIP_CLOSE_MIN

    def _session_bounds_minutes_et(self, d) -> tuple[int, int] | None:
        try:
            window = session_window_for_date(d)
        except LookupError:
            return None
        open_et = datetime.fromtimestamp(window.open_ms_utc / 1000, tz=UTC).astimezone(_ET)
        close_et = datetime.fromtimestamp(window.close_ms_utc / 1000, tz=UTC).astimezone(_ET)
        return (open_et.hour * 60 + open_et.minute, close_et.hour * 60 + close_et.minute)

    def _on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        # Bar close time in exchange-local (ET); the reader yields ET bars.
        t = bar.end_time
        self._maybe_reset_session(t.date())

        self._vwap.update(
            t,
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=float(bar.volume),
        )
        vwap = self._vwap.current_value
        if vwap is None:
            return
        close = float(bar.close)
        self._sigma.update(close - vwap)

        holding = self._in_position

        # Force-flat before the scheduled close; half-days close earlier.
        if self._should_force_flat(t):
            if holding:
                self.ctx.liquidate(self._symbol)
                self._in_position = False
            return

        if not self._sigma.is_ready:
            return
        sigma = self._sigma.current_value
        assert sigma is not None
        lower = vwap - self.K * sigma

        if not holding:
            if (
                close < lower
                and self._in_entry_window(t)
                and self._trades_today < self.MAX_TRADES_PER_DAY
            ):
                self.ctx.market_order(self._symbol, self.QUANTITY, tag="VwapReversionEntry")
                self._in_position = True
                self._trades_today += 1
        else:
            if close >= vwap:
                self.ctx.liquidate(self._symbol)
                self._in_position = False
