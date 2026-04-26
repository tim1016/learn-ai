# Numerical authority migration plan

**Status:** Active — Phase 0/1/2 shipped; Phase 3 and Phase 4 reformulated (see § Status as of 2026-04-27)
**Owner:** Inkant (single-developer migration)
**Started:** 2026-04-26
**Target window:** 2-3 weeks of focused work
**Calibration:** localhost research project, math rigor first, no operational ceremony beyond what improves reproducibility

## Status as of 2026-04-27

**Shipped:**
- Phase 0 — governance (commit `e52e7c3`): vendored LEAN refs, registry rows added, engine-authority-map.md, this plan, AGENTS.md update.
- Phase 1.1–1.2 — server payload + frontend rewire (commit `451394d`): `OptionsStrategyLabComponent` consumes server `currentCurve` / `greekCurves` / `legDiagnostics`. No client-side BS in that component.
- Phase 1.4 — cross-engine BS parity (commit `451394d` + fix `69d2bfe`): 361/361 cases pass at `atol=1e-10` between `app/services/bs_greeks.py` and `app/services/quantlib_pricer.py` (analytic_bs engine). The `69d2bfe` fix removed an `round(npv, 8)` in `quantlib_pricer.price_option` that was leaking precision into the parity comparison; downstream consumers either round explicitly at their serialization boundary or pass through.
- Phase 2 — portfolio scenario / live-Greeks (commit `d9738a5`): `/api/portfolio/scenario` and `/api/portfolio/live-greeks` endpoints; `IPolygonService.PortfolioScenarioAsync` + `PortfolioLiveGreeksAsync` passthroughs; `PortfolioRiskService.RunScenarioAsync` rewritten; old `EntryVega/EntryTheta` shock-propagation deleted.
- Phase 3 prep (commit `d3c3c18`): `BacktestService.cs` annotated as deprecated.

