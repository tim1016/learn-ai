# Data Lab — Complete Indicator Reference

All indicators are calculated server-side by the **pandas-ta** Python library. The Data Lab component dynamically loads the catalog from the Python service and lets you configure multiple instances of each indicator with custom parameters.

---

## Overlay Indicators (Main Price Chart)

These render as line series directly on the candlestick chart.

### 1. EMA — Exponential Moving Average
- **Key:** `ema`
- **Parameters:** `length` (1–500, default: 10)
- **Default instances:** 5, 10, 20, 30, 40, 50, 100, 200
- **Calculation:** Weights recent prices exponentially. Each value = `price × k + prev_EMA × (1 − k)` where `k = 2 / (length + 1)`. Reacts faster to price changes than SMA.
- **Use case:** Trend direction, dynamic support/resistance, crossover signals.

### 2. SMA — Simple Moving Average
- **Key:** `sma`
- **Parameters:** `length` (1–500, default: 20)
- **Default instances:** None
- **Calculation:** Arithmetic mean of the last `length` closing prices. All bars weighted equally.
- **Use case:** Baseline trend filter, Bollinger Band midline.

### 3. DEMA — Double Exponential Moving Average
- **Key:** `dema`
- **Parameters:** `length` (1–500, default: 10)
- **Default instances:** None
- **Calculation:** `2 × EMA(length) − EMA(EMA(length))`. Subtracts the lagged EMA of EMA to reduce delay.
- **Use case:** Faster trend detection than single EMA with reduced lag.

### 4. TEMA — Triple Exponential Moving Average
- **Key:** `tema`
- **Parameters:** `length` (1–500, default: 10)
- **Default instances:** None
- **Calculation:** `3 × EMA − 3 × EMA(EMA) + EMA(EMA(EMA))`. Triple-smoothed with lag correction.
- **Use case:** Ultra-low-lag moving average for fast-moving markets.

### 5. WMA — Weighted Moving Average
- **Key:** `wma`
- **Parameters:** `length` (1–500, default: 10)
- **Default instances:** None
- **Calculation:** Linearly weighted — newest bar gets weight `n`, next gets `n−1`, ..., oldest gets `1`. Sum of (weight × price) / sum of weights.
- **Use case:** Middle ground between SMA (equal weight) and EMA (exponential weight).

### 6. HMA — Hull Moving Average
- **Key:** `hma`
- **Parameters:** `length` (1–500, default: 9)
- **Default instances:** None
- **Calculation:** `WMA(2 × WMA(n/2) − WMA(n), √n)`. Uses weighted moving averages at different periods with a square-root smoothing step.
- **Use case:** Extremely smooth with minimal lag. Good for identifying trend changes early.

### 7. KAMA — Kaufman Adaptive Moving Average
- **Key:** `kama`
- **Parameters:** `length` (1–500, default: 10)
- **Default instances:** None
- **Calculation:** Adapts smoothing constant based on an Efficiency Ratio = `|direction| / volatility`. Fast smoothing constant (~2-period EMA) in trends, slow (~30-period EMA) in chop.
- **Use case:** Reduces whipsaws in sideways markets while staying responsive in trends.

### 8. ZLMA — Zero-Lag Moving Average
- **Key:** `zlma`
- **Parameters:** `length` (1–500, default: 10)
- **Default instances:** None
- **Calculation:** Applies EMA to a lag-compensated series: `adjusted = close + (close − EMA(close))`, then `EMA(adjusted, length)`.
- **Use case:** Attempts to eliminate the inherent lag of moving averages.

### 9. RMA — Rolling Moving Average (Wilder's Smoothing)
- **Key:** `rma`
- **Parameters:** `length` (1–500, default: 10)
- **Default instances:** None
- **Calculation:** EMA variant with `alpha = 1/length` (instead of `2/(length+1)`). Equivalent to Wilder's smoothing used in RSI and ATR calculations.
- **Use case:** Smoother than standard EMA. Building block for RSI, ATR, and ADX.

### 10. ALMA — Arnaud Legoux Moving Average
- **Key:** `alma`
- **Parameters:** `length` (1–500, default: 10)
- **Default instances:** None
- **Calculation:** Gaussian-weighted moving average. Applies a bell-curve kernel centered at an offset point within the window. The sigma parameter controls the width of the Gaussian.
- **Use case:** Smooth trend line with configurable responsiveness. Avoids the ringing artifacts of other low-lag MAs.

