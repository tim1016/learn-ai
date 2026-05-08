# Golden Fixtures System — Opus Session Handoff

**Date:** 2026-05-08  
**From:** Sonnet 4.6 (design session)  
**For:** Opus — full implementation  
**Spec file:** `docs/superpowers/specs/2026-05-08-golden-fixtures-design.md`

---

## What this session produced

A complete design for a golden-fixture validation system for learn-ai's canonical mathematical implementations. The design has been reviewed by the repo owner and refined. Your job is to implement it.

**Do not re-design. Read the existing spec, then implement phase by phase.**

---

## Step 1 — Before you touch anything

Read in this order:

1. `AGENTS.md` (repo root)
2. `.claude/rules/numerical-rigor.md` — the scientific standards this entire system enforces
3. `docs/math-sources-of-truth.md` — the registry of every canonical math concept; you will update it as fixtures are certified
4. `docs/superpowers/specs/2026-05-08-golden-fixtures-design.md` — the full spec (also summarised below)
5. `PythonDataService/tests/fixtures/golden/bs-price-cross-engine/attribution.md` — the existing fixture style to match
6. `PythonDataService/tests/fixtures/golden/bs-price-cross-engine/cases.json` — the existing fixture schema to match

The existing `bs-price-cross-engine` fixture is the **style authority**. New fixtures must follow its conventions (JSON schema, attribution.md structure, no stored output unless needed).

---

## Step 2 — Understand the three-layer proof model

This is the most important design decision. Every fixture must prove equivalence via **three distinct evidence layers**:

### Layer 1 — Market Input Provenance
Where the input data came from. Usually Polygon via Massive Market Data MCP, or a seeded synthetic array. This proves inputs are real and reproducible. **This is not numerical proof.**

### Layer 2 — Methodology Provenance  
The academic/industry source that defines the formula, conventions, and tolerance. Usually a paper, Hull, CBOE white paper, Lopez de Prado, or BigData MCP research retrieval. **This proves we are implementing the right formula. It does not produce expected outputs.**

### Layer 3 — Independent Numerical Oracle
The source of **expected outputs**. This is the only layer that constitutes numerical proof. Examples:

| Oracle type | When to use |
|---|---|
| `cross_engine` | Two independent implementations agree — e.g. `bs_greeks.py` vs QuantLib |
| `external_reference` | A vendored reference (LEAN) produces outputs we match |
| `literature_formula` | A small independent script derives expected outputs from the cited formula directly (no learn-ai code in the loop) |
| `hand_computed` | Expected outputs can be manually verified by algebra — use for engine stats with small synthetic curves |
| `vendor_observed` | Polygon/broker reported field — useful for comparison, but not strict proof (vendor may use different conventions) |
| `internal_regression` | Pinning our own output — only valid for drift detection, **never** presented as external certification |

**A fixture is "Certified" only if its oracle is `cross_engine`, `external_reference`, `literature_formula`, or `hand_computed`.** The UI must distinguish these from `vendor_observed` and `internal_regression`.

---

## Step 3 — Key corrections from repo owner (incorporate exactly)

These override the initial design spec where they conflict:

### 1. EMA smoothing factor
**Correction:** EMA in this repo uses `k = 2/(1+period)` — this is **standard exponential smoothing**, not Wilder smoothing. Wilder smoothing uses `k = 1/period`. RSI uses Wilder. EMA does **not**. Confirmed in `app/engine/indicators/ema.py` line 11: `k = 2 / (1 + period)`.

### 2. VIX vs model-free variance
**Correction:** The `vix_replication.py` implementation uses SPY options via Polygon. This is **model-free variance replication**, not true VIX replication. True VIX uses SPX options and CBOE methodology. Document RV-004 as "model-free variance replication (SPY)" — do **not** call it VIX replication. If CBOE EOD calculation inputs are accessible via BigData MCP, they are a stronger oracle; check and document.

