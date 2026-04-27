# Volatility Methodology — IV-RV Alignment Research Brief

**Version:** 2026-04-26 (PR [#33](https://github.com/tim1016/learn-ai/pull/33))
**Audience:** Quant engineers, reviewers, future-self.
**Scope:** All volatility computation in the learn-ai stack — realized
vol, implied vol, basis alignment, the variance risk premium signal that
drives `/edge/realized-vs-iv`, and the rate/dividend sourcing that feeds
the pricing pages.

This brief documents what was implemented, what is wired into production
paths, and what is a callable helper not yet consumed by any caller. The
distinction matters; aspirations should not be confused with shipped
behavior.

---

## 1. Problem statement

The `/edge/realized-vs-iv` route computes a **Variance Risk Premium**:

$$VRP_t = \sigma^2_{IV}(t) - \sigma^2_{RV}(t)$$

Before this work, the two operands were silently on **different
annualization bases**:

- `σ_IV` came from a Black-Scholes solver that uses `TTM = days / 365`
  (ACT/365 — the QuantLib and market-screen convention).
- `σ_RV` was annualized with `√252` (TRD/252 — the realized-vol literature
  convention used by `pandas`-shaped pipelines).

Subtracting `σ²_IV − σ²_RV` directly mixes these. The bias depends on the
holiday count inside the IV's tenor; for a 30-calendar-day window it
ranges from −0.7% (typical, ~21 trading days) to +7.3% (dense holiday
weeks, ~18 trading days). The mismatch is small enough that backtests
"look right" but corrupts the rolling z-score that drives signal
generation, and accumulates inside a 252-bar lookback.

A second class of issues coexisted:

1. The IV solver and surface code accepted hardcoded `r=0.0` and `q=0.0`
   defaults from callers.
2. Three pricing pages displayed `0.043` (pricing-lab default `0.05`) for
   the risk-free rate and `0` for dividend yield, regardless of date or
   underlying.
3. The IV30 pipeline had **no external authority** to validate against —
   only round-trip self-consistency tests of the form
   `IV(BS_price(σ)) ≈ σ`, which prove the BS↔IV inverse, not that the
   IV30 number is comparable to anyone else's IV30.
4. The regime classifier consumes IV30 features but had **no stability
   test**, leaving it vulnerable to chasing chain-perturbation noise
   (small chain re-fits producing large IV30 jumps that drive spurious
   regime transitions).

This brief documents the eight-step fix that landed in PR #33.

---

## 2. Implementation overview

The work was structured as eight self-contained, mergeable commits — one
per concern. Sequencing and dependencies:

```
                   ┌───────────────────────────────────────────────────────┐
                   │                    Step 1                             │
                   │      Basis converter (ACT/365 ↔ TRD/252)              │
                   │      app/volatility/basis.py                          │
                   └────────────┬──────────────────────┬───────────────────┘
                                │                      │
                                ▼                      ▼
              ┌───────────────────────┐    ┌──────────────────────────────┐
              │       Step 3          │    │           Step 4              │
              │   HF realized vol     │    │  VIX-style replication        │
              │   + production VRP    │    │  (CBOE 2019 whitepaper)       │
              │   wiring              │    │  + golden fixture            │
              └───────────────────────┘    └──────────────────────────────┘
                                                      ▲
                                                      │
                       ┌──────────────────────────────┴───────────────────┐
                       │                  Step 2                          │
                       │   Rate + dividend yield service                  │
                       │   FRED DGS1MO + Polygon TTM dividends            │
                       └────────────┬──────────────────┬──────────────────┘
                                    │                  │
                                    ▼                  ▼
                  ┌───────────────────────┐   ┌───────────────────────────┐
                  │       Step 8          │   │   Step 5                  │
                  │   Snapshot exposes    │   │   py_vollib parity test   │
                  │   (r, q) → 3 pricing  │   │   (576 grid cases)        │
                  │   pages auto-populate │   └───────────────────────────┘
                  └───────────────────────┘

   Step 6 (IV30 stability suite) and Step 7 (UI ETH/RTH toggle) are
   independent leaves.
```

Headline outcome: VRP on `/edge/realized-vs-iv` is now computed in
matched **TRD/252 basis** with a **high-frequency two-component RV**,
and the three pricing pages (`pricing-lab`, `options-strategy-lab`,
`strategy-builder`) no longer hardcode `r=0.043, q=0`.

---

## 3. Mathematical foundations

### 3.1 Annualization conventions and basis

Annualized volatility is integrated variance scaled by time. The
bookkeeping question is what "1 year" means:

| Convention | Year length | Used by |
|---|---|---|
| **ACT/365** | 365 calendar days | BS solver, QuantLib default, market screens |
| **TRD/252** | 252 trading days | Realized-vol literature, `pandas` pipelines |

Under the practitioner assumption that variance accrues only on trading
days (zero on weekends/holidays — the assumption that makes
√252-annualized RV well-defined), total integrated variance over a
tenor of `D` calendar days that contains `N` NYSE trading sessions
satisfies:

$$\sigma^2_{ACT/365} \cdot \frac{D}{365} = \sigma^2_{TRD/252} \cdot \frac{N}{252}$$

Rearranging gives the conversion factor:

$$\boxed{\sigma_{TRD/252} = \sigma_{ACT/365} \cdot \sqrt{\frac{D \cdot 252}{365 \cdot N}}}$$

**Implementation:** `app/volatility/basis.py:convert_iv_act365_to_trading252`
queries `pandas_market_calendars` (NYSE) for the session count over the
half-open window `[asof_date, asof_date + D)`, then applies the factor.
The window is half-open on the right: a 30-day option from 2024-03-04
covers sessions on 03-04 through 04-02, but not 04-03 (the expiry day
contributes settlement, not forward variance).

The function is **per-timestamp** (N varies with date), not a static
constant. A static `√(365/252) ≈ 1.215` would be wrong in both
directions; depending on N, the correct factor can lie either side of 1.

### 3.2 Realized variance estimators

Four daily-bar estimators remain on the chart as visualization
(`app/engine/edge/features_realtime/realized_vol.py`):

- **Close-to-close (CtC):**
  $\sigma^2 = \frac{1}{n-1} \sum (r_t - \bar{r})^2,\quad r_t = \ln(C_t/C_{t-1})$
- **Parkinson (1980):**
  $\sigma^2 = \frac{1}{4n \ln 2} \sum \left[\ln(H_t/L_t)\right]^2$.
  Drift-zero, ignores overnight gaps. ~5× more efficient than CtC under
  GBM.
- **Garman-Klass (1980):**
  $\sigma^2 = \frac{1}{n} \sum \left[0.5 (\ln H_t/L_t)^2 - (2\ln 2 - 1)(\ln C_t/O_t)^2\right]$
- **Yang-Zhang (2000):** drift-independent and gap-aware.
  $\sigma^2_{YZ} = \sigma^2_O + k \sigma^2_C + (1-k) \sigma^2_{RS}$
  with $k = 0.34/(1.34 + (n+1)/(n-1))$, where $\sigma^2_O$ is the
  overnight-return variance, $\sigma^2_C$ is the open-to-close variance,
  and $\sigma^2_{RS}$ is the Rogers-Satchell estimator.

These are rolling estimators on daily bars and are useful for the chart
overlay, but are **not** the headline RV that drives VRP.

#### High-frequency two-component estimator (Step 3 — drives VRP)

`app/engine/edge/features_realtime/hf_realized_vol.py` implements the
two-component realized variance:

$$RV^2_d = \underbrace{\sum_{i \in \text{session}(d)} r^2_i}_{\text{intraday 15-min returns}} + \underbrace{r^2_{\text{overnight}}(d)}_{\text{single overnight log-return}}$$

where the intraday returns sum over consecutive bars *within the chosen
session* (no return crosses a session boundary), and the overnight
return spans the gap from the previous session's last close to today's
first:

$$r_{\text{overnight}}(d) = \ln\frac{\text{first\_close}(d)}{\text{last\_close}(d-1)}$$

Aggregated over a window of W trading days and annualized in TRD/252:

$$\sigma^2_{TRD/252} = \frac{252}{W} \sum_{d \in W} RV^2_d$$

The **session selector** parameterizes which 15-min bars count as
"intraday" and how the overnight gap is defined:

| Session | Hours ET | Bars / day | Overnight gap |
|---|---|---|---|
| **ETH** (default) | 04:00 – 20:00 | 64 | 8 h (20:00 → 04:00 next day) |
| **RTH** | 09:30 – 16:00 | 26 | 17.5 h (16:00 → 09:30 next day) |

**Zero-volume bar policy:** bars with `volume == 0` are dropped before
returns are computed. Polygon ETH bars in the wee hours often have no
trades and stale prices; treating them as "no movement" biases RV
downward. Dropping them lets the surviving returns carry the
information.

The **forward variant** `app/engine/edge/labels_oracle/hf_forward_rv.py`
applies `.shift(-W)` so `RV[t] = vol over [t+1, t+W]` for ex-post
analytics. It is architecturally segregated under `labels_oracle/`; a
CI grep guard prevents `features_realtime/` from importing from
`labels_oracle/`, eliminating a class of look-ahead bug.

### 3.3 Implied volatility solver

`app/volatility/solver.py:implied_volatility` returns σ such that the
Black-Scholes-Merton price evaluated at `(S, K, T_years, r, q, is_call)`
equals the observed market price. **Three-tier solver chain**:

1. **Newton-Raphson primary**, vega-based, warm-started with the previous
   bar's IV. Typical convergence: 3–5 iterations.
2. **QuantLib `VanillaOption.impliedVolatility`** secondary, tolerance
   1e-8, IV bounds [0.005, 5.0], 200 iterations.
3. **scipy `brentq` fallback**, bracket [0.005, 5.0], `xtol=rtol=1e-10`.

Day-count: ACT/365 Fixed (the `T_years` argument is calendar-days/365).
Calendar: `NullCalendar` (every date is a session, since we already pass
`T_years` directly rather than letting QL construct it from a schedule).

#### 0DTE handling

For `TTM < 1 minute = 1/(365 · 24 · 60)`, QuantLib's serial-day
arithmetic rounds sub-day TTM to zero, collapsing the BS Greeks. The
solver chain detects this case and skips QL, going straight to the
closed-form Newton path with the brentq fallback. This is critical for
the 0DTE options companion service that prices contracts within
minutes of expiry.

#### Solver-parity validation (Step 5)

`tests/volatility/test_solver_parity_pyvollib.py` runs a 576-case grid
against `py_vollib.black_scholes_merton`:

| Axis | Values |
|---|---|
| Moneyness K/S | 0.7, 0.85, 1.0, 1.15, 1.3 |
| TTM (days) | 7, 30, 90, 365 |
| σ_input | 0.05, 0.20, 0.60, 1.50 |
| r | 0%, 2.5%, 7% |
| q | 0%, 1.3%, 3.0% |
| Type | call, put |

**Tolerances:**
- Price diff < 1 × 10⁻⁸ (numerical noise floor).
- IV diff < 5 × 10⁻⁵ (5 bps absolute) on contracts with vega > 0.01.

Both pass on every grid point. py_vollib is itself a thin reference
implementation; any larger divergence would indicate a real bug in our
pricer or solver, not floating-point coincidence.

### 3.4 IV30 constant-maturity (parametric ATM)

`app/engine/edge/features_realtime/iv30_constructor.py:iv30_atm_50d`
computes a constant-maturity 30-day vol from per-expiry ATM vols by
**variance-time interpolation** between two expiries straddling 30 days:

$$\sigma^2_{30}(T_{30}) = \frac{w \cdot \sigma^2_{T_1} \cdot T_1 + (1-w) \cdot \sigma^2_{T_2} \cdot T_2}{T_{30}}, \qquad w = \frac{T_2 - T_{30}}{T_2 - T_1}$$

The input `iv_by_expiry: pd.Series` maps expiry-in-days → ATM IV. The
"ATM" IV per expiry is selected at 50Δ via the closed-form delta
inversion in `delta_inversion.py`:

$$K_{50\Delta} = S \cdot \exp\left((r - q + \sigma^2/2) T\right)$$

(constant-σ approximation), refined by a fixed-point iteration on
`(K, σ(K))` pairs until convergence; brentq fallback if the iteration
diverges.

Output is on **ACT/365 basis** (the input σ values come from the IV
solver). The TRD/252 wrapper `iv30_atm_50d_trading_basis` calls the
basis converter for callers that want a VRP-ready output.

**Wiring status:** the wrapper exists but is **not** the path used by
`routers/edge.py`. The router applies `convert_iv_act365_to_trading252`
per-timestamp directly to the caller-supplied `iv_series`, since the
router doesn't itself do the per-expiry variance interpolation — that
would require access to the full chain rather than a precomputed IV30
series. The `iv30_atm_50d_trading_basis` wrapper is available for
callers that *do* have the chain (e.g., the golden-fixture build
script).

### 3.5 VIX-style variance replication (Step 4 — ground truth)

`app/volatility/vix_replication.py` implements the CBOE 2019 VIX
whitepaper formula on a chain of listed options as **independent ground
truth** for our parametric IV30:

$$\sigma^2_T = \frac{2}{T} \sum_i \frac{\Delta K_i}{K_i^2} e^{rT} Q(K_i) \;-\; \frac{1}{T} \left(\frac{F}{K_0} - 1\right)^2$$

**Components:**

- **Forward F via put-call parity** at the strike $K^* = \arg\min_K \lvert C(K) - P(K) \rvert$:
  $$F = K^* + e^{rT}(C(K^*) - P(K^*))$$
- **K₀** = highest listed strike at or below F.
- **Q(K)** is the **OTM mid** for that strike: put for $K < K_0$, call
  for $K > K_0$, average at $K = K_0$.
- **ΔK** for interior strikes: $(K_{i+1} - K_{i-1})/2$. Edge strikes use
  single-side diff $(K_1 - K_0)$ or $(K_n - K_{n-1})$.
- **Strike walk** outward from K₀ in both directions, **stopping after
  two consecutive zero-bid strikes** in each direction. This matches
  CBOE's truncation rule and is what determines the effective wing
  coverage.

Constant-maturity 30-day σ from two straddling expiries uses the same
variance-time interpolation as §3.4.

**Wiring status:** the function is exposed in `app/volatility/`, used by
`tests/volatility/test_vix_replication.py` and by the golden-fixture
build script. It is **not exposed as a live HTTP endpoint** — there is
no `/api/edge/iv30/vix-style` route. Adding one would let the UI overlay
"VIX-replicated" vs "parametric" series for visual diagnosis; this is
straightforward but out of scope for this PR.

### 3.6 Variance Risk Premium

`app/engine/edge/vrp.py:compute_vrp` is one line:

$$VRP_t = \sigma^2_{IV}(t) - \sigma^2_{RV}(t)$$

The non-trivial work is upstream — ensuring both inputs are TRD/252
basis. The signal generator `vrp_signal` z-scores VRP over a rolling
252-bar lookback:

$$z_t = \frac{VRP_t - \mu_{252}(VRP)}{\sigma_{252}(VRP)}, \qquad \text{action} = \begin{cases} +1 & z < -1 \quad \text{(LONG VOL)}\\ -1 & z > +1 \quad \text{(SHORT VOL)}\\ 0 & \text{otherwise} \end{cases}$$

**Sign convention:** VRP > 0 → options "expensive" relative to realized
→ short-vol favored.

**Wiring (`routers/edge.py:realized_vs_iv_series`):**

```
1. iv_act365 ← request.iv_series       # caller-supplied per timestamp
2. iv_trd252 ← convert_iv_act365_to_trading252(
                  iv_act365[t], asof=t, tenor_calendar_days=req.tenor_days)
3. rv_hf     ← hf_forward_rv_trd252(
                  bars, window_trading_days=21, session=req.session)
                # for 1d bars: yang_zhang(bars, window=21) — session-agnostic
4. vrp_fwd   ← compute_vrp(iv_trd252, rv_hf)
5. signal    ← vrp_signal(iv=iv_trd252.ffill(), rv=rv_hf.ffill(),
                          lookback=252, threshold=1.0)
```

The response carries both the new TRD/252-aligned values
(`iv30_trd252`, `rv_hf_trailing`, `rv_hf_forward`, `vrp_forward`,
`vrp_z`) and the legacy daily-bar 4-estimator chips (`rv_trailing`,
`rv_forward`) for visualization. Coverage metadata records
`session` and `vrp_basis = "TRD/252 (IV converted from ACT/365 via NYSE
calendar)"`.

---

## 4. Rate and dividend sourcing

### 4.1 Risk-free rate (`fred_service.py`, pre-existing)

`app/services/fred_service.py:get_risk_free_rate(dte_days, observation_date)`
fetches the four short-end Treasury rates from FRED:

| Series | Tenor (days) |
|---|---|
| `DTB4WK` | 28 |
| `DTB3` | 91 |
| `DTB6` | 182 |
| `DTB1YR` | 365 |

and **linearly interpolates** to the requested DTE. Below the shortest
tenor, the 4-week rate is used. Above the longest, the 1-year. In-memory
24-hour TTL cache. Falls back to `FALLBACK_RATE = 0.043` on any error.

### 4.2 Dividend yield (Step 2 — new)

`app/services/dividend_service.py:compute_dividend_yield(ticker, spot, polygon, observation_date)`
returns a continuous-yield proxy:

$$q \approx \frac{\sum_{i \in [T-365d, T]} \text{cash}_i}{S}$$

where the sum is over Polygon-reported cash dividend events with
`ex_dividend_date` in the trailing 365-calendar-day window ending on
`observation_date`. In-memory cache keyed by `(ticker_upper, date)` with
24-hour TTL — same pattern as `fred_service`.

Failures (Polygon error, parsing error) log a warning and **return 0.0**
(non-payer-equivalent). Negative or zero spot raises `ValueError`.

### 4.3 Facade

`app/services/rate_dividend_service.py:get_rate_and_dividend(ticker, spot, polygon, dte_days, observation_date)`
composes both into a `RateAndDividend` dataclass:

```python
@dataclass(frozen=True)
class RateAndDividend:
    rate: float
    dividend_yield: float
    source_rate: str           # "FRED"
    source_dividend: str       # "Polygon TTM"
```

Provenance fields let callers detect "we got a fallback, not real data"
without changing the float interface.

### 4.4 Cross-page propagation (Step 8)

The chain-snapshot endpoint (`POST /api/options-chain` in Python,
`getOptionsChainSnapshot` GraphQL in .NET) now includes these fields in
its response:

```json
{
  "underlying": { ... },
  "contracts": [ ... ],
  "risk_free_rate": 0.0424,
  "dividend_yield": 0.0120,
  "rate_source": "FRED",
  "dividend_source": "Polygon TTM"
}
```

Three Angular pages auto-populate their `riskFreeRate` signal from this
response on chain load: `pricing-lab` (was hardcoded `0.05`),
`options-strategy-lab` and `strategy-builder` (both were hardcoded
`0.043`). The user can still override via the UI input.

The lookup is best-effort inside `routers/snapshot.py`; a
`rate_dividend_service` failure logs a warning and leaves the new fields
`null`, so the snapshot payload is never broken.

---

## 5. Validation methodology

### 5.1 Three-layer test pyramid

| Layer | File / pattern | What it proves |
|---|---|---|
| **Unit** | `tests/volatility/test_basis.py`, `tests/edge/test_hf_realized_vol.py`, `tests/services/test_dividend_service.py` | Per-function correctness on synthetic input |
| **Integration** | `tests/edge/test_iv30_stability.py`, `tests/volatility/test_solver_parity_pyvollib.py` | Cross-function stability and external solver parity |
| **Anchor** | `tests/volatility/test_vix_replication.py::TestSpyGoldenFixture` | Frozen golden fixture, deterministic recomputation against published VIX index |

### 5.2 Golden fixture (`spy-2024-12-20-chain.parquet`)

Built once by `scripts/build_iv30_golden.py` from real Polygon data.
Contents:

- **`spy-2024-12-20-chain.parquet`**: 881 SPY option contracts with
  `expiry_days`, `strike`, `contract_type`, `close`, `ticker`. Strikes
  span well beyond ±5σ on either side of spot; expiries 21d, 28d, 35d,
  42d.
- **`spy-2024-12-20-chain.meta.json`** (attribution sidecar):

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

The golden test (`TestSpyGoldenFixture`) does **not** assert against an
external tolerance. It re-runs the replication against the parquet and
asserts the result matches the meta-stored value within 1 × 10⁻⁹ —
i.e., **deterministic recomputation**. If the math drifts, the test
fails; if the input chain drifts (it shouldn't, the parquet is
content-addressed in the fixture), the meta should be regenerated and
the new value committed with justification.

Two sanity tests bound the absolute number:

- `test_vix_replication_in_realistic_band`: σ_VIX-replicated must lie in
  [13%, 22%] (CBOE published VIX closed at 17.5% on 2024-12-20).
- `test_skew_premium_below_300bps`: the gap between VIX-replication and
  parametric ATM is < 300 bps (typical SPY OTM-put skew).

### 5.3 IV30 stability suite (Step 6)

`app/volatility/iv30_health.py:compute_iv30_health` returns
`Iv30HealthBreakdown` with three sub-scores in [0, 1]:

| Sub-score | Perturbation | Half-life |
|---|---|---|
| `resampling_score` | Drop 5% random strikes | exp(−ΔIV_bps / 10 bps) |
| `strike_grid_score` | Half-resolution grid (drop alternates) | exp(−ΔIV_bps / 20 bps) |
| `parametric_vs_replication_score` | Parametric ATM vs VIX-replication | exp(−ΔIV_bps / 50 bps) |

Composite is the unweighted mean of available sub-scores.

The unit tests in `test_iv30_stability.py` lock the synthetic-chain
behavior at: round-trip < 1 bp; resampling median < 10 bps;
half-resolution < 20 bps; intraday spot-shift jumps < 50 bps.

**Wiring status:** `compute_iv30_health` is a callable helper. **It is
not currently invoked** by the regime classifier or any production VRP
path. The "regime feature should degrade when health.score < 0.5" is a
design decision for a follow-up (the helper is the prerequisite, not
the integration). Adding the gate is mechanical: the regime classifier
should call `compute_iv30_health` per refit and weight features by
`max(0, 2·score − 1)` or similar.

---

## 6. Empirical findings

### 6.1 SPY 2024-12-20 anchor

| Metric | Value |
|---|---|
| Spot SPY close | $591.15 |
| FRED 30d rate (DGS1MO interpolated) | 4.24% |
| Polygon TTM dividend yield | 1.20% |
| Straddling expiries | 28d / 35d |
| **σ_VIX-replicated (ours, SPY chain)** | **17.31%** |
| σ_parametric ATM (50Δ, ours) | 15.58% |
| **Gap** | **172 bps** |
| **CBOE VIX index closing value** (independent reference) | **~17.5%** |
| **Disagreement vs CBOE** | **~19 bps** |

The 19-bp agreement between our SPY-chain replication and the published
CBOE VIX (which uses the SPX chain) is the strongest external
validation we have for the math. SPY and SPX chains are not identical
— SPX is European-style on the index level, SPY is American-style on
the ETF — so a few basis points of disagreement is expected, and
17.31% vs 17.5% lands inside that envelope.

### 6.2 Bias size by holiday count

Empirical conversion factors at three reference dates:

| asof | Trading days `N` in `[asof, asof+30d)` | factor² | factor `σ_TRD/σ_ACT` | Δσ relative |
|---|---|---|---|---|
| 2024-03-04 (Mon, no holidays in window) | 21 | 0.9863 | 0.9931 | **−0.7%** |
| 2024-11-25 (Mon, Thanksgiving Thu) | 21 | 0.9863 | 0.9931 | **−0.7%** |
| 2024-12-23 (Mon, Christmas/NY/Carter mourning/MLK) | 18 | 1.1507 | 1.0727 | **+7.3%** |

The sign of the bias **flips** as N drops. The 2024-12-23 window is
unusually dense — four NYSE closures in 30 calendar days (Christmas Day,
New Year's Day, the National Day of Mourning for President Carter
2025-01-09, and MLK Day) reduce N from the typical 21 to 18.

A static `√(365/252) ≈ 1.215` correction (`+21.5%`) would be wrong in
both directions. Only the **dynamic NYSE-calendar conversion** gives
the correct sign and magnitude.

### 6.3 SPY skew premium

The 172-bp gap between our VIX-style (whole-surface integration) and
parametric ATM (50Δ only) on 2024-12-20 is the well-known **VIX premium
over ATM IV**:

$$\sigma_{VIX} - \sigma_{ATM} \approx \int_{wings} (\sigma(K) - \sigma_{ATM})\, w(K)\, dK > 0$$

SPY OTM puts trade at higher implied vol than ATM calls (negative
skew is the empirical regularity). The VIX-style estimator integrates
the whole skew, so it systematically lands **above** ATM-only. This is
a feature, not a bug, and is documented in the test docstring so
future readers don't try to "fix" it. The `test_skew_premium_below_300bps`
test bounds the gap — anything wider would be unusual and warrants
investigation, but 172 bps is squarely in normal range.

---

## 7. Architecture: file map

### 7.1 Python — `PythonDataService/`

| File | Status | Purpose |
|---|---|---|
| `app/volatility/basis.py` | NEW | ACT/365 ↔ TRD/252 converter (Step 1) |
| `app/volatility/conventions.py` | EDITED | `TRADING_DAYS_PER_YEAR=252`, `CALENDAR_DAYS_PER_YEAR=365` |
| `app/volatility/vix_replication.py` | NEW | CBOE VIX whitepaper replication (Step 4) |
| `app/volatility/iv30_health.py` | NEW | Stability sub-scores + composite (Step 6) |
| `app/volatility/solver.py` | unchanged | 3-tier IV solver chain |
| `app/volatility/surface.py` | unchanged | SVI/SABR/variance-interp smile fitting |
| `app/services/dividend_service.py` | NEW | TTM dividend yield from Polygon (Step 2) |
| `app/services/rate_dividend_service.py` | NEW | (r, q) facade composing FRED + Polygon (Step 2) |
| `app/services/fred_service.py` | unchanged | DTB tenor fetch + interp |
| `app/services/bs_greeks.py` | unchanged | Closed-form BSM with continuous q |
| `app/engine/edge/features_realtime/hf_realized_vol.py` | NEW | Two-component HF RV (Step 3) |
| `app/engine/edge/features_realtime/realized_vol.py` | unchanged | Daily 4-estimator (chip overlay) |
| `app/engine/edge/features_realtime/iv30_constructor.py` | EDITED | +`iv30_atm_50d_trading_basis` wrapper |
| `app/engine/edge/labels_oracle/hf_forward_rv.py` | NEW | Forward-shifted HF RV (Step 3) |
| `app/engine/edge/labels_oracle/forward_rv.py` | unchanged | Forward-shifted daily 4-estimator |
| `app/engine/edge/vrp.py` | EDITED | Docstring locks TRD/252 input contract |
| `app/routers/edge.py` | EDITED | Wires HF RV + IV→TRD252 into VRP path |
| `app/routers/snapshot.py` | EDITED | Exposes `(r, q)` on chain-snapshot response |
| `app/models/responses.py` | EDITED | +`risk_free_rate`, +`dividend_yield`, +sources |
| `requirements-heavy.txt` | EDITED | +`pyarrow>=18.0,<20` |
| `requirements-light.txt` | EDITED | +`py_vollib==1.0.1` |
| `scripts/build_iv30_golden.py` | NEW | Polygon → golden parquet + meta sidecar |
| `tests/volatility/test_basis.py` | NEW | NYSE day count + factor + round-trip |
| `tests/volatility/test_vix_replication.py` | NEW | Replication unit + golden fixture |
| `tests/volatility/test_solver_parity_pyvollib.py` | NEW | 576-case grid sweep (Step 5) |
| `tests/edge/test_hf_realized_vol.py` | NEW | HF RV unit tests |
| `tests/edge/test_iv30_stability.py` | NEW | Stability suite (Step 6) |
| `tests/services/test_dividend_service.py` | NEW | Dividend service unit |
| `tests/services/test_rate_dividend_service.py` | NEW | Facade unit |
| `tests/fixtures/golden/iv30/spy-2024-12-20-chain.{parquet,meta.json}` | NEW | Anchor fixture |

### 7.2 .NET — `Backend/`

| File | Status | Purpose |
|---|---|---|
| `GraphQL/Query.cs` | EDITED | Passes `(r, q)` through `OptionsChainSnapshotResult` |
| `Models/DTOs/PolygonResponses/OptionsChainSnapshotResponse.cs` | EDITED | +`RiskFreeRate`, +`DividendYield`, +sources |

### 7.3 Frontend — `Frontend/src/app/`

| File | Status | Purpose |
|---|---|---|
| `components/edge/realized-vs-iv/realized-vs-iv.component.{ts,html,scss}` | EDITED | ETH/RTH chips, readout split, Polygon caveat banner |
| `components/edge/services/edge-api.service.ts` | EDITED | `Session` type, projects new response fields |
| `components/edge/services/edge-mock-data.service.ts` | EDITED | +`rvHf21d`, +`iv30Trd252` mock fields |
| `components/pricing-lab/pricing-lab.component.ts` | EDITED | Auto-populates `riskFreeRate` |
| `components/options-strategy-lab/options-strategy-lab.component.ts` | EDITED | Same |
| `components/strategy-builder/strategy-builder.component.ts` | EDITED | Same |
| `graphql/types.ts` | EDITED | +`riskFreeRate`, +`dividendYield`, +sources on `OptionsChainSnapshotResult` |
| `services/market-data.service.ts` | EDITED | Selects new fields in `getOptionsChainSnapshot` query |

---

## 8. Wired vs. available (truthful inventory)

This section is deliberately separate. "Implemented" is not the same as
"in production path."

### 8.1 Wired into a production code path

| Capability | Production path |
|---|---|
| ACT/365 → TRD/252 conversion | `routers/edge.py:realized_vs_iv_series` calls per-timestamp converter on `iv_series` before VRP |
| HF two-component RV | Same — drives `vrp_forward` / `vrp_z` for 15-min bars (YZ-21 fallback for daily bars) |
| ETH/RTH session toggle | UI chip in `realized-vs-iv.component.html` → `session` field on request → `hf_realized_vol_trd252(..., session=...)` |
| FRED + Polygon (r, q) | `routers/snapshot.py` populates response fields → 3 pricing pages auto-populate `riskFreeRate` signal |
| py_vollib parity | CI-only (test); not a runtime dependency |
| Daily 4-estimator chips | `routers/edge.py` still computes them; UI renders as multi-overlay |

### 8.2 Available as a callable helper, not yet consumed

| Capability | Why not (yet) |
|---|---|
| `iv30_atm_50d_trading_basis` wrapper | Router converts caller-supplied `iv_series` directly; the wrapper would be needed if the router itself constructed IV30 from the chain (it currently doesn't) |
| `vix_style_iv30` | No live HTTP endpoint exposes it; consumed only by tests + golden-fixture build script |
| `compute_iv30_health` | Regime classifier doesn't yet call it; the "score < 0.5 degrades feature" gate is a design decision, not active code |

### 8.3 Out of scope for this PR

| Item | Status |
|---|---|
| **IV-from-OptionIvSnapshots** historical pipeline | Not built. Router accepts `iv_series` as request input; when not supplied, `iv30` is empty and the "IV pipeline not wired" coverage banner kicks in. Separate workstream. |
| **`/api/edge/iv30/{vix-style,parametric}` endpoints** | Not built. Adding these would let the UI overlay both series for visual diagnosis. Mechanical to add. |
| **Frontend `black-scholes.ts` parity test** | The frontend pricer is a callable mirror of `bs_greeks.py`. Under the lifted "single source of truth" rule (no fixed layer assignment), this is *permitted* as a parity-tested mirror — but the parity test does not yet exist. Owed. |
| **Surface-fitting endpoints** (`routers/volatility.py`) | Built but no UI consumes them. Possibly a future "Vol Surface Lab" page. |

---

## 9. Known caveats

### 9.1 Polygon Starter spread synthesis

Our subscription tier (Polygon Starter) provides daily option aggregates
with OHLCV but **no historical bid/ask**. The golden-fixture builder
synthesizes spreads as `bid = max(0, close − h)`, `ask = close + h`,
with `h = max($0.05, 0.005 · close)`. Contracts with `close < $0.05`
are treated as zero-bid (which then triggers CBOE's truncation rule).

This is disclosed in the UI via the `banner-data-source` coverage banner
on `/edge/realized-vs-iv` and in the meta sidecar of every fixture.
**Live (current) chain data uses real bid/ask** from the snapshot
endpoint and is unaffected by this caveat.

A Polygon Options plan upgrade would eliminate the synthesis. The
fixture-build script is structured so swapping in real bid/ask is a
local change (replace `_half_spread(close)` with the real OPRA quote).

### 9.2 Dividend yield accuracy

Trailing-12-month dividends ÷ spot is a standard *continuous-dividend
proxy*, not the actual continuous yield. For dividend-payers like SPY
(quarterly cash dividends) this works because:

- the BS solver only consumes `q` to discount the forward, and
- TTM/spot is the same scale as the time-weighted average forward
  discount over a 30-day option's life.

It will be inaccurate for:

- Underlyings with irregular special dividends in the trailing window
  (one-off events distort the proxy).
- Dividend-paying underlyings on/around an ex-date (the proxy doesn't
  shift on ex-date; the option's forward does).

Neither is a blocker for the SPY/QQQ/IWM/DIA universe in the edge
feature roadmap, but options on individual stocks may need a more
careful treatment.

### 9.3 N(t) calendar query cost

`nyse_trading_days_in_window` calls `pandas_market_calendars.NYSE.schedule`
once per timestamp during the IV→TRD252 conversion loop. For a series
of K timestamps, that's K schedule queries. `mcal` caches its calendar
internally per-process, so the marginal cost is fast (sub-millisecond
per query on cached calendars), but it's not free. For very long
series this could be optimized into a single batch query — currently
not a bottleneck.

### 9.4 The 172-bp skew premium is not "wrong"

To repeat from §6.3 in case anyone is tempted: VIX-style and parametric
ATM are **different constructs**. They will *always* disagree by the
amount of skew priced into the chain. The disagreement is informative —
it's a cheap measure of skew that you can read off the readout panel —
not a sign of a bug.

---

## 10. References

### Primary sources

- **CBOE Volatility Index (VIX) Whitepaper** (2019). The replication
  formula in §3.5 is from this document.
- **Hull, J. (10e).** *Options, Futures, and Other Derivatives*. The BS
  pricing and Greeks (`bs_greeks.py`) follow Hull's notation.
- **Parkinson, M. (1980).** "The Extreme Value Method for Estimating the
  Variance of the Rate of Return." *Journal of Business* 53(1).
- **Garman, M. B., Klass, M. J. (1980).** "On the Estimation of Security
  Price Volatilities from Historical Data." *Journal of Business* 53(1).
- **Yang, D., Zhang, Q. (2000).** "Drift-Independent Volatility
  Estimation Based on High, Low, Open, and Close Prices." *Journal of
  Business* 73(3).
- **Andersen, T. G., Bollerslev, T. (1998).** "Answering the Skeptics:
  Yes, Standard Volatility Models Do Provide Accurate Forecasts."
  *International Economic Review* 39(4) — the realized-variance estimator
  in §3.2.

### In-repo references

- [`docs/references/iv-rv-basis-alignment.md`](../references/iv-rv-basis-alignment.md)
  — Step 1 deep-dive on the basis math.
- [`docs/architecture/edge-feature-design.md`](./edge-feature-design.md)
  — broader edge-route design (this brief is a sub-spec for the
  volatility layer).
- [`docs/math-sources-of-truth.md`](../math-sources-of-truth.md)
  — registry of canonical math implementations and parity-test status.
- [`PythonDataService/tests/fixtures/golden/iv30/spy-2024-12-20-chain.meta.json`](../../PythonDataService/tests/fixtures/golden/iv30/spy-2024-12-20-chain.meta.json)
  — anchor-fixture attribution.

---

## Appendix A — Concrete numerical examples

### A.1 Worked basis-conversion example

`σ_ACT365 = 0.18`, asof `2024-03-04`, tenor `30 days`, NYSE schedule
returns `N = 21`:

$$\text{factor}^2 = \frac{30 \cdot 252}{365 \cdot 21} = \frac{7560}{7665} = 0.98630$$

$$\text{factor} = \sqrt{0.98630} = 0.99313$$

$$\sigma_{TRD/252} = 0.18 \cdot 0.99313 = 0.17876$$

VRP impact: if RV (TRD/252) is also 0.18:

- **Wrong** (mixed-basis): `VRP = 0.18² − 0.18² = 0` (no signal).
- **Right** (matched-basis): `VRP = 0.17876² − 0.18² = −0.000446`
  (slightly negative — RV is 12 bps higher than IV in matched basis,
  weak long-vol).

For a holiday-dense window with N=18:

$$\text{factor} = \sqrt{(30 \cdot 252) / (365 \cdot 18)} = 1.07273$$

$$\sigma_{TRD/252} = 0.18 \cdot 1.07273 = 0.19309$$

`VRP = 0.19309² − 0.18² = +0.00488` — meaningfully positive (short-vol
favored).

The same input vols, same RV, same tenor — but different VRP signs
depending on whether we converted basis or not, and depending on the
date. This is precisely the bug the converter eliminates.

### A.2 Worked HF realized-vol example

Synthetic 100-day GBM at σ = 0.20, ETH session (64 bars/day):

$$\Delta t_{\text{intra}} = \frac{1}{64 \cdot 252} = 6.20 \times 10^{-5} \text{ trading-years}$$

$$\text{Var}(r_{\text{intra},i}) \approx \sigma^2 \Delta t = 2.48 \times 10^{-6}$$

Per trading-day intraday-RV (sum of 64 squared returns plus the
overnight squared return):

$$E[RV^2_d] \approx 64 \cdot 2.48 \times 10^{-6} + 2.48 \times 10^{-6} = 1.61 \times 10^{-4}$$

(overnight contributes one bar's worth of variance under our same-Δt
synthetic; the real bias from a longer overnight gap is folded in via
the realized return rather than this simple expectation.)

Window of W=21 days:

$$\Sigma_{d \in 21} RV^2_d \approx 21 \cdot 1.61 \times 10^{-4} = 3.38 \times 10^{-3}$$

Annualized:

$$\sigma^2 \approx 3.38 \times 10^{-3} \cdot \frac{252}{21} = 0.04057$$

$$\sigma \approx 0.2014$$

Recovers the input σ = 0.20 within ~0.7% sample-variance error, which
is what `test_recovers_known_sigma_within_5pct` asserts.

### A.3 Worked VIX-replication example (SPY 2024-12-20)

From the meta sidecar: spot $591.15, r = 4.24%, q = 1.20%, T₁ = 28d
(0.0767 years), T₂ = 35d (0.0959 years).

**Per-expiry σ²·T** (computed by the replication formula on each
chain):

- σ²(T₁) · T₁ ≈ (0.1731)² · 0.0822 ≈ 0.002463 (variance × years)
- σ²(T₂) · T₂ ≈ similar order of magnitude

(Exact per-expiry numbers are in the test logs; the sidecar stores only
the interpolated 30d value.)

**Variance-time interpolation to 30d (0.0822 years):**

$$w = \frac{T_2 - T_{30}}{T_2 - T_1} = \frac{35 - 30}{35 - 28} = 0.714$$

$$\sigma^2_{30} \cdot T_{30} = w \cdot \sigma^2_{T_1} T_1 + (1-w) \cdot \sigma^2_{T_2} T_2$$

$$\sigma_{30} = \sqrt{\frac{w \cdot \sigma^2_{T_1} T_1 + (1-w) \cdot \sigma^2_{T_2} T_2}{T_{30}}} = 0.17305$$

The test asserts this matches the meta-stored value within 1 × 10⁻⁹.

---

## Appendix B — Glossary

- **ATM, OTM, ITM** — at/out-of/in-the-money, relative to spot or forward.
- **ACT/365, TRD/252** — annualization conventions; see §3.1.
- **Delta (Δ)** — option-price sensitivity to spot. 50Δ = ATM (approximately).
- **ETH, RTH** — extended trading hours (04:00–20:00 ET) vs regular
  trading hours (09:30–16:00 ET).
- **IV** — implied volatility. The σ that makes the BS price equal the
  observed market price.
- **IV30** — implied vol at constant 30-day maturity. Constructed by
  variance-time interpolation between two listed expiries.
- **N(t)** — count of NYSE trading sessions in `[asof_date,
  asof_date + tenor)`. The dynamic input to the basis converter.
- **Q(K)** — OTM mid price at strike K (used in VIX replication; put
  below K₀, call above K₀).
- **RV** — realized volatility, computed from historical price returns.
- **Skew premium** — the gap between VIX-style (whole-surface
  integration) and parametric ATM (50Δ only) IV30. Positive for SPY due
  to OTM-put richness.
- **TTM, T_years** — time to maturity, expressed in years.
- **VRP** — variance risk premium, σ²_IV − σ²_RV.
- **YZ** — Yang-Zhang (2000) realized-vol estimator.