### 11. Bollinger Bands
- **Key:** `bbands`
- **Parameters:** `length` (1–200, default: 20), `std` (0.1–5.0, default: 2.0)
- **Default instances:** length=20, std=2.0
- **Output columns:** `bbl` (lower), `bbm` (middle), `bbu` (upper), `bbb` (bandwidth), `bbp` (%B)
- **Calculation:**
  - Middle = SMA(length)
  - Upper = SMA + `std` × StdDev(length)
  - Lower = SMA − `std` × StdDev(length)
  - Bandwidth = (Upper − Lower) / Middle × 100
  - %B = (Close − Lower) / (Upper − Lower)
- **Use case:** Volatility bands. Squeeze (narrow bands) signals breakout potential. %B shows where price sits within the bands.

### 12. Supertrend
- **Key:** `supertrend`
- **Parameters:** `length` (1–100, default: 10), `multiplier` (0.5–10.0, default: 3.0)
- **Default instances:** length=10, multiplier=3.0
- **Output columns:** `supert` (value), `supertd` (direction), `supertl` (long), `superts` (short)
- **Calculation:**
  - Basic upper band = HL/2 + multiplier × ATR(length)
  - Basic lower band = HL/2 − multiplier × ATR(length)
  - Direction flips when close crosses a band
- **Use case:** Trend-following stop-loss overlay. Green line below = uptrend, red line above = downtrend.

### 13. VWAP — Volume Weighted Average Price
- **Key:** `vwap`
- **Parameters:** None
- **Default instances:** None
- **Calculation:** `Cumulative(typical_price × volume) / Cumulative(volume)` where typical_price = (H+L+C)/3. Resets at the start of each trading session.
- **Use case:** Institutional benchmark price. Price above VWAP = bullish bias, below = bearish.

### 14. Parabolic SAR — Stop and Reverse
- **Key:** `psar`
- **Parameters:** `af0` (0.001–0.1, default: 0.02), `af` (0.001–0.1, default: 0.02), `max_af` (0.05–1.0, default: 0.2)
- **Default instances:** None
- **Calculation:** Trailing stop that accelerates toward price. Acceleration Factor (AF) starts at `af0`, increases by `af` each time a new extreme point is made, capped at `max_af`. SAR flips from below to above price (or vice versa) on crossover.
- **Use case:** Trailing stop-loss placement. Dots below = long, dots above = short.

### 15. Keltner Channel
- **Key:** `kc`
- **Parameters:** `length` (1–200, default: 20), `scalar` (0.5–5.0, default: 1.5)
- **Default instances:** None
- **Output columns:** `kcl` (lower), `kcb` (basis), `kcu` (upper)
- **Calculation:**
  - Basis = EMA(length)
  - Upper = EMA + scalar × ATR(length)
  - Lower = EMA − scalar × ATR(length)
- **Use case:** Similar to Bollinger Bands but uses ATR instead of standard deviation. More stable width. Used with BBands for Squeeze detection.

### 16. Donchian Channel
- **Key:** `donchian`
- **Parameters:** `lower_length` (1–200, default: 20), `upper_length` (1–200, default: 20)
- **Default instances:** None
- **Output columns:** `dcl` (lower), `dcm` (middle), `dcu` (upper)
- **Calculation:**
  - Upper = Highest high over `upper_length` bars
  - Lower = Lowest low over `lower_length` bars
  - Middle = (Upper + Lower) / 2
- **Use case:** Breakout trading (Turtle Trading system). Price breaking above upper channel = buy signal.

---

## Sub-Panel Indicators (Oscillators & Volume)

These render in their own synced chart panels below the main price chart.