**Phase 1.3 shipped (2026-04-27).** `Frontend/src/app/utils/black-scholes.ts` header upgraded to `[LEGACY-OK — RENDER-HELPER ONLY, NO NEW CALLERS]`. Two intentional callers documented:
1. `pricing-lab.component.ts` — comparison harness whose explicit purpose is to show the TS pricer alongside the server pricer side-by-side. Removing TS would defeat the comparison.
2. `strategy-builder.component.ts` — interactive leg builder; evaluates BS over a 1200-point grid on every leg edit. Server round-trips per change would defeat the "live builder" UX. Server-side migration is feasible (mirror OptionsStrategyLab's debounced-fetch + parallel-what-if pattern, ~1 hour of focused work + UI smoke test) but deferred until measured UX impact justifies it.

Both callers produce exploratory feedback, not numbers users compare against another number; the canonical authorities `bs_greeks.py` + `quantlib_pricer.py` remain the only math authorities (parity-pinned to `atol=1e-10`).

**Phase 2.3 shipped (2026-04-27).** `ComputeDollarDeltaAsync` + `ComputePortfolioVegaAsync` in `PortfolioRiskService.cs` switched from stored `EntryDelta`/`EntryVega` to `IPolygonService.PortfolioLiveGreeksAsync` (commit `334d419`). Stocks short-circuit to delta=1 without hitting Python. `ComputePortfolioVegaAsync`'s GraphQL resolver doesn't take prices, so the method fetches underlying spots itself via `FetchStockSnapshotsAsync` (one batched call). Test rewrites at `Backend.Tests/Unit/Services/PortfolioRiskServiceTests.cs` mock the live-Greeks calls; the math under test is unchanged (same DollarDelta = 328,250 and totalVega = 150) but the source shifts from stored to recomputed. 7/7 tests pass.

**Phase 3 deferred — structural blocker.** Phase 3 as originally written assumed `runBacktest` becomes a passthrough to `/api/engine/backtest`. Investigation on 2026-04-27 found:
1. Python's newer engine (`app/engine/strategy/algorithms/`) ports only 2 of the 4 strategies the .NET path runs (sma_crossover, rsi_mean_reversion). `RunMomentumRsiStochastic` and `RunRsiReversal` exist only in .NET and in the older `app/services/strategies/` (function-based, pandas-ta) registry — the older registry is not exposed via `/api/engine/backtest`.
2. The `runBacktest` mutation has two consumers: Strategy Lab (deprecated UI) AND `lean-engine` (its eventual replacement). `lean-engine` currently re-imports `ReplayChartComponent`, `ReplayControlsComponent`, `LeanStatisticsComponent` from inside `strategy-lab/`, and calls `marketData.runBacktest(...)` itself. So lean-engine is **not yet self-sufficient** to replace strategy-lab.

The cleanest end state — confirmed by user 2026-04-27 — is **lean-engine fully replaces strategy-lab**, at which point the four .NET strategies + Strategy Lab UI + the older `app/services/strategies/` strategy files all delete together. The current state is intermediate. Phase 3 is deferred until `lean-engine` reaches feature parity with Strategy Lab; Phase 3.0 (the deprecation comment in `BacktestService.cs`) remains the only Phase 3 work shipped on this branch.

**Phase 4 needs reformulation.** Phase 4 as originally written said "thin adapter to `app/engine/strategy/algorithms/*.py`" for `rule_based_backtest.py`. Investigation on 2026-04-27 found that `rule_based_backtest.py` is **not a strategy implementation** — it is a **configurable rule engine** (271 lines): composable entry conditions (EMA crossover, RSI band, ADX filter, gap filter), multiple exit modes (fixed-bar, indicator-based), parameterized via JSON. The newer engine has only **fixed strategies** (each algorithm is a `Strategy` subclass with hardcoded rules — `SpyEmaCrossoverAlgorithm`, etc.), so there is no equivalent "configurable rule engine" to delegate to. Reformulated options:
- Build a new `RuleBasedAlgorithm(Strategy)` subclass that takes config and dispatches — meaningful new code, not an adapter conversion.
- Map specific configurations to specific fixed algorithms — fragile because `rule_based_backtest` has more parameters than any single fixed strategy.
- Accept `rule_based_backtest.py` as a permanent alternate engine, document the divide, and cite parity tests against `app/engine/` for the configurations both can run.

Live consumers of `rule_based_backtest`: `Backend/GraphQL/Mutation.cs:704 runRuleBasedBacktest`, `PythonDataService/app/routers/jobs.py:91 POST /backtest` (Redis-backed async job), `Frontend/src/app/services/market-data.service.ts:931 runRuleBasedBacktest`. 638 lines of validation tests at `tests/test_rule_based_backtest_validation.py`. Dropping is not an option without UI consequences.

Phase 4 is therefore deferred pending a design decision on which of the three reformulated options to take.

## Why this plan exists

The repo's stated rules (`AGENTS.md` § Python owns all math, `.claude/rules/numerical-rigor.md`, `docs/architecture/options-math-authorities.md`) declare a clean split: Python is the sole numerical authority, `.NET` is transport, Angular is rendering. The actual code still violates that split in three places that have already been identified and triaged — they just haven't been sequenced into a single migration arc.

The artifacts that already encode the destination:

- `docs/math-sources-of-truth.md` — concept-level registry naming canonical, legacy/duplicates, reference, validating test, and status. Already lists most of the rule-5 violations as `pending-migration`.
- `docs/engine-phase-1-2-refined-plan.md` — declares Engine Lab as the single home for backtesting and Strategy Lab as deprecated.
- `Frontend/src/app/components/strategy-lab/strategy-lab.component.html` — surfaces the deprecation in the live UI.
- `Frontend/src/app/utils/black-scholes.ts` — module header self-labels `[LEGACY]`.
- `docs/architecture/options-math-authorities.md` — names canonical options math modules and explicitly forbids math in C# or TS.
- `docs/architecture/backtesting-engine-grounding-2026-04-26.md` — diagnostic audit that motivated this plan.

This plan does **not** introduce new direction. It closes the gap between stated rules and on-disk state in the order that best supports a one-developer localhost workflow.

## Out of scope (deliberately)

These are reasonable items for a future date and explicitly excluded from this migration:

- EF Core migrations replacing `EnsureCreated()` in `Backend/Program.cs`.
- Structured-startup-logging replacing `Console.WriteLine` in `Backend/Program.cs`.
- Pydantic v1 / FastAPI deprecation warnings as a tracked validation program.
- Angular component-decomposition as a parallel workstream (will resolve naturally as math moves server-side).
- No-arbitrage invariant suites for the options stack (defer until authority consolidation is complete).

These are infrastructure hygiene, not numerical-trust blockers.

## Phase 0 — Governance (1-2 days, parallelizable)

The cheapest, highest-leverage work. Removes the inconsistency between stated rules and on-disk state. No engineering risk.

### 0.1 Vendor LEAN reference snapshots

`references/` currently contains only `.gitkeep`. `AGENTS.md` ranks `references/` as authority rank 1, ahead of official docs and rules. Pin the LEAN commit(s) the existing parity tests were derived from.

- For each indicator with a `pending-fixture` or `canonical` status in `math-sources-of-truth.md` that cites LEAN: identify the exact LEAN commit SHA, vendor the relevant `Indicators/*.cs` file (or a minimized legally-safe extract) into `references/lean/<commit-sha>/Indicators/`, with a sibling `attribution.md`.
- Same for any options-stack reference (Hull chapters as paper-citation rows, QuantLib snippets where math is ported rather than called).
- Pair each vendored subtree with the existing `docs/references/<construct-name>.md` note. Do not replace the docs/references notes — they are the explanation, the vendored subtree is the frozen source.

**Exit criteria:** `references/` contains at least one vendored LEAN subtree with a commit SHA in the path. Every row in `math-sources-of-truth.md` whose `Reference` column says "pin commit in `references/`" has its commit pinned.

### 0.2 Fill gaps in `math-sources-of-truth.md`

The registry is sound but incomplete. Add the missing rows:

- **Portfolio valuation and scenario math.** Rows for the .NET stale-entry-Greek path (`PortfolioValuationService.cs:80`, `PortfolioRiskService.cs:43`, `:205`). Status: `pending-migration`. Canonical target: a new Python endpoint defined in Phase 2.
- **`rule_based_backtest.py` role.** Currently has no row. Decide one of: adapter-to-engine, validation-only utility, supported-secondary-engine. Document chosen role with a row in `math-sources-of-truth.md` and a status of `pending-migration` or `canonical-supporting`.
- **Black-Scholes price / Greeks consolidation status.** The existing row notes three implementations (`quantlib_pricer.py`, `bs_solver.py`, `black-scholes.ts`). Tighten to reflect Phase 1 outcome: TS stops being a math authority, becomes render-only.

**Exit criteria:** `math-sources-of-truth.md` has no math concept used in production code that lacks a row. `pending-migration` rows have a target sibling and an owning phase number from this plan.

### 0.3 Write `docs/architecture/engine-authority-map.md`

A one-page companion to `math-sources-of-truth.md`. Concept-level registry exists; engine-level map does not. Single table answering:

- Which engine owns interactive backtests?
- Which engine owns research signal scoring?
- Which engine owns options strategy analysis?
- Which engine owns portfolio scenario / live-Greeks?
- Which paths are deprecated?
- Which paths are validation-only?

Cite by file:line. No prose ADR — just the table and a 2-paragraph "how this relates to math-sources-of-truth.md" preamble.

**Exit criteria:** Doc exists and is linked from `AGENTS.md`. Every engine path in the repo (Engine Lab, `BacktestService.cs`, `rule_based_backtest.py`, `app/research/signal/backtest.py`, `strategy_engine.py`, the .NET portfolio services) appears in the table with an explicit role.

## Phase 1 — Options math cutover (~1 week)

Finish the already-declared move of options math out of TypeScript. Use the existing server foothold (`strategy_engine.py::AnalyzeOptionsStrategy`); do not greenfield.

### 1.1 Extend `AnalyzeOptionsStrategy` payload

Today `strategy_engine.py:830` returns payoff-at-expiry, POP, expected value, and aggregate Greeks. The Angular `OptionsStrategyLabComponent` still computes additional fields client-side. Extend the response to cover everything the component currently computes in TS:

- Current-time P&L curve (per-spot grid, today's vol surface).
- What-if curve(s) (user-selected spot/time/IV shifts).
- Greek curves (delta/gamma/theta/vega per-spot grid).
- Diagnostic table rows (per-leg current value, per-leg current Greeks).

All using `bs_greeks.py` / `quantlib_pricer.py` / `volatility/solver.py` — no new options math.

**Exit criteria:** A single GraphQL query returns the complete payload required to render the Strategy Lab page with zero client-side options math.

### 1.2 Rewire `OptionsStrategyLabComponent`

Consume the new server fields. Remove the corresponding computation code at `options-strategy-lab.component.ts:372`, `:438`, `:527`. Component shrinks to leg editing, request state, chart/table rendering.

**Exit criteria:** `options-strategy-lab.component.ts` imports nothing from `Frontend/src/app/utils/black-scholes.ts`. No `bsPrice` / `bsDelta` / `normCdf` calls in the component or its children.

### 1.3 Freeze `Frontend/src/app/utils/black-scholes.ts`

After 1.2, the only callers (if any) should be UI helpers that don't produce numbers users compare. Either:

- Delete the file entirely if no callers remain, OR
- Keep with a header that explicitly states "render-helper-only — no callers in code paths that produce numbers users compare against another number" and a `@deprecated` JSDoc tag on every exported function.

**Exit criteria:** `grep -r "from.*black-scholes" Frontend/src` returns either zero results, or only files annotated as render helpers in their own headers.

### 1.4 Update registry

Update `math-sources-of-truth.md` rows for Black-Scholes price, Greeks, normCdf, and IV solver:

- Move the TS implementations out of the `Legacy / duplicates` column into a `Removed` note, or mark them as render-helper-only.
- Update `Status` from `pending-fixture` to `canonical` if the cross-engine parity test is also added (see 1.5).

### 1.5 Add the three-way parity fixture

Pre-existing TODO in `math-sources-of-truth.md`: cross-engine parity for Black-Scholes price across `quantlib_pricer.py`, `bs_solver.py`, and (for as long as it exists) `black-scholes.ts`. Once 1.3 freezes the TS file, the parity test becomes two-way and serves as the equivalence guarantee for the consolidation.

Fixture lives at `PythonDataService/tests/fixtures/golden/bs-price-cross-engine/` per `.claude/rules/numerical-rigor.md` § Golden fixtures.

**Exit criteria:** Test exists, runs in CI-equivalent local test command, and asserts `atol=1e-9, rtol=0` between QuantLib and `bs_solver.py`.

## Phase 2 — Portfolio scenario / live-Greeks to Python (~1 week)

Most technically substantive item. Stale entry-Greek shock propagation in `.NET` becomes a correctness bug the moment the page is used for scenario research, not a tolerable summary-card heuristic.

### 2.1 Design Python `/portfolio/scenario` endpoint

One endpoint that takes a position list (contracts + quantities + entry prices) and a scenario specification (spot grid, time grid, IV shifts) and returns:

- Theoretical option value at each scenario point, recomputed from current contract metadata + scenario inputs (not from entry Greeks).
- Greek decomposition at each scenario point, recomputed.
- Aggregate portfolio P&L per scenario point.
- Per-leg breakdown for diagnostic display.

Reuses existing options authorities. No new math.

**Exit criteria:** Endpoint exists, golden fixture covers a 3-leg options strategy across a 5×5 (spot, time) scenario grid, parity test against a hand-derived Hull-Eq reference at `atol=1e-6, rtol=1e-6` (Greeks tolerance per `.claude/rules/numerical-rigor.md`).

### 2.2 Rewire `.NET` portfolio services as passthroughs

`PortfolioValuationService.cs` and `PortfolioRiskService.cs` become typed-HttpClient passthroughs. They aggregate, persist, and serialize. They do not synthesize theoretical option values from entry Greeks.

**Exit criteria:** No `EntryDelta * spotShock` (or analogous) arithmetic in either file. All numbers in the GraphQL portfolio scenario response trace back to the Python endpoint via `decimal`-preserving passthrough.

### 2.3 Delete dead arithmetic

After 2.2, the entry-Greek shock-propagation helpers in the .NET services have no callers. Remove them.

**Exit criteria:** Files compile with the dead helpers removed. Backend.Tests passes.

### 2.4 Update registry

`math-sources-of-truth.md` rows for portfolio valuation, scenario analysis, and live Greeks move from `pending-migration` to `canonical` (Python) with the .NET path listed under `Legacy / duplicates` as `removed`.

## Phase 3 — Retire `.NET BacktestService` math (~3 days)

Closeout migration. Engine Lab is already canonical, Strategy Lab UI is already deprecated, the authority map (Phase 0) and registry already say so.

### 3.1 Passthrough `runBacktest` mutation

`Backend/GraphQL/Mutation.cs:98` currently calls `BacktestService.RunBacktestAsync` which dispatches into in-process C# strategies. Make it call the Python `/api/engine/backtest` endpoint and return the response with `decimal`-preserving shape.

**Exit criteria:** `runBacktest` produces the same response shape as before, but the numbers come from Python.

### 3.2 Delete in-process strategies

`BacktestService.cs:39` dispatch table and the `RunSmaCrossover` / `RunRsiMeanReversion` / `RunMomentumRsiStochastic` / `RunRsiReversal` implementations have no callers after 3.1. Remove them.

### 3.3 Delete local statistic helpers

`CalculateMaxDrawdown` / `CalculateSharpeRatio` in `BacktestService.cs` are no longer reachable. Remove them.

**Exit criteria:** `BacktestService.cs` is either deleted entirely or reduced to ~30 lines of HTTP passthrough plus persistence.

### 3.4 Update registry

`math-sources-of-truth.md` rows for max drawdown, Sharpe ratio, SPY EMA Crossover (.NET row), RSI Mean Reversion (.NET row), SMA Crossover (.NET row) move from `pending-migration` to `canonical`. The .NET entries move to a `Removed` note or are deleted from the row entirely.

## Phase 4 — Disambiguate `rule_based_backtest.py`

Independent of the sequencing above; can run anytime after Phase 0.

### 4.1 Decide fate

Recommended path: **adapter to `app/engine/`**. The configuration shape `rule_based_backtest.py` accepts is useful for some callers; the execution should delegate to the LEAN-ported engine. This keeps one execution authority while preserving the ergonomic surface.

If a different role is chosen (validation-only, supported-secondary-engine), record the choice in this plan.

### 4.2 Implement adapter

`rule_based_backtest.py` becomes a config translation layer that constructs the appropriate `app/engine/strategy/algorithms/*.py` instance and dispatches via the engine's standard run path. Local strategy implementations in `rule_based_backtest.py` are deleted.

### 4.3 Update registry

Row added to `math-sources-of-truth.md` reflecting adapter status. Validation test asserts that the same configuration produces the same trades when run via `rule_based_backtest.py` versus directly via the engine.

## Cross-cutting acceptance criteria

After all phases:

- `references/` has at least one vendored subtree with a commit SHA in its path.
- `math-sources-of-truth.md` has no `pending-migration` rows except for known future work explicitly out of scope of this plan.
- `grep -r "RunSma\|RunRsi\|RunMomentum" Backend/` returns zero results.
- `grep -r "from.*black-scholes" Frontend/src/app/components` returns zero results in non-render-only files.
- `grep -r "EntryDelta\|EntryVega\|EntryTheta" Backend/Services/Implementation/Portfolio*.cs` returns only persistence reads, never arithmetic.
- A single architecture doc (`docs/architecture/engine-authority-map.md`) answers "which engine owns X" for any X.

## Risk and rollback

- **Phase 1.2 risk:** The new server payload could be slower than client-side computation for highly interactive UIs. Mitigate by ensuring the Python endpoint runs vector ops over the scenario grid (no per-point Python loop) and by adding a response-time assertion to the test for 1.1.
- **Phase 2.1 risk:** A scenario grid recomputed against current vol surface depends on having a current vol surface available for every contract in the position. If the vol surface is stale, Greeks will be off in a way that is harder to detect than entry-Greek staleness. Mitigate by surfacing the surface timestamp in the response and rendering it in the UI.
- **Phase 3.1 risk:** GraphQL response shape drift between .NET assembly and Python passthrough. Mitigate by snapshot-testing the response shape before and after the cutover.
- **Rollback path:** Each phase ends with the registry in a consistent state, so partial completion is acceptable. The .NET legacy paths can stay around as a fallback for one phase boundary if needed; do not delete them in the same PR that introduces the new path.

## Sequencing summary

| Week | Phase | Owner | Status |
|---|---|---|---|
| 1 (days 1-2) | Phase 0 — governance | Inkant | **shipped 2026-04-26** (`e52e7c3`) |
| 1 (days 3-7) | Phase 1 — options math cutover | Inkant | **shipped** — 1.1/1.2 (`451394d`), 1.3 (header `legacy-ok` for pricing-lab + strategy-builder, see § Status as of 2026-04-27), 1.4 (`451394d` + fix `69d2bfe`) |
| 2 | Phase 2 — portfolio scenario / live-Greeks | Inkant | **shipped** — 2.1/2.2 (`d9738a5`), 2.3 (`334d419` switches `ComputeDollarDelta` + `ComputePortfolioVega` to live Greeks via `PortfolioLiveGreeksAsync`) |
| 3 (days 1-3) | Phase 3 — retire BacktestService math | Inkant | **deferred** — blocked on lean-engine reaching feature parity with Strategy Lab. Phase 3.0 deprecation comment shipped (`d3c3c18`). See § Status as of 2026-04-27. |
| 3 (days 4-5) | Phase 4 — `rule_based_backtest.py` adapter | Inkant | **deferred** — original "thin adapter" plan doesn't fit the actual code shape; needs reformulation. See § Status as of 2026-04-27. |

Phase 4 can move earlier and run in parallel with Phase 1 or 2 if convenient — it has no dependencies after Phase 0.
