# pandas-ta dispatch indicators — consolidated port attribution

> **Status:** active (consolidated 2026-09-12 from sixteen per-indicator
> stubs; the RMA, RSI, SMA and VWAP notes folded in 2026-10-01; git history
> retains the originals).

## Scope and shared conventions

The indicators in this note are **transport pass-throughs**: Data Lab's
`PythonDataService/app/services/dataset_service.py` dispatch calls the
`pandas_ta` function directly via the generic
`calculate_dynamic_indicators` reflection path. There is no in-repo port
of any function listed here.

Conventions shared by every entry:

- **Library:** pandas-ta v0.4.71b0, pinned in `PythonDataService/requirements-light.txt`.
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

The engine's own indicator ports (`app/engine/indicators/`) are not
covered here; their fixtures and attributions are under
`app/engine/tests/fixtures/golden/`.

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
| Wilder's MA (RMA) | `rma(c, length=N)` | `length=10` | — | `rma_length{N}` |
| Rate of Change (ROC) | `roc(c, length=N)` | `length=10` | `scalar=100` not exposed | `roc_length{N}` |
| Relative Strength Index (RSI) | `rsi(c, length=N)` | `length=14` | `scalar`, `mamode`, `drift` not exposed | `rsi_length{N}` |
| Simple MA (SMA) | `sma(c, length=N)` | `length=20` | **pandas-ta default `length=10`** | `sma_length{N}` |
| Triple EMA (TEMA) | `tema(c, length=N)` | `length=10` | — | `tema_length{N}` |
| VWAP | `vwap(h, l, c, v)` | none exposed | `anchor="D"`, `bands` not exposed | see VWAP below |
| Williams %R | `willr(h, l, c, length=N)` | `length=14` | — | `willr_length{N}` |
| Weighted MA (WMA) | `wma(c, length=N)` | `length=10` | — | `wma_length{N}` |
| Zero-Lag MA (ZLMA) | `zlma(c, length=N)` | `length=10` | `mamode="ema"` not exposed | `zl_ema_{N}` |

pandas-ta title-cases its column names; the Data Lab dispatch lowercases
them. Display names are keyed off the *exposed* params, not pandas-ta's
internal naming, so numeric values are identical even where names differ.

## Math summaries

Only the defaults and quirks a reader would not guess from the
indicator's name. The formulas are pandas-ta's; read them in the pinned
library source.

### Accumulation/Distribution (AD)

Source: Marc Chaikin. A flat bar (`high == low`) contributes zero money
flow. Cumulative and unbounded.

### Arnaud Legoux MA (ALMA)

Source: Legoux & Kouzis-Loukas (2009). `sigma=6.0` and `dist_offset=0.85`
(pandas-ta defaults) are not exposed in the Data Lab UI; because they
materially affect the curve shape, a future default change must be
treated as a math change.

### Ehlers Fisher Transform

Source: John F. Ehlers, *Using the Fisher Transform* (TASC V. 20:11,
2002). **Quirk:** the recursive `0.66`/`0.67` constants and the `±0.999`
clip are pandas-ta implementation choices, not Ehlers' original; any
in-house port must reproduce them exactly.

### Hull MA (HMA)

Source: Alan Hull. The default and Data Lab path use `mamode="wma"` (the
canonical Hull definition).

### Keltner Channels

Source: Chester Keltner, modernised by Linda Raschke to use true range.
**Quirk:** with pandas-ta defaults (`mamode="ema"`, `tr=True`) the band
width is an **EMA** of true range, the same smoother as the basis, not
Wilder's ATR.

### Money Flow Index (MFI)

Source: Quong & Soudack (TASC, 1989). **Quirk:** pandas-ta counts a bar
as positive flow only when its typical price is above the prior bar's; a
bar with an equal typical price counts as negative flow.

### Momentum (MOM)

Absolute price difference in price units — **not comparable across
instruments**; use ROC for cross-asset momentum.

### Normalized ATR (NATR)

**Quirk:** unlike most ATR implementations (and TA-Lib's `NATR`),
pandas-ta smooths TR with an **EMA**, not Wilder's RMA — switch
`mamode="rma"` if TA-Lib parity is ever required.

### Wilder's MA (RMA)

Source: J. Welles Wilder, *New Concepts in Technical Trading Systems*
(1978). pandas-ta computes `ewm(alpha=1/N, adjust=False)` seeded at the
first value, not at an SMA. Data Lab's `rma` has no test of its own: the
Wilder recursion is covered only through the engine's ADX and Supertrend
ports (`app/engine/indicators/adx.py`, `supertrend.py`), which reproduce
it in their own code, seeded with the first `N` values, and pin it at
`atol=1e-9, rtol=0`.

### Relative Strength Index (RSI)

Source: Wilder (1978). pandas-ta smooths gains and losses with its RMA
(`mamode="rma"`).

**Warm-up (re-checked against #2611 on 2026-10-01).** The Data Lab path
applies no `3 × length` warm-up mask; it emits raw pandas-ta output. In
pandas-ta 0.4.71b0 that output is `NaN` only at the first bar (the price
difference), because its RMA is not SMA-seeded, so early values are cold
rather than missing. What keeps cold values out of a window is the #2611
lead-in (at least `10 × length` bars for RSI), trimmed after the indicators run;
its accepted residual is in
[`data-lab-indicator-warmup.md`](data-lab-indicator-warmup.md). When less
history than that exists before the window, the first values in the
window carry more of the cold start.

### Simple MA (SMA)

pandas-ta's default is `length=10`; the Data Lab UI defaults to
`length=20`. The divergence is a deliberate UX choice. The engine's SMA
(`app/engine/indicators/sma.py`) is separate code.

### VWAP

pandas-ta's `vwap` is the anchored session VWAP: cumulative
`TP × volume` over cumulative volume, with `TP = (high + low + close) / 3`,
reset each `anchor` period (`"D"`, the calendar day of the index).

- **Polygon bar `vwap` is not the session VWAP.** Polygon's aggregate
  response carries a per-bar `vwap` field: the volume-weighted price of
  that one bar, computed by Polygon. It passes through unchanged on the
  OHLCV side and is not the cumulative session VWAP this indicator
  computes.
- pandas-ta needs an ordered `DatetimeIndex` to find the anchor. As of
  2026-10-01 the Data Lab frame carries a `RangeIndex`, so pandas-ta
  returns `None` and the dispatch logs and skips the indicator: the
  selector adds no column.

### Williams %R

Source: Larry Williams (1973). Inverted scale, bounded in `[−100, 0]`.

### Weighted MA (WMA)

`asc=True` (most recent bar weighs most) is left at the pandas-ta
default.
