# LEAN EMA-crossover template and unified backtest-run history

**Status:** Design approved 2026-05-19. Awaiting implementation plan (writing-plans).
**Authors:** Tim (owner), Claude (design).
**Related docs:** `.claude/rules/numerical-rigor.md`, `docs/references/lean-engine.md`, `docs/audits/computational-fidelity-2026-04-22.md`.

## Problem

LEAN Sidecar Lab and Engine Lab each produce backtest results, but:

1. The only LEAN trusted template today is a buy-and-hold (`MyAlgorithm` calling `SetHoldings(spy, 1.0)`). There is no way to validate the LEAN sidecar runner against the Engine Lab's existing reference algorithms — most notably the canonical EMA(5)/EMA(10) crossover with RSI(14) gate already defined in `PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json` and exposed in the Engine Lab dropdown as "SPY EMA(5)/EMA(10) Crossover (LEAN-pinned)".
2. LEAN runs persist only as filesystem artifacts under `/app/artifacts/lean-sidecar/<run_id>/`. Engine Lab runs persist to Postgres (`StrategyExecution` + `BacktestTrade`). The two histories are visually disjoint; there is no path to compare a LEAN run to an Engine Lab run in one view.

This design adds an EMA-crossover trusted template to the LEAN sidecar that mirrors the Engine Lab spec bit-for-bit (as a "third oracle" alongside the spec evaluator and the hand-coded reference at `PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py`), persists LEAN runs to Postgres so both engines share one storage substrate, and surfaces a unified run-history table with a select-two side-by-side comparison view.

## Goals

- **G1.** A new LEAN trusted template `ema_crossover` that any user can pick from the LEAN Lab dropdown.
- **G2.** LEAN-run results end up in the same Postgres tables (`StrategyExecution`, `BacktestTrade`) as Engine Lab results, tagged via `Source="lean-sidecar"`.
- **G3.** The existing `engine-results` UI page renders LEAN runs identically to Engine Lab runs (same component, no per-engine branching).
- **G4.** A single unified run-history table component, embedded on both lab pages, with an engine column and multi-select for comparison.
- **G5.** A side-by-side compare view that classifies divergences using the existing `qc_reconciler.DivergenceCategory` taxonomy.
- **G6.** A golden parity test asserting zero gating-set divergences between the LEAN template and the Engine Lab spec on a pinned window.

## Non-goals (Phase 1)

