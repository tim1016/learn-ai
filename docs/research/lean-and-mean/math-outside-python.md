# Math outside Python — inventory and exceptions (#2747)

Part of map #2700. Read at **`6a4d7d396108ef16471d8df888b9ded74d3c2892`** (`origin/master`, 2026-09-30). Paths are repo-relative.

**Method.** I checked the dead-code lists first: `dead-frontend-broker` (#2708), `dead-frontend-rest` (#2709), `backend` (#2710), `dead-cascade` (#2744) and entry 5 of `adrs-owed` (#2745). Then I swept the source:

- `Backend/` for `Math.*`, `Sum`/`Average`, rate literals and model expressions.
- `Frontend/src/` (non-spec) for `Math.sqrt|exp|log|pow`, `reduce` sums, `/365`, `*252`, grading rules, percentiles and rate literals.
- every tracked non-Python source outside the three stacks.

Liveness comes from caller search (`grep -rn`, `git grep`) plus the route table and `shell/app-menu.ts`. `docs/math-sources-of-truth.md` was used only as a lead.

**Bar.** Math means a number someone reads as an answer. Display formatting, layout arithmetic, counts and unit conversion for display are left out. "Parity test" means a test that compares the copy to the Python canonical; unit tests with hand values do not count.

## The answer in one paragraph

Outside the dead Portfolio stack, very little math lives outside Python.

- **One copy has a real reason.** The Strategy Builder re-runs Black-Scholes in the browser over a 1,200-point grid on every leg edit, which is a latency reason. Only its price kernel has a parity test.
- **One small rule is layer-local:** the whole-cent check on the loss cap.
- **The .NET FIFO engine is not a live exception.** The map names it as one, but it is reachable only from the Portfolio page, which ☆ rules dead. Neither #2710 nor #2744 took that cascade. It also has no test against Python, only unit tests.
- **The .NET Sortino, CAGR and Calmar are not copies.** Sortino and Calmar use different formulas from `statistics.py`, and no CAGR exists in .NET.
- **Some live math has no reason to be in the browser:** research verdict grades, null-distribution quantiles and the Edge pages' synthetic volatility. Those become follow-ups.

## Inventory

Verdicts: **Exception** (real reason + parity test), **Exception, parity owed**, **Dead** (owning list), **No reason** (follow-up issue). "Portfolio cascade" means reachable only from `/portfolio`, which is #2709 B1 and dead under ☆ ("Portfolio … included").

### Backend (.NET)

| # | `file:line` | Computes | Live? | Python canonical | Parity test | Reason | Verdict |
|---|---|---|---|---|---|---|---|
| B1 | `Backend/Services/Implementation/PositionEngine.cs:43,87,179-226,229-253` | FIFO lot close, realized P&L per lot, net qty, weighted-avg cost basis | **Dead.** It is reached only through `PortfolioMutation.cs:136` and `PortfolioQuery` roots. Every portfolio GraphQL root's only Frontend caller is `services/portfolio.service.ts` and `components/portfolio/**` (`grep -rlw` per root; `getAccount` hits are the unrelated REST `brokers.getAccount`, per #2710 A27) | `PythonDataService/app/broker/alpaca/clerk/fifo_pnl.py::apply_fill_to_lots` (the same concept on the Alpaca ledger) | **None.** `Backend.Tests/Unit/Services/PositionEngineTests.cs` (7 unit tests) and the in-app `PortfolioValidationService` self-check are not parity | Layer-locality (EF/Postgres lots inside DbContext transactions, `PositionEngine.cs:20-27`): a real reason | **Dead (Portfolio cascade, no owner yet).** If the owner keeps Portfolio: **Exception, parity owed** |
| B2 | `SnapshotService.cs:84-154` `ComputeMetrics` (Sharpe `:108`, Sortino `:111-113`, Calmar `:126-131`, win rate and profit factor `:134-139`), `:181-204` drawdown series, `:206-213` sample stdev | Portfolio Sharpe, Sortino, max DD, Calmar, win rate, profit factor, total return | **Dead:** `getPortfolioMetrics` and `getDrawdownSeries` are Portfolio cascade | `app/engine/results/statistics.py::_sharpe`, `::_sortino` (`:278`), `::_max_drawdown`, Calmar/CAGR (`:609-616`) | None (provenance says "Validated against: NONE", `SnapshotService.cs:78`) | "Avoid round-tripping equity curves" (`:76-77`). That is a convenience, not a reason | **Dead (Portfolio cascade).** If kept: **No reason.** It is not a copy: the Sortino here is the sample stdev of the negative returns, while the canonical uses downside RMS over all N (`statistics.py:285`). Calmar uses mean×252, while the canonical uses geometric CAGR (`:613`). There is no .NET CAGR at all |
| B3 | `PortfolioValuationService.cs:58-110` (`:79-80`) | Market value, cost basis, unrealized P&L, equity | Dead (Portfolio cascade; also feeds B2 and B4) | None for this ledger | None | Layer-locality | **Dead (Portfolio cascade)** |
| B4 | `PortfolioRiskService.cs:44-123` dollar delta (`:60,107`), `:125-193` portfolio vega (`:188`), `:195-275` risk rules (drawdown `:224`, concentration `:235`), `:277-385` scenario value (`:342`) | Greek aggregation and risk-rule checks; per-leg Greeks and prices come from Python `/api/portfolio/scenario` | Dead (Portfolio cascade) | `app/services/portfolio_scenario.py` (leg math) | None | Aggregation only | **Dead (Portfolio cascade)** |
| B5 | `PortfolioReconciliationService.cs:23-117` (`:65`, 0.01 tolerance) | Cached vs rebuilt realized P&L | Dead (Portfolio cascade) | None | None | — | **Dead (Portfolio cascade)** |
| B6 | `PortfolioValidationService.cs` (828 lines; e.g. `:657`, `:778`) | Runtime self-test of FIFO, valuation and risk with tolerances | Dead (`runPortfolioValidation` is Portfolio cascade) | — | — | — | **Dead (Portfolio cascade)** |
| B7 | `PortfolioService.cs:271` | `TotalRealizedPnL` sum | Dead | — | — | — | **Dead.** #2710 B29 already lists the field; the method dies with the cascade |
| B8 | `GraphQL/Query.cs:199-217` `AggregatesSummary` | Period high/low, avg volume, avg VWAP, price change and % change | Root live (`getOrFetchStockAggregates`), but **no reader of these fields.** `market-data.service.ts:44-45` selects them, and no template or TS reads `summary.*`. Strategy Builder recomputes high, low and avg volume itself (`strategy-builder.component.ts:276-292`) | None | None | None | **Dead (unread output fields), on no list.** It belongs with #2710's B28-style field cuts |
| B9 | `MarketDataService.cs:327-397` `DetectGaps` (`:353-354` 390 or 7 bars/day, `:383-385` coverage %) | Weekday count, expected bars, coverage % | Root live, but **`gapDetection` has no reader** (`market-data.service.ts:47-50` selects it, and no template or TS reads it) | Calendar module `expected_sessions`, plus data-lake coverage | None | None | **Dead (unread output), on no list** (as B8). If kept: **No reason.** Its hardcoded session length and weekday calendar break the calendar authority (it ignores holidays and half-days) |
| B10 | `Models/MarketData/Quote.cs:29,34` | Spread, mid | Dead | — | — | — | **Dead:** #2710 B19–B23 (the `Quotes` entity drop) |

### Frontend (TypeScript)

| # | `file:line` | Computes | Live? | Python canonical | Parity test | Reason | Verdict |
|---|---|---|---|---|---|---|---|
| F1 | `Frontend/src/app/utils/black-scholes.ts:69,90,98,112,128` (`normCdf`, `normPdf`, `bsD1`, `bsD2`, `bsPrice`) | BS price kernel (A&S 7.1.26 normal CDF) | Live: Strategy Builder (via `strategyPnlAtPrice`) and Pricing Lab | `app/services/bs_greeks.py::bs_european_price` | **`Frontend/src/app/utils/black-scholes.parity.spec.ts`** (360 cases, `atol=1e-4`, `rtol=0`, fixture `src/testing/bs-parity/grid.json`) | Latency: a 1,200-point grid is re-priced on every leg edit (`black-scholes.ts:28-34`) | **Exception** (see hazard H2) |
| F2 | `black-scholes.ts:158,178,191,221,234` (Greeks), `:273` `strategyPnlAtPrice`, `:299` `strategyGreekAtPrice` | Delta, gamma, theta (per day), vega and rho (per 1%); strategy P&L and Greek on a grid | Live (Strategy Builder; Pricing Lab uses the five Greeks) | `bs_greeks.py::black_scholes_greeks` (same units, checked: `bs_greeks.py:23-25,109`) | **None.** `black-scholes.spec.ts` checks Hull hand values to 2–3 dp | Latency (as F1) | **Exception, parity owed** |
| F3 | `strategy-builder.component.ts:588-593` net cost, `:595-603` DTE and T, `:675-708` expiry payoff and breakevens, `:711-850` T+0 and what-if curves, Greek curve, live Greeks | Interactive strategy curves | Live (`/options-lab/strategy-builder`, linked from `options-lab.component.html:16`) | `app/services/strategy_engine.py::analyze_strategy`, `::find_breakevens` (`:76`) | None | Latency (as F1) | **Exception, parity owed.** One exception with F1 and F2 (see H3, H4) |
| F4 | `black-scholes.ts:255` `lognormalCdf` | Lognormal CDF | Dead | — | — | — | **Dead:** #2709 C14 (◇ keeps it out) |
| F5 | `strategy-builder.component.ts:618-627` `weightedIv`; `shared/payoff-chart/payoff-chart.component.ts:32,34` inputs `weightedIv` and `riskFreeRate` | Premium-weighted IV | **Dead:** the chart declares both inputs but never reads them (`grep` finds only the declarations). They are left over from the `lognormalCdf` probability shading | — | — | — | **Dead, on no list.** It is a cascade of #2709 C14 and belongs on #2744 |
| F6 | `components/pricing-lab/pricing-lab.component.ts:255-260` (TS column: price and five Greeks), `:193` (default diff reference `legacy_bs`), `:275-300` (max and avg diff) | TS pricer as one compared engine | Live (`app-menu.ts:60`) | `bs_greeks.py`, `quantlib_pricer.py` | Price only (F1) | Self-referential: the column exists to compare the TS pricer (`black-scholes.ts:20-26`). That is none | **No reason → follow-up** (H5) |
| F7 | `components/edge/services/edge-mock-data.service.ts:95-330` (RV close-close and Parkinson `:145-151`, forward RV `:179`, z-score `:184`) | Seeded synthetic candles, realized vol, IV30, regimes | **Live:** the Edge landing (`edge.component.ts:54,66`), Cross-Asset (`cross-asset.component.ts:31`, average `:49`) and Regimes (`regimes.component.ts:34`) render only this. Realized-vs-IV shows it until Compute, tagged "synthetic" (`realized-vs-iv.component.html:189`), then merges real data over it (`.ts:155`). All are linked from `app-menu.ts:70-73` | Python realized-vol and IV30 modules (◇) | None | None (a placeholder) | **No reason → follow-up** (H6) |
| F8 | `components/research-lab/feature-report/feature-report.component.ts:245-330` | Statistical, stability and sample-depth letter grades (IC, t-stat critical values 1.645/1.96/2.576, sign consistency, OOS retention, effective N) | Live (host `feature-runner.component.html`) | **None:** the browser is the only implementation of these research verdicts (ADR owed #10) | None | None | **No reason → follow-up** |
| F9 | `components/research-lab/indicator-reliability/indicator-reliability.component.ts:525-538` (IS and OOS severity), `:934-940` rolling mean, `:950-951` mean daily IC | Verdict severities; IC chart overlays | Live (`getIsSeverity` and `getOosSeverity` are not among #2709 C24's dead helpers) | `app/research/indicator_reliability.py:398-410` (the same OOS thresholds) | None | None | **No reason → follow-up** (with F8; see H7) |
| F10 | `components/research-lab/baselines/baselines-detail-page/baselines-detail-page.component.ts:122-153` | Null-distribution p5, p50 and p95 (a port of numpy linear percentile) | Live | `numpy.percentile` in the baselines service (the server sends `empirical_percentile`, but not these three) | None | None | **No reason → follow-up** |
| F11 | `components/brokers/alpaca-desk/configuration/configuration-revision-draft.ts:241-248` `wholeCentLossCap` | Whole-cent check with a 4-ULP noise allowance | Live (Alpaca settings) | `app/broker_configuration/envelope.py:252` `require_whole_cent_loss_cap` (same rule: `4·EPSILON·2^⌊log2⌋` equals 4 ULP) | None shared. Each side has its own cases (`configuration-revision-draft.spec.ts:186`; `test_account_risk_routes.py::test_canonical_binary_noise_loss_cap_is_accepted_at_the_boundary`) | Layer-locality: inline form feedback before submit; Python still rejects at the boundary | **Exception, parity owed** |
| F12 | `components/broker/broker-options-chain/broker-options-chain.component.ts:408-418` mid (reprice trigger); `broker-options-surface.component.ts:566` mid; `options-lab/chain/options-lab-chain.component.ts:179-185` distance % and call/put-average "center IV" | Quote mid; an ATM-IV proxy | Live (IBKR read-only feed pages; the feed itself is sacred and untouched) | None named | None | Trivial; none | **No reason → one low-priority follow-up** |
| F13 | `shared/indicator-picker/indicator-picker.preview.ts:44` `ema` | EMA over a synthetic sine, used for the picker's icon | Live | — | — | — | **Not math** (decorative; nobody reads the number) |
| F14 | `components/lean-engine/metric-grade.util.ts`; `components/portfolio/**` | Metric grades; the Portfolio UI (unit conversion only, e.g. `scenario-explorer.component.ts:51-52`) | Dead | — | — | — | **Dead:** #2709 A4 and #2709 B1 |

### Other non-Python code

| # | Path | Computes | Live? | Verdict |
|---|---|---|---|---|
| O1 | `PythonDataService/app/volatility/docs/` (`dashboard.html`, `index.html`, `dashboard.js`, `dashboard_panels.js`, `sample_data.js`, `dashboard.css`, `DASHBOARD_PLAN.md`, `llms.txt`) | JS SVI sample smile (`sample_data.js:25-27`), forward at r = 0.05 (`:178`), fit RMSE and R² (`dashboard_panels.js:275-281`) | **Dead.** Nothing serves or links it: `git grep` finds no reference outside the folder, and no `StaticFiles` mount points at it | **Dead, on no list.** It belongs with #2704 (engine and volatility dead code). ◇ does not keep it: it is a JS mock over sample data, not the validated math |

The other tracked non-Python files are not math: `*.sql` fixtures and DDL, `deploy/fleet/sql/*`, Frontend config, e2e specs and the `no-money-alias-arithmetic` lint rule. Vendored `references/` code is out of scope.

## Named exceptions for the canonical-math ADR (#2745 entry 5)

1. **Strategy Builder live curves (Frontend):** `utils/black-scholes.ts` plus `strategy-builder.component.ts` (F1–F3).
   - Reason: latency, because each leg edit re-prices a 1,200-point grid.
   - Parity: price kernel only (`black-scholes.parity.spec.ts`). Owed: Greeks, strategy P&L and Greek helpers, expiry payoff and breakevens.
   - The ADR should say the module stays frozen to this one caller. Pricing Lab's use (F6) is not covered.
2. **Loss-cap whole-cent check (Frontend):** `configuration-revision-draft.ts:241-248` (F11).
   - Reason: layer-locality (inline form validation). Python re-checks at the boundary.
   - Parity owed: one shared edge-value fixture both sides read.
3. **Conditional — .NET FIFO lots** (`PositionEngine.cs`, B1). Name it only if the owner keeps the Portfolio page.
   - Reason: layer-locality.
   - Parity owed: no test compares it to `fifo_pnl.py` or to any Python oracle.
   - If Portfolio goes, the ADR names **no .NET exception**.

Entry 5's draft line "the Frontend Black-Scholes helper is a frozen render-only mirror under `test_bs_cross_engine_parity.py`" is wrong on the test. That test compares `bs_greeks` with QuantLib and never reads the TS. The TS's parity proof is `black-scholes.parity.spec.ts`.

## Follow-up issues to file (6)

Moving live math is out of scope for this map. These are issues, not cuts.

1. **Research verdicts belong to the server** (F8 + F9). Python should return the feature-report letter grades and the indicator-reliability severities. The browser's OOS severity is a hand copy of Python's label thresholds (H7). This pairs with ADR owed #10 (research verdict rules).
2. **Baselines quantiles from the server** (F10). The baselines response should carry p5, p50 and p95, and the page's numpy-percentile port should go.
3. **Edge pages render synthetic volatility** (F7). Landing, Cross-Asset and Regimes show seeded mock RV, IV30 and regimes with no "synthetic" label. Either wire them to the Python realized-vol and IV30 series or cut the pages. Their Python backing (cross-asset, regimes) is already going (#2744 H7).
4. **Pricing Lab: drop or demote the TS column** (F6). It is the default diff reference (`pricing-lab.component.ts:193`), so the Python engines are diffed against the non-canonical copy. Make a Python engine the default, and drop or clearly label the TS column.
5. **Parity owed on the two exceptions** (F2, F3, F11):
   - extend `grid.json` with Greeks, strategy P&L and breakevens, generated by importing `bs_greeks.py` and `strategy_engine.py` rather than an inline copy (H2)
   - add one shared whole-cent fixture
6. **Options-chain derived numbers** (F12, low priority). Serve `mid` and an ATM-IV value from the Python quote payload, which already cleans IBKR's `-1` sentinel.

Not filed as follow-ups, because they are dead: the .NET Sortino, Calmar and drawdown copies (B2). The "CAGR copy" the map's registry note lists does not exist. If the owner keeps Portfolio, file the B2 fix (wrong formulas) and the B1 parity gap instead.

## Hardcoded numerical constants

| Value | Where | Duplicates | Note |
|---|---|---|---|
| `r = 0.043` | Backend `GraphQL/Query.cs:882` (`analyzeOptionsStrategy` default); `Services/Implementation/PolygonService.cs:365,439,453`; `Services/Interfaces/IPolygonService.cs:130,229,241` | `app/services/fred_service.py:37` `FALLBACK_RATE` (the canonical fallback) | The Frontend always passes a rate, so these defaults fire only for other callers. `:365` and `:439` are Portfolio cascade |
| `r = 0.043` | Frontend `strategy-builder.component.ts:297` (initial value; replaced by the server's FRED rate at `:1006-1009`); `shared/payoff-chart/payoff-chart.component.ts:34` (a dead input, F5) | same | — |
| `r = 0.043` | Python `app/research/options/iv_builder.py:25`, `app/research/options/contract_finder.py:33`, `app/models/strategy.py:48`, `app/models/portfolio.py:99,186` | same | These are the "five places". Line numbers have moved from the math index's `:18/:26/:97/:184` |
| `r = 0.05` | Backend `Query.cs:1236` (`quantlibPrice`), `:1279` (`quantlibStrategy`, dead per #2710 A19), `:1339` (`pricingModelComparison`); `PolygonService.cs:723` (dead per #2710 B12), `:796`. Frontend `pricing-lab.component.ts:147` (replaced by the snapshot rate at `:367`), `data-lab/export/export.component.ts:79` (a user-editable default) | Python's own second default: `app/volatility/solver.py:127,407`, `conventions.py:36`, `surface.py:194`, `analytics.py:341` (◇); `app/engine/options/pricer.py:126,170,216` and `chain_resolver.py:171` (going per ◇) | **Python has no single default rate.** There are two (0.043 and 0.05). FRED is the only source of truth for the live rate, so the ADR should say which default wins |
| `252` | `SnapshotService.cs:108,113,130` | `statistics.py` `TRADING_DAYS_PER_YEAR = 252` | Dead (B2) |
| `365` | `strategy-builder.component.ts:603,735`; `black-scholes.ts` theta `/365` | `bs_greeks.py:109` (theta / 365) | Same convention; the anchor differs (H4) |
| `390` and `7` bars/day | `MarketDataService.cs:353-354` | Calendar `session_open_ms_utc` and `session_close_ms_utc` | A hardcoded session length; wrong on half-days (B9) |
| `0.05 / 0.10 / 0.6 / 0.03` | `indicator-reliability.component.ts:525-538` | `indicator_reliability.py:95,160-161,403-410` | A hand copy of the verdict thresholds (F9, H7) |

## Hazards

- **H1 — The named FIFO exception is reachable only from a page ☆ rules dead.**
  - Every Backend portfolio service (B1–B7, plus `PortfolioQuery`, `PortfolioMutation` and their tests) is reached only from `/portfolio`. #2710 assumed that page was routed (`backend.md` § Not reviewed). #2709 B1 cuts only the Frontend. #2744 traced only Python. So nobody owns the Backend portfolio cascade.
  - Cutting it also orphans Python `/api/portfolio/scenario`, whose only caller is `PortfolioRiskService` via `PolygonService.cs:360`. Under ◇ the route goes and `portfolio_scenario.py`'s tested math stays.
  - ◆ "money trails stay" reads on the paper-ledger **tables** (`Accounts`, `Orders`, `PortfolioTrades`, `Positions`, `PositionLots`, `PortfolioSnapshots`), not on the code. Recommended: cut the code and keep the tables (no drop migration), unless the owner rules that the hand-entered paper ledger is not money.
- **H2 — The TS parity fixture is generated from a third BS copy.** `Frontend/scripts/generate-bs-parity-fixture.py:63` defines its own `bs_european_price` and does not import `bs_greeks.py`. The chain TS ↔ canonical is therefore TS ↔ an inline copy. The "matches bit-for-bit" claim (`:75`) is untested. The TS header (`black-scholes.ts:11-13`) cites the wrong test as its pin.
- **H3 — Breakeven rounding differs.** The browser rounds breakevens to 2 dp (`strategy-builder.component.ts:705`); Python rounds to 4 dp (`strategy_engine.py:94`). The page shows both the client `liveBreakevens` (chart) and the server `result.breakevens` (stats, `.html:534-536`).
- **H4 — The client's expiry anchor is local time.** `new Date(exp + 'T16:00:00')` (`strategy-builder.component.ts:598`) means 16:00 in the viewer's zone, not 16:00 ET. That is on the temporal ban list. Client curves and server stats therefore price with a different T (off by one hour for America/Chicago).
- **H5 — Pricing Lab diffs against the copy by default** (`pricing-lab.component.ts:193`).
- **H6 — Fabricated numbers on live menu pages.** The Edge landing, Cross-Asset and Regimes show seeded mock volatility and regime numbers with no synthetic tag (F7).
- **H7 — The browser re-derives OOS verdicts instead of reading them.** `getOosSeverity` re-implements Python's label thresholds (`indicator_reliability.py:403-410`) rather than mapping the server's label. Today they agree: Python's "Partial OOS Retention" (`:407`, `retention >= 0.4`) and "Weak OOS Signal" both land on the browser's `warn`. Nothing pins this, so a threshold change on either side drifts silently.
- **H8 — The .NET statistics share names with the canonical but not formulas** (B2). If anything outside Portfolio ever reads them, they disagree with `statistics.py` by construction.
- **H9 — Contracts.** Cutting the B8 and B9 fields, or the Portfolio roots, regenerates `contracts/graphql/backend.schema.graphql`, which is a serial CI gate. Cutting the Portfolio tables, if the owner chooses it, needs an EF migration generated where the SDK runs.
- **H10 — `docs/math-sources-of-truth.md` is wrong in four places this inventory checked:**
  - "CAGR" listed as a .NET copy (`:173`)
  - the `0.043` line numbers (`:361`)
  - `ReplayDeterminismTests.cs` (`:175`, which no longer exists)
  - the claim that `test_bs_cross_engine_parity.py` pins the TS (`:149-150`, `:358`)

  The doc is being cut whole, so this is just a reason not to trust its rows.

## For the map

- **Rule-level question:** does the Portfolio cut (☆) take the Backend paper ledger's code with it? The recommendation is yes, keeping the tables (◆ money trails).
  - Then the ADR names no .NET exception, and the map's Notes need correcting where they say "the .NET FIFO position engine is one".
  - If no, FIFO becomes "Exception, parity owed", and the B2 statistics need fixing, because they are wrong rather than duplicated.
- **Owners missing for four dead items:**
  - the Backend portfolio cascade (H1)
  - the unread `AggregatesSummary` and `gapDetection` output (B8, B9 → #2710-style field cut)
  - `weightedIv` and the payoff-chart inputs (F5 → #2744)
  - `app/volatility/docs/` (O1 → #2704)

## Not reviewed

- `PortfolioValidationService.cs` internals beyond confirming it is Portfolio cascade.
- Frontend chart and layout code (`trading-chart`, `candle-renderer`, `edge-charts`, `recency-swimlane-layout`, `reliability-diagram`). These were skimmed as display arithmetic only.
- `signal-runner`, `lean-statistics`, `monte-carlo-detail-page`, `walk-forward-*` and `grid-search-*`. Token hits in them were type fields and prose, and no arithmetic line matched, but they were not read line by line.
- Interface and type members in generated `api/broker.types.ts`, and Backend DTO computed members beyond `Models/`.
- Whether `broker-options-chain`'s reprice sends the browser mid to the server as a pricing input (F12). That affects only that follow-up's priority.
