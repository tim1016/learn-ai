# Edge Feature — Functionality Testing Guide

**Audience:** an engineer with strong applied math but limited finance background. This guide translates the financial concepts to engineering analogues, then walks through testing each layer of the Edge feature concretely.

**Date:** 2026-04-26
**Related:** `docs/architecture/edge-feature-design.md` (engineering spec), `docs/architecture/design-handoff-edge-2026-04-25.md` (UI handoff)

---

## 0. Mental model — translating the domain

Before testing anything, here's the dictionary you need. Each row maps a finance term to a concept you already know.

| Finance term | What it really is | Where it appears in the code |
|---|---|---|
| **Return** ($r_t = \ln(C_t / C_{t-1})$) | First difference of the log signal — i.e. the per-bar log-amplitude change | Everywhere; the input to RV estimators |
| **Realized volatility (RV)** | RMS of returns over a window, annualized — a *measured* noise level | `realized_vol.py` — close-to-close, Parkinson, Garman-Klass, Yang-Zhang |
| **Implied volatility (IV)** | The market's *forecast* of RV for the next τ days, back-solved from option prices | `volatility/solver.py` (existing), wrapped by `iv30_constructor.py` |
| **Variance Risk Premium (VRP)** | Forecast minus actual: $\text{IV}^2 - \text{RV}_\text{forward}^2$. Like the innovation in a Kalman filter — sign-aware. | `vrp.py` |
| **Option** | A side-contract whose price is a *function* of the underlying price + time + IV. The function is Black-Scholes. | Reused via `volatility/solver.py` |
| **Delta (Δ)** | $\partial C / \partial S$ — the option's sensitivity to underlying price. ∈ (0, 1) for calls. 50Δ ≈ at-the-money. | `delta_inversion.py` |
| **Strike (K)** | The price at which the option lets you transact. Just a parameter of the contract. | Same |
| **Expiry / tenor (T)** | Time until the contract expires. | Same |
| **Regime** | A hidden discrete state of the market. Mathematically: the latent variable $z_t$ in a Hidden Markov Model. | `regime_clustering.py` |
| **K-means** | Lloyd 1982. You know it. | `regime_clustering.kmeans` |
| **HMM** | Categorical-state Kalman filter. Forward-backward = belief propagation. Viterbi = MAP path. | `regime_clustering.fit_gaussian_hmm` |
| **Sharpe ratio** | SNR of returns: $\bar r / \hat\sigma_r \cdot \sqrt{252}$ | `trade_simulator.simulate` stats |
| **Drawdown** | Peak-to-trough excursion of cumulative P&L. Maximum bilge depth. | `_drawdown()` in trade simulator |
| **Backtest overfitting** | Curve-fitting on history; the model's apparent edge doesn't generalize. | `robustness_stats.py` (DSR + PBO) |
| **Walk-forward** | Sliding train/test split — exactly like out-of-sample CV in ML. | `period_splitter.walk_forward` |

**The single most important conceptual point:** the whole feature is a measurement system that compares a forecast (IV) to a measurement (RV) and uses the discrepancy to make decisions. If you understand a Kalman filter's innovation, you understand VRP. If you understand cross-validation, you understand walk-forward. The financial vocabulary is window dressing.

---

## 1. The system under test (architecture review)

```
                  ┌───────────────────────────────────────────────┐
                  │ Frontend (Angular)                            │
                  │   /edge → 4 routes + 2 drawers                │
                  │   Charts render via canvas (no SVG > 10k pts) │
                  └─────────────────┬─────────────────────────────┘
                                    │ HTTP
                  ┌─────────────────▼─────────────────────────────┐
                  │ FastAPI router  (PythonDataService/app/       │
                  │                  routers/edge.py)             │
                  │   9 endpoints under /api/edge/*               │
                  └─────────────────┬─────────────────────────────┘
                                    │
┌──────────┐                        │
│ Postgres │ ←── (future)           │
└──────────┘                        │
                  ┌─────────────────▼─────────────────────────────┐
                  │ engine/edge/  (the math layer)                │
                  │   features_realtime/  ← past-only             │
                  │   labels_oracle/      ← forward-only          │
                  │   regime_clustering · trade_simulator         │
                  │   spread_model · edge_score · etc.            │
                  └───────────────────────────────────────────────┘
```

