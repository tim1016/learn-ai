# Math sources of truth

One row per mathematical concept in learn-ai. Names **the** canonical implementation, calls out every known duplicate, and records what test proves the canonical is right.

Paired with `.claude/skills/learn-ai-validation/SKILL.md` (the Math Provenance Contract) and `.claude/rules/numerical-rigor.md` (the scientific standards). The **skill enforces the contract**; **this file is the lookup** that tells the contract which file to point at.

## How to read this

| Column | Meaning |
|---|---|
| **Concept** | The named math (e.g., "EMA", "Black-Scholes call price"). One row per concept; if the math is parameterized (e.g., EMA period), one row covers all parameterizations. |
| **Canonical** | The **one** file that is source-of-truth. Per root `CLAUDE.md` rule 5, canonical is Python unless explicitly justified otherwise. |
| **Legacy / duplicates** | Every other file in the repo that implements the same math. Each must either (a) call canonical at runtime or (b) carry a parity test naming the canonical file. |
| **Reference** | The external source the canonical was ported from. `references/` path, paper citation, or authoritative URL. |
| **Validated against** | The test that proves canonical ↔ reference equivalence. If no fixture exists yet, write `NONE — pending`. |
| **Status** | `canonical` · `legacy-ok` (duplicate with parity test) · `pending-migration` (rule-5 violation) · `pending-fixture` (canonical but no equivalence proof) · `external-unvalidated` (we trust the vendor) |

## Registry

### Indicators — Python-canonical, ported from LEAN

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| SMA | `PythonDataService/app/engine/indicators/sma.py` | `PythonDataService/app/services/ta_service.py` (pandas-ta passthrough, used by `/api/indicators/calculate`) | `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/Indicators/SimpleMovingAverage.cs` | `PythonDataService/tests/test_indicator_parity.py` | canonical — provenance block pending on sma.py |
| EMA | `PythonDataService/app/engine/indicators/ema.py` | `ta_service.py` (pandas-ta path) | `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/Indicators/ExponentialMovingAverage.cs` | `PythonDataService/tests/test_indicator_parity.py` | canonical — existing docstring cites LEAN; needs 4-field conversion on next touch |
| RSI (Wilders) | `PythonDataService/app/engine/indicators/rsi.py` | `ta_service.py` | `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/Indicators/RelativeStrengthIndex.cs` with `MovingAverageType.Wilders` | `PythonDataService/tests/test_indicator_parity.py` | canonical — existing docstring is already close to the 4-field format |
| MACD, Bollinger Bands | `PythonDataService/app/services/ta_service.py` (pandas-ta) | — | pandas-ta (external) | `PythonDataService/tests/test_indicators.py` | external-unvalidated — no LEAN parity yet; flag if strategy depends on exact match |

