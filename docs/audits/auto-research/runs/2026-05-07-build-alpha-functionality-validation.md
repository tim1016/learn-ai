# Build Alpha Functionality Validation — 2026-05-07

**Mode:** `build-alpha-validation-pending`
**Started:** 2026-05-07T22:45:00-06:00
**Ended:** 2026-05-07T23:30:00-06:00
**Stop reason:** complete
**Analyst:** auto-research-tick (claude-sonnet-4-6)

---

## 1. Executive Verdict

**6 of 8 features validated (4 fully, 2 partially). 2 features not yet implemented.**

| Feature | Verdict | Critical issues |
|---|---|---|
| F1 — Strategy Spec + Run Ledger | **validated** | engine_git_commit = "unknown" in container |
| F2 — Signal Catalog | **partially validated** | No spec-layer catalog API; docs-only endpoints |
| F3 — Backtest Results Workbench | **validated** | Exposure calc unit mismatch (P1); display rounds 0.06% → 0% |
| F4 — Walk-Forward / OOS | **validated** | OOS retention not wired; very few trades per fold |
| F5 — Monte Carlo Risk Lab | **validated** | Terminal PnL order-invariant for SetHoldings(1.0); cosmetic concern |
| F6 — Noise / Robustness Tests | **not implemented** | No router, no Python module, no UI |
| F7 — Null Baselines | **partially validated** | B&H shows 0 logged trades; metric direction not labelled |
| F8 — Parameter Sensitivity | **not implemented** | No router, no Python module, no UI |

One P1 numerical finding: `exposure_pct` is computed as `(consolidated 15-min bars held) / (minute-bar equity curve length)`, mixing units by a factor of 15.

---

## 2. Environment and Services Checked

| Item | Value |
|---|---|
| Git branch | master |
| Git commit | cccd6aa3d9553aecef530846ecc981e76c4f1767 |
| Frontend | http://localhost:4200 — healthy |
| Backend (.NET) | http://localhost:5000 — healthy |
| Python service | http://localhost:8000 — healthy |
| Postgres | localhost:5432 — healthy |
| Redis | localhost:6379 — healthy |
| Playwright | Available (MCP tool) — desktop viewport 1440×900 |
| Browser timezone | UTC-6 (CST) — affects date display |

---

## 3. Feature Coverage Matrix

| Feature | Implemented | API endpoint(s) | UI route | Verdict |
|---|---|---|---|---|
| F1 — Run Ledger | Yes | `POST/GET /api/research/strategy-runs` | `/research-lab/backtests/strategy-runs` | validated |
| F2 — Signal Catalog | Partial | `/api/research/features` (docs), `/api/research/indicators` (pandas-ta) | None in research-lab | partially validated |
| F3 — Results Workbench | Yes | `GET /api/research/strategy-runs/{run_id}` | `/research-lab/strategy-runs/{run_id}` | validated |
| F4 — Walk-Forward | Yes | `POST/GET /api/research/strategy-runs/walk-forward` | `/research-lab/walk-forward/{wf_id}` | validated |
| F5 — Monte Carlo | Yes | `POST/GET /api/research/strategy-runs/monte-carlo` | `/research-lab/monte-carlo/{mc_id}` | validated |
| F6 — Noise / Robustness | No | — | — | not implemented |
| F7 — Null Baselines | Yes | `POST/GET /api/research/strategy-runs/baselines` | `/research-lab/baselines/{baseline_id}` | partially validated |
| F8 — Sensitivity | No | — | — | not implemented |

---

## 4. Parameter Matrix Tried

| Parameter | Value used |
|---|---|
| Symbol | SPY |
| Resolution | 15-minute bars |
| Strategy | EMA(5)/EMA(10) crossover + RSI(14) gate 50-70 + 0.20 gap threshold |
| Exit | 5-bar hold (BarsSinceEntry >= 5) |
| Fill model | signal_bar_close |
| Cost model | zero-cost (commission=0, slippage=0) |
| Data window | 2024-01-02 → 2024-12-31 |
| Initial cash | $100,000 |
| Random seed | 0 |
| Walk-forward | Rolling 60d train / 30d test / 30d step |
| Monte Carlo | Reshuffle, 1000 simulations, seed=0 |
| Baselines | buy_and_hold (N=1); random_ema_windows (N=30, seed=42, fast 3-12, slow 10-30) |

---

## 5. Visual Inspection Notes

**Screenshots saved:** `docs/audits/auto-research/snapshots/2026-05-07/`