### 3. Black-Scholes inputs must include dividend yield `q`
The existing `bs-price-cross-engine/cases.json` already includes `dividend` as a dimension. All new BS fixtures must also parameterise `q`. Do not use a simplified zero-dividend formula.

### 4. Document Greek units explicitly
Every Greek fixture attribution.md must state:
- Theta: per calendar day, per trading day, or per year?
- Vega: per 1.0 volatility unit, or per 1 vol point (i.e. per 0.01)?
- Rho: per 1.0 rate unit, or per basis point?
Read `app/services/bs_greeks.py` and record the actual convention before writing attribution.

### 5. Prefer synthetic inputs for closed-form BS fixtures
Do **not** require a Polygon market snapshot to run BS price/Greek tests. Synthetic deterministic grids (like the existing `bs-price-cross-engine/cases.json`) are better: no network required, no date drift, no corporate action risk. Polygon chain data can be a **separate** integration fixture labelled `vendor_observed`.

### 6. Do not create a disconnected fixture directory
The existing fixture location is `PythonDataService/tests/fixtures/golden/`. New fixtures go there, following the same structure. Do **not** create a parallel `PythonDataService/app/engine/tests/fixtures/` tree.

### 7. Polygon reported IV is not the strict oracle for IV fixtures
For IV-001 (Newton-Brent solver), the correct oracle is: generate synthetic market prices from a **known** volatility using `bs_greeks.py`, then solve back. The solved IV must equal the input IV to within tolerance. Polygon's reported IV is stored as a `vendor_observed` comparison column — not the expected output.

---

## Step 4 — What already exists (leverage, do not recreate)

### Existing fixtures (in `PythonDataService/tests/fixtures/golden/`)
```
bs-price-cross-engine/
  cases.json           # input grid: spot × strike × ttm × vol × rate × dividend × type
  attribution.md       # style authority — match this for all new fixtures

iv30/
  spy-2024-12-20-chain.parquet  # existing SPY options chain
  spy-2024-12-20-chain.meta.json

portfolio-scenario-3leg/
  cases.json
  attribution.md
```

### Existing tests that already do cross-engine or parity work

| Test file | What it does | Status |
|---|---|---|
| `tests/services/test_bs_cross_engine_parity.py` | bs_greeks.py vs QuantLib on 360-case grid; atol=1e-10 | **Passing — this IS a cross_engine fixture already** |
| `tests/test_indicator_parity.py` | EMA/SMA/RSI via calculate_dynamic_indicators vs pandas-ta direct | Passing — internal parity only, not external_reference |
| `tests/research/runs/test_ema_acceptance.py` | EMA Crossover strategy end-to-end acceptance gate | Passing |

### Key canonical files (confirmed paths)

| Concept | File | Key callable |
|---|---|---|
| BS price | `app/services/bs_greeks.py` | `bs_european_price` |
| BS Greeks | `app/services/bs_greeks.py` | `black_scholes_greeks` |
| QuantLib pricer | `app/services/quantlib_pricer.py` | `price_option(engine=PricingEngine.ANALYTIC_BS)` |
| IV solver | `app/volatility/solver.py` | `implied_volatility` |
| IV surface | `app/volatility/fitting.py` | — (read file to confirm callable) |
| IV30 | `app/volatility/iv30_health.py` | — |
| IV-RV basis | `app/volatility/basis.py` | — |
| VIX-style variance | `app/volatility/vix_replication.py` | — |
| Close-to-close RV | `app/engine/edge/features_realtime/realized_vol.py` | — |
| HF realized vol | `app/engine/edge/features_realtime/hf_realized_vol.py` | — |
| Engine stats | `app/engine/results/statistics.py` | `TradeStatistics`, `EquityStatistics` |
| EMA | `app/engine/indicators/ema.py` | `ExponentialMovingAverage` |
| SMA | `app/engine/indicators/sma.py` | `SimpleMovingAverage` |
| RSI | `app/engine/indicators/rsi.py` | `RelativeStrengthIndex` |
| IC | `app/research/validation/ic.py` | — |
| Quantile stats | `app/research/validation/quantile.py` | — |
| Block bootstrap | `app/research/validation/robustness.py` | — |
| Signal z-score | `app/research/signal/standardize.py` | — |
| Indicator reliability | `app/research/indicator_reliability.py` | — |