### Options pricing and Greeks

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| Black-Scholes price (European call/put) | `PythonDataService/app/services/quantlib_pricer.py` (+ router `app/routers/quantlib_options.py`) | `PythonDataService/app/research/options/bs_solver.py` (retained for IV-solving use case, see below); `Frontend/src/app/utils/black-scholes.ts` (self-marked `[LEGACY]`, UI-render-speed only — **scheduled for removal in Phase 1.3** per `docs/architecture/numerical-authority-migration-plan.md`) | Hull, Options Futures and Other Derivatives (10e), §15.8; QuantLib C++ reference | `NONE — pending` cross-engine parity (QuantLib ↔ `bs_solver.py`); planned in **Phase 1.4** | pending-fixture — three implementations exist today; the TS one is being removed and the remaining two-way parity test is the equivalence guarantee |
| Greeks — Delta, Gamma, Theta, Vega, Rho | `PythonDataService/app/services/quantlib_pricer.py` | `PythonDataService/app/research/options/bs_solver.py::bs_delta` (used by `contract_finder.py`); `Frontend/src/app/utils/black-scholes.ts::bsDelta/bsGamma/bsTheta/bsVega/bsRho` (UI-only — **scheduled for removal in Phase 1.3**) | Hull §19 (Greek Letters); QuantLib | `Frontend/src/app/utils/black-scholes.spec.ts` (self-consistency only, NOT cross-engine); planned cross-engine fixture in **Phase 1.4** | pending-fixture — Frontend spec does not parity-test against QuantLib; will be obsolete once the TS path is removed |
| Normal CDF / PDF | `PythonDataService/app/services/quantlib_pricer.py` (QuantLib internal); `scipy.stats.norm` in `bs_solver.py` | `Frontend/src/app/utils/black-scholes.ts::normCdf` (Abramowitz & Stegun 7.1.26, \|error\| < 1.5e-7) — **scheduled for removal in Phase 1.3** | A&S (1964) 7.1.26 | `NONE — pending` | pending-fixture — TS A&S approx is render-only and will be removed; no separate canonical fixture needed once removal lands |
| Implied volatility (root-finding) | `PythonDataService/app/research/options/bs_solver.py::implied_volatility` | — | Brent's method (scipy.optimize); no external reference beyond Hull §19.11 | `NONE — pending` | pending-fixture — IV is the one concept where `bs_solver.py` is the canonical, not QuantLib |
| IV term-structure interpolation (30-day constant-maturity) | `PythonDataService/app/research/options/iv_builder.py` | — | `docs/math-rigor.md` Upgrade 1 (variance-time interpolation, industry standard) | `NONE — pending` | pending-fixture — currently uses linear-in-σ (known bias per math-rigor.md); variance interpolation is scheduled |
| Risk-free rate | **Hardcoded `r = 0.043`** in `bs_solver.py` and `iv_builder.py` | — | `docs/math-rigor.md` Upgrade 4 proposes FRED (DTB3/DTB4WK/DTB6/DTB1YR) | n/a (constant) | pending-migration — constant, known bias; FRED migration is Upgrade 4 |

### Backtesting engine and statistics

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| Bar consolidation, event replay, fill models | `PythonDataService/app/engine/` (37 files, LEAN-ported) | `Backend/Services/Implementation/BacktestService.cs` — **runs its own `RunSmaCrossover`, `RunRsiMeanReversion`, `RunMomentumRsiStochastic`, `RunRsiReversal` in-process** (scheduled for removal in **Phase 3.2** per migration plan) | LEAN Engine, `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/` (vendored extract) | `PythonDataService/tests/test_strategy_engine.py`, `tests/test_rule_based_backtest_validation.py`, `docs/validation/*.pdf` reports | **pending-migration** — rule-5 violation; cutover sequenced in `docs/architecture/numerical-authority-migration-plan.md` Phase 3 |
| Configurable rule-based strategy runner | `PythonDataService/app/engine/` (post-Phase 4) | `PythonDataService/app/services/rule_based_backtest.py` — **standalone path today**; **scheduled for conversion to a thin adapter in Phase 4** that translates rule-config into `app/engine/strategy/algorithms/*.py` and dispatches via the engine's standard run path | Internal (no external port reference; behavior validated by parity test below) | `PythonDataService/tests/test_rule_based_backtest_validation.py` | **pending-migration** — Phase 4 of migration plan; chosen role is `adapter to app/engine/`, not a separate engine |
| Max drawdown | `PythonDataService/app/engine/` (see `docs/audits/computational-fidelity-2026-04-22.md`) | `Backend/Services/Implementation/BacktestService.cs::CalculateMaxDrawdown` (scheduled for removal in **Phase 3.2**) | Bacon, Practical Portfolio Performance Measurement (2e), §8.2 | `Backend.Tests/Unit/Services/BacktestServiceTests.cs` (service-level, no parity fixture) | pending-migration |
| Sharpe ratio | `PythonDataService/app/engine/` results layer | `Backend/Services/Implementation/BacktestService.cs::CalculateSharpeRatio` (scheduled for removal in **Phase 3.2**) | Sharpe (1994), *The Sharpe Ratio*, Journal of Portfolio Management | `Backend.Tests/Unit/Services/BacktestServiceTests.cs` | pending-migration |
| Replay determinism (same input → same output) | `PythonDataService/app/engine/` | — | Internal invariant | `Backend.Tests/Unit/Services/ReplayDeterminismTests.cs` | canonical-supporting — this test belongs in Python once engine migration completes |