| Screen | File | Observation |
|---|---|---|
| Research Lab root | `research-lab-initial.png` | Loads at `/research-lab/features/validate` (feature runner). Sub-nav shows Features / Signals / Backtests groups. |
| Strategy-runs list (empty) | `strategy-runs-list-empty.png` | Table shows empty state; "Run SPY EMA fixture" button present. Correct route: `/research-lab/backtests/strategy-runs` (not `/research-lab/strategy-runs`). |
| Strategy-runs list (completed) | `strategy-runs-completed.png` | Row shows: `2026-05-07 22:49 · spy_ema_crossover · SPY · 2024-01-01→2024-12-30 15m · completed`. |
| Run detail — metrics | `run-detail-overview.png` | All metric cards populated, charts visible, trades table populated (16 rows). |
| Run detail — full page | `run-detail-metrics.png` | Full page including walk-forward, Monte Carlo, baselines sections (all with "no results yet" state initially). |
| Walk-forward detail | `walk-forward-detail.png` | OOS aggregates card and fold table visible. |
| Monte Carlo detail | `monte-carlo-detail.png` | Quantile summary cards populated. Equity band chart visible. Warning shown. |
| Baselines detail | `baselines-detail.png` | Six null-distribution cards populated with correct values. |

**Routing gap found:** `/research-lab/strategy-runs` (without `:run_id`) has no matching route and falls through to `/data-lab`. The listing page lives at `/research-lab/backtests/strategy-runs`. No user-facing impact (links are correct) but may confuse direct navigation.

---

## 6. Playwright Screenshots / Snapshots

See section 5 for file paths and descriptions. Key states captured for each implemented surface:

- **F1/F3:** Strategy-runs list (empty state, completed state), run detail page (metrics card, full page)
- **F4:** Walk-forward detail page (OOS aggregates, fold table)
- **F5:** Monte Carlo detail page (quantile summary cards)
- **F7:** Baselines detail page (null distribution cards)

---

## 7. Numerical Trace and Display Parity

### F3 — Backtest Metrics (SPY EMA fixture run `aa7ce95522de`)

| Metric | Python API value | UI displayed | Match |
|---|---|---|---|
| Total return | 0.020076 = 2.0076% | 2.01% | ✓ (display rounding) |
| Max drawdown | 0.007732 = 0.773% | 0.77% | ✓ |
| Sharpe ratio | 2.740995 | 2.74 | ✓ |
| Sortino ratio | 9.095872 | 9.10 | ✓ |
| Profit factor | 5.741685 | 5.74 | ✓ |
| Payoff ratio | 1.325004 | 1.33 | ✓ |
| Win rate | 0.8125 = 81.25% | 81.3% | ✓ |
| Trades (W/L) | 16 (13/3) | 16 (13W / 3L) | ✓ |
| Expectancy | 0.001249 = 0.1249% | 0.12% | ✓ |
| Exposure | 0.000604 = 0.060% | **0%** | ✗ display issue |
| Final equity | $102,007.59 | $102,008 | ✓ |
| Initial cash | $100,000 | $100,000 | ✓ |

**Exposure display issue:** Angular's `PercentPipe` with default `'1.0-2'` format rounds 0.0604% to "0%". The value is correct on the wire; only the display is misleading.

**Exposure calculation P1 bug:** `exposure_pct = bars_held_total / total_bars` where `bars_held_total` counts 15-min consolidated bars (~79) and `total_bars = len(equity_curve)` counts 1-min bars (130,869). This produces 79/130,869 = 0.0006, which is 15× too small. Correct value: ~79 × 15 / 130,869 = 0.91%, or equivalently 79 / (130,869/15) = 0.91%. See finding F-BA-001.

### F4 — Walk-Forward OOS Aggregates (`dc8d870058b3`)

| Metric | API value | UI displayed | Match |
|---|---|---|---|
| Folds | 10 | 10 | ✓ |
| % profitable | 60% | 60% | ✓ |
| Mean OOS Sharpe | 3.4740 | 3.47 | ✓ |
| Median OOS Sharpe | 3.5500 | 3.55 | ✓ |
| Alpha decay | 0.1813/fold | 0.181 per fold | ✓ |
| OOS retention | None | — | ✓ (not wired) |

Fold timestamps verified: fold 0 test_start_ms = 1709355600000 = 2024-03-02 00:00 ET ✓. Fold boundaries are non-overlapping (verified mathematically). Combined OOS curve built only from test windows ✓.

### F5 — Monte Carlo (`aa5d574d9530`)

| Metric | API value | UI displayed | Match |
|---|---|---|---|
| DD P5 | 0.00220 = 0.22% | 0.22% | ✓ |
| DD P50 | 0.00220 = 0.22% | 0.22% | ✓ |
| DD P95 | 0.00363 = 0.36% | 0.36% | ✓ |
| PnL P5 | $2,014.28 | $2,014 | ✓ |
| PnL P50 | $2,014.28 | $2,014 | ✓ |
| PnL P95 | $2,014.28 | $2,014 | ✓ |
| Streak P5 | 1 | 1 trades | ✓ |
| Streak P50 | 1 | 1 trades | ✓ |
| Streak P95 | 2 | 2 trades | ✓ |