**Three layers to test:**
1. **Math layer (Python)** — pure functions, no I/O. Test with pytest + property checks.
2. **HTTP layer (FastAPI)** — request/response contract validation. Test with curl + JSON inspection.
3. **UI layer (Angular)** — visual + interaction. Test in a browser with dev tools open.

**Plus end-to-end:** click in UI → HTTP fires → math runs → result renders.

---

## 2. Tooling you'll need

| Purpose | Tool | Where it runs |
|---|---|---|
| Math tests | `pytest` | Inside `polygon-data-service` container OR install pytest locally |
| HTTP probes | `curl` (or `httpie` / Postman if you prefer JSON pretty-printing) | Anywhere — endpoints listen on `localhost:8000` |
| Interactive math sanity | Python REPL or a Jupyter notebook | Anywhere — needs `numpy`, `pandas`, `scipy` |
| Browser dev tools | Chrome/Firefox DevTools — Console, Network, Performance tabs | Browser at `http://localhost:4200/edge` |
| Container management | `podman compose ps`, `podman logs -f <container>` | Host shell |

**Bring up the stack:**
```bash
cd /home/inkant/Documents/learn-ai
podman compose up -d                          # all services
podman compose ps                             # confirm healthy
```

Frontend at `http://localhost:4200/edge` · Python API docs at `http://localhost:8000/docs` (Swagger UI) · Backend GraphQL playground at `http://localhost:5000/graphql` (not used in v1).

---

## 3. Math layer — what to test and how

### 3.1 The three test categories

Every math function gets tested in three ways:

1. **Property tests** — assert mathematical invariants (e.g., constant input → zero variance). These are the cheapest and most informative.
2. **Reference parity** — assert numerical equivalence to a trusted external implementation (the R `TTR` package, `py_vollib`, `sklearn`, etc.) on a fixed input.
3. **Edge cases** — empty input, NaN-laden windows, single-bar windows, mid-series gaps.

### 3.2 Where the tests live

```
PythonDataService/tests/edge/
├── test_spread_model.py          # Madhavan-Smidt option spread
├── test_trade_simulator.py       # T+1 fill, exits, costs
├── test_realized_vol.py          # 4 RV estimators
├── test_iv30_and_vrp.py          # delta inversion + VIX-style interp
├── test_regime_clustering.py     # K-means + Gaussian HMM
├── test_regime_drift.py          # Hungarian alignment + KL divergence
├── test_period_splitter.py       # rolling / calendar / walk-forward
├── test_robustness_stats.py      # DSR + PBO
└── test_edge_score.py            # composite scalar
```

### 3.3 Running the math tests

Inside the container (when `pytest` is installed):
```bash
podman exec polygon-data-service python -m pytest tests/edge/ -v --tb=short
```

If pytest isn't in the runtime image (current state), the import-only smoke test:
```bash
podman exec polygon-data-service python -c "
from app.engine.edge.spread_model import option_spread
from app.engine.edge.trade_simulator import simulate, TradeSimConfig
from app.engine.edge.regime_clustering import kmeans, fit_gaussian_hmm
print('OK')
"
```

### 3.4 Properties to spot-check by hand

These are quick REPL checks that exercise the most important invariants — they're the engineering analogue of "did the unit converter return 1 ft when I gave it 12 in?"

#### Realized vol estimators

```python
import numpy as np, pandas as pd
from app.engine.edge.features_realtime.realized_vol import (
    close_to_close, parkinson, garman_klass, yang_zhang
)

# Constant prices → variance must be zero
flat = pd.DataFrame({"open": 100, "high": 100, "low": 100, "close": 100},
                    index=pd.RangeIndex(60))
assert close_to_close(flat, window=20).dropna().abs().max() < 1e-12
assert parkinson(flat,    window=20).dropna().abs().max() < 1e-12
assert garman_klass(flat, window=20).dropna().abs().max() < 1e-12
assert yang_zhang(flat,   window=20).dropna().abs().max() < 1e-12
```

If any estimator returns non-zero on flat input, **the estimator is broken** — there's no signal so RMS must be zero. This is your "DC input → zero AC output" check.

#### Annualization