---

## Step 5 — Full fixture registry (34 fixtures, 7 categories)

### Fixture manifest schema

Each fixture entry in `manifest.yaml`:

```yaml
id: BS-001
name: Black-Scholes call price
category: options-pricing
canonical:
  module: app.services.bs_greeks
  callable: bs_european_price
reference:
  kind: cross_engine          # cross_engine | external_reference | literature_formula | hand_computed | vendor_observed | internal_regression
  oracle: QuantLib analytic European engine (app/services/quantlib_pricer.py)
  citation: "Hull (10e) §15.8"
market_input:
  source: synthetic           # synthetic | polygon_snapshot | polygon_bars | seeded_rng
  vendor: null
tolerance:
  atol: 1e-9
  rtol: 0
files:
  input: options-pricing/BS-001/input.parquet
  expected: options-pricing/BS-001/output.parquet   # null if cross_engine (both sides compute live)
  attribution: options-pricing/BS-001/attribution.md
status: planned               # planned | generated | validated | certified | breach
```

---

### Category 1 — Options Pricing & Greeks

Inputs: synthetic deterministic grid (matches `bs-price-cross-engine` style). Oracle: QuantLib. These are `cross_engine` — no stored output file needed (both sides compute live, like the existing fixture).

| ID | Name | Oracle | atol |
|---|---|---|---|
| BS-001 | Black-Scholes call price | cross_engine (QuantLib analytic_bs) | 1e-10 |
| BS-002 | Black-Scholes put price | cross_engine (QuantLib analytic_bs) | 1e-10 |
| BS-003 | Delta Δ | cross_engine (QuantLib) | 1e-9 |
| BS-004 | Gamma Γ | cross_engine (QuantLib) | 1e-9 |
| BS-005 | Theta Θ | cross_engine (QuantLib) | 1e-9 |
| BS-006 | Vega ν | cross_engine (QuantLib) | 1e-9 |
| BS-007 | Rho ρ | cross_engine (QuantLib) | 1e-9 |

**Note:** The existing `bs-price-cross-engine` fixture covers BS-001/002 cross-engine parity. New work for BS-001/002 is: (1) register it in manifest.yaml with `kind: cross_engine`, (2) confirm Greeks parity is also tested or create BS-003–007 fixture files. Do not recreate what already exists.

**Greek unit documentation required (read bs_greeks.py, then record in attribution.md):**
- Theta: per calendar day? per year? (`-S·φ(d₁)·σ/(2√T) − r·K·e^{−rT}·N(d₂)` gives per-year; divide by 365 for per-calendar-day)
- Vega: per 1.0 vol unit (multiply by 0.01 for per-vol-point)
- Rho: per 1.0 rate unit (multiply by 0.01 for per-bp)

---

### Category 2 — Implied Volatility

| ID | Name | Oracle | Input | atol |
|---|---|---|---|---|
| IV-001 | IV solver (Newton-Brent) | literature_formula — synthetic: BS-price(known σ) → solve back → must recover σ | synthetic prices from known vol grid | 1e-6 |
| IV-002 | IV surface — SVI fit | literature_formula — evaluate fitted `w(k)` on fixed moneyness grid vs Gatheral reference | synthetic vol smile (known SVI params) | 1e-6 rtol=1e-6 |
| IV-003 | IV 30d constant-maturity | literature_formula — deterministic two-expiry slices, variance-time interpolation | deterministic option slices | 1e-6 |
| IV-004 | IV rank (60d rolling) | hand_computed — trivial rolling min/max, verify algebraically | seeded synthetic IV series | 1e-9 |

