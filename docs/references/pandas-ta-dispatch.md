# pandas-ta dispatch indicators — consolidated port attribution

> **Status:** active (consolidated 2026-09-12 from sixteen per-indicator
> stubs; git history retains the originals).

## Scope and shared conventions

The indicators in this note are **transport pass-throughs**: Data Lab's
`PythonDataService/app/services/dataset_service.py` dispatch calls the
`pandas_ta` function directly via the generic
`calculate_dynamic_indicators` reflection path. There is no in-repo port
of any function listed here.

Conventions shared by every entry:

- **Library:** pandas-ta v0.4.71b0, pinned in `PythonDataService/requirements.txt`.
  Equivalence is *by reference* — the dataset value at any bar is exactly
  what the pinned `pandas_ta` function returns for the same OHLCV slice.
  Any library upgrade must be paired with a regression check; an upgrade
  that changes a default that materially affects output is a math change,
  not a refactor.
- **Tests:** none specific to the individual functions; the dispatch path
  is covered by the Data Lab dataset-generation integration tests under
  `PythonDataService/tests/`.
- **Golden fixtures:** none. If an in-house port is later required (to
  drop the pandas-ta dependency), capture pandas-ta output on a fixed
  SPY window and pin `atol=1e-9, rtol=0`.

Indicators with in-repo ports or dedicated notes are **not** covered
here — see `rsi.md`, `sma.md`, `vwap.md`, `adx.md`, `macd.md`, and
`supertrend.md` in this directory.

## Summary table

| Indicator | `pandas_ta` call | Data Lab defaults | pandas-ta default divergence | Output columns |
|---|---|---|---|---|
| Accumulation/Distribution (AD) | `ad(h, l, c, v)` | none exposed | — | `ad` |
| Arnaud Legoux MA (ALMA) | `alma(c, length=N)` | `length=10` | **pandas-ta default `length=9`** | `alma_length{N}` |
| Chaikin Money Flow (CMF) | `cmf(h, l, c, v, length=N)` | `length=20` | — | `cmf_length{N}` |
| Double EMA (DEMA) | `dema(c, length=N)` | `length=10` | — | `dema_length{N}` |
| Donchian Channels | `donchian(h, l, lower_length=L, upper_length=U)` | `20 / 20` | — | `dcl/dcm/dcu_{L}_{U}` |
| Fisher Transform | `fisher(h, l, length=N)` | `length=9` | `signal=1` not exposed | `fishert(s)_{N}_{1}` |
| Hull MA (HMA) | `hma(c, length=N)` | `length=9` | **pandas-ta default `length=10`** | `hma_length{N}` |
| Keltner Channels | `kc(h, l, c, length=N, scalar=S)` | `20 / 1.5` | **pandas-ta default `scalar=2`** | `kcle/kcbe/kcue_{N}_{S}` |
| Money Flow Index (MFI) | `mfi(h, l, c, v, length=N)` | `length=14` | `drift=1` not exposed | `mfi_length{N}` |
| Momentum (MOM) | `mom(c, length=N)` | `length=10` | — | `mom_length{N}` |
| Normalized ATR (NATR) | `natr(h, l, c, length=N)` | `length=14` | **smoother is EMA, not Wilder RMA** | `natr_length{N}` |
| Rate of Change (ROC) | `roc(c, length=N)` | `length=10` | `scalar=100` not exposed | `roc_length{N}` |
| Triple EMA (TEMA) | `tema(c, length=N)` | `length=10` | — | `tema_length{N}` |
| Williams %R | `willr(h, l, c, length=N)` | `length=14` | — | `willr_length{N}` |
| Weighted MA (WMA) | `wma(c, length=N)` | `length=10` | — | `wma_length{N}` |
| Zero-Lag MA (ZLMA) | `zlma(c, length=N)` | `length=10` | `mamode="ema"` not exposed | `zl_ema_{N}` |

pandas-ta title-cases its column names; the Data Lab dispatch lowercases
them. Display names are keyed off the *exposed* params, not pandas-ta's
internal naming, so numeric values are identical even where names differ.

## Math summaries

### Accumulation/Distribution (AD)

Source: Marc Chaikin (chartschool.stockcharts.com specification).

1. Money Flow Multiplier: `MFM = ((close − low) − (high − close)) / (high − low)`; pandas-ta sets `MFM = 0` when `high == low`.
2. Money Flow Volume: `MFV = MFM × volume`.
3. `AD_t = AD_{t-1} + MFV_t`, with `AD_0 = MFV_0`.