```python
# A daily series with σ_daily = X annualizes to σ_annual = X * sqrt(252)
rng = np.random.default_rng(0)
n = 2000
log_ret = rng.normal(0, 0.20 / np.sqrt(252), size=n)   # 20% annual vol
close = 100 * np.exp(np.cumsum(log_ret))
bars = pd.DataFrame({"open": close, "high": close * 1.001, "low": close * 0.999, "close": close})
rv = close_to_close(bars, window=252, annualize=True).dropna().iloc[-1]
assert abs(rv - 0.20) < 0.02   # within 2 vol points = within ~10%
```

The 2-vol-point tolerance is a finite-sample artifact — with N=2000 you're estimating a population σ from a finite sample, so the estimator has its own variance. Larger N → tighter bound.

#### Yang-Zhang `k` constant

The published constant is $k = 0.34 / (1.34 + (n+1)/(n-1))$. For n=20, that's ~0.165. Check the source against the paper: `realized_vol.py` line 79.

#### Delta inversion (Black-Scholes)

```python
from app.engine.edge.features_realtime.delta_inversion import strike_for_delta_constant_vol
import numpy as np

# At zero rates, 50Δ call has K = S * exp(σ²T/2)
K = strike_for_delta_constant_vol(S=100, T=30/365, r=0, q=0, sigma=0.20, target_delta=0.50)
expected = 100 * np.exp(0.5 * 0.20**2 * 30/365)
assert abs(K - expected) < 1e-9
```

If this fails, the BS delta closed-form is wrong — and every IV/skew/term-structure metric downstream is suspect.

#### K-means

```python
from app.engine.edge.regime_clustering import kmeans
import numpy as np

# Three well-separated 2D Gaussian blobs → labels should partition them
rng = np.random.default_rng(7)
X = np.vstack([
    rng.normal([0, 0],   0.5, (200, 2)),
    rng.normal([10, 10], 0.5, (200, 2)),
    rng.normal([0, 10],  0.5, (200, 2)),
])
res = kmeans(X, n_clusters=3, seed=42)
counts = np.bincount(res.labels, minlength=3)
assert (counts > 100).all()    # roughly balanced
```

The seed makes it deterministic — re-run with the same seed and you must get bit-identical labels. Re-run with a different seed and the *labels* may permute (e.g. cluster "0" becomes "2"), but the partition is the same.

#### HMM stickiness

```python
from app.engine.edge.regime_clustering import fit_gaussian_hmm
import numpy as np

# Long runs of one state, then another → diagonal transition matrix
rng = np.random.default_rng(0)
states = np.repeat(np.arange(3), 200)
means = np.array([[0, 0], [5, 5], [0, 5]])
X = means[states] + rng.normal(0, 0.3, size=(600, 2))
res = fit_gaussian_hmm(X, n_states=3, seed=42, n_iter=30)
# Diagonal entries should dominate
assert (np.diag(res.transition_matrix) > 0.5).all()
```

If diagonals are low and off-diagonals are large, the HMM is treating each bar as independent (i.e., it's basically k-means) — that's a bug or a numerical-instability tell.

#### Trade simulator conservation

```python
from app.engine.edge.trade_simulator import simulate, TradeSimConfig
import pandas as pd, numpy as np

bars = pd.DataFrame({
    "open":  [100, 101, 102, 103, 104],
    "high":  [101, 102, 103, 104, 105],
    "low":   [99, 100, 101, 102, 103],
    "close": [100, 101, 102, 103, 104],
}, index=pd.Index([0, 1, 2, 3, 4], dtype="int64"))
sigs = pd.Series([1, 0, 0, 0, 0], index=bars.index)
res = simulate(bars=bars, signals=sigs,
               config=TradeSimConfig(time_stop_bars=2, slippage_pct=0.001,
                                     commission_per_unit=0.005, spread_bps_stock=2.0))
attr = res.cost_attribution
assert abs(attr["gross_pnl"] - attr["total_costs"] - attr["net_pnl"]) < 1e-9
```

**Conservation**: gross − costs = net. This is the engineering equivalent of energy conservation. If it fails, money is being created or destroyed somewhere — usually a sign-error bug.

#### Edge Score weights

```python
from app.engine.edge.edge_score import DEFAULT_WEIGHTS
assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9
```

Composite weights must sum to 1 — otherwise the output isn't bounded in [-1, +1].

### 3.5 What "looks suspicious" in math output

| Symptom | Likely cause |
|---|---|
| Estimator returns NaN where you expect a number | Window length > available data, or std=0 in a denominator |
| Yang-Zhang variance goes negative | Numerical issue with small samples — caller should clip to 0 (we do) |
| HMM transition matrix isn't row-stochastic (rows don't sum to 1) | EM step normalization bug |
| K-means crashes with `ValueError: high <= 0` | Empty input array — your input pipeline produced 0 rows |
| VRP series has discontinuity | Likely an IV30 data-source transition; check the coverage probe |
| Sharpe is `inf` | Standard deviation of returns is 0 — only one trade or all identical |
| Forward-RV non-NaN at the end of the series | **Look-ahead bug — this is the worst kind.** The last τ bars MUST be NaN. |

