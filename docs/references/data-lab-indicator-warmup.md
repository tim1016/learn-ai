# Data Lab indicator warm-up — request-sized lead-ins (#2611)

> **Status:** active (2026-09-30). Owner decision on #2611: "reduce the
> accuracy needed at warmup, judiciously."

## Scope

How many bars of history the Data Lab reads before a window so its
indicators are warm at the window's first bar: the chart, the dataset
export, the indicator table, indicator reliability and the quality
report's indicator step. The live bots' warm-up
(`configured_indicator_warmup_bars`) is not covered and did not change.

- **Canonical implementation:**
  `PythonDataService/app/services/indicator_warmup_policy.py::requested_indicator_warmup_lookback`
  sizes the lookback; `dataset_service.resolve_indicator_window` multiplies
  it by `INDICATOR_WARMUP_MULTIPLIER` (5) and counts the bars in scheduled
  NYSE sessions.
- **Reference:** repository-internal policy. The smoothing each indicator
  uses is read from pandas-ta v0.4.71b0 (TA-Lib absent, so the pure pandas
  paths run): `ema` is `ewm(span=N, adjust=False)` with an SMA seed; `rma`
  is `ewm(alpha=1/N, adjust=False)`; `rsi`, `atr` and `adx` smooth with
  `rma`, and `adx` smooths DX a second time; `natr` and `kc` default to an
  EMA; `kama` reads hidden `fast=2, slow=30`; `stochrsi` reads a hidden
  `rsi_length=14`; `tsi` a hidden `signal=13`; `fisher` recurses with fixed
  0.67/0.5 weights; `squeeze` reads hidden `mom_length=12, mom_smooth=6`.

## What changed

Before #2611 every request warmed up on a lookback of at least 200 bars —
1,000 bars at ×5 — whatever it asked for. Now the lookback is the
indicator's own length `N` (its largest whole-number parameter, with the
catalog defaults filling any parameter the request leaves out) times a
memory factor `m` set by how the indicator smooths. The lead-in is
`5 × m × N` bars.

| Family | Catalog indicators | m | Seed left after the lead-in |
|---|---|---:|---|
| Finite window | sma, wma, hma, alma, bbands, stoch, cci, willr, roc, mom, donchian, aroon, cmf, mfi | 1 | none — the value reads only its window |
| EMA-smoothed | ema, dema, tema, zlma, macd, kc, natr | 1 | `((N−1)/(N+1))^{5N} ≤ e^{−10} ≈ 4.5e-5` |
| Wilder-smoothed | rma, rsi, atr | 2 | `(1 − 1/N)^{10N} ≤ e^{−10}` |
| Double-Wilder | adx | 3 | `15·e^{−15} ≈ 4.6e-6` |
| Path-dependent | obv, ad, vwap, psar, supertrend | — | today's lead-in: `max(N, 200)` |
| Hidden memory | kama, stochrsi, tsi, fisher, squeeze | — | today's lead-in: `max(N, 200)` |