### F7 — Baselines (`47b5808017284917`)

| Metric | Parent (EMA) | B&H null | Percentile | Match |
|---|---|---|---|---|
| Sharpe | 2.7410 | 1.8617 | 100% | ✓ |
| Total return | 0.0201 | 0.1705 | 0% | ✓ |
| Max drawdown | 0.0077 | 0.1000 | 0% | ✓ (lower is better — EMA wins) |
| Profit factor | 5.7417 | 0.0000 | 100% | ✓ |
| Win rate | 0.8125 | 0.0000 | 100% | ✓ |
| Expectancy | 0.0012 | 0.0000 | 100% | ✓ |

All UI values match API within display rounding.

---

## 8. Quant Conclusions

### Strategy interpretation

The EMA(5)/EMA(10) crossover with RSI(14) 50-70 gate and 0.20-point gap threshold is highly selective. It fired only 16 times in 2024 — roughly once every 3 weeks. This extreme selectivity produces:

- High in-sample Sharpe (2.74) and win rate (81%) because the gate only admits very clean momentum setups
- Near-zero exposure (correct ~0.91%, displayed as 0%) — the strategy is essentially always flat
- Dramatic underperformance vs buy-and-hold on total return (2.01% vs 17.05%) because it almost never participates

### Pre-market entries

Trades 1, 5, 6, and 12 have entry times of 03:14-03:15 ET, which is pre-market. SPY trades pre-market on ARCA. The strategy spec does not restrict to RTH. Whether pre-market EMA crossovers on 15-min bars are valid research signals (liquidity, price action quality) should be explicitly decided and documented in the spec.

### Walk-forward: noisy Sharpe estimates

With 0-4 trades per fold, per-fold Sharpe ratios are not statistically meaningful. The mean OOS Sharpe of 3.47 is driven by tiny positive returns in short windows. The alpha decay slope (+0.18/fold) is positive (strategy seems to improve over the year) but is constructed from 6 folds with 1-4 trades each — far too few to draw conclusions. The walk-forward validates the machinery is working correctly; it does not validate the strategy.

### Monte Carlo: terminal PnL is order-invariant for full-equity strategies

For a `SetHoldings(fraction=1.0)` strategy, final equity = initial × ∏(1 + r_i) for all round-trips. Because multiplication is commutative, reshuffling trade order cannot change terminal PnL. The result (P5=P50=P95=$2,014) is mathematically correct. Resample with a projection count would yield meaningful path diversity; fractional-equity sizing would make reshuffle meaningful. The warning "only 16 trades — treat as illustrative" fires correctly.

### B&H baseline

B&H returned 17.05% in 2024 vs the EMA strategy's 2.01%. The EMA strategy beats B&H only on Sharpe, max drawdown, and per-unit-of-risk metrics — because it barely participates. This is a valid research verdict: the strategy is not generating alpha; it is essentially an extremely selective hedged position. The null baseline surface correctly surfaces this.

### Signal catalog gap

The build-alpha spec requires a signal catalog that exposes each primitive (EMA, RSI, crossover, hold-exit) with warmup bars, timestamp alignment, and validation status. No such endpoint exists. `/api/research/features` returns 10 documentation blurbs (formula LaTeX, interpretation text). `/api/research/indicators` returns pandas-ta categories. Neither is usable as a machine-readable spec-layer primitive registry. This is a gap between the implemented and specified surface for Feature 2.

---

## 9. Architect Recommendations

Ordered by correctness risk:

**1. Fix exposure_pct unit mismatch (P1 numerical)**
In `PythonDataService/app/research/runs/runner.py:_summarize_metrics()`: multiply `bars_held_total` by `resolution_minutes` before dividing by `total_bars`, or divide `total_bars` by `resolution_minutes` to convert to consolidated bar count.
```python
# Current (wrong): exposure = bars_held_total / total_bars
# Fixed:
exposure = (bars_held_total * resolution) / total_bars
```

**2. Fix Exposure display formatter (P2 display)**
In `run-detail-page.component.html`, change the exposure Percent pipe from `percent` (default 1.0-0) to `percent:'1.2-2'` so 0.0604% renders as "0.06%" rather than "0%".

**3. Fix date display timezone in run listing (P2 display)**
In `strategy-runs.component.html`, `DatePipe` renders `start_ms`/`end_ms` in browser local timezone (CST = UTC-6, causing off-by-one day at the NY midnight boundary). Either pass `'UTC'` as the timezone argument to `DatePipe`, or annotate the column heading to indicate the timezone shown, or display the raw date from the ledger's `strategy_spec_json.start_date` string instead.