The last one is the show-stopper. The whole point of the data-isolation directory split (`features_realtime/` vs `labels_oracle/`) is to make this kind of bug impossible. If you ever see forward-RV light up at the right edge of the chart, stop — there's contamination.

---

## 4. HTTP layer — endpoint testing

### 4.1 Available endpoints

```
GET  /api/edge/cross-asset/strategies          # list registered strategies
GET  /api/edge/realized-vs-iv/coverage/{sym}   # coverage probe (placeholder)

POST /api/edge/realized-vs-iv/series           # RV/IV/VRP series
POST /api/edge/realized-vs-iv/signals          # oracle vs realtime signals
POST /api/edge/cross-asset/run                 # multi-asset, multi-period backtest
POST /api/edge/regimes/cluster                 # K-means + HMM regime labels
POST /api/edge/regimes/strategy-fit            # per-regime trade stats
POST /api/edge/trade-sim/run                   # pessimistic-first trade simulation
POST /api/edge/edge-score/series               # composite Edge Score series
```

Browse interactively: `http://localhost:8000/docs` (Swagger UI). It generates a UI from the Pydantic schemas — you can fill out request bodies and click "Execute" to fire the endpoint.

### 4.2 The smoke-test sequence

These five calls verify the whole HTTP layer in under 30 seconds.

```bash
# 1. Health
curl -s http://localhost:8000/health
# → {"status":"healthy","service":"polygon-data-service"}

# 2. Strategy registry (no payload)
curl -s http://localhost:8000/api/edge/cross-asset/strategies | head -c 200
# → {"available_strategies":[{"name":"placeholder_buy_and_hold", ...}]}

# 3. Coverage placeholder
curl -s http://localhost:8000/api/edge/realized-vs-iv/coverage/SPY | head -c 200
# → {"symbol":"SPY","iv_first_ts":null,...}

# 4. Series endpoint with synthetic bars (the live test)
podman exec polygon-data-service python -c "
import json, urllib.request, math
n = 200; ts0 = 1700000000000
bars = [{'ts': ts0 + i*86_400_000,
         'open': 400+0.1*i+10*math.sin(i/10), 'high': 401+0.1*i+10*math.sin(i/10),
         'low': 399+0.1*i+10*math.sin(i/10),  'close': 400.5+0.1*i+10*math.sin(i/10),
         'volume': 1e6 + (i%5)*1e4} for i in range(n)]
body = {'symbol': 'SPY', 'bar_size': '1d', 'tenor_days': 30,
        'estimators': ['yz'], 'windows': [10, 30], 'bars': bars}
req = urllib.request.Request('http://localhost:8000/api/edge/realized-vs-iv/series',
    method='POST', headers={'Content-Type': 'application/json'},
    data=json.dumps(body).encode())
with urllib.request.urlopen(req, timeout=10) as r: out = json.loads(r.read())
print('keys:', sorted(out.keys()))
print('rv_trailing keys:', sorted(out['rv_trailing'].keys()))
print('coverage:', out['coverage'])
print('forward NaNs:', sum(1 for v in out['rv_forward']['yz_30'] if v is None))
"
# → forward NaNs should equal 30 (window size); rv_trailing should be populated past warmup

# 5. Regime cluster
podman exec polygon-data-service python -c "
import json, urllib.request, math
n = 200; ts0 = 1700000000000
bars = [{'ts': ts0 + i*86_400_000,
         'open': 400+0.1*i+10*math.sin(i/10), 'high': 401+0.1*i+10*math.sin(i/10),
         'low': 399+0.1*i+10*math.sin(i/10),  'close': 400.5+0.1*i+10*math.sin(i/10),
         'volume': 1e6 + (i%5)*1e4} for i in range(n)]
body = {'symbol': 'SPY', 'n_states': 3, 'algorithms': ['hmm', 'kmeans'], 'bars': bars}
req = urllib.request.Request('http://localhost:8000/api/edge/regimes/cluster',
    method='POST', headers={'Content-Type': 'application/json'},
    data=json.dumps(body).encode())
with urllib.request.urlopen(req, timeout=60) as r: out = json.loads(r.read())
print('hmm transition matrix diagonals:',
      [out['hmm_transition_matrix'][i][i] for i in range(3)])
print('regime_active fraction:', sum(out['regime_active'])/len(out['regime_active']))
"
# → diagonals should all be > 0.5 (sticky)
# → regime_active fraction should be 0.5 to 0.9 (most bars are confidently in some state)
```