Cumulative and unbounded; reacts only to the close's location within the
bar's H–L range, not to bar-over-bar price change.

### Arnaud Legoux MA (ALMA)

Source: Legoux & Kouzis-Loukas (2009). Gaussian-weighted, offset-shifted
moving average. With window `N`, sigma `σ`, and `dist_offset ∈ [0, 1]`:

```
m   = dist_offset × (N − 1)
s   = N / σ
w_i = exp(−(i − m)² / (2 × s²))     for i = 0, 1, ..., N−1
ALMA_t = sum(w_i × close_{t−N+1+i}) / sum(w_i)
```

The weight peak sits at index `m`. With `dist_offset=0.85` the peak is
shifted toward recent bars (~85 % of the way along the window); lower
offsets shift it toward older bars (smoother, more lagged). Larger `σ` →
sharper peak; smaller `σ` → flatter weights (closer to a SMA). First
`N−1` bars are `NaN`. `sigma=6.0` and `dist_offset=0.85` (pandas-ta
defaults) are not exposed in the Data Lab UI; because they materially
affect the curve shape, a future default change must be treated as a
math change.

### Chaikin Money Flow (CMF)

Source: Marc Chaikin. With MFM/MFV as in AD:

```
CMF_t = sum(MFV over last N bars) / sum(volume over last N bars)
```

Bounded in `[−1, +1]`; NaN until the rolling window is full. Bounded
rolling oscillator where AD is a cumulative line; the underlying
MFM/MFV math is shared.

### Double EMA (DEMA)

Source: Patrick Mulloy, *Smoothing Data with Faster Moving Averages*
(TASC, January 1994). With `EMA_N(x)` = `α = 2/(N+1)` EMA, SMA-seeded:

```
EMA1 = EMA_N(close);  EMA2 = EMA_N(EMA1)
DEMA = 2 × EMA1 − EMA2
```

The smoothed-lag term `EMA2` shifts the curve left in time, reducing lag
relative to a single EMA at the same `N`. First `2(N−1)` bars are `NaN`.

### Donchian Channels

Source: Richard Donchian's price-channel breakout system (1960s).

```
Lower_t = min(low over lower_length);  Upper_t = max(high over upper_length)
Mid_t   = (Lower_t + Upper_t) / 2
```

Pure price-extrema envelope; lower and upper bands take independent
lookbacks (asymmetric channels). First
`max(lower_length, upper_length) − 1` bars are `NaN`. Integer min/max
plus an arithmetic mean — bit-exact reproducible across platforms; a
golden fixture is not strictly needed.

### Ehlers Fisher Transform

Source: John F. Ehlers, *Using the Fisher Transform* (TASC V. 20:11,
2002). Coerces a near-uniform input distribution into a near-Gaussian
output, sharpening turning points at range extrema.

```
hl2_t = (high_t + low_t) / 2
raw_t = 2 × (hl2_t − LL_t) / (HH_t − LL_t) − 1        (rolling N-window normalization)
val_t = 0.66 × raw_t + 0.67 × val_{t−1},  clipped to [−0.999, +0.999]
fisher_t = 0.5 × ln((1 + val_t) / (1 − val_t)) + 0.5 × fisher_{t−1}
signal_t = fisher_{t−1}                                 (signal=1 default, not exposed)
```

Output approximately ranges in `[−5, +5]` in normal regimes. First
`N − 1` bars are `NaN`. **Quirk:** the recursive `0.66`/`0.67` constants
and the `±0.999` clip are pandas-ta implementation choices (Ehlers'
original uses `2/3` and `1/3`); any in-house port must reproduce them
exactly.

### Hull MA (HMA)

Source: Alan Hull (alanhull.com).

```
half = floor(N/2);  sqrt_len = floor(sqrt(N))
HMA_t = WMA(2 × WMA_half(close) − WMA_N(close), sqrt_len)
```

The double-WMA construction projects price forward enough to compensate
for the outer WMA's smoothing lag. First `N + sqrt_len − 2` bars are
`NaN`. `mamode` swaps the inner MA; the default and Data Lab path use
`mamode="wma"` (the canonical Hull definition).

### Keltner Channels

Source: Chester Keltner's volatility envelope, modernised by Linda
Raschke to use ATR rather than the original high–low average. With
pandas-ta defaults `mamode="ema"`, `tr=True`:

