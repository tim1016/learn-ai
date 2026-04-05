# Data Lab — Available Indicators

## Overlay Indicators (Main Chart)

These render as line series directly on the price chart.

| Indicator | Key | Parameters | Defaults | Description |
|-----------|-----|------------|----------|-------------|
| EMA | `ema` | `length` (1–500) | 10 | Exponential Moving Average |
| SMA | `sma` | `length` (1–500) | 20 | Simple Moving Average |
| DEMA | `dema` | `length` (1–500) | 10 | Double Exponential Moving Average |
| TEMA | `tema` | `length` (1–500) | 10 | Triple Exponential Moving Average |
| WMA | `wma` | `length` (1–500) | 10 | Weighted Moving Average |
| HMA | `hma` | `length` (1–500) | 9 | Hull Moving Average |
| KAMA | `kama` | `length` (1–500) | 10 | Kaufman Adaptive Moving Average |
| ZLMA | `zlma` | `length` (1–500) | 10 | Zero-Lag Moving Average |
| RMA | `rma` | `length` (1–500) | 10 | Rolling Moving Average (Wilder's) |
| ALMA | `alma` | `length` (1–500) | 10 | Arnaud Legoux Moving Average |
| Bollinger Bands | `bbands` | `length` (1–200), `std` (0.1–5.0) | 20, 2.0 | Upper/middle/lower bands based on SMA ± standard deviations |
| Supertrend | `supertrend` | `length` (1–100), `multiplier` (0.5–10.0) | 10, 3.0 | ATR-based trend-following overlay (up line + down line) |
| VWAP | `vwap` | — | — | Volume Weighted Average Price |
| Parabolic SAR | `psar` | `af0` (0.001–0.1), `af` (0.001–0.1), `max_af` (0.05–1.0) | 0.02, 0.02, 0.2 | Parabolic Stop and Reverse dots |
| Keltner Channel | `kc` | `length` (1–200), `scalar` (0.5–5.0) | 20, 1.5 | EMA-based channel using ATR multiplier |
| Donchian Channel | `donchian` | `lower_length` (1–200), `upper_length` (1–200) | 20, 20 | Highest high / lowest low channel |

## Sub-Panel Indicators (Oscillators & Volume)

These render in their own synced chart panels below the main chart.

| Indicator | Key | Panel | Parameters | Defaults | Reference Lines | Description |
|-----------|-----|-------|------------|----------|-----------------|-------------|
| RSI | `rsi` | `rsi` | `length` (1–100) | 14 | 30, 70 | Relative Strength Index |
| MACD | `macd` | `macd` | `fast` (1–100), `slow` (1–200), `signal` (1–50) | 12, 26, 9 | — | Moving Average Convergence Divergence (histogram + MACD line + signal line) |
| Stochastic | `stoch` | `stoch` | `k` (1–100), `d` (1–50) | 14, 3 | 20, 80 | Stochastic Oscillator (%K and %D lines) |
| ADX | `adx` | `adx` | `length` (1–100) | 14 | 25 | Average Directional Index (ADX + DI+ + DI−) |
| ATR | `atr` | `atr` | `length` (1–100) | 14 | — | Average True Range |
| Stoch RSI | `stochrsi` | `stochrsi` | `length` (1–100) | 14 | — | Stochastic RSI |
| CCI | `cci` | `cci` | `length` (1–100) | 14 | −100, 100 | Commodity Channel Index |
| Williams %R | `willr` | `willr` | `length` (1–100) | 14 | — | Williams Percent Range |
| ROC | `roc` | `roc` | `length` (1–100) | 10 | — | Rate of Change |
| Momentum | `mom` | `mom` | `length` (1–100) | 10 | — | Momentum |
| NATR | `natr` | `natr` | `length` (1–100) | 14 | — | Normalized ATR (percentage) |
| OBV | `obv` | `obv` | — | — | — | On-Balance Volume |
| A/D | `ad` | `ad` | — | — | — | Accumulation/Distribution Line |
| CMF | `cmf` | `cmf` | `length` (1–100) | 20 | — | Chaikin Money Flow |
| MFI | `mfi` | `mfi` | `length` (1–100) | 14 | 20, 80 | Money Flow Index |
| TSI | `tsi` | `tsi` | `fast` (1–100), `slow` (1–200) | 13, 25 | — | True Strength Index |
| Fisher Transform | `fisher` | `fisher` | `length` (1–100) | 9 | — | Fisher Transform |
| Squeeze | `squeeze` | `squeeze` | `bb_length` (1–200), `kc_length` (1–200) | 20, 20 | — | TTM Squeeze (Bollinger inside Keltner detection) |
| Aroon | `aroon` | `aroon` | `length` (1–100) | 25 | — | Aroon Up/Down |

## Totals

- **33 indicators** total
- **16 overlay** indicators (rendered on the main price chart)
- **17 sub-panel** indicators (rendered in synced panels below)
- **3 parameter-free** indicators (VWAP, OBV, A/D)

## Chart Visibility

All indicators support real-time visibility toggling via the indicator toolbar above the chart. Toggling is purely visual — no re-fetch is required. Use the eye icons on each indicator chip, or the bulk Show All / Hide All buttons.