### 4.3 What to assert in HTTP responses

For every endpoint:
- HTTP status is 200 (or whatever the spec says — check `app/routers/edge.py`)
- Response is valid JSON, no truncation
- Required keys are present (the Pydantic response model in `edge.py` enumerates them)
- Numeric values are within plausible ranges (e.g., IV ∈ [0, 5]; Sharpe ∈ [-5, 5]; not NaN unless the spec allows it)
- Time fields are `int64` ms UTC — never strings, never DateTime objects
- Length-aligned arrays actually have the same length

For error paths:
- Empty `bars[]` array → 400 with a clear error message
- Unknown estimator name → 400
- Mismatched array lengths in `edge-score/series` → 400
- Nonsense `start_ms > end_ms` → 400 or returns empty result (currently returns empty — you may want stricter validation)

### 4.4 Common HTTP-layer gotchas

| Symptom | Where to look |
|---|---|
| 500 Internal Server Error | `podman logs polygon-data-service` — Python traceback gives you the line |
| Pydantic validation error in 422 response | Your request body shape doesn't match the schema; check `edge.py` BaseModel definitions |
| Endpoint returns immediately with empty arrays | Bars probably outside requested time range, or all NaN after warmup. Check coverage in response. |
| Slow response (> 5s) on regime cluster | EM is iterating; tune `n_iter` in the body or reduce bar count |

---

## 5. UI layer — visual and interaction testing

### 5.1 The four routes

| URL | What it shows |
|---|---|
| `http://localhost:4200/edge` | 3 nav cards with sparklines + capabilities row |
| `http://localhost:4200/edge/realized-vs-iv` | Coverage banner · form · price/IV chart · VRP histogram · signal readout |
| `http://localhost:4200/edge/cross-asset` | Robustness scorecard · universe chips · split-mode form · Sharpe heatmap · equity small-multiples |
| `http://localhost:4200/edge/regimes` | Symbol/states/algo form · regime-tinted price chart · transition matrix · feature radar · stability sparkline · per-regime P&L |

### 5.2 Per-route smoke checks

Open the browser console (F12 → Console tab) before clicking around. Any red errors are bugs to investigate.

#### `/edge` (parent home)

- [ ] Three cards render with distinct colored top rails (amber, blue, violet)
- [ ] Each card has a sparkline preview that draws (canvas, not blank)
- [ ] Hover any card → border picks up the category color, slight upward translate, drop shadow
- [ ] Click a card → navigates to its child route, parent header replaced by route content
- [ ] "Cross-cutting capabilities" panel shows two rows (Edge Score · Inspector, Trade Simulator · Drawer)

#### `/edge/realized-vs-iv`