### 17. RSI — Relative Strength Index
- **Key:** `rsi` | **Panel:** `rsi`
- **Parameters:** `length` (1–100, default: 14)
- **Reference lines:** 30 (oversold), 70 (overbought)
- **Default instances:** None
- **Calculation:** `100 − 100 / (1 + RS)` where RS = average gain / average loss over `length` bars (using Wilder's smoothing). Range: 0–100.
- **Use case:** Overbought/oversold momentum oscillator. Divergences between RSI and price signal potential reversals.

### 18. MACD — Moving Average Convergence Divergence
- **Key:** `macd` | **Panel:** `macd`
- **Parameters:** `fast` (1–100, default: 12), `slow` (1–200, default: 26), `signal` (1–50, default: 9)
- **Default instances:** fast=12, slow=26, signal=9
- **Output columns:** `macd` (line), `macdh` (histogram), `macds` (signal)
- **Calculation:**
  - MACD line = EMA(fast) − EMA(slow)
  - Signal line = EMA(signal) of MACD line
  - Histogram = MACD − Signal
- **Use case:** Trend momentum and crossover signals. Histogram shows acceleration/deceleration of trend.

### 19. Stochastic Oscillator
- **Key:** `stoch` | **Panel:** `stoch`
- **Parameters:** `k` (1–100, default: 14), `d` (1–50, default: 3)
- **Reference lines:** 20 (oversold), 80 (overbought)
- **Default instances:** None
- **Output columns:** `stochk` (%K), `stochd` (%D)
- **Calculation:**
  - %K = `(close − lowest_low) / (highest_high − lowest_low) × 100` over `k` bars
  - %D = SMA(d) of %K
- **Use case:** Mean-reversion oscillator. %K/%D crossovers in extreme zones signal reversals.

### 20. ADX — Average Directional Index
- **Key:** `adx` | **Panel:** `adx`
- **Parameters:** `length` (1–100, default: 14)
- **Reference lines:** 25 (trending threshold)
- **Default instances:** None
- **Output columns:** `adx`, `dmp` (DI+), `dmn` (DI−)
- **Calculation:**
  - +DM = current high − previous high (if positive and > −DM, else 0)
  - −DM = previous low − current low (if positive and > +DM, else 0)
  - +DI = 100 × smoothed(+DM) / smoothed(TR)
  - −DI = 100 × smoothed(−DM) / smoothed(TR)
  - ADX = smoothed(|+DI − −DI| / (+DI + −DI)) × 100
- **Use case:** Measures trend strength (0–100), not direction. ADX > 25 = trending. DI+/DI− crossovers give direction.

### 21. ATR — Average True Range
- **Key:** `atr` | **Panel:** `atr`
- **Parameters:** `length` (1–100, default: 14)
- **Default instances:** None
- **Calculation:** RMA (Wilder's smoothing) of True Range, where TR = max(high−low, |high−prev_close|, |low−prev_close|).
- **Use case:** Volatility in absolute price terms. Used for position sizing, stop-loss placement, and as input to Supertrend/Keltner.

### 22. Stochastic RSI
- **Key:** `stochrsi` | **Panel:** `stochrsi`
- **Parameters:** `length` (1–100, default: 14)
- **Default instances:** None
- **Calculation:** Applies the Stochastic formula to RSI values: `(RSI − min(RSI, length)) / (max(RSI, length) − min(RSI, length))`. Range: 0–1.
- **Use case:** More sensitive than plain RSI. Better at detecting short-term overbought/oversold conditions.

### 23. CCI — Commodity Channel Index
- **Key:** `cci` | **Panel:** `cci`
- **Parameters:** `length` (1–100, default: 14)
- **Reference lines:** −100, +100
- **Default instances:** None
- **Calculation:** `(typical_price − SMA(tp, length)) / (0.015 × mean_deviation)` where typical_price = (H+L+C)/3. Unbounded oscillator.
- **Use case:** Values above +100 = overbought, below −100 = oversold. Can trend beyond these levels in strong moves.

### 24. Williams %R
- **Key:** `willr` | **Panel:** `willr`
- **Parameters:** `length` (1–100, default: 14)
- **Default instances:** None
- **Calculation:** `(highest_high − close) / (highest_high − lowest_low) × −100`. Range: −100 to 0.
- **Use case:** Inverted stochastic. −80 to −100 = oversold, 0 to −20 = overbought. Fast-reacting momentum oscillator.

### 25. ROC — Rate of Change
- **Key:** `roc` | **Panel:** `roc`
- **Parameters:** `length` (1–100, default: 10)
- **Default instances:** None
- **Calculation:** `((close − close[length]) / close[length]) × 100`. Percentage change over `length` bars.
- **Use case:** Momentum as a percentage. Zero-line crossovers signal trend changes. Divergences with price signal weakening trends.

### 26. Momentum
- **Key:** `mom` | **Panel:** `mom`
- **Parameters:** `length` (1–100, default: 10)
- **Default instances:** None
- **Calculation:** `close − close[length]`. Raw price difference (not normalized).
- **Use case:** Simplest momentum measure. Positive = price rising vs N bars ago. Useful for comparing momentum magnitude across time.

### 27. NATR — Normalized Average True Range
- **Key:** `natr` | **Panel:** `natr`
- **Parameters:** `length` (1–100, default: 14)
- **Default instances:** None
- **Calculation:** `(ATR / close) × 100`. ATR expressed as a percentage of the current price.
- **Use case:** Compare volatility across different price levels or tickers. A $500 stock with ATR $5 = 1% NATR.

### 28. OBV — On-Balance Volume
- **Key:** `obv` | **Panel:** `obv`
- **Parameters:** None
- **Default instances:** None
- **Calculation:** Cumulative sum: if close > prev_close, add volume; if close < prev_close, subtract volume; if equal, no change.
- **Use case:** Volume flow direction. Rising OBV with rising price confirms trend. Divergence (price up, OBV flat/down) warns of weakening.

### 29. A/D — Accumulation/Distribution Line
- **Key:** `ad` | **Panel:** `ad`
- **Parameters:** None
- **Default instances:** None
- **Calculation:** `cumsum(((close − low) − (high − close)) / (high − low) × volume)`. The Money Flow Multiplier weights volume by where the close falls in the bar's range.
- **Use case:** Like OBV but considers where price closed within the bar. Close near high = accumulation, near low = distribution.

### 30. CMF — Chaikin Money Flow
- **Key:** `cmf` | **Panel:** `cmf`
- **Parameters:** `length` (1–100, default: 20)
- **Default instances:** None
- **Calculation:** `sum(A/D_volume, length) / sum(volume, length)`. Bounded between −1 and +1.
- **Use case:** Sustained buying/selling pressure over a period. Positive = net buying, negative = net selling.

### 31. MFI — Money Flow Index
- **Key:** `mfi` | **Panel:** `mfi`
- **Parameters:** `length` (1–100, default: 14)
- **Reference lines:** 20 (oversold), 80 (overbought)
- **Default instances:** None
- **Calculation:** Volume-weighted RSI. `100 − 100 / (1 + positive_money_flow / negative_money_flow)` where money flow = typical_price × volume. Range: 0–100.
- **Use case:** "RSI with volume." Overbought/oversold signals that incorporate volume confirmation.

### 32. TSI — True Strength Index
- **Key:** `tsi` | **Panel:** `tsi`
- **Parameters:** `fast` (1–100, default: 13), `slow` (1–200, default: 25)
- **Default instances:** None
- **Calculation:** `EMA(fast, EMA(slow, price_change)) / EMA(fast, EMA(slow, |price_change|)) × 100`. Double-smoothed momentum. Range: −100 to +100.
- **Use case:** Trend direction and strength. Zero-line crossovers and signal line crossovers for entries/exits.

### 33. Fisher Transform
- **Key:** `fisher` | **Panel:** `fisher`
- **Parameters:** `length` (1–100, default: 9)
- **Default instances:** None
- **Calculation:** Normalizes price to a −1 to +1 range, then applies the inverse Fisher function: `0.5 × ln((1 + x) / (1 − x))`. This creates a near-Gaussian distribution with sharp peaks.
- **Use case:** Turning-point detection. Sharp spikes mark reversals. Crosses of the Fisher line and its signal line generate trades.

### 34. Squeeze
- **Key:** `squeeze` | **Panel:** `squeeze`
- **Parameters:** `bb_length` (1–200, default: 20), `kc_length` (1–200, default: 20)
- **Default instances:** None
- **Calculation:** Detects when Bollinger Bands contract inside the Keltner Channel (low volatility "squeeze"). The momentum histogram (based on a linear regression of price minus the midline) shows direction for the anticipated breakout.
- **Use case:** Volatility compression precedes expansion. Squeeze ON = coiling. Histogram direction during squeeze predicts breakout direction.

### 35. Aroon
- **Key:** `aroon` | **Panel:** `aroon`
- **Parameters:** `length` (1–100, default: 25)
- **Default instances:** None
- **Output columns:** `aroond` (down), `aroonu` (up), `aroonosc` (oscillator)
- **Calculation:**
  - Aroon Up = `((length − bars_since_highest_high) / length) × 100`
  - Aroon Down = `((length − bars_since_lowest_low) / length) × 100`
  - Oscillator = Aroon Up − Aroon Down
- **Use case:** Identifies trend starts and strength. Aroon Up near 100 = strong uptrend. Crossovers signal trend changes.

---

## Summary

| Category | Count | Indicators |
|----------|-------|------------|
| **Overlay (main chart)** | 16 | EMA, SMA, DEMA, TEMA, WMA, HMA, KAMA, ZLMA, RMA, ALMA, BBands, Supertrend, VWAP, PSAR, Keltner, Donchian |
| **Sub-panel (oscillators)** | 19 | RSI, MACD, Stochastic, ADX, ATR, StochRSI, CCI, Williams %R, ROC, Momentum, NATR, OBV, A/D, CMF, MFI, TSI, Fisher, Squeeze, Aroon |
| **Parameter-free** | 3 | VWAP, OBV, A/D |
| **Total** | **35** | |

### Default Selection (11 instances)
- EMA: 5, 10, 20, 30, 40, 50, 100, 200
- Bollinger Bands: length=20, std=2.0
- Supertrend: length=10, multiplier=3.0
- MACD: fast=12, slow=26, signal=9