**Important for IV-001:** `reference.kind = literature_formula`. The oracle is: generate `market_price = bs_european_price(S, K, r, T, σ_known, q)` for a grid of known σ values. Run `solver.implied_volatility(market_price, S, K, r, T, q)`. The result must equal `σ_known` to within atol=1e-6. Polygon reported IV is stored as a separate `vendor_observed` comparison column in the attribution — not the expected output.

**Important for IV-002:** Compare the fitted total variance `w(k)` on a fixed moneyness grid, not just SVI parameters (`a, b, ρ, m, σ`). SVI parameters may not be uniquely identified across different optimisers; the surface evaluated on a grid is what matters.

---

### Category 3 — Realized Volatility

| ID | Name | Oracle | Input | atol |
|---|---|---|---|---|
| RV-001 | Close-to-close σ (annualized) | hand_computed — formula is σ̂=√(252/n·Σrᵢ²), trivially verifiable on synthetic log-returns | seeded synthetic price series | 1e-9 |
| RV-002 | HF realized vol (ABDL estimator) | external_reference — ABDL (2003) formula; verify against independent numpy implementation of the same formula | seeded synthetic 5-min returns | 1e-8 |
| RV-003 | IV-RV basis | hand_computed — basis = IV30 − RV30; verify algebraically on synthetic inputs | synthetic IV + RV inputs | 1e-6 |
| RV-004 | Model-free variance replication (SPY) | literature_formula — CBOE methodology on deterministic strike strip; NOT called "VIX replication" | deterministic synthetic option strip | 1e-4 |

**Important for RV-004 naming:** Do not call this "VIX replication". It is "model-free variance replication" using SPY options. True VIX replication uses SPX options and the official CBOE methodology. Call the fixture `RV-004-model-free-variance`. If CBOE EOD calculation inputs are retrievable via BigData MCP, cite them and use as a stronger oracle; otherwise use literature_formula.

---

### Category 4 — Engine Statistics

| ID | Name | Oracle | Input | atol |
|---|---|---|---|---|
| ENG-001 | Sharpe ratio (annualized) | hand_computed — small 10-trade synthetic equity curve, manually computed Sharpe | seeded synthetic equity curve | 1e-9 |
| ENG-002 | Max drawdown | hand_computed | same | 1e-9 |
| ENG-003 | Sortino ratio | hand_computed | same | 1e-9 |
| ENG-004 | Calmar ratio | hand_computed | same | 1e-9 |
| ENG-005 | CAGR | hand_computed | same | 1e-9 |
| ENG-006 | Win rate | hand_computed | same | 1e-9 (exact ratio) |

**Important:** Primary fixtures are synthetic hand-computable curves. The EMA acceptance test trade log (`tests/research/runs/test_ema_acceptance.py`) can be a **secondary integration fixture** labelled `internal_regression` (drift detection only). The hand-computed synthetic fixtures are the certification proof.

**Be explicit in attribution.md about n:**
- Sharpe: n = number of daily equity observations
- Max drawdown: n/a (peak-to-trough ratio)
- Calmar: CAGR annualization uses 252 trading days (see `TRADING_DAYS_PER_YEAR = 252` in `statistics.py`)

---

### Category 5 — Research Primitives

| ID | Name | Oracle | Input | atol |
|---|---|---|---|---|
| RP-001 | IC (Information Coefficient) | literature_formula — Spearman rank correlation, trivially computed with scipy.stats.spearmanr as independent impl | seeded synthetic (forecast, realized) array | 1e-9 |
| RP-002 | Quantile monotonicity | hand_computed | seeded synthetic returns + quantile bins | 1e-9 |
| RP-003 | Block bootstrap p-value | literature_formula — Phipson-Smyth (2010): p=(1+count(null≥obs))/(N+1) | seeded synthetic null distribution | 1e-9 |
| RP-004 | Signal z-score | hand_computed — algebraically trivial | seeded synthetic series | 1e-9 |

---

### Category 6 — Technical Indicators