Derivation. An EMA keeps `(1 − α)^k` of whatever state it started the
lead-in with after `k` bars, `α = 2/(N+1)`. Wilder's RMA is an EMA with
`α = 1/N` — the EMA of span `2N − 1` — so at `5N` bars it still holds
`e^{−5} ≈ 6.7e-3` of its seed; `m = 2` gives it the EMA's own `e^{−10}`.
ADX feeds one RMA's output through a second, so the first stage's seed
reaches the output as `(k/N)·e^{−k/N}`: `m = 2` leaves `10·e^{−10}`, and
`m = 3` leaves `15·e^{−15}`. A cumulative sum never forgets, a trend latch
(PSAR, Supertrend's band) carries a state no lead-in bounds, and a hidden
parameter sets a memory the catalog cannot size — those keep today's
lead-in, and so today's values exactly.

Also kept at today's lead-in: an indicator outside the catalog, a request
that sets a parameter the catalog does not expose (a smoothing mode or a
second length can change the memory), and a request with no whole-number
length. `test_every_catalog_indicator_has_a_warmup_family` fails a new
catalog indicator until it is classified.

`m × N` exceeds today's 200-bar floor only where today's lead-in was too
short for the family: an ADX longer than 66 bars or an RMA longer than 100.
There the lead-in grows, and the values move toward the fully warmed value
by at most the residual today's lead-in left. This subsumes the indicator
table's former `adx_length × 2` widening; its `rsi_length + rsi_ma_length`
widening never reached the RSI MA, which is computed after the trim.

## Accepted tolerance

Against today's 1,000-bar lead-in, the owner's bound is about **0.01 points
on a 0–100 oscillator** and about **1e-4 of the value on a price scale**;
finite-window and today's-lead-in indicators stay at **`atol=1e-9,
rtol=0`**. This is a documented convergence tolerance, not a precision
one: a shorter lead-in trades the seed's residual for a shorter read.

Measured with 20 seeded daily paths (`numpy.random.default_rng(seed)`,
seeds 0–19; close a geometric random walk from 200 with 1.5% daily
volatility, high/low ±|N(0, 0.8%)|, volume uniform 1M–5M), 1,250 bars each:
each indicator at its catalog defaults computed over the policy's lead-in
and over 1,000 bars, compared on the last 250. Largest difference over all
paths:

| Indicator | Column | Lead-in | Max abs diff | Relative to price |
|---|---|---:|---:|---:|
| EMA-10 / EMA-20 | `ema_length10` / `ema_length20` | 50 / 100 | 8.1e-4 / 1.3e-3 | 3e-6 / 5e-6 |
| DEMA-10 | `dema_length10` | 50 | 4.2e-3 | 1e-5 |
| TEMA-10 | `tema_length10` | 50 | 1.1e-2 | 4e-5 |
| ZLMA-10 | `zlma_length10` | 50 | 1.6e-3 | 6e-6 |
| MACD 12/26/9 | `macds_12_26_9` (largest of three) | 130 | 1.7e-3 | 5e-6 |
| Keltner 20 | `kcle_20_1.5` (largest of three) | 100 | 1.4e-3 | 5e-6 |
| NATR-14 | `natr_length14` (percent of price) | 70 | 6.7e-5 | — |
| RMA-10 | `rma_length10` | 100 | 4.6e-4 | 2e-6 |
| ATR-14 | `atr_length14` | 140 | 5.4e-5 | 2e-7 |
| RSI-14 | `rsi_length14` (points) | 140 | 6.8e-3 | — |
| ADX-14 | `adx_14` / `adxr_14_2` (points) | 210 | 4.3e-4 / 4.6e-4 | — |
| +DI / −DI 14 | `dmp_14` / `dmn_14` (points) | 210 | 2.6e-5 / 3.1e-5 | — |
| Bollinger 20 | `bbl`/`bbu`/`bbb`/`bbp` | 100 | ≤ 3.4e-10 | — |
| Other finite windows | sma, wma, hma, alma, stoch, cci, willr, roc, mom, donchian, aroon, cmf, mfi | 5N | ≤ 1.7e-16 | — |
| Today's lead-in | obv, ad, psar, supertrend, kama, stochrsi, tsi, fisher, squeeze | 1,000 | 0 | — |

Bollinger's `≤ 3.4e-10` is pandas' rolling variance carrying float
accumulation through the whole series, inside `atol=1e-9`. Before this
fix (a flat `5N` for every family) RSI-14 moved by 0.76 points, ADX-14 by
3.4, ATR-14 by 1.3e-2, KAMA-10 by 0.52, TSI by 4.1e-2 and Supertrend by
5.9e-2.

## Tests

- `PythonDataService/tests/services/test_data_lab_chart_indicator_warmup.py::test_a_request_sized_lead_in_keeps_warmed_values_within_their_family_bound`
  — SMA-20 and OBV at `atol=1e-9, rtol=0`; EMA-20 at `rtol=1e-4, atol=0`;
  RSI-14 and ADX-14 at `atol=1e-2, rtol=0`; seeded daily bars through the
  shared compute-then-trim path.
- Same file: the no-params, PSAR and OBV requests warm up and note a
  shortfall; weekly and daily EMA-20 with enough history carry no note;
  EMA-20 daily sizes to 100 sessions through the policy.
- `PythonDataService/tests/services/test_indicator_warmup_policy.py` —
  family sizing, catalog defaults, the today's-lead-in fallbacks, catalog
  coverage.