```
Basis_t = EMA_length(close)
ATR_t   = RMA(true_range, length)        (Wilder smoothing of TR)
Upper_t = Basis_t + scalar × ATR_t;  Lower_t = Basis_t − scalar × ATR_t
```

True Range = `max(high − low, |high − prev_close|, |low − prev_close|)`.
The channel is `NaN` until both the basis EMA and the RMA-smoothed ATR
are warm. The Bollinger/Keltner "squeeze" pattern uses this and
`bbands`; the dedicated `squeeze` indicator already encodes that signal.

### Money Flow Index (MFI)

Source: Quong & Soudack, *Volume-Weighted RSI: Money Flow* (TASC, 1989).

1. `TP = (high + low + close) / 3`; raw money flow `MF = TP × volume`.
2. Classify each bar's `MF` as positive/negative by comparing `TP` to
   the prior bar's `TP` (equal contributes zero; `drift=1`).
3. Over the window `N`:
   `MFR = sum(positive MF) / sum(negative MF)`; `MFI = 100 − 100 / (1 + MFR)`.

Bounded in `[0, 100]`; first `N` bars are `NaN` (the comparison needs a
prior bar, so the rolling sums require `N+1` observations).

### Momentum (MOM)

`MOM_t = close_t − close_{t−N}`. Absolute price difference, unbounded,
in price units — **not comparable across instruments**; use ROC for
cross-asset momentum. First `N` bars are `NaN`. Single subtraction —
bit-exact reproducible.

### Normalized ATR (NATR)

With pandas-ta defaults (`mamode="ema"`, `scalar=100`, `drift=1`):

```
TR_t   = max(high_t − low_t, |high_t − close_{t−1}|, |low_t − close_{t−1}|)
ATR_t  = EMA_length(TR)
NATR_t = 100 × ATR_t / close_t
```

Volatility as a percentage of close — scale-invariant, suitable for
cross-asset comparison or volatility-parity sizing; magnitude only, no
directional bias. First bar is `NaN` (TR needs a prior close) plus the
EMA warmup. **Quirk:** unlike most ATR implementations (and TA-Lib's
`NATR`), pandas-ta smooths TR with an **EMA**, not Wilder's RMA — switch
`mamode="rma"` if TA-Lib parity is ever required.

### Rate of Change (ROC)

```
ROC_t = 100 × (close_t − close_{t−N}) / close_{t−N} = 100 × (close_t / close_{t−N} − 1)
```

Standardized percent momentum — unbounded but scale-invariant and
comparable across instruments. First `N` bars are `NaN`. `scalar=100`
not exposed.

### Triple EMA (TEMA)

Source: Mulloy 1994 (as DEMA).

```
EMA1 = EMA_N(close); EMA2 = EMA_N(EMA1); EMA3 = EMA_N(EMA2)
TEMA = 3 × EMA1 − 3 × EMA2 + EMA3
```

Successive lag-correction terms; faster (and noisier) than DEMA at the
same `N`. First `3(N−1)` bars are `NaN`. Useful as a near-zero-lag price
proxy; structurally vulnerable to whipsaw in low-liquidity regimes.

### Williams %R

Source: Larry Williams (1973).

```
HH_t = max(high over N);  LL_t = min(low over N)
WR_t = −100 × (HH_t − close_t) / (HH_t − LL_t)
```

Bounded in `[−100, 0]` (inverted scale): close at the period's high →
`0`, at the low → `−100`. Structurally a re-scaled, inverted Stochastic
%K. First `N − 1` bars are `NaN`.

### Weighted MA (WMA)

Classical linear weighting, `w_i = i` (most recent bar gets weight `N`):

```
WMA_t = sum(w_i × close_{t−N+i}) / (N × (N+1) / 2)
```

First `N−1` bars are `NaN`. `asc=True` (weight direction) left at the
pandas-ta default.

### Zero-Lag MA (ZLMA)

Source: Ehlers & Way, *Zero Lag (Well, Almost)* (TASC V. 28:11, 2010).

```
lag       = floor(0.5 × (N − 1))
deLagged  = 2 × close − close.shift(lag)
ZLMA_t    = EMA_N(deLagged)
```

The de-lagged series adds the current momentum vector to price,
projecting forward by the EMA's nominal lag; the standard
`α = 2/(N+1)` EMA then smooths it. First `lag + N − 1` bars are `NaN`.
`mamode="ema"` default.