| ID | Name | Oracle | Input | atol |
|---|---|---|---|---|
| IND-001 | EMA (period=10, k=2/11) | external_reference — LEAN vendored source | seeded synthetic bars | 1e-9 |
| IND-002 | SMA (period=20) | external_reference — LEAN vendored source | seeded synthetic bars | 1e-9 |
| IND-003 | RSI Wilders (period=14) | external_reference — LEAN vendored source | seeded synthetic bars | 1e-9 |
| IND-004 | MACD (12,26,9) | internal_regression — pandas-ta; no LEAN reference | seeded synthetic bars | 1e-6 |
| IND-005 | Bollinger Bands (20,2) | internal_regression — pandas-ta; no LEAN reference | seeded synthetic bars | 1e-6 |

**EMA smoothing factor confirmed:** `k = 2/(1+period)` (standard exponential smoothing, LEAN-matched). This is **not** Wilder smoothing. Wilder smoothing (`k = 1/period`) is used by RSI only. Attribution must state this explicitly to prevent future confusion.

**LEAN oracle path:** The LEAN vendored reference is at `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/`. For IND-001/002/003, generate expected outputs by running the LEAN C# indicator logic on the same input — or, equivalently, confirm that the existing `test_indicator_parity.py` fixture's pandas-ta outputs agree with LEAN to tolerance. If LEAN outputs are not directly available as stored data, the `test_indicator_parity.py` parity with pandas-ta is `internal_regression`; only LEAN-matched outputs qualify as `external_reference`.

**IND-004/005 oracle:** pandas-ta is the external library being wrapped. Cross-check is `internal_regression` (our wrapper vs the library directly). There is no stronger oracle for MACD/BBands currently.

---

### Category 7 — Indicator Reliability

| ID | Name | Oracle | Input | atol |
|---|---|---|---|---|
| REL-001 | Win-rate stability (rolling IC) | literature_formula | seeded synthetic signal + bars | 1e-9 |
| REL-003 | Calibration curve (Brier-like) | hand_computed | seeded synthetic forecast + realized | 1e-9 |
| REL-004 | IC time series (EMA-10 signal) | literature_formula | seeded synthetic bars | 1e-9 |
| REL-002 | (**DEFERRED**) Regime-conditioned hit rate | Blocked on `regime_clustering.py` canonical verification | — | — |

**REL-002 deferral reason:** `app/engine/edge/regime_clustering.py` only has partial test coverage (EM convergence + posterior shape — see `tests/edge/test_regime_clustering.py`). A fixture that depends on uncertified regime labels is not trustworthy. Do not generate REL-002 until regime clustering has its own certified fixture.

---

## Step 6 — Implementation phases

### Phase 0 — Foundation (implement first, before generating any fixtures)

Create the manifest and support utilities. Everything else builds on top of these.

**Files to create:**

```
PythonDataService/tests/fixtures/golden/
  manifest.yaml          # fixture registry — all 34 entries
  README.md              # what this directory is, how to regenerate

PythonDataService/tests/fixtures/golden_support/
  __init__.py
  manifest.py            # load/validate manifest.yaml; FixtureEntry dataclass
  hashing.py             # sha256_file(path) → hex; sha256_json(obj) → hex
  io.py                  # load_parquet, load_json, save_parquet, save_json (consistent dtypes)
  compare.py             # compare_arrays(ref, calc, atol, rtol) → CompareResult with delta table
  registry.py            # map fixture_id → FixtureEntry; validate all canonical modules importable
```

**`manifest.py` must enforce:**
- Every fixture has `id`, `name`, `category`, `canonical.module`, `canonical.callable`, `reference.kind`, `tolerance.atol`, `tolerance.rtol`
- `reference.kind` is one of the six valid values
- `tolerance.atol` and `tolerance.rtol` are explicit floats (never null)

**Test for manifest:** `tests/fixtures/test_golden_manifest.py` — validates schema, checks all declared fixture files exist (if status != planned), checks all canonical modules are importable.

