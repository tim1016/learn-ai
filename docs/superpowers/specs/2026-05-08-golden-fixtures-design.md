# Golden Fixtures System — Design Spec

**Date:** 2026-05-08  
**Status:** Awaiting user approval  
**Scope:** New `/golden-fixtures` frontend route + fixture generation pipeline + docs  

---

## 1. Purpose

Every mathematical calculation in learn-ai currently exists without an external numerical receipt. The `math-sources-of-truth.md` registry has ~34 concepts, the majority marked `pending-fixture`. This system closes that gap by:

1. Pulling real market data from two authoritative MCP sources
2. Running each canonical implementation against that data to produce reference outputs
3. Storing the (input, reference output, citation) triple as a pinned fixture
4. Displaying a live validation page in the frontend where static fixture data and freshly-calculated model output are compared cell-by-cell — earning a gold star when they match, showing a signed delta when they don't

This is standard practice in sell-side model validation (FRTB, SR 11-7 model risk). We are building it as a first-class feature, not an afterthought.

---

## 2. Two Data Sources

### Source 1 — Massive Market Data MCP (Polygon.io)
- **Role:** Primary input data for all fixtures
- **What it provides:** Historical OHLCV bars, options chain snapshots (mid-price, reported IV, Delta, Gamma, Theta, Vega), tickers, expirations
- **Fixture anchor:** SPY options chain snapshot at market close 2024-01-02 16:00 ET (clean date: no corporate actions, VIX ~13, normal vol regime)
- **Used for:** Options pricing inputs, realized vol inputs, indicator inputs, engine stat inputs
- **Cited on every fixture detail page**

### Source 2 — BigData MCP (Methodology Authority)
- **Role:** Academic and industry citation source — confirms our formula implementations match published methodology
- **What it provides:** Research reports, CBOE white papers, broker methodology notes, academic papers via the `research` and `filings` categories
- **Used for:** Retrieving CBOE VIX white paper (IV methodology), broker SVI/SABR notes (surface fitting), Hull chapter references, Lopez de Prado IC methodology
- **Cited on every fixture detail page alongside the Polygon source**

The two sources serve different purposes: Polygon gives us numbers, BigData gives us the authority that says those numbers are being computed correctly.

---

## 3. Fixture Categories and IDs

34 fixtures across 7 categories. Primary instrument: **SPY**. Fixture date anchor: **2024-01-02**.

### Category 1 — Options Pricing & Greeks (7 fixtures)

| ID | Calculation | Formula | Canonical File | Tolerance |
|---|---|---|---|---|
| BS-001 | Black-Scholes call price | C = S·N(d₁) − K·e^{−rT}·N(d₂) | `bs_greeks.py::bs_european_price` | atol=1e-9 |
| BS-002 | Black-Scholes put price | P = K·e^{−rT}·N(−d₂) − S·N(−d₁) | `bs_greeks.py::bs_european_price` | atol=1e-9 |
| BS-003 | Delta (Δ = ∂C/∂S) | N(d₁) for calls, N(d₁)−1 for puts | `bs_greeks.py::black_scholes_greeks` | atol=1e-9 |
| BS-004 | Gamma (Γ = ∂²C/∂S²) | φ(d₁) / (S·σ·√T) | `bs_greeks.py::black_scholes_greeks` | atol=1e-9 |
| BS-005 | Theta (Θ = ∂C/∂t) | −S·φ(d₁)·σ/(2√T) − r·K·e^{−rT}·N(d₂) | `bs_greeks.py::black_scholes_greeks` | atol=1e-9 |
| BS-006 | Vega (ν = ∂C/∂σ) | S·φ(d₁)·√T | `bs_greeks.py::black_scholes_greeks` | atol=1e-9 |
| BS-007 | Rho (ρ = ∂C/∂r) | K·T·e^{−rT}·N(d₂) for calls | `bs_greeks.py::black_scholes_greeks` | atol=1e-9 |

**Input grid:** 5 strikes (ATM−2 to ATM+2, 5pt spacing) × 4 expiries (15d, 30d, 60d, 90d) × 2 types = 40 cells per fixture  
**Cross-validation:** Our bs_greeks.py output vs QuantLib analytic_bs engine (already parity-pinned in `test_bs_cross_engine_parity.py`). Both run against Polygon snapshot data.  
**Reference:** Hull (10e) §15.8 (pricing), §19 (Greeks)

---

### Category 2 — Implied Volatility (4 fixtures)

