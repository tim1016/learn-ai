# PR B — Unified Engine Lab: Python + LEAN behind one launch surface

**Status:** Design, pending review
**Date:** 2026-05-19
**Depends on:** PR A (#301 — LEAN Polygon parity hardening gate)
**Predecessor planning context:** brainstorming session 2026-05-19

---

## 1. Context

PR A landed the Polygon parity contract for LEAN sidecar runs — strict timestamp validation, manifest provenance, and a fixture-driven receipt. Today the LEAN sidecar still ships as its own page at `/lean-lab` with a separate launch form, parameter pin literals (`bar_minutes=15`, `adjustment="raw"`), and a non-trivial UX gap from the main Python Engine Lab at `/engine`. Run history is split across two tables: REST-backed `EngineHistoryComponent` and GraphQL-backed `EngineLabRunHistoryComponent`. Cross-engine comparison (Python vs LEAN runs of the same strategy) is scaffolded at `/runs/compare` but not meaningfully wired.

PR B unifies both engines behind a single launch surface, persists a shared `DataPolicy` contract on every run, and stands up a parity-compare view that gates on data-equivalence and surfaces the trade-by-trade diff that proves (or disproves) agreement.

## 2. Goals

1. **One launch surface.** `/engine` hosts both engines via an Engine dropdown (`Python` | `LEAN`). The user picks shared inputs once (symbol, date range, timeframe, session, starting cash); the form's script-input pane swaps based on the engine choice.
2. **`DataPolicy` as the backend-neutral shared contract.** Both engines accept a request whose `data_policy` block has the identical canonical shape; both engines persist it to the same `StrategyExecution` column.
3. **Unified run history.** One GraphQL-backed table with an `Engine` column; multi-select on rows enables the parity-compare action.
4. **Parity-compare view.** `/runs/compare?left=&right=` renders a compatibility header (DataPolicy + starting_cash + commission/fill model), summary cards (with deltas, no equivalence claims on stats), trade-by-trade diff, first-divergence callout, raw-run links, and an optional state-trace drilldown.
5. **Retire `/lean-lab`** and consolidate to `/engine` with a redirect.

## 3. Non-goals

- **No `engine="both"` runner.** Parity is workflow-driven (run twice, compare). No automatic fan-out, no shared staging across runs in a single submit.
- **No auto-translation** between Python strategies and LEAN source. The user supplies each independently.
- **No state-trace requirement** for user-written LEAN scripts. The state-trace drilldown surfaces only when both runs ship the artifacts (bundled-template scenario).
- **No new shared-control widgets.** No price-adjustment toggle (deferred until a user needs raw corporate-action reconciliation), no LEAN-specific bar-period picker.
- **No `/lean-lab` backwards-compat** beyond a single redirect.
- **No retirement of REST `/api/studies`** in PR B. The REST table component (`EngineHistoryComponent`) goes; the endpoint stays (still used by replay/inspection flows).

## 4. Architecture

### 4.1 Surface consolidation

- `/engine` becomes the single launch surface. `LeanEngineComponent` (existing) hosts the unified Configure form with the new Engine dropdown.
- `/lean-lab` route, `LeanLabComponent`, and its service helpers are deleted. A redirect guard sends `/lean-lab/*` to `/engine`.
- The LEAN sidecar's algorithm-source editor is the only feature of `/lean-lab` that ports forward; it becomes `LeanScriptEditorComponent`, conditionally rendered when Engine = LEAN.

### 4.2 `DataPolicy` as shared contract

PR A's `DataPolicyManifest` (in `app/lean_sidecar/manifest.py`) is renamed to `DataPolicy` and becomes backend-neutral. It is:

- Embedded inside `RunManifest.data_policy` for LEAN runs (unchanged structurally; rename only).
- Persisted as JSONB on every `StrategyExecution` row, regardless of engine, in a new `DataPolicyJson` column.
- Exposed as a non-null GraphQL field on `BacktestRun` for new rows (legacy rows return null).

`DataPolicyManifest = DataPolicy` is kept as a re-export alias with `DeprecationWarning` for one cycle so PR A's manifest contract doesn't break; removal is a later cleanup PR.

### 4.3 `BarsSpec` everywhere; no `bar_minutes`

PR A's `bar_minutes: Literal[15]` pin is replaced. Timeframe is carried by:

- `DataPolicy.input_bars: BarsSpec` — what's fetched from Polygon (typically `{minute, 1}`).
- `DataPolicy.strategy_bars: BarsSpec` — what the strategy operates on (e.g., `{minute, 15}` or `{day, 1}`).

The bundled EMA template's 15-min consolidation is pinned by the *template code itself*, not by a global Literal. User-written LEAN scripts pick their own consolidation; `DataPolicy.strategy_bars` records the intent.

**v1 equivalence semantics:** no normalization. `BarsSpec(minute, 60)` and `BarsSpec(hour, 1)` are NOT equal. The compare view surfaces the difference in a mismatch list instead of canonicalizing silently. Pinned in a unit test so the v1 contract requires an explicit flip to change.

### 4.4 Adjustment semantics

`DataPolicy.adjusted: bool` describes the **staging pipeline's** adjustment policy — *not* LEAN's `DataNormalizationMode`. Two engines under PR B both default to `adjusted=true`:

- Polygon bars are fetched with adjustment applied (Python engine's existing default).
- For LEAN, the staging pipeline pre-adjusts the bars before writing zips; LEAN's `data_normalization_mode` stays `"Raw"` because LEAN's role is to consume the already-adjusted file as-is.

PR A's `_assert_adjustment_vocabulary_consistent` is widened to accept the new pairing:

| `adjusted` | `data_normalization_mode` | Verdict | Reason |
|---|---|---|---|
| `False` | `"Raw"` | ✅ accept | PR A's existing case (raw → raw). |
| `True`  | `"Raw"` | ✅ accept | **New in PR B.** Pre-adjusted staging; LEAN reads adjusted bars as Raw. |
| `False` | `"Adjusted"` | ❌ reject | LEAN would adjust against unadjusted Polygon data → divergence. |
| `True`  | `"Adjusted"` | ❌ reject | Double-adjustment. |

A price-adjustment toggle in the shared form is deferred to a later PR. Until then, `adjusted=true` is the hidden default, surfaced explicitly in three places: the `DataPolicy` block, the run-history detail view, and the parity-compare compatibility header.

### 4.5 Compare-view design philosophy

The compatibility verdict is **modest by design**. It claims the two runs are *comparable* — same DataPolicy, same starting cash, same commission/fill model — not that "strategy behavior is equivalent." The platform stages data and passes params; user LEAN code can ignore `bar_minutes`, hardcode symbols, or use a different consolidator. The gate proves the *intended contract* matches across what mechanically affects output; the trade-by-trade diff proves whether the runs actually agreed.

The header breaks the verdict into distinct claims so the user can see exactly what was checked:

- **Data policy** — symbol, window, session, adjusted flag, input bars, strategy bars (§ 9.1).
- **Run parameters** — starting cash, commission per order, fill model (§ 9.1).
- **Brokerage** — soft-match when either side is `algorithm_default` or null (§ 9.2).
- **Strategy / template / source** — informational, never gates (§ 9.3).

State-trace drilldown is hidden for arbitrary user scripts and surfaces only when both runs ship the artifacts.

## 5. Build order & phases

PR B can be one PR with logical commits or split into four sub-PRs along these phase boundaries. Each phase compiles green, has tests, and is shippable on its own.

### Phase 1 — Shared `DataPolicy` contract (backend-only, no UI)

- Extract `DataPolicy` from `app/lean_sidecar/manifest.py` into `app/lean_sidecar/data_policy.py`.
- Rename `DataPolicyManifest` → `DataPolicy`; keep `DataPolicyManifest = DataPolicy` alias with `DeprecationWarning`.
- Drop `bar_minutes` from `TrustedRunRequest`, `TrustedRunRequestModel`, and `_build_data_policy`; thread `input_bars` + `strategy_bars` (BarsSpec values) through instead.
- Relax `adjustment` Literal: accepts `"raw"` and `"adjusted"`. Default = `"adjusted"`.
- Widen `_assert_adjustment_vocabulary_consistent` per § 4.4.
- Tests:
  - `tests/unit/lean_sidecar/test_data_policy.py` — JSON roundtrip, alias deprecation warning, vocab assertion truth table.
  - `tests/unit/lean_sidecar/test_bars_spec.py` — `BarsSpec(minute, 60) != BarsSpec(hour, 1)` (pins no-normalization v1).
  - `tests/lean_sidecar/test_manifest.py` — `RunManifest.data_policy` reads as `DataPolicy`; schema version stays at 4 (rename is non-breaking).

### Phase 2 — Engine-side persistence (backend-only, no UI)

- Postgres migration: add `StrategyExecution.DataPolicyJson` (jsonb, nullable), `CommissionPerOrder` (numeric, nullable), `BrokeragePolicy` (varchar, nullable). Index on `DataPolicyJson->>'symbol'`.
- Python engine path: `EngineBacktestRequest` gains `data_policy: DataPolicy`. `EngineBacktestResponse` gains `data_policy: DataPolicy` (echo). `Backend BacktestRunPersistenceService.PersistEngineAsync` writes `DataPolicyJson`, `CommissionPerOrder`, `FillMode`, `BrokeragePolicy="algorithm_default"` (Python engine doesn't model brokerage; default this).
- LEAN path: orchestrator already writes `data_policy` to manifest; the persist endpoint passes the manifest's `data_policy` through to `PersistLeanAsync`, which writes it to `DataPolicyJson`. Commission per order is pulled from LEAN's normalized result (the fee that was actually charged).
- Tests:
  - `Backend.Tests/Services/BacktestRunPersistenceServiceTests.cs` — both engines write the same JSON shape; `BarsSpec` roundtrips; default adjustment is `true` when omitted.
  - `tests/integration/test_engine_persistence_data_policy.py` — Python engine end-to-end: launch with `data_policy={...}` → row exists with identical JSON.
  - Migration test: column is jsonb + nullable, index exists.

### Phase 3 — History surfaces (GraphQL + UI)

- GraphQL schema:
  - New types: `Engine` enum (`PYTHON`, `LEAN`), `BarsSpec`, `DataPolicy`.
  - `BacktestRun.engine: Engine!` (derived from `Source` column: `"engine"` → `PYTHON`, `"lean-sidecar"` → `LEAN`).
  - `BacktestRun.dataPolicy: DataPolicy` (nullable for legacy rows).
  - `backtestRuns(engine: Engine = null, symbol: String, first: Int): [BacktestRun!]!` — engine null = all.
- Frontend:
  - `EngineLabRunHistoryComponent` adds Engine column + DataPolicy summary column (e.g., "minute/15 RTH adj"). Engine filter dropdown drives the GraphQL variable.
  - Multi-select preserved; "Compare" enables when exactly two rows selected.
  - `EngineHistoryComponent` (REST `/api/studies` 17-column table) is deleted. Its unique features port forward across commits in this phase:
    - Notes editing → new mutation `updateBacktestRunNotes(id, notes)` + inline edit in the GraphQL table.
    - CSV export → client-side serialization of the GraphQL result.
    - Column visibility toggle → preserved in the GraphQL table via localStorage key `engine-lab-history.columns.v1`.
- Tests:
  - `Backend.Tests/Resolvers/BacktestRunResolverTests.cs` — `engine=null` returns both kinds; engine derived correctly from `Source`.
  - `Frontend/.../engine-lab-run-history.component.spec.ts` — Engine column renders, filter drives query, multi-select compare CTA enables.

### Phase 4 — Compare endpoint + view

- New backend endpoint: `GET /api/runs/compare?left=<id>&right=<id>` (Backend .NET). Implementation in `Backend/Controllers/CompareController.cs`. Response shape per § 6.5.
- Compatibility gate strictness:
  - **Gate-strict** (must match for `compatible=true`): every DataPolicy field, `starting_cash`, `commission_per_order`, `fill_mode`.
  - **Gate-strict when both sides declare it**: `brokerage_policy` (when one side is `null`/`"algorithm_default"` and the other isn't, surface in informational header but don't fail the gate).
  - **Informational only**: `strategy_identity` (Python strategy name vs LEAN algorithm-source sha — different shapes; user judges).
- Compare logic reuses existing helpers: `app/services/lean_sidecar_compare_service.py`, `app/lean_sidecar/cross_reconciler.py`, PR A's `reconcile_trade_lists`, `tests/_helpers/parity.py`. The `DivergenceCategory` `StrEnum` from `qc_reconciler.py` is the trade-diff vocabulary.
- Frontend `/runs/compare` route renders the spec in § 6.6.
- Tests:
  - `Backend.Tests/Controllers/CompareControllerTests.cs` — compatible path, incompatible-policy path, first-divergence detection, unmatched-trade handling, state-trace asymmetry (only one side has `state.csv` → `state_trace_available=false`, no error).
  - `tests/integration/test_runs_compare_e2e.py` — `@pytest.mark.slow`, gated on `LEAN_LAUNCHER_URL` + DB. Runs Python EMA + LEAN EMA template against PR A's pinned Polygon fixture; asserts the compare response matches expected reconciler output.

### Phase 5 — Unified Engine Lab UI

- `LeanEngineComponent`:
  - Engine dropdown (`Python` | `LEAN`) at the top of the Configure form.
  - Conditional script-input pane:
    - Python: existing strategy dropdown + params (no change).
    - LEAN: new `LeanScriptEditorComponent`.
  - Shared controls unchanged: ticker, date range, timeframe, session, starting cash.
  - `composeDataPolicy(form)` builds the canonical `DataPolicy` from shared controls.
  - Submit branches by engine:
    - Python: `jobsService.startJob("engine_backtest", { backtest: { ..., data_policy } })`.
    - LEAN:   `leanSidecarService.startTrustedRun({ ..., data_policy })`.
- `LeanScriptEditorComponent` (new):
  - Monaco editor (or CodeMirror if Monaco isn't in the dep tree).
  - Default source = PR A's `EMA_CROSSOVER_SOURCE` template so first-time users have a working algorithm.
  - 500ms debounced lint via `POST /api/lean-sidecar/lint`.
  - "Problems" panel under the editor shows ruff diagnostics; clicking a diagnostic scrolls to the line.
- New Python endpoint `POST /api/lean-sidecar/lint`:
  - Request: `{ source: str }`.
  - Implementation: subprocess to `ruff check --output-format json --stdin-filename main.py -`; 5-second hard timeout; cap on input size = `MAX_ALGORITHM_SOURCE_BYTES`.
  - Response: `{ diagnostics: [{ line, col, end_line, end_col, rule, severity, message, fix }] }`.
  - Errors: 413 on oversize input, 504 on subprocess timeout (named explicitly).
- `/lean-lab` route + `LeanLabComponent` deleted. Redirect guard sends `/lean-lab/*` to `/engine`.
- Tests:
  - `Frontend/.../lean-engine.component.spec.ts` extended: Engine dropdown swaps the script pane; `composeDataPolicy(form)` produces the expected canonical object for each engine (exact assertions, no snapshots); submit calls the correct service.
  - `Frontend/.../lean-script-editor.component.spec.ts` (new): default template loads, `(sourceChange)` emits on typing, ruff diagnostics render in the Problems panel, 500ms debounce verified with fake timers.
  - `tests/unit/lean_sidecar/test_lint_endpoint.py`: empty source → empty diagnostics; oversize → 413; ruff timeout → 504; `import pandas` unused → `F401` in diagnostics. **Concurrency test deferred.**

### Phase 6 — Cleanup verification

- `/lean-lab` route absent; redirect works.
- `EngineHistoryComponent` and its template/SCSS deleted from `Frontend/`.
- `git grep "DataPolicyManifest"` in `app/` returns only the alias declaration line in `app/lean_sidecar/data_policy.py` — production code uses `DataPolicy`.
- Lint clean (`ruff check PythonDataService/app/ PythonDataService/tests/`), type-check clean.

## 6. Wire contracts

### 6.1 `DataPolicy` (canonical shape)

```jsonc
{
  "source": "polygon",
  "symbol": "SPY",
  "adjusted": true,
  "session": "regular",
  "input_bars":    { "timespan": "minute", "multiplier": 1 },
  "strategy_bars": { "timespan": "minute", "multiplier": 15 },
  "timestamp_policy": "bar_close_ms_utc",
  "timezone": "America/New_York",
  "provider_kind": "live",
  "fixture_id": null,
  "fixture_sha256": null
}
```

- `BarsSpec.timespan ∈ {"minute", "hour", "day"}`, `multiplier ∈ positive int`.
- `adjusted: bool` is the staging-pipeline policy, not LEAN's `DataNormalizationMode`.
- `provider_kind`, `fixture_id`, `fixture_sha256` are inherited from PR A.

### 6.2 Python engine launch request (extended)

`POST /api/jobs/engine_backtest`

```jsonc
{
  "backtest": {
    "strategy_name": "spy_ema_crossover",
    "starting_cash": 100000.0,
    "fill_mode": "signal_bar_close",
    "params": { ... },
    "start_date": "2025-01-13",
    "end_date":   "2025-01-17",
    "data_policy": { /* canonical shape */ }
  }
}
```

Response (`EngineBacktestResponse`) gains `data_policy: DataPolicy` (echo, post-normalization).

### 6.3 LEAN sidecar launch request (refactored)

`POST /api/lean-sidecar/trusted-runs`

```jsonc
{
  "run_id": "...",
  "algorithm_source": "<python source>",
  "starting_cash": 100000.0,
  "start_ms_utc": 1736777400000,
  "end_ms_utc":   1737298200000,
  "template": "ema_crossover",
  "data_policy": { /* canonical shape */ }
}
```

Top-level `symbol`, `bar_minutes`, `session`, `adjustment` are **removed**. Backwards-compat at the router: accepts the legacy shape for one deprecation cycle, converts to canonical internally, logs `WARNING`. Rejects requests that mix shapes (legacy + `data_policy` both present) with HTTP 422.

### 6.4 Ruff lint endpoint (new)

`POST /api/lean-sidecar/lint`

```jsonc
// Request
{ "source": "<python source>" }

// Response (200)
{
  "diagnostics": [
    {
      "line": 12, "col": 5, "end_line": 12, "end_col": 18,
      "rule": "F401", "severity": "warning",
      "message": "'pandas' imported but unused",
      "fix": null
    }
  ]
}
```

Error responses: 413 on oversize input, 504 on subprocess timeout, 200 with empty diagnostics on clean source.

### 6.5 Compare endpoint (new)

`GET /api/runs/compare?left=<id>&right=<id>`

```jsonc
{
  "left":  { "id": 123, "engine": "PYTHON", "data_policy": { ... }, "summary": { ... },
             "starting_cash": 100000.0, "commission_per_order": "0.00", "fill_mode": "signal_bar_close",
             "brokerage_policy": "algorithm_default", "strategy_identity": { "kind": "python_registry", "name": "spy_ema_crossover", "sha256": null } },
  "right": { "id": 124, "engine": "LEAN",   "data_policy": { ... }, "summary": { ... },
             "starting_cash": 100000.0, "commission_per_order": "0.00", "fill_mode": "lean_default",
             "brokerage_policy": "algorithm_default", "strategy_identity": { "kind": "lean_template", "name": "ema_crossover", "sha256": "abc..." } },

  "compatible": true,
  "mismatches": [],

  "summary_deltas": {
    "total_trades": { "left": 7, "right": 7, "delta": 0 },
    "total_pnl":    { "left": "421.50", "right": "419.80", "delta": "-1.70" },
    "total_fees":   { "left": "0.00", "right": "0.00", "delta": "0.00" },
    "win_rate":     { "left": 0.571, "right": 0.571, "delta": 0.0 },
    "max_drawdown": { "left": "-15.20", "right": "-15.20", "delta": "0.00" },
    "sharpe":       { "left": 1.42, "right": null, "delta": null }
  },

  "trade_diff": {
    "matched_pairs": [
      { "left_trade_id": 11, "right_trade_id": 24,
        "entry_ts_delta_ms": 0, "exit_ts_delta_ms": 60000,
        "entry_price_delta": "0.00", "exit_price_delta": "0.02",
        "qty_delta": 0, "pnl_delta": "1.70",
        "category": "fill_price_drift" }
    ],
    "python_only": [],
    "lean_only":   []
  },

  "first_divergence": {
    "trade_index": 3,
    "what": "exit_price_delta",
    "category": "fill_price_drift",
    "left_value": "421.50",
    "right_value": "421.52"
  },

  "state_trace_available": false,
  "raw_run_links": {
    "left":  { "manifest_path": null, "log_path": null, "staged_zip_sha256": {} },
    "right": { "manifest_path": "/.../manifest.json", "log_path": "/.../log.txt", "staged_zip_sha256": { ... } }
  }
}
```

`mismatches` is populated when `compatible=false`, naming the specific fields (e.g., `["strategy_bars", "starting_cash"]`).

### 6.6 GraphQL schema additions

```graphql
enum Engine { PYTHON LEAN }

type BarsSpec {
  timespan: String!
  multiplier: Int!
}

type DataPolicy {
  source: String!
  symbol: String!
  adjusted: Boolean!
  session: String!
  inputBars: BarsSpec!
  strategyBars: BarsSpec!
  timestampPolicy: String!
  timezone: String!
  providerKind: String!
  fixtureId: String
  fixtureSha256: String
}

extend type BacktestRun {
  engine: Engine!
  dataPolicy: DataPolicy
  commissionPerOrder: Decimal
  brokeragePolicy: String
}

extend type Query {
  backtestRuns(engine: Engine, symbol: String, first: Int): [BacktestRun!]!
}

extend type Mutation {
  updateBacktestRunNotes(id: Int!, notes: String!): BacktestRun!
}
```

## 7. UI design

### 7.1 `/engine` Configure tab (unified)

```
┌────────────────────────────────────────────────────────────────┐
│ Engine: [ Python   ▼ ]                                          │
│                                                                 │
│ Symbol: [SPY      ]   Range: [Jan 13 - Jan 17, 2025]            │
│ Timeframe: [Minute ▼] Session: [RTH ▼]  Cash: [$100,000]        │
│                                                                 │
│ ─── Script ────────────────────────────────────────────────── │
│ [Engine == Python]                                              │
│   Strategy: [ SPY EMA Crossover            ▼ ]                  │
│   Params:                                                       │
│     fast_period:  [5  ]                                         │
│     slow_period:  [10 ]                                         │
│     rsi_period:   [14 ]                                         │
│                                                                 │
│ [Engine == LEAN]                                                │
│   ┌──────────────────────────────────────────────────────────┐ │
│   │ <Monaco editor — Python syntax highlight>                │ │
│   │ class MyAlgorithm(QCAlgorithm):                          │ │
│   │     def Initialize(self):                                │ │
│   │         self.SetStartDate(2025, 1, 13)                   │ │
│   │         ...                                              │ │
│   └──────────────────────────────────────────────────────────┘ │
│   Problems: 0 ⚠ 0 ℹ                                             │
│                                                                 │
│ [ Run ]                                                         │
└────────────────────────────────────────────────────────────────┘
```

### 7.2 Unified history table

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Engine: [ All ▼ ]   Filter: [          ]   [ Compare ]                       │
├─────────────────────────────────────────────────────────────────────────────┤
│ ✓ Engine    Date          Symbol  Range          Strategy/Script   Bars    │
│ ─ ──────── ───────────── ──────── ──────────── ────────────────── ───────  │
│ ☑ Python    2026-05-19    SPY      Jan 13-17     spy_ema_crossover m/1→m/15 │
│ ☑ LEAN      2026-05-19    SPY      Jan 13-17     ema_crossover     m/1→m/15 │
│ ☐ Python    2026-05-18    QQQ      Jan 02-10     rsi_mean_revert   m/1→m/15 │
└─────────────────────────────────────────────────────────────────────────────┘
```

Two rows selected (one each engine) enables Compare. The Bars column shows `input_bars→strategy_bars` in shorthand: `m/1→m/15` for minute-1 input consolidated to minute-15 strategy bars; `d/1` for daily.

### 7.3 `/runs/compare` view

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Compatibility                                                                │
│ ─────────────────────────────────────────────────────────────────────────── │
│ ✅ Comparable                                                                 │
│   • Data policy:    matches                                                  │
│   • Run parameters: match                                                    │
│   • Brokerage:      algorithm_default on both                                │
│                                                                              │
│ Symbol:        SPY                       Cash:          $100,000             │
│ Window:        2025-01-13 – 2025-01-17   Commission:    $0.00                │
│ Timeframe:     minute/1 → minute/15      Fill model:    signal_bar_close     │
│ Session:       regular                   Brokerage:     algorithm_default    │
│ Adjusted:      true                                                          │
│                                                                              │
│ Strategy / source (informational):                                           │
│   Left:  PYTHON · spy_ema_crossover                                          │
│   Right: LEAN   · ema_crossover (sha 36d3c9...)                              │
│                                                                              │
│ ─── Summary ────────────────────────────────────────────────────────────── │
│ Trades   Δ 0     Net P&L  Δ -1.70      Fees     Δ 0.00                       │
│ Win %    Δ 0     Max DD   Δ 0.00       Sharpe   left only (1.42)             │
│                                                                              │
│ ─── First divergence ───────────────────────────────────────────────────── │
│ Trade #3 · category: fill_price_drift · exit price 421.50 vs 421.52          │
│                                                                              │
│ ─── Trade diff ─────────────────────────────────────────────────────────── │
│ #  Entry        Exit         Δentry  Δexit  Δqty  ΔP&L    Category          │
│ 1  10:30 / 10:30 11:15 / 11:15  0.00   0.00   0    0.00    matched           │
│ 2  10:45 / 10:45 12:30 / 12:30  0.00   0.00   0    0.00    matched           │
│ 3  11:30 / 11:30 14:00 / 14:01  0.00   0.02   0   -1.70    fill_price_drift  │
│ ...                                                                          │
│                                                                              │
│ ─── Raw run links ──────────────────────────────────────────────────────── │
│ Left:  manifest · log · staged_zip_sha256                                    │
│ Right: manifest · log · staged_zip_sha256                                    │
│                                                                              │
│ ─── State trace ────────────────────────────────────────────────────────── │
│   (hidden — state_trace_available=false)                                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 8. Data flow

### 8.1 Run launch

```
User picks Engine + fills form
         │
         ▼
LeanEngineComponent.onSubmit()
  data_policy = composeDataPolicy(form)
  switch (engine) {
    case "python": POST /api/jobs/engine_backtest  { backtest: {..., data_policy } }
    case "lean":   POST /api/lean-sidecar/trusted-runs { ..., data_policy }
  }
         │
         ▼
(Python path)                                    (LEAN path)
┌───────────────────────────┐                   ┌────────────────────────────────────┐
│ Python runs in-process    │                   │ LEAN sidecar orchestrator:         │
│ EngineBacktestResponse    │                   │  · stages Polygon per data_policy  │
│ includes data_policy echo │                   │  · launches LEAN container         │
│                           │                   │  · writes manifest.json with       │
│ Frontend posts to .NET    │                   │    data_policy embedded            │
│ /api/backtest-runs/       │                   │  · frontend posts to .NET          │
│   persist-engine          │                   │    /api/backtest-runs/persist-lean │
└────────────┬──────────────┘                   └────────────────┬───────────────────┘
             │                                                   │
             └─────────────────────┬─────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Backend BacktestRunPersistenceService                                       │
│   INSERT INTO StrategyExecution                                             │
│     (..., Source, DataPolicyJson, CommissionPerOrder, BrokeragePolicy, ...) │
│   Source ∈ {"engine", "lean-sidecar"}                                       │
│   DataPolicyJson = serialize(data_policy)  ← identical shape both engines   │
│   Returns Id; frontend uses for results + history navigation                │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 8.2 History

```
GraphQL: backtestRuns(engine: <ALL|PYTHON|LEAN>, first: 50) {
    id, source, engine, executedAt, strategyName, symbol, startDate, endDate,
    totalTrades, totalPnL, commissionPerOrder, brokeragePolicy,
    dataPolicy { source, symbol, adjusted, session,
                 inputBars { timespan, multiplier },
                 strategyBars { timespan, multiplier } }
}
         │
         ▼
EngineLabRunHistoryComponent renders Engine | Date | Symbol | Range | Strategy/Script | Bars | Trades | P&L
Multi-select two rows → "Compare" → routes to /runs/compare?left=&right=
```

### 8.3 Compare

```
GET /api/runs/compare?left=<id>&right=<id>
         │
         ▼
CompareController:
  1. Load both rows + trades
  2. Equivalence gate over canonical fields (see § 4.5)
  3. Reconcile trades via reconcile_trade_lists
  4. Detect state artifacts on each side
         │
         ▼
CompareResponse (§ 6.5)
         │
         ▼
/runs/compare frontend renders compatibility header → summary cards → first-divergence callout
  → trade-by-trade diff → raw-run links → optional state-trace drilldown
```

## 9. Equivalence-gate semantics

The compatibility gate operates on a normalized field set; **no semantic normalization in v1** (e.g., `{minute,60}` ≠ `{hour,1}`).

### 9.1 Gate-strict fields (must match for `compatible=true`)

| Field | Source | Notes |
|---|---|---|
| `data_policy.symbol` | DataPolicy | Uppercased on both sides. |
| `data_policy.session` | DataPolicy | Literal match. |
| `data_policy.adjusted` | DataPolicy | Bool match. |
| `data_policy.input_bars` | DataPolicy | Full `BarsSpec` equality. |
| `data_policy.strategy_bars` | DataPolicy | Full `BarsSpec` equality. |
| Window | left.startDate / endDate vs right's | Compared as `(start_ms_utc, end_ms_utc)`. |
| `starting_cash` | StrategyExecution column | Decimal equality. |
| `commission_per_order` | StrategyExecution column | Decimal equality. |
| `fill_mode` | StrategyExecution column | String equality. |

### 9.2 Gate-conditional (gate-strict when both sides declare it)

| Field | Behavior |
|---|---|
| `brokerage_policy` | If either side is `null` or `"algorithm_default"`, surface in informational header without failing the gate. If both are explicit and different (e.g., `"interactive_brokers"` vs `"algorithm_default"`), the gate fails. |

### 9.3 Informational only (shown in header, never failure)

| Field | Notes |
|---|---|
| `strategy_identity` | `{ kind, name, sha256 }`. Python's `python_registry` vs LEAN's `lean_template` / `lean_source` are inherently different shapes; user judges. |
| `provider_kind`, `fixture_id`, `fixture_sha256` | Audit; PR A artifacts. |

## 10. Testing strategy

Mirrors the build order. Numerical rigor per `.claude/rules/numerical-rigor.md`: every `np.allclose` / float compare specifies `atol` + `rtol` explicitly. Decimal comparisons use exact equality.

### 10.1 Phase 1 — `DataPolicy` contract

- `tests/unit/lean_sidecar/test_data_policy.py`:
  - JSON roundtrip with sorted keys.
  - `DataPolicyManifest` alias emits `DeprecationWarning`; production code in `app/` does not import the alias (only the alias declaration line).
  - `_assert_adjustment_vocabulary_consistent`: full truth table from § 4.4.
- `tests/unit/lean_sidecar/test_bars_spec.py`:
  - `BarsSpec(minute, 60) != BarsSpec(hour, 1)` — pins v1 no-normalization contract.
  - JSON shape: `{timespan, multiplier}`, no aliases.
- `tests/lean_sidecar/test_manifest.py` updated: `RunManifest.data_policy` is `DataPolicy`; `MANIFEST_SCHEMA_VERSION == 4` unchanged.

### 10.2 Phase 2 — Persistence

- `Backend.Tests/Services/BacktestRunPersistenceServiceTests.cs`:
  - Python path stores `DataPolicyJson`, `CommissionPerOrder`, `FillMode`.
  - LEAN path stores the same shape from manifest passthrough.
  - **Default-adjustment tests** (both paths): omitted `adjustment` in request → row has `data_policy.adjusted = true`.
  - **LEAN-uses-Raw test**: LEAN manifest has `data_normalization_mode="Raw"` despite `data_policy.adjusted=true` (pre-adjusted staging).
- `tests/integration/test_engine_persistence_data_policy.py`: Python e2e — launch with `data_policy={...}` → row has matching JSON.
- Migration test: column types + nullable + index.

### 10.3 Phase 3 — History surfaces

- `Backend.Tests/Resolvers/BacktestRunResolverTests.cs`:
  - `backtestRuns(engine: null)` returns both engines.
  - `engine` derived correctly from `Source` (`"engine"` → `PYTHON`, `"lean-sidecar"` → `LEAN`).
  - `dataPolicy` non-null on new rows, null on legacy rows.
- Frontend: Engine column rendering, filter drives query, multi-select compare CTA.

### 10.4 Phase 4 — Compare

- `Backend.Tests/Controllers/CompareControllerTests.cs`:
  - Compatible path: identical DataPolicy + cash + commission + fill_mode → `compatible=true`.
  - Incompatible DataPolicy (different `strategy_bars`) → `compatible=false`, `mismatches=["strategy_bars"]`.
  - **Incompatible-cash path**: same DataPolicy, different `starting_cash` → `compatible=false`, `mismatches=["starting_cash"]`. (Per user feedback: same DataPolicy + different cash produces different quantities and P&L.)
  - **Incompatible-fill-model path**: similar, mismatches=`["fill_mode"]`.
  - **Brokerage soft-match**: one side `"algorithm_default"`, other side `null` → `compatible=true`, brokerage in informational header.
  - **Brokerage strict-match**: both sides declare different non-default brokerages → `compatible=false`, `mismatches=["brokerage_policy"]`.
  - First-divergence detection: synthetic pair #3 has fill_price drift > 0.01 → `first_divergence.trade_index=3, category="fill_price_drift"`.
  - **State-trace asymmetry**: only LEAN side has `state.csv` → `state_trace_available=false`, no error.
  - Unmatched trades: 7 vs 6 → 6 matched + 1 in `python_only`.
- **Live LEAN-as-engine integration test**: submit a LEAN run through the unified launch path (the `/engine` form's service facade), not directly via `/lean-sidecar/trusted-runs`. Confirms the unified launch surface is wired.
- `tests/integration/test_runs_compare_e2e.py`: `@pytest.mark.slow` + `LEAN_LAUNCHER_URL` skip. Runs Python + LEAN against PR A's pinned fixture; asserts compare response matches expected reconciler output.

### 10.5 Phase 5 — UI

- `Frontend/.../lean-engine.component.spec.ts`:
  - Engine dropdown shows Python + LEAN.
  - Selecting LEAN hides strategy dropdown, shows editor.
  - `composeDataPolicy(form)` produces expected canonical object — **exact object assertions** for `data_policy` shape, engine choice, and endpoint selection (no snapshot tests per user feedback).
  - Submit branches to correct service.
- `Frontend/.../lean-script-editor.component.spec.ts`:
  - Default template loads.
  - `(sourceChange)` emits on typing.
  - Ruff diagnostics render; clicking scrolls.
  - 500ms debounce verified with fake timers.
- `tests/unit/lean_sidecar/test_lint_endpoint.py`:
  - Empty source → empty diagnostics.
  - Oversize → 413.
  - Ruff timeout → 504.
  - Unused import → `F401` in diagnostics.
  - **Concurrency test deferred** (not architecturally critical).

### 10.6 Cross-cutting

- Tolerances on compare-view tests: `atol=1e-6, rtol=0` for accumulated P&L; `atol=0.01` for fill-price deltas (matching PR A's reconciler defaults).
- Fixture reuse: PR A's pinned Jan 13-17 SPY fixture is the integration backbone. No new fixtures.
- Baseline-against-master before each phase merges, per PR A's pattern (stash, run on master, compare failure set).
- Heavy compare E2E is gated/skippable like PR A's parity test.

## 11. Migration & cleanup

### 11.1 Database migration

Single migration `AddDataPolicyAndCommissionToStrategyExecution`:

```sql
ALTER TABLE StrategyExecution
  ADD COLUMN DataPolicyJson jsonb NULL,
  ADD COLUMN CommissionPerOrder numeric(18,8) NULL,
  ADD COLUMN BrokeragePolicy varchar(40) NULL;

CREATE INDEX ix_StrategyExecution_DataPolicy_Symbol
  ON StrategyExecution ((DataPolicyJson->>'symbol'));
```

No backfill. Legacy rows show "DataPolicy unavailable" in the unified history; compare view refuses any pair where either side lacks `data_policy`.

### 11.2 Frontend deletions

- `Frontend/src/app/components/lean-lab/` directory deletion (component + service + spec).
- `/lean-lab` route + child routes deleted; redirect guard for `/lean-lab/*` → `/engine`.
- `Frontend/src/app/components/engine-lab/engine-history/` directory deletion (REST-backed component); its features port forward to `engine-lab-run-history/`.
- App routes test asserts redirect.

### 11.3 Backend changes

- `TrustedRunRequestModel` (Python `app/routers/lean_sidecar.py`): accepts both shapes for one cycle.
  - Legacy shape (top-level `symbol`, `bar_minutes`, `session`, `adjustment`) → converted to canonical, logs `WARNING`.
  - New shape (`data_policy` block) → passes through.
  - Both shapes present → HTTP 422.
- `TrustedRunRequest` (Python `app/services/lean_sidecar_service.py`): top-level legacy fields **removed**; `data_policy: DataPolicy` is the canonical input. Router adapter handles legacy-shape conversion before construction.
- `EngineBacktestRequest` (Python `app/routers/engine.py`): gains `data_policy: DataPolicy`. Legacy callers (any UI version before Phase 5 ships) get a `data_policy` synthesized at the router from their existing `symbol` + dates + `resolution` fields; UI calls fully migrate in Phase 5.

### 11.4 Schema versioning

`MANIFEST_SCHEMA_VERSION = 4` is **not bumped** for the `DataPolicyManifest` → `DataPolicy` rename — the JSON shape is unchanged, only the Python class name moves. The schema doc comment in `manifest.py` adds a note recording the rename. If a later phase (e.g., adding `commission_per_order` to the manifest's `data_policy` block) introduces a JSON-shape change, that's when we bump to 5.

### 11.5 Rollout sequence

Recommended split into four sub-PRs along phase boundaries:

| Sub-PR | Phases | Scope | Shippable on its own? |
|---|---|---|---|
| **PR B.1** | 1 + 2 | Backend contract + persistence. Pure backend, no UI. Both engines persist `DataPolicy` to `StrategyExecution.DataPolicyJson`. | ✅ — just more data captured |
| **PR B.2** | 3 | GraphQL schema additions, unified history table, retirement of `EngineHistoryComponent`, note/CSV/column-toggle features ported forward. | ✅ — old & new tables coexist briefly |
| **PR B.3** | 4 | Compare endpoint + `/runs/compare` view (existing route becomes meaningful). | ✅ — compare view rendered against persisted DataPolicy |
| **PR B.4** | 5 + 6 | Engine dropdown, `LeanScriptEditorComponent`, ruff lint endpoint, `/lean-lab` retirement, final cleanup. | ✅ — depends on B.1-B.3 |

Each sub-PR has its own test suite; each is reviewable independently. If you prefer a single PR B, the same phases apply as logical commits inside it.

### 11.6 Deferred to later cleanup PRs (not PR B)

- `DataPolicyManifest` alias removal.
- REST `/api/studies` endpoint retirement (still used by replay/inspection flows).
- Price-adjustment toggle in the shared form (add when a user needs raw corporate-action reconciliation).
- Lint endpoint concurrency tests (add when endpoint is heavily used).
- Heavier state-trace drilldown (auto-emitted decision snapshots from user-written LEAN scripts via a convention).

## 12. Open questions

- **Monaco vs CodeMirror.** Phase 5 needs an editor. Monaco is heavier (~3MB bundle) but richer; CodeMirror is leaner. Check whether either is already a transitive frontend dependency before adopting. Decision at PR B.4 kickoff.
- **`commission_per_order` for LEAN.** Pulled from LEAN's normalized result (the fee actually charged). When LEAN runs with zero commission, the field is `"0.00"` (Decimal zero), not `null`. Confirm this matches the normalized parser's output shape.
- **Strategy identity sha for Python.** Python's `python_registry` kind has `sha256: null` per § 6.5 because strategies are class-based (no source-text fingerprint). If we want byte-exact provenance, sha the strategy module's source. Not required for v1.

## 13. References

- `.claude/rules/numerical-rigor.md` — equivalence levels, fixture tolerances, reconciliation taxonomy.
- `.claude/rules/python.md` — ruff scope, pandas/NumPy conventions.
- `.claude/rules/dotnet.md` — Hot Chocolate v15 patterns, EF Core.
- `.claude/rules/angular.md` — Angular 21 conventions, signals, Vitest.
- PR A (#301) — Polygon parity hardening; provides `DataPolicy` manifest seed, `RecordedPolygonFixtureProvider`, `fetch_canonical_minute_bars`, `reconcile_trade_lists`.
- PR #299 — LEAN ↔ engine parity on Polygon-sourced bars.
- PR #291 — engine-side persistence + LEAN run persistence into `StrategyExecution`.
- `app/services/lean_sidecar_compare_service.py` — `reconcile_trade_lists` (reused).
- `app/lean_sidecar/cross_reconciler.py` — `DivergenceCategory` enum (reused).
- `tests/_helpers/parity.py` — `assert_state_traces_match`, `assert_trade_equivalence` (reused).