**Immutability rule (enforce in code, not just docs):** If `output.parquet` or `output.json` exists and `--force` is not passed, the generation script raises a descriptive error and exits non-zero. `--force` requires `--justification "..."` to be non-empty. Both old and new SHA-256 are printed.

---

### Phase 1 — Highest-confidence fixtures

Implement after Phase 0. Focus on fixtures with the strongest, most available oracle paths.

**Priority order:**
1. **BS price/Greeks (BS-001–007):** The existing `bs-price-cross-engine` fixture already does cross-engine parity for price. Register it in manifest.yaml. Create Greek fixtures (BS-003–007) following the same pattern. Input grid: same as existing cases.json — extend with `dividend` dimension if not already present (it is — confirmed in the file).

2. **Engine stats (ENG-001–006):** Write a small `generate_engine_stats_fixtures.py` that creates a 20-bar synthetic equity curve with a known set of trades. The expected outputs (Sharpe, MDD, Sortino, Calmar, CAGR, win rate) must be computed by a **reference script that does not import `statistics.py`** — implement each formula directly in numpy. Store both the expected output JSON and the reference computation script.

3. **Indicators (IND-001–003):** Generate expected EMA/SMA/RSI outputs from LEAN vendored source if possible. Otherwise use the `test_indicator_parity.py` approach (compare against pandas-ta directly) — but label these `internal_regression`, not `external_reference`. Do not mis-label.

**After Phase 1, add:**
- `GET /api/golden-fixtures` FastAPI endpoint (catalog only; reads `artifacts/fixture-validation/latest.json`)
- Test for the catalog endpoint

---

### Phase 2 — Volatility fixtures

IV solver, IV surface, IV30, IV-RV basis, realized vol, model-free variance. These require careful oracle construction — do not rush.

For IV-001: write the oracle script `scripts/fixtures/iv_solver_oracle.py` that:
1. Takes a grid of (S, K, r, T, q, σ_known)
2. Computes `market_price = bs_european_price(...)` for each row
3. Stores `(input_row, market_price, σ_known)` — σ_known is the expected IV
4. The validation test runs `solver.implied_volatility(market_price, S, K, r, T, q)` and asserts it recovers σ_known to atol=1e-6

For IV-002: use the known-parameter approach. Construct a synthetic vol smile from known SVI params. Fit SVI to the smile. Evaluate `w(k)` on a fixed moneyness grid. The reference values are computed by evaluating the SVI formula directly (independent of `fitting.py`).

---

### Phase 3 — Research primitives and reliability

Pure statistical fixtures. All use seeded synthetic arrays. Oracle scripts are simple numpy/scipy implementations of the cited formula.

For RP-001 (IC): oracle is `scipy.stats.spearmanr(forecast_ranks, realized_ranks)`. Our `ic.py` should produce the same result.

---

### Phase 4 — Frontend

**Angular route:** `/golden-fixtures` with two child routes. Lazy-loaded.

**Components:**
- `GoldenFixturesCatalogComponent` — reads from catalog API; stats bar, category tabs, fixture grid
- `GoldenFixtureDetailComponent` — reads from detail API; validation table, delta heatmap, provenance

**UI certification levels (distinct visual treatment — do not show gold star for internal regression):**

| Level | When | Badge |
|---|---|---|
| ⭐ Certified | `cross_engine`, `external_reference`, `literature_formula`, `hand_computed` | Gold star |
| ≈ Cross-Engine | `cross_engine` specifically | Blue badge |
| ~ Vendor Comparison | `vendor_observed` | Amber badge |
| ↺ Regression Pinned | `internal_regression` | Grey badge |
| ⏳ Pending | status = planned/generated | Amber pending |
| ⚠ Breach | any status with cells failing | Red badge + Δ number |

**Angular must not compute validation math.** It renders API results only.

---

### Phase 5 — Generation CLI