- Tunable EMA-crossover parameters in the LEAN template (period, gap, RSI band). Parameters are hard-coded to match the Engine Lab spec exactly. Tunability is Phase 2.
- "Run in other engine" button on each history row. Phase 2.
- Equity-curve overlay in the compare view. Phase 2 (depends on persisting LEAN's native minute-by-minute equity curve to Postgres).
- Multi-engine (3+) comparison.
- Fixing the pre-existing `BacktestTrade.EntryTimestamp` / `ExitTimestamp` `DateTime` rule violation per `numerical-rigor.md`. Tracked as separate cleanup.
- New spec-strategy primitives in Engine Lab. The `spy_ema_crossover` fixture already exists.
- Sandbox/sidecar hardening changes. Phase 1c is complete; this rides on it.

## Approach summary

| Decision | Choice |
|---|---|
| Equivalence model | **Staged.** Phase 1 = LEAN output visualized through Engine Lab results UI. Phase 2 = full reconciliation via Engine Lab implementation. |
| Persistence | **Write LEAN runs to Postgres** (`StrategyExecution` + `BacktestTrade`) at the tail of `run_trusted_sample()`. Reuse existing `Source` discriminator. |
| EMA spec | **Mirror `spy_ema_crossover.spec.json` exactly** — EMA(5)/EMA(10), RSI(14) Wilders, 15-min consolidated bars, fresh-cross + gap ≥ 0.20 + RSI ∈ [50,70], 5-bar time stop, SetHoldings 1.0, liquidate at end-of-algorithm. |
| Template list | **Additive.** Three templates total: `trusted_default` (buy-and-hold), `reconciliation` (buy-and-hold IBKR-pinned), new `ema_crossover`. |
| Compare UX | **Select-2 + side-by-side panels** + divergence table using `DivergenceCategory` taxonomy. |

## Architecture

### Where new code lives

| Concern | Layer | New artifact |
|---|---|---|
| EMA crossover LEAN algorithm | Python | `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py` (constant `EMA_CROSSOVER_SOURCE`) |
| Template registry entry | Python | extend `lean_sidecar_service.py` — `TrustedTemplate` Literal + `_SOURCE_FOR_TEMPLATE` + `_BROKERAGE_POLICY_FOR_TEMPLATE` |
| LEAN run → Postgres normalizer | Python | `PythonDataService/app/services/lean_sidecar_persistence.py` (new) |
| Postgres write hook | Python | invoked at the tail of `run_trusted_sample()` after manifest finalization |
| Schema additions | .NET | one EF migration adding `LeanRunId` to `StrategyExecution` and `IsSyntheticExit` to `BacktestTrade` |
| Unified backtest-runs GraphQL query | .NET | extend `StrategyExecution` resolvers — engine filter + cursor pagination + `leanRunId` + `isSyntheticExit` |
| Compare GraphQL query | .NET | new `compareBacktestRuns(leftId, rightId): RunComparisonResult` |
| Compare service (Python) | Python | new `POST /api/lean-sidecar/compare` endpoint that calls the existing `qc_reconciler` |
| Shared run-history component | Angular | `Frontend/src/app/components/shared/run-history/run-history.component.ts` |
| Compare view | Angular | new `Frontend/src/app/components/run-comparison/run-comparison.component.ts`, lazy-loaded at `/runs/compare` |
| Template dropdown entry | Angular | `lean-lab.component.ts` — add `<option value="ema_crossover">` + extend `template` literal type |

### Data flow — LEAN run

```
[User picks ema_crossover on LEAN page]
  ↓
POST /api/lean-sidecar/trusted-runs  (template="ema_crossover", symbol="SPY", dates, cash)
  ↓
lean_sidecar_service.run_trusted_sample()
  ↓ stage workspace/project/main.py from EMA_CROSSOVER_SOURCE
  ↓ run LEAN container → workspace/output/*-order-events.json + normalized/result.json
  ↓
lean_sidecar_persistence.normalize_and_persist(run_id)
  ↓ pair buy/sell events FIFO into BacktestTrade rows
  ↓ synthesize MTM exit for any half-open position (buy-and-hold case)
  ↓ compute totals (TotalTrades, TotalPnL, FinalEquity, WinRate, TotalFees, …)
  ↓ INSERT StrategyExecution(Source="lean-sidecar", LeanRunId=run_id, …)
  ↓ INSERT BacktestTrade × N
  ↓
return RunSummary { lean_run_id, strategy_execution_id } to UI
  ↓
[Unified history table refetches; new row appears tagged engine="lean-sidecar"]
```

### Data flow — compare

```
[User checks 2 rows in unified history, clicks "Compare selected"]
  ↓ route to /runs/compare?left=<id>&right=<id>
  ↓
run-comparison.component fires GraphQL compareBacktestRuns(leftId, rightId)
  ↓
.NET resolver calls IComparisonService → POST /api/lean-sidecar/compare
  ↓
Python reuses qc_reconciler primitives on the two StrategyExecution + BacktestTrade sets
  ↓ classifies each divergence into DivergenceCategory
  ↓ returns RunComparisonResult (left, right, guardrails, summary, divergences, firstDivergenceMsUtc)
  ↓
Frontend renders: guardrail banner → summary strip → side-by-side <engine-results> → divergence table
```

## Component design

### 1. EMA crossover LEAN template

`PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py` exports `EMA_CROSSOVER_SOURCE: str` (same pattern as `BUY_AND_HOLD_SOURCE`). Algorithm parameters are class constants on `MyAlgorithm`; only `symbol`, `start_date`, `end_date`, `starting_cash` come from `self.GetParameter(...)`.

```python
class MyAlgorithm(QCAlgorithm):
    FAST_PERIOD = 5
    SLOW_PERIOD = 10
    RSI_PERIOD = 14
    BAR_MINUTES = 15
    EXIT_BARS = 5
    GAP_MIN = 0.20
    RSI_LO = 50
    RSI_HI = 70
```

**Initialize:**
- Parse and apply `symbol` / `start_date` / `end_date` / `starting_cash` parameters (defaults: SPY, 2025-01-06, 2025-01-10, 100k).
- `AddEquity(symbol, Resolution.Minute, fillForward=False)`, `DataNormalizationMode.Raw`.
- Build `TradeBarConsolidator(timedelta(minutes=15))`, wire `DataConsolidated` to `OnConsolidatedBar`, register via `SubscriptionManager.AddConsolidator(symbol, consolidator)`.
- Instantiate `ExponentialMovingAverage(5)`, `ExponentialMovingAverage(10)`, `RelativeStrengthIndex(14, MovingAverageType.Wilders)`. **Critically**, indicators are not registered to the raw minute stream — they are updated manually inside `OnConsolidatedBar` so they consume only the consolidated bar close (matches the spec's indicator-input contract).
- Initialize crossover-state vars: `prev_fast = None`, `prev_slow = None`, `bars_held = 0`, `in_trade = False`.
- `SetWarmUp(...)` long enough to seed all three indicators.

**OnConsolidatedBar:**

```
update ema_fast, ema_slow, rsi at bar.EndTime with bar.Close
if not (all three IsReady):
    prev_fast, prev_slow = fast, slow                  # prime crossover state
    return
if self.IsWarmingUp:
    prev_fast, prev_slow = fast, slow                  # prime crossover state
    return

if in_trade:
    bars_held += 1
    if bars_held >= 5:
        Liquidate(symbol); in_trade = False; bars_held = 0
else:
    fresh_cross = prev_fast <= prev_slow AND fast > slow
    gap_ok     = (fast - slow) >= 0.20
    rsi_ok     = 50 <= rsi <= 70
    if fresh_cross AND gap_ok AND rsi_ok:
        SetHoldings(symbol, 1.0); in_trade = True; bars_held = 0

prev_fast, prev_slow = fast, slow
```

**OnEndOfAlgorithm:** if `Portfolio[symbol].Invested`, `Liquidate(symbol)`.

### 2. Template registry wiring

In `lean_sidecar_service.py`:

```python
TrustedTemplate = Literal["trusted_default", "reconciliation", "ema_crossover"]

from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE

_SOURCE_FOR_TEMPLATE: dict[TrustedTemplate, str] = {
    "trusted_default":  BUY_AND_HOLD_SOURCE,
    "reconciliation":   BUY_AND_HOLD_RECONCILIATION_SOURCE,
    "ema_crossover":    EMA_CROSSOVER_SOURCE,
}

_BROKERAGE_POLICY_FOR_TEMPLATE: dict[TrustedTemplate, str] = {
    "trusted_default":  "algorithm_default",
    "reconciliation":   "ibkr",
    "ema_crossover":    "algorithm_default",
}
```

Frontend: `lean-lab.component.ts` extends the `template` FormControl literal type and adds the `<option value="ema_crossover">SPY EMA(5)/EMA(10) Crossover (LEAN parity oracle)</option>` to the dropdown.

### 3. Persistence layer

`PythonDataService/app/services/lean_sidecar_persistence.py` invoked synchronously at the tail of `run_trusted_sample()`.

**Schema additions** (one EF Core migration):

| Table | New column | Type | Purpose |
|---|---|---|---|
| `StrategyExecution` | `LeanRunId` | `string?` | Links back to `/app/artifacts/lean-sidecar/<run_id>/` for drill-down |
| `BacktestTrade` | `IsSyntheticExit` | `bool` default `false` | Flag for half-open positions whose exit was synthesized via MTM at end of window |

`Source` already exists on `StrategyExecution`; this design adds `"lean-sidecar"` as a recognized value alongside the existing `"engine"` (Engine Lab spec-strategy runner) and `"strategy-lab"` (legacy Strategy Lab runs, distinct configurator but same persistence). The discriminator is convention-only — no migration. `LeanStatisticsJson` is reused for LEAN-specific metadata (manifest notes, `lean_image_digest`, `bars_consumed_by_symbol`).

**Pairing algorithm** (FIFO buy/sell into round-trip trades):

```
events = normalized_result["order_events"]            # already int64 ms UTC
fills = [e for e in events if e["status"] == "filled"]
open_lot = None
trade_number = 0
trades = []

for fill in fills:
    if fill["direction"] == "buy":
        if open_lot is None:
            open_lot = {
                "entry_ms":    fill["ms_utc"],
                "entry_price": fill["fill_price"],
                "qty":         fill["fill_quantity"],
                "fees":        [fill["order_fee_amount"] or 0],
            }
        else:
            # ema_crossover has pyramiding=1 → not expected. Buy-and-hold templates
            # also never hit this branch (one buy, no sell). For Phase 1, raise
            # NotImplementedError("Pyramiding not supported"). Phase 2 can add
            # weighted-average accumulation if a multi-lot template is introduced.
            raise NotImplementedError("Pyramiding not supported in Phase 1")
    elif fill["direction"] == "sell" and open_lot is not None:
        trade_number += 1
        trades.append(BacktestTrade(
            trade_number=trade_number,
            entry_timestamp=ms_to_dt(open_lot["entry_ms"]),
            exit_timestamp=ms_to_dt(fill["ms_utc"]),
            entry_price=open_lot["entry_price"],
            exit_price=fill["fill_price"],
            quantity=open_lot["qty"],
            pnl=(fill["fill_price"] - open_lot["entry_price"]) * open_lot["qty"]
                - sum(open_lot["fees"]) - (fill["order_fee_amount"] or 0),
            signal_reason="EMA crossover exit (5-bar time stop)",   # template-specific
            is_synthetic_exit=False,
        ))
        open_lot = None

# Half-open at end of window → synthesize MTM exit at last equity-curve point.
if open_lot is not None:
    last_eq = normalized_result["equity_curve"][-1]
    last_price = (last_eq["value"] - starting_cash
                  + open_lot["entry_price"] * open_lot["qty"]
                  + sum(open_lot["fees"])) / open_lot["qty"]
    trade_number += 1
    trades.append(BacktestTrade(
        trade_number=trade_number,
        entry_timestamp=ms_to_dt(open_lot["entry_ms"]),
        exit_timestamp=ms_to_dt(last_eq["ms_utc"]),
        entry_price=open_lot["entry_price"],
        exit_price=last_price,
        quantity=open_lot["qty"],
        pnl=(last_price - open_lot["entry_price"]) * open_lot["qty"] - sum(open_lot["fees"]),
        signal_reason="EndOfAlgorithm:MTM (synthetic exit)",
        is_synthetic_exit=True,
    ))
```

**Aggregate computation:** `TotalTrades = len(trades)`, `WinningTrades = sum(1 for t in trades if t.pnl > 0)`, `TotalPnL = sum(t.pnl)`, `FinalEquity = starting_cash + TotalPnL - total_fees`, `WinRate = WinningTrades / TotalTrades` (guarded), `TotalFees = sum(all fee events)`. Richer LEAN-only stats (Sharpe, Sortino, drawdown) live in `normalized_result["statistics"]` and `runtime_statistics`; copy verbatim into `LeanStatisticsJson` rather than recomputing.

**Idempotency.** `LeanRunId` is the uniqueness key — re-running the normalizer on the same artifact path is a no-op.

**Failure handling:**
- LEAN crashed (`exit_code != 0`) → write a row with `Source="lean-sidecar"`, `TotalTrades=0`, `LeanStatisticsJson` containing the error notes. UI shows it greyed-out with the failure reason.
- Normalizer crashed → log + propagate to the FastAPI caller, which returns 500 with the artifact path for manual inspection.
- No `order_events` despite `exit_code=0` → legitimate "ran but did nothing" (warmup never finished within window). Write a zero-trade row.

**Timestamps.** LEAN's `normalized/result.json` provides `ms_utc` (int64 ms UTC, canonical). `BacktestTrade.EntryTimestamp` / `ExitTimestamp` are .NET `DateTime` — a pre-existing rule violation per `numerical-rigor.md` that is explicitly out of scope here. The persistence layer converts `ms_utc → datetime` at the write boundary and back to ms when serializing for the API.

### 4. Unified history table

**Component:** `Frontend/src/app/components/shared/run-history/run-history.component.ts` (standalone, OnPush, signals). Both lab pages embed it.

**Inputs / outputs:**

```ts
engineFilter = input<EngineSource | null>(null);   // null = all engines
allowCompare = input<boolean>(true);
pageSize     = input<number>(25);

compareRequested = output<{ leftId: string; rightId: string }>();
```

**Engine column display:**

| `Source` value | Badge label | Token |
|---|---|---|
| `engine` | Engine Lab | accent |
| `strategy-lab` | Strategy Lab | accent |
| `lean-sidecar` | LEAN | secondary |

All colors via `var(--token)` from `_tokens.scss`. No hex literals (per existing repo rule).

**Columns (left to right):**

1. ☐ checkbox (only when `allowCompare()`)
2. Engine badge
3. Run ID (truncated with hover for full; click → `/runs/<engine>/<id>` results)
4. Algorithm / strategy name
5. Symbol
6. Date range (`StartDate` – `EndDate`)
7. Executed at (formatted from `ExecutedAt`)
8. Trades count (`TotalTrades`)
9. Net PnL (`TotalPnL`, color-coded)
10. Status — derived: any `synthetic_exit` trade ⇒ "open-at-end"; `TotalTrades=0` + error notes ⇒ "failed"; else "complete"
11. Overflow ⋮ — "View artifacts" (LEAN only), "Rerun with same params" (Phase 2), "Delete"

**GraphQL query change** (extend existing `StrategyExecution` resolver in `Backend/GraphQL/`):

```graphql
type Query {
  backtestRuns(
    engine:  EngineSource           # null = all
    symbol:  String
    after:   String                 # cursor
    first:   Int = 25
    orderBy: BacktestRunOrderBy = EXECUTED_AT_DESC
  ): BacktestRunConnection!
}

enum EngineSource { ENGINE STRATEGY_LAB LEAN_SIDECAR }
```

Returns Relay-style cursor connection. `BacktestRun` is the existing `StrategyExecution` exposed via `[GraphQLName("backtestRun")]` (Hot Chocolate v15 convention) plus new `leanRunId` field and per-trade `isSyntheticExit` field.

**Multi-select for compare:**
- `selectedIds = signal<Set<string>>(new Set())`.
- "Compare selected" button disabled unless `selectedIds().size === 2`.
- Tooltip shows current count: "Select 2 runs to compare" / "Compare these 2".
- On click, emits `compareRequested`; parent route navigates to `/runs/compare?left=…&right=…`.

**Sorting and filtering:**
- Default: `ExecutedAt DESC`.
- Server-side pagination + sort.
- Engine filter is a chip group above the table: `[All] [Engine Lab] [LEAN]`. The two lab pages pass their engine as default; the Compare entry path passes `null`.
- Search box on Run ID / Symbol / Algorithm is a stretch goal — backlog, not Phase 1.

**Drill-down to results:**
- For `engine` / `strategy-lab` rows: existing `engine-results` route.
- For `lean-sidecar` rows: same `engine-results` page, since trades are now in Postgres in the same shape. The "View raw LEAN artifacts" overflow item links to a small new viewer that shows the manifest, log tail, and order-events JSON from the workspace path. Phase 1.5 nice-to-have; can stub with an "Open artifact path" copy button for Phase 1.

### 5. Compare view

**Route:** `/runs/compare?left=<id>&right=<id>` (lazy-loaded).
**Component:** `Frontend/src/app/components/run-comparison/run-comparison.component.ts`.

**GraphQL schema additions:**

```graphql
type Query {
  compareBacktestRuns(leftId: ID!, rightId: ID!): RunComparisonResult!
}

type RunComparisonResult {
  left:                 BacktestRun!
  right:                BacktestRun!
  guardrails:           ComparisonGuardrails!
  summary:              ComparisonSummary!
  divergences:          [TradeDivergence!]!
  firstDivergenceMsUtc: Long          # null if perfectly aligned
}

type ComparisonGuardrails {
  sameAlgorithm:  Boolean!
  sameSymbol:     Boolean!
  sameWindow:     Boolean!            # start/end date equality
  sameParameters: Boolean!            # JSON-equal parameters blob
  warnings:       [String!]!          # human-readable
}

type ComparisonSummary {
  pnlDelta:         Decimal!          # right - left
  tradeCountDelta:  Int!
  winRateDelta:     Float!
  feesDelta:        Decimal!
  finalEquityDelta: Decimal!
}

type TradeDivergence {
  category:       DivergenceCategory!
  tradeNumber:    Int
  msUtc:          Long
  message:        String!
  leftFillPrice:  Decimal
  rightFillPrice: Decimal
  leftQuantity:   Int
  rightQuantity:  Int
}

enum DivergenceCategory {
  DECISION_MISMATCH
  DIRECTION_MISMATCH
  QUANTITY_MISMATCH
  FILL_PRICE_DRIFT
  COMMISSION_DRIFT
  PNL_DRIFT
  ORDER_TYPE_MISMATCH
  FIXTURE_INSUFFICIENT
}
```

`DivergenceCategory` mirrors `qc_reconciler.DivergenceCategory` `StrEnum` exactly — no new categories.

**Resolver delegation.** .NET resolver calls `IComparisonService` → `POST /api/lean-sidecar/compare` with `{leftId, rightId}`. Python reads both `StrategyExecution` + `BacktestTrade` sets via its existing SQLAlchemy session (same DB connection the persistence layer writes through), runs `qc_reconciler.reconcile(left_trades, right_trades, ...)`, and returns the classified divergence list + summary deltas. Why .NET → Python: the reconciler logic, tolerance config, and taxonomy already live in Python and are canonical. .NET owns the GraphQL surface only.

**Page layout (top to bottom):**

```
┌─────────────────────────────────────────────────────────────────────┐
│  Guardrail banner (yellow, hidden if guardrails.warnings is empty) │
│  "These runs use different symbols: SPY vs QQQ. Comparison may     │
│   not be meaningful."                                              │
├─────────────────────────────────────────────────────────────────────┤
│  Summary strip                                                      │
│  ┌─────────────┬─────────────┬─────────────┬─────────────┐          │
│  │ Net PnL Δ   │ Trades Δ    │ Win rate Δ  │ First       │          │
│  │ +$12.40     │ +2          │ −3.4%       │ divergence  │          │
│  │ (green/red) │             │             │ 2025-01-07  │          │
│  │             │             │             │ 10:15 EST   │          │
│  └─────────────┴─────────────┴─────────────┴─────────────┘          │
├─────────────────────────────────────────────────────────────────────┤
│  Side-by-side run panels                                            │
│  ┌──────────────────────────┬──────────────────────────┐            │
│  │ [LEAN badge]              │ [Engine Lab badge]      │            │
│  │ Run #ui_run_20260519...  │ Run #spec_20260519...    │            │
│  │ <engine-results>          │ <engine-results>        │            │
│  └──────────────────────────┴──────────────────────────┘            │
├─────────────────────────────────────────────────────────────────────┤
│  Divergences table (collapsible, expanded by default if N > 0)      │
│  ┌────┬──────────────────┬──────────────────────┬─────────┬──────┐  │
│  │ #  │ Timestamp        │ Category             │ Left    │ Right│  │
│  │ 1  │ 2025-01-07 10:15 │ DECISION_MISMATCH    │ trade   │ —    │  │
│  │ 2  │ 2025-01-07 11:30 │ FILL_PRICE_DRIFT     │ 100.05  │ 100.10│  │
│  └────┴──────────────────┴──────────────────────┴─────────┴──────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

**Edge cases:**

| Scenario | Behavior |
|---|---|
| One run failed | Failed-run panel renders error inline; summary strip greys out deltas; divergences table is empty, labeled "Cannot compare — left run failed" |
| Algorithm mismatch | Banner shows it; classifier flags all trades as `DECISION_MISMATCH` |
| Window mismatch | Banner shows it; comparison restricted to intersection of windows |
| Symbol mismatch | Banner shows it; comparison runs anyway (user may be checking a regression) |
| Synthetic exit present | Banner: "Left has a synthetic mark-to-market exit — comparison treats it as an exit at that price" |
| Same-engine compare (e.g., param-tuning A/B) | Allowed; no banner; full comparison runs |

**Tolerance config.** Phase 1 uses the existing `qc_reconciler` defaults (`fill_price_atol=0.01`, `commission_atol=...`). Surfacing as URL query params is backlog, not Phase 1.

**Reuse of `engine-results`.** The compare component fetches `compareBacktestRuns` once, transforms `result.left → EngineResultData` and binds it to the left child component; same for right. No duplication.

## Testing strategy

| Layer | Test file | Coverage |
|---|---|---|
| Python — pairing | `PythonDataService/tests/services/test_lean_sidecar_persistence.py` | Buy/sell FIFO pairing; multi-trade EMA fixture; half-open synthetic exit for buy-and-hold |
| Python — normalizer | same file | Idempotency on re-run with same `LeanRunId`; failure cases (no normalized result, `exit_code != 0`); aggregate math (`TotalPnL`, `WinRate` /0 guard) |
| Python — template smoke | `PythonDataService/tests/lean_sidecar/test_ema_crossover_template.py` | `EMA_CROSSOVER_SOURCE` parses; class constants match the spec (`FAST_PERIOD=5`, `GAP_MIN=0.20`, …); template is registered in `_SOURCE_FOR_TEMPLATE` |
| Python — parity (gating) | `PythonDataService/tests/integration/parity/test_ema_crossover_lean_vs_spec.py` | Runs LEAN template via `run_trusted_sample()` on a pinned window; runs Engine Lab spec on same window+symbol; calls `qc_reconciler`; asserts zero divergences in the gating set. Emits report into `docs/references/reconciliations/ema-crossover-lean-vs-engine-lab.md`. Fixture committed under `tests/fixtures/golden/ema_crossover/` with attribution: LEAN image digest + spec file SHA + Engine Lab commit SHA |
| .NET — compare resolver | `Backend.Tests/GraphQL/CompareBacktestRunsTests.cs` | Returns expected shape; guardrails detect window/symbol/parameter mismatches; both-runs-failed case; missing run returns GraphQL error |
| .NET — runs connection | `Backend.Tests/GraphQL/BacktestRunsConnectionTests.cs` | Engine filter; cursor pagination; `orderBy` honored; new fields `leanRunId` + `isSyntheticExit` returned |
| .NET — schema migration | snapshot test under `Backend.Tests/Data/` | New columns present; no renames; `Source` discriminator accepts all three values |
| Angular — history component | `run-history.component.spec.ts` | Renders mocked rows; engine badge maps correctly; multi-select toggles; "Compare" disabled unless exactly 2; emits `compareRequested` with correct IDs |
| Angular — compare component | `run-comparison.component.spec.ts` | Guardrail banner when warnings present; embeds two `engine-results` panels; divergence table sorts by timestamp; URL deep-link loads correctly |
| Angular — lean-lab dropdown | `lean-lab.component.spec.ts` | `ema_crossover` option present; submission posts the slug; existing `trusted_default` / `reconciliation` still work |

**The acceptance gate** is the parity test in `test_ema_crossover_lean_vs_spec.py`: zero divergences in the gating set `{DECISION_MISMATCH, DIRECTION_MISMATCH, QUANTITY_MISMATCH, FILL_PRICE_DRIFT, ORDER_TYPE_MISMATCH, PNL_DRIFT, FIXTURE_INSUFFICIENT}` (plus `COMMISSION_DRIFT` only on Branch-A fixtures, per `numerical-rigor.md`).

## Open implementation questions (settle during Phase 1)

| # | Question | When |
|---|---|---|
| 1 | ~~Fill-model parity~~ — **Resolved 2026-05-19 by spike.** LEAN's default fill model already fills market orders at `bar.EndTime` / `bar.Close`, matching Engine Lab's `signal_bar_close` mode. No custom `EquityFillModel` override is needed in the LEAN template. The parity test (PR 4) will run Engine Lab with `fill_mode=signal_bar_close`. Re-investigate only if a future LEAN image version changes this behavior. See `docs/references/fill-model-parity-spike-2026-05-19.md`. | n/a — resolved |
| 2 | `BacktestTrade.EntryTimestamp` / `ExitTimestamp` as `DateTime` violates `numerical-rigor.md` int64 ms UTC rule. Out of scope for this PR. File follow-up ticket; link from spec. | After Phase 1 ships |
| 3 | Retire `/api/lean-sidecar/runs` REST endpoint or keep for filesystem drill-down? Recommend: keep, scoped to artifact-retrieval semantics; remove the "run summary" fields that now duplicate Postgres. | End of Phase 1 |
| 4 | Backfill historical LEAN runs (already on disk under `/app/artifacts/lean-sidecar/`) into Postgres, or only persist runs from this point forward? Recommend: backfill via a one-shot CLI (`python -m app.scripts.backfill_lean_runs`) so existing artifacts show in unified history. | Phase 1, after main path lands |
| 5 | `PNL_DRIFT` tolerance when fees differ: propagate per the taxonomy rule `Σ \|fill_qty_i\| × $0.01 + Σ fee_atol_i`, or fix at flat $1? Recommend: use the taxonomy formula. | When writing the reconciliation test |

## Phase 2 (deferred)

- "Run in other engine" button on each history row — creates a paired run automatically.
- Equity-curve overlay in the compare view (requires deciding whether to persist LEAN's native equity curve to Postgres).
- Tunable fill-model parameters in the LEAN template, exposed via the trusted-runs request body.
- Engine Lab implementations of additional trusted templates (SMA crossover, RSI mean reversion) for broader parity coverage.
- Three-or-more way comparison.

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Fill-model parity spike reveals LEAN cannot hit `signal_bar_close` bit-exact | Medium | Fall back to option (b) — pin Engine Lab to `next_bar_open` mode for parity test. Unified history work is unaffected; only the parity test target changes. |
| Schema migration breaks an in-flight branch | Low | Migration is additive (two new nullable columns); no renames or type changes. Coordinate with any in-progress migrations via the standard EF migration ordering rules. |
| LEAN run output format changes between image versions | Low | Manifest already records `lean_image_digest`; persistence layer reads from `normalized/result.json` (versioned schema with `parser_version`). Tests pin a specific digest. |
| Compare view performance with many trades | Low | Phase 1 only supports SPY runs over short windows (≤ days). Pagination + virtualization is a Phase 2 concern. |
| Multi-engine UX confusion (user picks two LEAN runs and thinks they're comparing engines) | Medium | Engine badges in compare view; banner is silent on same-engine compares but engine labels are visible at the panel level. |

## References

- `PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json` — canonical EMA-crossover spec.
- `PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py` — hand-coded reference.
- `PythonDataService/app/research/parity/qc_reconciler.py` — `DivergenceCategory` taxonomy.
- `PythonDataService/app/services/lean_sidecar_service.py` — current orchestration (template registry, manifest writing).
- `PythonDataService/app/lean_sidecar/trusted_samples/buy_and_hold.py` — existing template pattern to follow.
- `Backend/Models/MarketData/StrategyExecution.cs` — unified persistence row.
- `Backend/Models/MarketData/BacktestTrade.cs` — unified trade row.
- `Frontend/src/app/components/lean-lab/lean-lab.component.ts` — current LEAN dropdown.
- `Frontend/src/app/components/lean-engine/engine-results/engine-results.component.ts` — result-rendering component reused by compare view.
- `.claude/rules/numerical-rigor.md` — timestamp + tolerance rules.
- `docs/references/lean-engine.md` — LEAN vendor pin.