### Strategies (LEAN-ported algorithms)

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| SPY EMA Crossover | `PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py` (+ options variant `spy_ema_crossover_options.py`) | `Backend/Services/Implementation/BacktestService.cs::RunSmaCrossover` (similar intent, not identical algo — scheduled for removal in **Phase 3.2**) | LEAN; TradingView parity: `docs/validation/SPY_EMA_Crossover_RSI.pine`; validation report `docs/validation/SPY_EMA_Crossover_Validation_Report.pdf` | `PythonDataService/tests/test_strategy_engine.py`; TV parity via Pine | canonical — strongest external validation in the repo |
| SPY ORB (Opening Range Breakout) | `PythonDataService/app/engine/strategy/algorithms/spy_orb.py` | — | `docs/validation/SPY_ORB_Strategy.pine`, `docs/validation/SPY_ORB_Strategy_Plan.md`, `docs/validation/ORB_Cross_System_Validation_Report.pdf` | TV Pine + PDF report | canonical |
| QQQ ORB | — | — | `docs/validation/QQQ_ORB_Strategy.pine`, `docs/validation/QQQ_ORB_Validation_Report.pdf` | Pine only | external-validated — Python port not yet started |
| RSI Mean Reversion | `PythonDataService/app/engine/strategy/algorithms/rsi_mean_reversion.py` | `Backend/Services/Implementation/BacktestService.cs::RunRsiMeanReversion` (scheduled for removal in **Phase 3.2**) | LEAN | `PythonDataService/tests/test_strategy_engine.py` | pending-migration |
| SMA Crossover | `PythonDataService/app/engine/strategy/algorithms/sma_crossover.py` | `Backend/Services/Implementation/BacktestService.cs::RunSmaCrossover` (scheduled for removal in **Phase 3.2**) | LEAN | `PythonDataService/tests/test_strategy_engine.py` | pending-migration |
| Momentum RSI/Stochastic | (no Python canonical yet — `Backend/Services/Implementation/BacktestService.cs::RunMomentumRsiStochastic` is the only implementation today) | `Backend/Services/Implementation/BacktestService.cs::RunMomentumRsiStochastic` | LEAN (verify) | `Backend.Tests/Unit/Services/BacktestServiceTests.cs` | **pending-migration** — needs a Python port to `app/engine/strategy/algorithms/` *before* Phase 3.2 deletes the .NET implementation, OR the strategy is dropped if not in use |
| RSI Reversal | (no Python canonical yet — `Backend/Services/Implementation/BacktestService.cs::RunRsiReversal` is the only implementation today) | `Backend/Services/Implementation/BacktestService.cs::RunRsiReversal` | LEAN (verify) | `Backend.Tests/Unit/Services/BacktestServiceTests.cs` | **pending-migration** — same as Momentum RSI/Stochastic above |

### Portfolio / valuation

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| Position mark-to-market valuation (position × live price aggregation) | `Backend/Services/Implementation/PortfolioValuationService.cs::ComputeValuationInternal` (compliant — pure aggregation) | — | Elementary accounting; no external reference | `Backend.Tests/Unit/Services/PortfolioValuationServiceTests.cs` | **pending rule-5 review** — borderline: aggregation arithmetic over persistence data. If/when this computes FX, option theoretical, or unrealized PnL with cost-basis lot selection, the canonical moves to Python. |
| Portfolio reconciliation | `Backend/Services/Implementation/PortfolioReconciliationService.cs` | — | Internal | `Backend.Tests/Unit/Services/PortfolioReconciliationServiceTests.cs` | pending rule-5 review — same reasoning as above |
| Portfolio scenario / what-if (theoretical option value across spot, time, IV grid) | (Python `/portfolio/scenario` endpoint, **scheduled for delivery in Phase 2.1**) | `Backend/Services/Implementation/PortfolioRiskService.cs:50,205` and `PortfolioValuationService.cs:80` — **shock-propagates from stored `EntryDelta` / `EntryVega` / `EntryTheta` instead of recomputing Greeks** | Hull §19 (Greek Letters), put-call parity bounds; canonical Python options authorities `bs_greeks.py` / `quantlib_pricer.py` / `volatility/solver.py` | `NONE — pending` (planned: golden fixture for 3-leg strategy across 5×5 spot/time grid, atol=1e-6 rtol=1e-6 vs hand-derived Hull reference) | **pending-migration** — most technically substantive remaining options-math gap; stale entry Greeks are a correctness bug for scenario research, not a tolerable summary-card heuristic |
| Portfolio live Greeks (current-time delta, gamma, theta, vega per position) | (Python — **scheduled for delivery in Phase 2.1**, recomputed against current spot, time, IV) | `Backend/Services/Implementation/PortfolioRiskService.cs` — currently uses stored `EntryDelta` / `EntryVega` / `EntryTheta` (scheduled for removal in **Phase 2.3**) | Hull §19; `bs_greeks.py` canonical | `Backend.Tests/Unit/Services/PortfolioRiskServiceTests.cs` (service-level, validates aggregation of stale values; will be replaced by Python parity test) | **pending-migration** — Phase 2 of migration plan |
| Portfolio risk (Greek aggregation across positions, post-recompute) | `Backend/Services/Implementation/PortfolioRiskService.cs` (aggregation only — **compliant** once Greeks themselves come from Python passthrough per Phase 2.2) | — | — | `Backend.Tests/Unit/Services/PortfolioRiskServiceTests.cs` | **pending — will be canonical post-Phase 2.2** — aggregating Python-supplied Greeks in .NET is rule-5 compliant; the violation is *computing* the Greeks in .NET |
| Strategy attribution (trade ↔ strategy linking) | `Backend/Services/Implementation/StrategyAttributionService.cs` | — | — | `Backend.Tests/Unit/Services/StrategyAttributionServiceTests.cs` | **not math** — persistence only, compliant |