- [ ] Toolbar shows `/edge › /realized-vs-iv` breadcrumb
- [ ] `edge_score` pill shows a colored dot + the current score value (mono font)
- [ ] `↻ Run trade simulator` button is visible and clickable
- [ ] Coverage banner is amber, non-dismissible, mentions "21 trading days" forward-blind
- [ ] Form: Symbol, Bar size, Tenor dropdowns + estimator chips (CtC, Parkinson, GK, YZ)
- [ ] Click an estimator chip → it toggles the `.on` state (background tint, border change)
- [ ] Main chart renders with: price candles (green/red), IV30 line (amber), greyed-with-hatch right edge
- [ ] Signal triangles (green up = long-vol, red down = short-vol) appear on the chart
- [ ] Move mouse across the chart → vertical crosshair tracks; signal readout panel updates with iv30, rv_yz, vrp_z, etc.
- [ ] VRP histogram shows a current marker (orange triangle) that moves with hover
- [ ] Click `edge_score` pill → Edge Score Inspector drawer slides in from the right with composite + 4 component bars
- [ ] Click `↻ Run trade simulator` → Trade Simulator drawer slides in from the right
- [ ] Click outside either drawer (on the dark scrim) → drawer closes
- [ ] No console errors throughout

#### `/edge/cross-asset`

- [ ] Three "big stat" cards at the top: Robustness, DSR (mean), PBO — each with a colored left border (green/green/orange) and a big mono number
- [ ] Universe card shows 4 SPY/QQQ/IWM/DIA chips + a dashed `+ add ticker` chip
- [ ] Split-mode segmented control (rolling / calendar / walk-forward) — clicking changes the active state
- [ ] Heatmap renders 4 rows × 8 cols of colored Sharpe cells with numbers
- [ ] Hover a cell → blue ring around the cell + a tooltip with sharpe, n_trades, win_rate, max_dd
- [ ] "Equity" tabs: Per-asset (small mult.) · Equal-weight composite · Vol-weighted parity
  - Per-asset shows a 2×2 grid of mini equity curves
  - Composites show a single full-width chart
- [ ] Toolbar pill + Trade Simulator button work as in route 1

#### `/edge/regimes`

- [ ] Form: Symbol, States, Algo (HMM/KMEANS/COMPARE) segmented, Decoding (Viterbi/Posterior) segmented, regime legend
- [ ] Main price chart renders — price line, regime-colored stripes underneath (green / amber / red)
- [ ] Switch view to "posterior" → stripes become stacked bands at varying opacity (3 bands per bar)
- [ ] Switch algo to "compare" → chart height grows; small "HMM (sticky) ↑ · K-means (flickering) ↓" caption appears
- [ ] Bottom row: 3 cards (transition matrix · feature radar · stability sparkline + per-regime P&L)
- [ ] Transition matrix is a 3x3 heatmap with diagonal-dominant blue cells
- [ ] Feature radar shows three filled polygons (one per regime) over a 7-axis radar
- [ ] Stability sparkline shows a violet line with red dots at "structural-break" lows (< 0.5)
- [ ] Per-regime P&L bars show signed bars centered on a zero line

### 5.3 Cross-route interactions

- [ ] Drawer state is per-route (open it on RV-vs-IV, navigate to Cross-Asset → drawer is closed; open it again, drawer opens fresh)
- [ ] Browser back/forward button navigates between Edge routes correctly
- [ ] Direct URL navigation works (paste `localhost:4200/edge/regimes` in a fresh tab)
- [ ] Resizing the browser window doesn't break the layouts (charts inside `overflow-x: auto` containers)

### 5.4 Accessibility / keyboard

- [ ] Tab key moves focus through the cards on `/edge`
- [ ] Enter on a focused card navigates to its route
- [ ] Drawer opens with focus trapped (you can Esc out — wired in v2)
- [ ] No focus traps that strand the user
- [ ] Color is never the *only* signal (e.g. "tradable" badge has both color AND a checkmark vs "theo" text)

### 5.5 Dev-tools red flags

| What to watch | Where |
|---|---|
| **Console errors** in red | DevTools → Console tab. Throw-on-error during render is the most common bug surfacer |
| **Failed network requests** | DevTools → Network tab. Filter to `Fetch/XHR`. Look for 4xx/5xx |
| **Layout shift / paint flashes** | DevTools → Performance tab → record while you click around. Long tasks > 50ms or excessive paint events suggest re-render storms |
| **Memory creep** | DevTools → Memory tab → take a heap snapshot, navigate routes 10×, take another. The delta should be small |

---

## 6. End-to-end test plan (a runbook you can execute)

Take 30 minutes. Follow these in order. Each step has a clear pass/fail.