| ID | Calculation | Method | Canonical File | Tolerance |
|---|---|---|---|---|
| IV-001 | IV solver — per contract | Newton-Raphson + Brent fallback, Brenner-Subrahmanyam seed | `volatility/solver.py::implied_volatility` | atol=1e-6 |
| IV-002 | IV surface — SVI parametric fit | w(k)=a+b{ρ(k−m)+√((k−m)²+σ²)}, Gatheral 2014 | `volatility/fitting.py` | atol=1e-6, rtol=1e-6 |
| IV-003 | IV 30-day constant-maturity | Variance-time interpolation between bracketing expiries | `volatility/iv30_health.py` + `research/options/iv_builder.py` | atol=1e-6 |
| IV-004 | IV rank (rolling 60-day) | (IV_current − IV_min) / (IV_max − IV_min) | `research/features/options_features.py::compute_iv_rank` | atol=1e-9 |

**Tolerance justification for IV-001/002/003:** Root-finding and interpolation accumulate floating-point error beyond closed-form precision. 1e-6 is the tightest achievable at double precision for iterative methods; documented per `numerical-rigor.md`.  
**Cross-validation:** IV-001 additionally compared against Polygon's reported `implied_volatility` field per contract — expected divergence <0.5 vol point (Polygon uses a different solver; divergence is documented, not a bug).

---

### Category 3 — Realized Volatility (4 fixtures)

| ID | Calculation | Formula | Canonical File | Tolerance |
|---|---|---|---|---|
| RV-001 | Close-to-close σ (annualized) | σ̂=√(252/n · Σrᵢ²), rᵢ=ln(Sᵢ/Sᵢ₋₁) | `engine/edge/features_realtime/realized_vol.py` | atol=1e-9 |
| RV-002 | HF realized vol — ABDL estimator | RV=Σrᵢ² over intraday returns, 5-min bars | `engine/edge/features_realtime/hf_realized_vol.py` | atol=1e-8 |
| RV-003 | IV-RV basis | Basis = IV30 − RV30, annualized vol points | `volatility/basis.py` | atol=1e-6 |
| RV-004 | VIX replication (model-free variance) | E[V]=2/T · Σ(ΔK/K²)·e^{rT}·Q(K) − [F/K₀−1]² | `volatility/vix_replication.py` | atol=1e-4 |

**Input for RV-001/002:** SPY 15-min bars, 2024-01-02 to 2024-01-31 (21 trading days)  
**Input for RV-004:** Full SPY options strip for 2024-01-02 (all OTM strikes, near+next expiry)  
**RV-004 tolerance justification:** Model-free variance replication uses a discrete sum over a finite strike grid; discretization error is O(ΔK²). 1e-4 is the tightest achievable with Polygon's available strikes; documented.  
**Reference:** Andersen-Bollerslev-Diebold-Labys (2003) for RV-002; Demeterfi-Derman-Kamal-Zou (1999) for RV-004; CBOE white paper for VIX methodology cross-check via BigData

---

### Category 4 — Engine Statistics (6 fixtures)

| ID | Calculation | Formula | Canonical File | Tolerance |
|---|---|---|---|---|
| ENG-001 | Sharpe ratio (annualized) | SR=(R̄−Rƒ)/σ_R · √252 | `engine/results/statistics.py` | atol=1e-9 |
| ENG-002 | Max drawdown | MDD=max(peak−trough)/peak over equity curve | `engine/results/statistics.py` | atol=1e-9 |
| ENG-003 | Sortino ratio | Sortino=(R̄−Rƒ)/σ_downside · √252 | `engine/results/statistics.py` | atol=1e-9 |
| ENG-004 | Calmar ratio | Calmar=CAGR/|MDD| | `engine/results/statistics.py` | atol=1e-9 |
| ENG-005 | CAGR | (terminal/initial)^(252/n)−1 | `engine/results/statistics.py` | atol=1e-9 |
| ENG-006 | Win rate | winning_trades / total_trades | `engine/results/statistics.py` | atol=1e-9 (exact ratio) |

**Input:** SPY EMA Crossover strategy run over 2024-01-02 to 2024-03-28 (the existing parity-pinned fixture `test_ema_acceptance.py` is the input source — reuse its trade log)  
**Cross-validation:** ENG-001/002 additionally computed by `SnapshotService.cs` (legacy .NET path, still live) — expected to agree; any divergence triggers F-0011 resolution  
**Reference:** Sharpe (1994) JPM; Bacon (2e) §8.2 for MDD

---

### Category 5 — Research Primitives (4 fixtures)