### Research / divergence pipeline

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| Bar divergence (Polygon vs TradingView) | `PythonDataService/app/research/divergence/analysis/bar_divergence.py` | — | `docs/tv-polygon-validation-gotchas.md`; TV Pine files in `docs/validation/` | `PythonDataService/tests/research/divergence/test_bar_divergence.py` | canonical |
| Trade divergence | `PythonDataService/app/research/divergence/analysis/trade_divergence.py` | — | Internal (reconciliation taxonomy from `reconcile-backtest` skill) | `NONE — pending` | pending-fixture |
| Dividend adjustment | `PythonDataService/app/research/divergence/ingest/dividend_adjuster.py` | — | CRSP methodology (or similar — verify) | `NONE — pending` | pending-fixture — **reference needs verification**, CRSP is a placeholder guess |
| Indicator reliability methodology | `PythonDataService/app/research/indicator_reliability.py` | — | `Frontend/src/assets/docs/indicator-reliability-methodology.md`, `docs/indicator-reliability-methodology.md` | `PythonDataService/tests/research/test_indicator_reliability.py` | canonical |

### Data / transport (rule-5 compliant by design)

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| Technical analysis endpoint fan-out | `Backend/Services/Implementation/TechnicalAnalysisService.cs` | — | n/a (transport) | `Backend.Tests/Unit/Services/TechnicalAnalysisServiceTests.cs` | **not math** — pure HTTP passthrough to Python `/api/indicators/calculate`. Compliant. |
| Market data, bars, aggregates | `Backend/Services/Implementation/MarketDataService.cs` | `PolygonService.cs` (external client) | Polygon.io Aggregates v2 API | `Backend.Tests/Unit/Services/MarketDataServiceTests.cs`, `PolygonServiceTests.cs` | transport — not a math concept |

## External data provenance

Per the contract, external data still needs a provenance row once — it's acceptable provenance to say "we trust the vendor, not independently validated."

| Source | Canonical client | Scope | Notes |
|---|---|---|---|
| **Polygon.io REST (Aggregates v2, Options Chain snapshots)** | `PythonDataService/app/services/polygon_client.py` | OHLCV bars, live options snapshots, tickers | Starter plan: 2-year max history, **15-min delayed**, options snapshots only for live contracts. **external: Polygon.io, not independently validated.** Sanitizer at `app/services/sanitizer.py` enforces gap/monotonicity invariants at the ingestion boundary. |
| **TradingView Pine (strategy validation)** | `docs/validation/*.pine` | Parity reference for SPY EMA Crossover, SPY/QQQ ORB | Used for one-time validation runs; not a runtime data source |
| **LEAN Engine (vendored reference)** | `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/` (vendored 2026-04-26) | Ground-truth for indicators and engine semantics | Pinned by commit `7986ed0aade3ae5de06121682409f05984e32ff7` (master HEAD as of 2026-04-26). See `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/attribution.md` for vendored-files manifest. Regeneration triggers a fixture rebuild with commit-message justification. |
| **FRED (pending)** | — | Risk-free rate for BS solver | Proposed in `docs/math-rigor.md` Upgrade 4; not yet wired |

