# region imports
from collections import deque

import numpy as np
from AlgorithmImports import *  # noqa: F403  (QuantConnect convention)
# endregion


class SpyVwapReversionReference(QCAlgorithm):
    """Parity reference for the learn-ai SPY 1-min VWAP-band reversion shadow
    strategy (PRD-C / PR-K).

    This algorithm is the *reference oracle*: it runs on QuantConnect Cloud
    (LEAN), and its exported orders are reconciled trade-by-trade against the
    Python port in `app/engine/strategy/algorithms/spy_vwap_reversion.py` via
    `app/research/parity/qc_reconciler.py` at atol=1e-9.

    Formulation (PRD-C accepted defaults — keep these EXACT so the port can
    match bit-for-bit):

      * Symbol / resolution: SPY, 1-minute.
      * VWAP: session-anchored, reset at each RTH open. Cumulative
        Σ(typical_price · volume) / Σ(volume), typical_price = (H+L+C)/3.
      * Distance: dist = close − vwap.
      * Sigma: population standard deviation (ddof=0) of the last LOOKBACK
        `dist` values (rolling window).
      * Bands: lower = vwap − K·sigma, upper = vwap + K·sigma.
      * Entry (long-only): flat AND close < lower AND inside the session
        window AND trades_today < MAX_TRADES_PER_DAY → buy a fixed quantity.
      * Exit: long AND close >= vwap (reverted to fair value) → sell to flat.
      * Session filter: skip the first SKIP_OPEN_MIN and last SKIP_CLOSE_MIN
        minutes of the RTH session for *entries* (exits/force-flat still run).
      * Force-flat: liquidate any open position at FORCE_FLAT (15:55 ET).
      * Fixed quantity (not SetHoldings %) keeps the share count deterministic
        and trivially reproducible in the port — no cash-buffer rounding.

    Pinned parameters live as class attributes so the Python port reads the
    same numbers. Do not change them without regenerating the golden fixture
    and bumping `docs/references/spy-vwap-reversion-port.md`.
    """

    # ── Pinned parameters (the port must mirror these EXACTLY) ───────────
    K = 2.0                     # band multiplier (PRD: k ≈ 1.5–2.0)
    LOOKBACK = 30               # bars for the rolling sigma of dist
    QUANTITY = 100              # fixed share quantity per entry
    SKIP_OPEN_MIN = 5           # no entries in the first 5 min of RTH
    SKIP_CLOSE_MIN = 5          # no entries in the last 5 min before force-flat
    MAX_TRADES_PER_DAY = 4      # entry cap per session
    FORCE_FLAT_HOUR = 15
    FORCE_FLAT_MINUTE = 55

    def initialize(self):
        # PIN the backtest window here before running (see the walkthrough in
        # docs/references/spy-vwap-reversion-port.md). Use a quiet RTH window
        # with no SPY corporate actions for a clean golden fixture.
        self.set_start_date(2024, 3, 4)
        self.set_end_date(2024, 3, 8)
        self.set_cash(100_000)
        self.set_time_zone(TimeZones.NEW_YORK)

        equity = self.add_equity("SPY", Resolution.MINUTE)
        equity.set_data_normalization_mode(DataNormalizationMode.RAW)
        self.spy = equity.symbol

        # Session-anchored VWAP accumulators (reset each RTH open).
        self._cum_pv = 0.0
        self._cum_vol = 0.0
        self._session_date = None

        # Rolling window of dist = close − vwap for the sigma band.
        self._dist = deque(maxlen=self.LOOKBACK)
        self._trades_today = 0

    def _maybe_reset_session(self, bar_time):
        d = bar_time.date()
        if self._session_date != d:
            self._session_date = d
            self._cum_pv = 0.0
            self._cum_vol = 0.0
            self._dist.clear()
            self._trades_today = 0

    def _in_entry_window(self, t):
        # RTH is 09:30–16:00 ET. Entries allowed only between
        # 09:30+SKIP_OPEN_MIN and the force-flat minute − SKIP_CLOSE_MIN.
        minutes = t.hour * 60 + t.minute
        open_min = 9 * 60 + 30 + self.SKIP_OPEN_MIN
        last_entry_min = (
            self.FORCE_FLAT_HOUR * 60 + self.FORCE_FLAT_MINUTE - self.SKIP_CLOSE_MIN
        )
        return open_min <= minutes < last_entry_min

    def on_data(self, data: Slice):
        if not data.bars.contains_key(self.spy):
            return
        bar = data.bars[self.spy]
        t = self.time
        self._maybe_reset_session(t)

        # Update session-anchored VWAP with this bar's typical price.
        typical = (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0
        vol = float(bar.volume)
        self._cum_pv += typical * vol
        self._cum_vol += vol
        if self._cum_vol <= 0.0:
            return
        vwap = self._cum_pv / self._cum_vol

        close = float(bar.close)
        dist = close - vwap
        self._dist.append(dist)

        holding = self.portfolio[self.spy].quantity

        # Force-flat at 15:55 ET — exit any open position, no new entries.
        if t.hour == self.FORCE_FLAT_HOUR and t.minute >= self.FORCE_FLAT_MINUTE:
            if holding != 0:
                self.liquidate(self.spy, tag="ForceFlat")
            return

        # Need a full lookback window before the sigma band is defined.
        if len(self._dist) < self.LOOKBACK:
            return
        sigma = float(np.std(np.array(self._dist), ddof=0))
        lower = vwap - self.K * sigma
        upper = vwap + self.K * sigma  # noqa: F841 (documents the symmetric band)

        if holding == 0:
            if (
                close < lower
                and self._in_entry_window(t)
                and self._trades_today < self.MAX_TRADES_PER_DAY
            ):
                self.market_order(self.spy, self.QUANTITY, tag="VwapReversionEntry")
                self._trades_today += 1
        else:
            # Exit when price reverts to (or above) fair value (VWAP).
            if close >= vwap:
                self.liquidate(self.spy, tag="VwapReversionExit")