`PythonDataService/scripts/generate_fixtures.py` — see spec for full CLI design. Key requirement: network/MCP access is only needed during generation; validation tests must run offline.

---

### Phase 6 — Docs

`docs/references/golden-fixtures/` — one markdown file per fixture. Each file contains formula, canonical implementation, independent oracle, methodology citation, input provenance, tolerance and justification, known limitations, regeneration command, SHA-256 hashes.

Update `docs/math-sources-of-truth.md` — change each `pending-fixture` row to `canonical` **only** when its fixture is generated, test passes, and oracle is `cross_engine/external_reference/literature_formula/hand_computed`. Do not update the registry for `internal_regression` or `vendor_observed` fixtures.

---

## Step 7 — First PR (keep it small)

The recommended first PR contains:

1. `tests/fixtures/golden/manifest.yaml` — all 34 entries in `planned` status
2. `tests/fixtures/golden/README.md`
3. `tests/fixtures/golden_support/__init__.py`, `manifest.py`, `hashing.py`, `io.py`, `compare.py`, `registry.py`
4. `tests/fixtures/test_golden_manifest.py` — schema validation + importability checks
5. Register existing `bs-price-cross-engine` fixture in manifest.yaml as `status: validated`, `kind: cross_engine`
6. BS Greek fixtures (BS-003–007) — input grid + cross-engine parity test using QuantLib
7. Engine stats fixtures (ENG-001–006) — synthetic curve + hand-computed expected values
8. `GET /api/golden-fixtures` catalog endpoint — reads latest.json, no live recalculation
9. `PythonDataService/app/routers/golden_fixtures.py` registered in `app/main.py`

**Defer to later PRs:** IV fixtures, RV fixtures, research primitives, Angular frontend, docs.

---

## Step 8 — Acceptance criteria (full system)

- [ ] `manifest.yaml` validates (no schema errors, all canonical modules importable)
- [ ] All Phase 1 fixtures generated and in `status: validated`
- [ ] Every fixture declares explicit `atol` and `rtol` — no defaults
- [ ] Every fixture declares `reference.kind` — no fixtures with undeclared oracle type
- [ ] Validation tests run offline (no network, no MCP required)
- [ ] No fixture output overwritten without `--force --justification`
- [ ] `GET /api/golden-fixtures` serves status from pre-computed JSON
- [ ] `GET /api/golden-fixtures/{id}` reruns canonical live; returns reference + calculated + deltas
- [ ] Angular UI shows distinct badges for certified vs regression-pinned vs pending
- [ ] `docs/math-sources-of-truth.md` updated only for certified fixtures
- [ ] REL-002 remains deferred until regime clustering is certified

---

## What Sonnet confirmed about the existing codebase

- **EMA smoothing factor:** `k = 2/(1+period)` confirmed in `ema.py:11`
- **Existing cross-engine fixture:** `tests/fixtures/golden/bs-price-cross-engine/` — style authority, 360-case grid, `atol=1e-10`
- **`statistics.py` constant:** `TRADING_DAYS_PER_YEAR = 252` — use this for all annualization
- **QuantLib pricer:** `price_option(engine=PricingEngine.ANALYTIC_BS)` in `app/services/quantlib_pricer.py`
- **IV30 fixture already exists:** `tests/fixtures/golden/iv30/spy-2024-12-20-chain.parquet` — check if this can seed IV-003
- **Volatility module:** 12 files in `app/volatility/` — read each before writing tests
- **No existing `manifest.yaml` or `golden_support/` module** — create from scratch

---

## MCP servers available

- **Massive Market Data MCP** (`mcp__Massive_Market_Data__*`) — Polygon.io; use for market input data when real-market integration fixtures are needed. Not needed for Phase 0/1 (all synthetic).
- **BigData MCP** (`mcp__4f6d44ef-*__bigdata_search`) — research reports, CBOE white papers, academic papers. Use to retrieve CBOE methodology notes for RV-004 attribution and ABDL paper citation for RV-002.