## Adding a new entry

1. You're adding a new math function. Before you write it, decide: is this a new concept, or a new parameterization of an existing one?
2. New concept → add a row here in the matching section (or create a new section). Fill all six columns. Commit the row in the same PR as the implementation.
3. New parameterization (e.g., new indicator period, new option expiry) → no new row; the existing row already covers it.
4. The `Validated against` field must either name a real test file OR say `NONE — pending` with a follow-up plan in the PR description. `Validated against: manually checked` is not allowed.

## Legacy-debt burn-down rule (not a backfill mandate)

Existing canonical files that predate this registry (EMA, SMA, RSI) already carry first-class prose docstrings citing LEAN but not in the 4-field format. **Do not open a PR that just converts every existing docstring.** Instead, when you touch one of these files for a real reason, convert its docstring in the same PR. This keeps the registry honest without creating churn.

## Known rule-5 non-compliance (tracked here, not lost)

As of 2026-04-26, these are the known cases where the Python-owns-all-math rule is violated. Each is sequenced in `docs/architecture/numerical-authority-migration-plan.md`.

1. **`Backend/Services/Implementation/BacktestService.cs`** — runs four strategies in-process (`RunSmaCrossover`, `RunRsiMeanReversion`, `RunMomentumRsiStochastic`, `RunRsiReversal`) and computes `CalculateMaxDrawdown` / `CalculateSharpeRatio` locally. **Migration: Phase 3** of the migration plan. Cutover sequence: (3.1) make GraphQL `runBacktest` a passthrough to `/api/engine/backtest`; (3.2) delete the in-process strategies and stat helpers. Open question for `RunMomentumRsiStochastic` and `RunRsiReversal`: no Python canonical exists yet — port or drop before Phase 3.2.
2. **`Frontend/src/app/utils/black-scholes.ts`** — client-side pricing and Greeks for UI responsiveness. Already self-labeled `[LEGACY]`, header was upgraded with `@deprecated` and a "no new callers" hard rule on 2026-04-26. **Migration status (2026-04-26):** Phase 1.1 (extend `strategy_engine.py::analyze_strategy` payload) — DONE. Phase 1.2 (rewire `OptionsStrategyLabComponent` to consume server fields, drop BS imports) — DONE. Phase 1.3 (freeze module) — partial: header updated to forbid new callers, but two remaining importers (`strategy-builder.component.ts`, `pricing-lab.component.ts`) need their own migration before the file can be deleted. Phase 1.4 (cross-engine BS parity fixture between `quantlib_pricer.py` and `bs_greeks.py`) — DONE.
3. **`Backend/Services/Implementation/PortfolioRiskService.cs` / `PortfolioValuationService.cs`** — shock-propagate from stored `EntryDelta` / `EntryVega` / `EntryTheta` instead of recomputing Greeks at current spot/time/IV. **Migration status (2026-04-26):** Phase 2.1 (Python `/portfolio/scenario` endpoint) — DONE. Phase 2.2 (`RunScenarioAsync` rewired to call Python via `IPolygonService.PortfolioScenarioAsync`; old EntryVega/EntryTheta shock-propagation deleted) — DONE. Phase 2.3 partial: remaining `ComputeDollarDeltaAsync` and `ComputePortfolioVegaAsync` still pick up entry Greeks for risk-rule evaluation; flagged with STALE-GREEK NOTICE comment in source pointing to `IPolygonService.PortfolioLiveGreeksAsync` follow-up.
4. **`PythonDataService/app/services/rule_based_backtest.py`** — standalone backtest path that duplicates engine semantics. **Migration: Phase 4** of the migration plan. Convert to a thin adapter that delegates execution to `app/engine/`.
5. **Hardcoded `r = 0.043`** in `bs_solver.py` / `iv_builder.py`. **Migration: deferred** — out of scope of the current migration plan; tracked in `docs/math-rigor.md` Upgrade 4 (FRED integration).
