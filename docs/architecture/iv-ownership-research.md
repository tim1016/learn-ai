# IV-Ownership Research Document

> **Status:** Reference note. The volatility decisions and their whys are in
> [ADR 0071](adrs/0071-volatility-ownership-and-iv-recording.md). Each
> formula is stated in the provenance block of the module that implements it.
> This note keeps only what neither holds: the CBOE replication's known
> disagreement (§4.5), the dividend-yield proxy's scope (§4.9.1), the
> tolerances and validation receipts (§6), the primary sources (§11) and the
> worked examples (Appendix A). Section numbers are unchanged because code and
> tests cite them.

---

## Table of contents

- [4. Mathematical foundations](#4-mathematical-foundations)
- [6. Tolerances and validation](#6-tolerances-and-validation)
- [11. References](#11-references)
- [12. Appendix A — worked numerical examples](#12-appendix-a--worked-numerical-examples)

---

## 4. Mathematical foundations

The formulas behind §4.1–§4.10 are stated in the provenance blocks of the
modules that implement them, under `PythonDataService/app/volatility/`,
`app/engine/edge/` and `app/services/`. The basis converter's derivation and
its open overnight-noise question are in
`docs/references/iv-rv-basis-alignment.md`. Only the outside facts below stay
here.

### 4.5 VIX-style IV30 replication

**Source:** [CBOE VIX Methodology white paper, 2019](https://res-certification.cboe.com/resources/vix/VIX_Methodology.pdf).

The formula and its components are stated in
`app/volatility/vix_replication.py`.

**External validation.** SPY 2024-12-20: ours 17.31% vs CBOE published VIX
~17.5%, **disagreement ~19 bps**. SPY and SPX chains are not identical — SPX
is European-style on the index level, SPY is American-style on the ETF — so a
few basis points of disagreement is expected.

**Known disagreement caveat (called out explicitly).** We replicate the **CBOE
VIX formula** but not the operational **dissemination pipeline** (baseline
rules, republishing logic, noise filtering on individual quotes). This is the
structural reason day-level disagreement against the published index can
persist even with correct formula implementation. The ~19 bps is consistent
with formula-correct + dissemination-mismatch; it should not be interpreted as
a formula bug.

### 4.9 Risk-free rate and dividend yield

The rate and dividend inputs are computed in `app/services/fred_service.py`,
`app/services/dividend_service.py` and `app/services/rate_dividend_service.py`.

#### 4.9.1 Dividend-yield accuracy caveats

Trailing-12-month dividends ÷ spot is a standard *continuous-dividend proxy*,
not the actual continuous yield. For SPY (quarterly cash dividends) it works
because:

- the BS solver only consumes `q` to discount the forward, and
- TTM/spot is the same scale as the time-weighted average forward discount over
  a 30-day option's life.

It will be inaccurate for:

- Underlyings with irregular special dividends in the trailing window (one-off
  events distort the proxy).
- Dividend-paying underlyings on/around an ex-date (the proxy doesn't shift on
  ex-date; the option's forward does).

Neither is a blocker for the SPY/QQQ/IWM/DIA/EFA universe; **the proxy must
not be reused for single-name equities without a discrete-dividend
present-value adjustment** — single-name dividends are large relative to
spot and clustered around ex-dates, so the smooth TTM proxy mis-prices
options on those names by the size of the adjacent dividend.

---

## 6. Tolerances and validation

### 6.1 Tolerance table

| Construct | Test | Tolerance | Sample size |
|---|---|---|---|
| Black–Scholes price | py_vollib parity | `atol = 1e-8` | 576 grid cases |
| IV solver | py_vollib parity (vega>0.01) | `atol = 5e-5` (5 bps) | 576 grid cases |
| Frontend BS parity | py_vollib parity | `atol = 1e-4` (CDF approximation floor) | 360 grid cases (single looped test) |
| VIX-style replication | golden fixture, deterministic recomputation | `atol = 1e-9` | 1 fixture (SPY 2024-12-20, 881 contracts) |
| VIX-style replication | external — vs CBOE published VIX | ~19 bps (informational, not asserted) | 1 day |
| Basis converter | per-timestamp NYSE calendar | per-day deterministic factor | n/a (closed-form) |
| Confidence gate | hard floor | `confidence < 0.1 ⇒ action = 0` | n/a |

### 6.2 Three-layer test pyramid

| Layer | File / pattern | What it proves |
|---|---|---|
| **Unit** | `tests/volatility/test_basis.py`, `tests/edge/test_hf_realized_vol.py`, `tests/services/test_dividend_service.py` | Per-function correctness on synthetic input |
| **Integration** | `tests/edge/test_iv30_stability.py`, `tests/volatility/test_solver_parity_pyvollib.py` | Cross-function stability and external solver parity |
| **Anchor** | `tests/volatility/test_vix_replication.py::TestSpyGoldenFixture` | Frozen golden fixture, deterministic recomputation against published VIX index |

### 6.3 Golden fixture

`tests/fixtures/golden/iv30/spy-2024-12-20-chain.{parquet,meta.json}` — 881
SPY option contracts. Built once by `scripts/build_iv30_golden.py` from real
Polygon data.

| Field | Value |
|---|---|
| `as_of_date` | 2024-12-20 |
| `spot` | $591.15 |
| `rate` | 0.0424 (FRED) |
| `dividend` | 0.01195 (Polygon TTM) |
| `straddle.below_30d` | 28 |
| `straddle.above_30d` | 35 |
| `vix_style_iv30_act365` | 0.17305 |
| `parametric_iv30` | 0.15584 |
| `iv30_diff_bps` | 172.18 |
| `half_spread_policy` | `max($0.05, 0.5%·close)`; zero-bid below $0.05 |

The golden test re-runs the replication against the parquet and asserts the
result matches the meta-stored value within `1e-9` (deterministic
recomputation). Two sanity tests bound the absolute number:

- σ_VIX-replicated must lie in `[13%, 22%]` (CBOE published VIX closed at
  17.5% on 2024-12-20).
- The gap between VIX-replication and parametric ATM is `< 300 bps` (typical
  SPY OTM-put skew).

### 6.4 Empirical bias-by-holiday-count

| asof | Trading days `N` in `[asof, asof+30d)` | factor² | factor `σ_TRD/σ_ACT` | Δσ relative |
|---|---|---|---|---|
| 2024-03-04 (Mon, no holidays in window) | 21 | 0.9863 | 0.9931 | **−0.7%** |
| 2024-11-25 (Mon, Thanksgiving Thu) | 21 | 0.9863 | 0.9931 | **−0.7%** |
| 2024-12-23 (Mon, Christmas/NY/Carter mourning/MLK) | 18 | 1.1507 | 1.0727 | **+7.3%** |

The sign of the bias **flips** as N drops. A static `√(365/252) ≈ 1.215`
correction would be wrong in both directions.

### 6.5 SPY skew premium (informative, not a bug)

The **172-bp gap** between VIX-style (whole-surface integration) and parametric
ATM (50Δ only) on 2024-12-20 is the well-known **VIX premium over ATM IV**:

$$\sigma_{VIX} - \sigma_{ATM} \approx \int_{wings} (\sigma(K) - \sigma_{ATM})\, w(K)\, dK > 0$$

SPY OTM puts trade at higher implied vol than ATM calls (negative skew is the
empirical regularity). The VIX-style estimator integrates the whole skew and
systematically lands **above** ATM-only. This is a feature, not a bug, and is
documented in the test docstring so future readers don't try to "fix" it. The
`test_skew_premium_below_300bps` test bounds the gap.

---

## 11. References

### Primary sources

- **CBOE VIX Methodology white paper (2019).** The replication formula in
  [§4.5](#45-vix-style-iv30-replication) is from this document.
  https://res-certification.cboe.com/resources/vix/VIX_Methodology.pdf
- **Hull, J. (10e).** *Options, Futures, and Other Derivatives.* The BS
  pricing and Greeks (`bs_greeks.py`) follow Hull's notation.
- **Parkinson, M. (1980).** "The Extreme Value Method for Estimating the
  Variance of the Rate of Return." *Journal of Business* 53(1).
- **Garman, M. B., Klass, M. J. (1980).** "On the Estimation of Security
  Price Volatilities from Historical Data." *Journal of Business* 53(1).
- **Yang, D., Zhang, Q. (2000).** "Drift-Independent Volatility Estimation
  Based on High, Low, Open, and Close Prices." *Journal of Business* 73(3).
- **Andersen, T. G., Bollerslev, T. (1998).** "Answering the Skeptics: Yes,
  Standard Volatility Models Do Provide Accurate Forecasts."
  *International Economic Review* 39(4).
- **NBER w17422 — Andersen, Bollerslev, Diebold, Vega.** Used as the
  reference for the overnight-variance share discussed in
  `docs/references/iv-rv-basis-alignment.md`.
  https://www.nber.org/system/files/working_papers/w17422/w17422.pdf

---

## 12. Appendix A — worked numerical examples

### A.1 Basis-conversion VRP

`σ_ACT365 = 0.18`, asof `2024-03-04`, tenor `30 days`, `N = 21`:

$$\text{factor}^2 = \frac{30 \cdot 252}{365 \cdot 21} = 0.98630, \quad \sigma_{TRD/252} = 0.17876$$

VRP impact, if RV (TRD/252) is also 0.18:

- **Wrong** (mixed-basis): `VRP = 0.18² − 0.18² = 0` (no signal).
- **Right** (matched-basis): `VRP = 0.17876² − 0.18² = −0.000446` (slightly
  negative — RV is 12 bps higher than IV in matched basis, weak long-vol).

For a holiday-dense window with `N = 18`:

$$\sigma_{TRD/252} = 0.18 \cdot 1.07273 = 0.19309$$

`VRP = 0.19309² − 0.18² = +0.00488` — meaningfully positive (short-vol
favoured).

Same input vols, same RV, same tenor — but different VRP signs depending on
whether the basis was converted and the date. This is precisely the bug the
converter eliminates.

### A.2 HF realised-vol expectation under GBM

Synthetic 100-day GBM at σ = 0.20, ETH session (64 bars/day):

$$\Delta t_{\text{intra}} = \frac{1}{64 \cdot 252} = 6.20 \times 10^{-5} \text{ trading-years}$$

$$\text{Var}(r_{\text{intra},i}) \approx \sigma^2 \Delta t = 2.48 \times 10^{-6}$$

Per trading-day intraday-RV:

$$E[RV^2_d] \approx 64 \cdot 2.48 \times 10^{-6} + 2.48 \times 10^{-6} = 1.61 \times 10^{-4}$$