### Step 1 — bring the stack up
```bash
cd /home/inkant/Documents/learn-ai
podman compose up -d
podman compose ps   # all 4 services should be "healthy" eventually
```
**Pass:** four containers, status `healthy`. **Fail:** any container in `unhealthy`/`exited` — check `podman logs <name>` for the cause.

### Step 2 — math layer smoke test
```bash
podman exec polygon-data-service python -c "
from app.engine.edge.spread_model import option_spread, is_tradable
from app.engine.edge.regime_clustering import kmeans, fit_gaussian_hmm
from app.engine.edge.features_realtime.realized_vol import yang_zhang
from app.engine.edge.edge_score import edge_score, DEFAULT_WEIGHTS
print('imports OK')
print('weights sum:', sum(DEFAULT_WEIGHTS.values()))
"
```
**Pass:** `imports OK` and `weights sum: 1.0`. **Fail:** any traceback.

### Step 3 — Property checks at the REPL
Run the property-check snippets from §3.4. Each `assert` should pass silently.
**Pass:** zero AssertionErrors. **Fail:** any assertion → that's a real math bug, file it with the snippet that broke.

### Step 4 — HTTP layer smoke
Run the five curl/python calls from §4.2.
**Pass:** all five return non-error responses with the documented shapes. **Fail:** any 500 → check `podman logs polygon-data-service`.

### Step 5 — UI smoke
Open `http://localhost:4200/edge` with DevTools console open. Walk the per-route checklists in §5.2.
**Pass:** every checkbox passes; no red errors. **Fail:** any visual artifact, broken interaction, or console error.

### Step 6 — Cross-validation: math vs UI
Pick a specific `currentIdx` in the RV-vs-IV view (e.g., hover until the readout shows `iv30 = 21.4%`). Then in a Python REPL:
```python
from app.engine.edge.services.edge_mock_data import EdgeMockDataService  # if importable
# Or in the UI: the data is in window.__edge_data after navigation? (requires devtools probe)
```
Read the underlying value at the same index. The number on screen should be the number in the data.
**Pass:** numbers match to displayed precision. **Fail:** a UI rendering bug or a unit conversion bug.