| ID | Calculation | Formula | Canonical File | Tolerance |
|---|---|---|---|---|
| RP-001 | Information coefficient (IC) | IC = Spearman rank correlation(forecast, realized) | `research/validation/ic.py` | atol=1e-9 |
| RP-002 | Quantile monotonicity | Returns by decile; monotonicity score | `research/validation/quantile.py` | atol=1e-9 |
| RP-003 | Block bootstrap p-value | Phipson-Smyth (2010): p=(1+#{null≥obs})/(N+1) | `research/validation/robustness.py` | atol=1e-9 (exact formula) |
| RP-004 | Signal z-score standardization | z=(x−μ)/σ, population params | `research/signal/standardize.py` | atol=1e-9 |

**Input:** Synthetic deterministic sequence (seeded RNG, reproducible) — real market data not needed for pure statistical primitives; the fixture pins (input array, expected output) at seed=42  
**Reference:** Lopez de Prado (2018) §8 for IC; Phipson & Smyth (2010) for RP-003; Conover (1999) for RP-002

---

### Category 6 — Technical Indicators (5 fixtures)

| ID | Calculation | Method | Canonical File | Tolerance |
|---|---|---|---|---|
| IND-001 | EMA (period=10) | Wilder smoothing: EMAₜ = α·Pₜ + (1−α)·EMAₜ₋₁ | `engine/indicators/ema.py` | atol=1e-9 |
| IND-002 | SMA (period=20) | SMAₜ = (1/n)·Σᵢ Pᵢ | `engine/indicators/sma.py` | atol=1e-9 |
| IND-003 | RSI Wilders (period=14) | RS=avg_gain/avg_loss (Wilder smoothing); RSI=100−100/(1+RS) | `engine/indicators/rsi.py` | atol=1e-9 |
| IND-004 | MACD (12,26,9) | MACD=EMA12−EMA26; Signal=EMA9(MACD) | `services/ta_service.py` (pandas-ta) | atol=1e-6 |
| IND-005 | Bollinger Bands (20,2) | Upper/Lower = SMA20 ± 2·σ_20 | `services/ta_service.py` (pandas-ta) | atol=1e-6 |

**Input:** SPY 15-min bars, 2024-01-02 to 2024-03-28, from Polygon via Massive Market Data MCP  
**Cross-validation for IND-001/002/003:** LEAN reference outputs from `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/` (already used in `test_indicator_parity.py`)  
**Tolerance justification for IND-004/005:** pandas-ta (external) uses float32 internally for some operations; 1e-6 is the tightest achievable  
**Reference:** LEAN vendored source; Wilder (1978) for RSI

---

### Category 7 — Indicator Reliability (4 fixtures)

| ID | Calculation | Method | Canonical File | Tolerance |
|---|---|---|---|---|
| REL-001 | Win-rate stability (rolling IC) | Rolling 20-bar IC with min_periods=10 | `research/indicator_reliability.py` | atol=1e-9 |
| REL-002 | Regime-conditioned hit rate | Hit rate per HMM regime label | `research/indicator_reliability.py` + `engine/edge/regime_clustering.py` | atol=1e-8 — **risk: regime_clustering.py only partially tested (EM convergence + posterior shape); fixture deferred to Phase 2 pending regime canonical verification** |
| REL-003 | Calibration curve | Binned forecast vs realized return; Brier-like score | `research/indicator_reliability.py` | atol=1e-9 |
| REL-004 | IC time series (EMA-10 on SPY) | IC per bar over 60-day rolling window | `research/indicator_reliability.py` | atol=1e-9 |

**Input:** SPY 15-min bars, 2024-01-02 to 2024-03-28; EMA-10 signal  
**Reference:** `docs/indicator-reliability-methodology.md` (existing authority doc); `Frontend/src/assets/docs/indicator-reliability-methodology.md`

---

## 4. Fixture File Layout

```
PythonDataService/tests/fixtures/golden/
  options-pricing/
    BS-001-black-scholes-call/
      input.parquet          # (strike, expiry, S, K, r, T, sigma) per row
      output.parquet         # (call_price) — reference output from canonical
      attribution.md         # source, date, command to regenerate, SHA-256
    BS-003-delta/
      ...
  implied-volatility/
    IV-001-newton-brent/
      input.parquet          # (strike, expiry, S, K, r, T, market_price)
      output.parquet         # (iv) — reference IV per contract
      attribution.md
  realized-volatility/
    RV-001-close-to-close/
      input.parquet          # (timestamp_ms, close) — SPY daily bars
      output.parquet         # (realized_vol_30d) per date
      attribution.md
  engine-statistics/
    ENG-001-sharpe/
      input.parquet          # equity curve from EMA acceptance test
      output.json            # { sharpe: 1.234, max_dd: 0.045, ... }
      attribution.md
  research-primitives/
    RP-001-ic/
      input.parquet          # (forecast, realized) — seeded synthetic
      output.json            # { ic: 0.142 }
      attribution.md
  indicators/
    IND-001-ema-10/
      input.parquet          # SPY 15-min closes
      output.parquet         # ema values per bar
      attribution.md
  reliability/
    REL-001-win-rate-stability/
      ...
```

Each `attribution.md` contains:
- Reference source (Polygon endpoint + snapshot datetime, or LEAN commit SHA)
- Generation script/command
- SHA-256 of `output.parquet`/`output.json`
- BigData document cited (title, section, retrieval date)
- Any tolerance exceptions with justification

---

## 5. Fixture Generation Pipeline (Python)

A new script `PythonDataService/scripts/generate_fixtures.py`:

```
usage: generate_fixtures.py [--category all|options-pricing|implied-volatility|...]
                             [--fixture BS-001|IV-001|...]
                             [--dry-run]
```

Steps per fixture:
1. Fetch input data from Polygon via Massive Market Data MCP (or load from existing Parquet if already fetched)
2. Run canonical implementation with that input
3. Write `input.parquet` + `output.parquet/json` + `attribution.md`
4. Print SHA-256 of output file

**Regeneration policy:** Output files are never overwritten silently. If the fixture already exists, the script requires `--force` and logs the old vs new SHA-256 with a justification prompt. This enforces the golden-fixture immutability rule from `numerical-rigor.md`.

---

## 6. Validation Test Structure (pytest)

One test file per category under `PythonDataService/tests/fixtures/`:

```
tests/fixtures/
  test_options_pricing_fixtures.py    # BS-001 through BS-007
  test_implied_vol_fixtures.py        # IV-001 through IV-004
  test_realized_vol_fixtures.py       # RV-001 through RV-004
  test_engine_stats_fixtures.py       # ENG-001 through ENG-006
  test_research_primitives_fixtures.py # RP-001 through RP-004
  test_indicator_fixtures.py          # IND-001 through IND-005
  test_reliability_fixtures.py        # REL-001 through REL-004
```

Each test:
1. Loads `input.parquet` + `output.parquet` from the fixture directory
2. Runs the current canonical implementation on the input
3. Asserts `np.allclose(our_output, reference_output, atol=<explicit>, rtol=<explicit>)`
4. On failure: prints per-cell delta table so the breach is immediately diagnosable

Validation results are also written to `PythonDataService/artifacts/fixture-validation/latest.json` — this is what the frontend API serves.

---

## 7. Backend API Endpoint

New FastAPI endpoint:

```
GET /api/golden-fixtures
→ { categories: [...], fixtures: [{ id, name, category, status, max_delta, cells_total, cells_pass, last_run_ms }] }

GET /api/golden-fixtures/{fixture_id}
→ { id, name, formula, canonical_file, tolerance, reference_data: [...], calculated_data: [...], deltas: [...], citation, provenance }
```

The `/api/golden-fixtures/{fixture_id}` endpoint:
- Loads the stored fixture from disk (`input.parquet` + `output.parquet`)
- Runs the canonical implementation **live** on the stored input (this is the "calculated data" column)
- Computes per-cell deltas
- Returns both columns + deltas to the frontend

This means every page load re-runs the canonical. If the implementation drifted, the delta immediately appears — no stale cache.

**Performance note:** Expensive fixtures (IV-002 SVI surface fit, RV-004 VIX replication) are cached in-process for 60 seconds. The cache key is the fixture ID; a forced refresh clears it. The catalog endpoint (`GET /api/golden-fixtures`) always reads from the pre-computed `artifacts/fixture-validation/latest.json` — it never triggers live recalculation. Only the detail endpoint (`/{fixture_id}`) runs the canonical live.

---

## 8. Frontend Route & Components

New route: `/golden-fixtures` with two child routes.

```
/golden-fixtures                    → GoldenFixturesCatalogComponent
/golden-fixtures/:category          → GoldenFixturesCategoryComponent
/golden-fixtures/:category/:id      → GoldenFixtureDetailComponent
```

### GoldenFixturesCatalogComponent
- Stats bar: validated / pending / total / sources
- Category filter tabs (All + 7 categories)
- Fixture grid: cards showing ID, name, formula, tolerance, source tags, status badge
- Status badge: ⭐ CERTIFIED (gold) | ⏳ PENDING | ⚠ BREACH (red with Δ number)

### GoldenFixtureDetailComponent
- Fixture header: ID, name, formula, gold star or breach badge
- Tolerance bar with justification
- Summary chips: cells pass/total, max |Δ|, mean |Δ|, fixture date
- Validation table: Reference | Calculated | |Δ| | Status per row
- Delta heatmap (Strike × Expiry grid for options fixtures; time series for indicator fixtures)
- Breach explanation block (when applicable): what diverged, possible cause, investigation link
- Provenance panel: Source 1 (Polygon data details) + Source 2 (BigData citation details)

### Angular conventions (per `.claude/rules/angular.md`):
- Standalone components, `ChangeDetectionStrategy.OnPush`
- `resource()` for async data fetching
- `signal()` for filter/tab state
- `computed()` for derived stats (cells_pass / cells_total ratio, etc.)
- Route: lazy-loaded via `loadComponent` / `loadChildren`

---

## 9. Docs Structure

```
docs/references/golden-fixtures/
  README.md                          # index — links to all fixture docs
  options-pricing/
    BS-001-black-scholes-call.md     # formula, derivation, tolerance justification, Hull cite
    BS-003-delta.md
    ...
  implied-volatility/
    IV-001-newton-brent-solver.md    # solver algorithm, convergence, Brenner-Subr seed, CBOE cite
    IV-002-svi-surface.md            # Gatheral SVI parameterization, no-arbitrage conditions
    ...
  realized-volatility/
    RV-002-abdl-hf-estimator.md      # ABDL (2003) realized variance, sampling frequency choice
    RV-004-vix-replication.md        # Demeterfi-Derman (1999), discrete strike grid, wing truncation
    ...
  engine-statistics/
    ENG-001-sharpe-ratio.md          # Sharpe (1994), annualization convention, 252 trading days
    ...
  research-primitives/
    RP-001-information-coefficient.md  # Lopez de Prado, Spearman vs Pearson choice
    RP-003-block-bootstrap.md          # Phipson-Smyth (2010), block length selection
    ...
```

Each markdown file contains: formula, derivation sketch, tolerance justification, academic reference (full citation), data source used, any known limitations.

---

## 10. Build Sequence

This is ordered to minimize rework. Each phase is independently shippable.

**Phase 1 — Foundation (fixtures + tests, no UI)**
- Generate fixture data for Categories 1, 4, 6 (Options Pricing, Engine Stats, Indicators) — these have the clearest canonical files and existing partial parity
- Write pytest fixture tests for those three categories
- Add `GET /api/golden-fixtures` (catalog endpoint only, status from JSON file)

**Phase 2 — Complete fixture coverage**
- Generate remaining fixtures: IV, RV, Research Primitives, Reliability
- Add IV surface delta heatmap computation
- Complete `GET /api/golden-fixtures/{id}` (live recalculation path)

**Phase 3 — Frontend**
- Catalog page with cards and status badges
- Detail page: table + heatmap + provenance
- Wire to live API

**Phase 4 — Docs**
- Write 34 markdown reference files (one per fixture)
- Update `math-sources-of-truth.md` — mark each pending-fixture row as `canonical` once its fixture is generated and test passes

---

## 11. What Is Out of Scope

- Automated fixture refresh (fixtures are pinned; they do not auto-update with new market data)
- Options Greeks cross-engine parity (already done in `test_bs_cross_engine_parity.py` — the golden fixture system reuses that result, does not replace it)
- Backend `.NET` migration (tracked separately in `numerical-authority-migration-plan.md`)
- Live/streaming validation (all validation is against the pinned 2024-01-02 snapshot)

---

## 12. Tolerance Reference Table

| Category | Default | Exceptions | Justification |
|---|---|---|---|
| Closed-form options math (BS price, Greeks) | atol=1e-9, rtol=0 | — | Double-precision closed-form; no accumulation |
| IV solvers (Newton-Brent, QuantLib) | atol=1e-6, rtol=0 | — | Iterative convergence limit at double precision |
| IV surface fitting (SVI/SABR) | atol=1e-6, rtol=1e-6 | — | Nonlinear least-squares; scale-relative error acceptable |
| VIX replication | atol=1e-4, rtol=0 | — | Discrete strike sum; discretization error O(ΔK²) |
| pandas-ta indicators (MACD, BBands) | atol=1e-6, rtol=0 | — | pandas-ta uses float32 internally on some paths |
| All others | atol=1e-9, rtol=0 | — | Default per `numerical-rigor.md` |