**4. Wire OOS retention (P2 completeness)**
`WalkForwardResult.oos_retention` is always `None` because the router doesn't pass the parent run's Sharpe to compare against. The walk-forward router in `research_runs.py` should load the parent run's metrics and compute `oos_retention = wf_result.mean_oos_sharpe / parent_sharpe`.

**5. Add spec-layer signal catalog endpoint (P2 Feature 2)**
Add `GET /api/research/signal-catalog` that returns each supported `StrategySpec` primitive (EMA, RSI, crossover, FreshCross, BarsSinceEntry, etc.) with warmup bars, timestamp alignment, parameter schema, canonical module, and validated_against reference. This powers Feature 2's acceptance gate (catalog exposes EMA/RSI/crossover/hold primitives) and makes the spec-layer self-documenting for researchers.

**6. Document pre-market entry behavior in strategy spec (P3)**
Add a `restrict_to_rth` field or session filter to `StrategySpec`, or add a docstring to the EMA fixture noting that pre-market bars are intentionally included. Currently 4 of 16 trades occur at 03:14-03:15 ET.

**7. Implement Features 6 and 8 (roadmap)**
- Feature 6 (Noise/Robustness): Python module at `PythonDataService/app/research/robustness/`, router at `POST /api/research/strategy-runs/{run_id}/robustness`, UI section in run-detail-page
- Feature 8 (Sensitivity): Python module at `PythonDataService/app/research/sensitivity/`, router at `POST /api/research/strategy-specs/sensitivity`, UI section

---

## 10. Blockers and Unvalidated Areas

| Area | Status | Reason |
|---|---|---|
| Feature 6 (Noise/Robustness) | Not run | Not implemented |
| Feature 8 (Sensitivity) | Not run | Not implemented |
| Random EMA windows baseline (F7) | Pending at time of writing | 30-sample run kicked off via API (background job) — results not yet captured in this report |
| OOS retention value | Not validated | Not wired in router |
| Seed=43 determinism cross-check | Not run | Only seed=0 tested; seed=43 not tried |
| Resample Monte Carlo | Not tested in UI | Only reshuffle tested; resample available via API |
| Engine git commit | "unknown" | Container does not have access to git history |
| F2 signal catalog | No spec-layer endpoint | `/api/research/features` returns doc strings, not primitive registry |

---

## 11. Appendix: Endpoints and Screens Inspected

### API endpoints called

```
GET  http://localhost:8000/api/research/strategy-runs
POST http://localhost:8000/api/research/strategy-runs          (fixture run)
GET  http://localhost:8000/api/research/strategy-runs/aa7ce95522de45fbbf01f35f78c46e05
POST http://localhost:8000/api/research/strategy-runs/walk-forward  (via UI button)
GET  http://localhost:8000/api/research/strategy-runs/walk-forward/dc8d870058b34d3a8ec7d63ebd2a18b7
GET  http://localhost:8000/api/research/strategy-runs/monte-carlo?parent_run_id=aa7ce95522de...
GET  http://localhost:8000/api/research/strategy-runs/monte-carlo/aa5d574d9530407a8367f735046af6d3
GET  http://localhost:8000/api/research/strategy-runs/baselines?parent_run_id=aa7ce95522de...
GET  http://localhost:8000/api/research/strategy-runs/baselines/47b5808017284917becc300e4bf6103b
GET  http://localhost:8000/api/research/features
GET  http://localhost:8000/api/research/indicators
POST http://localhost:8000/api/research/strategy-runs/baselines (random_ema_windows, 30 samples — background)
```

### Frontend routes visited

```
http://localhost:4200/research-lab
http://localhost:4200/research-lab/backtests/strategy-runs
http://localhost:4200/research-lab/strategy-runs/aa7ce95522de45fbbf01f35f78c46e05
http://localhost:4200/research-lab/walk-forward/dc8d870058b34d3a8ec7d63ebd2a18b7
http://localhost:4200/research-lab/monte-carlo/aa5d574d9530407a8367f735046af6d3
http://localhost:4200/research-lab/baselines/47b5808017284917becc300e4bf6103b
```

### Source files read for static analysis

```
PythonDataService/app/routers/research_runs.py
PythonDataService/app/research/runs/runner.py
PythonDataService/app/research/walk_forward/runner.py
PythonDataService/app/research/monte_carlo/runner.py
PythonDataService/app/research/baselines/runner.py
Frontend/src/app/components/research-lab/research-lab.routes.ts
Frontend/src/app/components/research-lab/strategy-runs/strategy-runs.component.ts
Frontend/src/app/components/research-lab/strategy-runs/strategy-runs.component.html
Frontend/src/app/components/research-lab/strategy-runs/run-detail-page/run-detail-page.component.ts
Frontend/src/app/services/strategy-runs.service.ts
```