### Step 7 — Look-ahead leak audit
Inspect the tail of the price chart on `/edge/realized-vs-iv`:
- The right ~21 bars MUST be inside the hatched amber overlay
- Forward-RV-derived signals (oracle, dashed) must NOT appear inside the hatched region
- Realtime signals (solid) ARE allowed in the hatched region (they're computed from trailing data)

**Pass:** hatched region is empty of dashed-oracle markers. **Fail:** dashed markers in the hatched region — you found the worst possible bug. Stop, report, do not ship.

### Step 8 — Conservation audit
Open `/edge/realized-vs-iv` → Trade Simulator drawer. In the cost-attribution waterfall:
- `gross_pnl` − (`spread_cost` + `slippage` + `commissions`) = `net_pnl`
- `net_pnl_tradable_only` ≤ `net_pnl` (it's a subset of trades)

Manually add the absolute values from the waterfall and verify within ±0.01 of `net_pnl`.
**Pass:** equality holds. **Fail:** a sign error somewhere in the simulator.

### Step 9 — Regime stickiness sanity
On `/edge/regimes`, switch algo to HMM, decoding to Viterbi. Visually inspect the regime stripes: each color band should persist for at least several consecutive bars on average. Then switch to K-means: the stripes should flicker noticeably more.

Open the transition matrix below: HMM diagonal entries should be > 0.7. (K-means doesn't have one.)
**Pass:** stickiness is visually obvious; diagonal > 0.7. **Fail:** HMM produces flickering output → EM converged to a bad local optimum (try a different seed) or there's a bug in the forward-backward implementation.

### Step 10 — Drawer parity
Open the Edge Score Inspector. The composite headline number should equal:
`w_vrp · S_vrp + w_regime · S_regime + w_iv · S_iv + w_trend · S_trend`

You can manually compute this from the four component bars.
**Pass:** within ±0.01 of the displayed composite. **Fail:** the composite isn't actually a sum of components — display bug.

---

## 7. Things you should *not* be testing (yet)

| Out of scope for v1 | Why |
|---|---|
| Real Polygon-backed bars | The frontend uses `EdgeMockDataService` for now; the live `/api/edge/*` endpoints accept inline `bars[]` payloads. Wire-up to a fetch service is a v2 task. |
| Multi-symbol cross-asset run with real data | The `cross_asset_runner` works against bars you supply; we haven't wired the multi-symbol fetch. |
| Bar-magnifier intra-bar fills | Not in v1 by design — the simulator is "pessimistic-first" using bar OHLC only. |
| Options margin / Greek-aware sizing | Deferred to v2. |
| .NET GraphQL passthrough | Deferred to v2 — frontend talks to Python directly. |

---

## 8. When something looks wrong — the debugging order

1. **Read the error message** — half the time it says exactly what's wrong.
2. **Bisect by layer** — does the math test pass? (If yes, it's not the engine.) Does the curl call return what you expect? (If yes, it's not the API layer.) Then it's the UI.
3. **Check the network tab** — is the UI even calling the right endpoint? With the right payload?
4. **Check the Python logs** — `podman logs -f polygon-data-service` while you click in the UI; the request will appear with the exact payload you sent.
5. **Reduce to a minimum failing case** — if a 200-bar series fails, does a 50-bar series? If a 50-bar series fails, does a 30-bar one with a single estimator?
6. **Check the data isolation invariant** — if the result "looks too good," double-check that no `forward_rv` snuck into a feature input. The CI grep check (`pytest -k test_no_leakage`, when wired) is the long-term guard.

---

## 9. A note on numerical tolerances

Throughout the codebase you'll see explicit tolerances like `atol=1e-9, rtol=0`. This is intentional and important:

- `np.allclose(a, b)` with default tolerances uses `rtol=1e-5, atol=1e-8` — generous enough that subtle bugs slip through. **Always specify tolerances explicitly.**
- The right tolerance depends on what you're comparing:
  - Two pure numpy operations on the same data → `atol=1e-12` (machine precision)
  - Two implementations of the same formula in different libraries → `atol=1e-9`
  - Estimator with iteration or numerical differentiation (Newton-Raphson, Greeks) → `atol=1e-6, rtol=1e-6`
- If a test passes with `atol=1e-9` but fails with `atol=1e-12`, that's *fine* — you're hitting genuine float arithmetic non-associativity. If it fails at `atol=1e-9`, that's a bug.

The convention here is the same one you'd use in any numerical-methods context: pick a tolerance that's tighter than the noise floor of the inputs but looser than machine precision.

---

## 10. Quick reference card

### Run all unit tests (when pytest is in the runtime image)
```bash
podman exec polygon-data-service python -m pytest tests/edge/ -v
```

### Run one test file
```bash
podman exec polygon-data-service python -m pytest tests/edge/test_realized_vol.py -v
```

### Tail the API logs while you click
```bash
podman logs -f polygon-data-service
```

### Reload the API after a code change
```bash
podman compose restart python-service
```

### Reload the frontend after a code change
The container is in `ng serve` watch mode — saves auto-reload. If they don't:
```bash
podman compose restart frontend
```

### Find the math fixtures (when added)
```
PythonDataService/tests/fixtures/golden/edge/
```

### Find the contract definitions (Pydantic schemas)
```
PythonDataService/app/routers/edge.py     # request/response models for every endpoint
```

### Find the canonical UI component
```
Frontend/src/app/components/edge/         # parent + 3 routes + drawers + charts + service
```

---

## 11. Suggested validation milestones

Use these as project gates — when you can put a checkmark next to all of them, the feature is ready for the next consumer.

- [ ] All math tests pass with explicit tolerances (`pytest tests/edge/`)
- [ ] All HTTP endpoints return 200 on the smoke-test payloads
- [ ] All UI checklists pass with no console errors
- [ ] Look-ahead leak audit (Step 7) passes — no oracle markers in the hatched region
- [ ] Conservation audit (Step 8) passes — costs sum correctly
- [ ] Regime stickiness sanity (Step 9) passes — HMM diagonal > 0.7 on synthetic data
- [ ] Drawer parity (Step 10) passes — Edge Score is genuinely a weighted sum of components
- [ ] CI pipeline runs the math tests on every commit (currently manual)
- [ ] Coverage probe is wired to real DB data (placeholder today)
- [ ] At least one fully-end-to-end run on real Polygon-fetched data has been observed

The first seven you can do in a single session. The last three are wire-up tasks that unblock real production usage.
