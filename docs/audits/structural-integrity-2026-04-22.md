# Structural Integrity & Computational Accuracy Audit

**Repo:** `learn-ai`
**Date:** 2026-04-22
**Auditor role:** Lead Quantitative Systems Architect + Compiler Theory
**Scope:** Mathematical precision and architectural synchronization from Python calculation engine → .NET GraphQL proxy → Angular 21 UI. Security disregarded per brief.
**Method:** Six parallel deep-dive agents across stacks + live empirical precision trace through running containers.

---

## 0. Executive summary (TL;DR)

The repo is substantially more rigorous than its surface suggests — the **backtest engine and its execution realism layer are numerically strict**, and the .NET layer is **type-consistent with `decimal(18,8)` end to end**. But the repo documents one thing and ships another, and the cross-layer data contract has several precision sinks that **foreclose bit-exact equivalence by construction**.

**Verdict on the "Single Source of Truth":** Fragmented. There are two parallel indicator engines, two parallel OHLCV shapes, two parallel BacktestResult schemas, and two parallel Greeks shapes. CLAUDE.md claims the LEAN-ported engine is canonical; reality is that `pandas-ta` is canonical and the LEAN port covers 3 of ~200 indicators. The correct fix is to **update CLAUDE.md to match reality**, not to finish the port.

**Top-5 load-bearing flags** (each expanded below):

| # | Severity | Finding | Location |
|---|---|---|---|
| 1 | **CRITICAL** | `_calc_sma/_calc_ema/_calc_rsi` have no None-guard; `window > len(bars)` yields `HTTP 500: 'NoneType' object has no attribute 'iloc'` (reproduced live) | `ta_service.py:61-73, 139-145` |
| 2 | **CRITICAL** | Naive timestamps are emitted with a `Z` suffix (falsely claiming UTC); violates `numerical-rigor.md` | `sanitizer.py:78` |
| 3 | **CRITICAL** | `calculateIndicators` GraphQL field returns `error`; Frontend reads `message` — Python errors silently disappear from the UI | `Backend/GraphQL/*` vs `Frontend/.../market-data.service.ts:86`, `types.ts:121` |
| 4 | **HIGH** | Precision funnel: `float` (Py) → `decimal` (.NET) → `number` (TS) round-trips every numerical field; forecloses `atol=1e-9` equivalence tests | Every cross-layer numeric field |
| 5 | **HIGH** | CLAUDE.md claims "indicators ported from LEAN with strict equivalence" and "external deps eliminated". Reality: 3 indicators ported with zero golden fixtures, pandas-ta pinned in all 3 requirements files, `/home/inkant/Documents/Lean` contains only an empty `Data/` folder | `CLAUDE.md:3,8-9,21` |

---

## 1. The "Single Source of Truth" Verdict

**Question the user asked:** "Check which is right — pandas-ta or LEAN port — and fix the wrong one."

**Answer: neither is wrong; the *documentation* is wrong.** The repo has two legitimate indicator paths:

| Path | Purpose | Files | Count | Caller |
|---|---|---|---|---|
| **`app/services/ta_service.py`** (pandas-ta) | Bulk/batch indicator compute for charts, datasets, research | `ta_service.py`, `dataset_service.py`, `rule_based_backtest.py`, `validation_service.py`, `strategies/*.py` | 6 in TechnicalAnalysisService + full pandas-ta catalog (~151) | 6+ routers (indicators, chart, data_quality, dataset, rule_based_backtest, …) |
| **`app/engine/indicators/`** (streaming) | One-bar-at-a-time streaming indicators inside the LEAN-shaped backtest engine | `base.py`, `sma.py`, `ema.py`, `rsi.py` | **3** | 1 router (engine) + 2 internal strategy algorithms |

These serve different needs (batch vectorised vs. streaming stateful). **Keeping both is correct.** What is wrong is:

1. `CLAUDE.md:3, 8-9, 21` claims strict-equivalence LEAN porting, golden fixtures, eliminated dependencies. None of those are true for the pandas-ta path and only partially true for the streaming path.
2. The streaming path has no golden fixtures against LEAN (see §3.6).
3. There is no LEAN source to reconcile against (see §3.5).
4. `references/` and `docs/references/` are empty (`.gitkeep` only) but CLAUDE.md references them as load-bearing.

**Recommended fix (§8.P1-A):** rewrite CLAUDE.md to describe both paths honestly and list the streaming-engine port status as in-progress.

---

## 2. Mathematical Red Flags

Ordered roughly by expected impact on numerical output.

### 2.1 Precision funnel: `float` → `decimal` → `number` → display

Every financial value crosses at least three type systems in series:

```
Python pandas-ta (numpy.float64)
    └─> ta_service.py:144 round(float(v), 6)     # <-- precision capped at 1e-6 here
        └─> Pydantic serializes as JSON `float`
            └─> .NET deserializes into `decimal`  # <-- value is exact if repr is 6dp
                └─> EF persists as numeric(18,8) # <-- OK
                    └─> GraphQL returns `decimal`
                        └─> Frontend TypeScript  `number` (IEEE-754 float64)
                            └─> Chart library consumes `number`
```

- `ta_service.py:144` caps values at **6 decimal places** *before* they leave Python. This cap propagates to every pandas-ta indicator. The default tolerance in `.claude/rules/numerical-rigor.md:62` is `atol=1e-9, rtol=0` for indicator values; **the pandas-ta path cannot meet that tolerance as-written**, because it is discarded three orders of magnitude upstream.
- `engine.py:802` converts internal `Decimal` → `float` at the response boundary for the streaming engine. Internal math is precise; the wire is lossy.
- `.NET`'s `System.Text.Json` deserializes `1.23` (JSON number) → `decimal 1.23M`. For numbers unrepresentable in IEEE-754 (e.g. `0.1 + 0.2 = 0.30000000000000004`) it rounds to a different value than Python held — a second, independent source of 1e-16-scale drift.
- TypeScript `number` is `float64` again; decimal precision is not preserved on parse.

**Verdict: `atol=1e-9` cross-layer equivalence is architecturally impossible.** The realistic cap is **~1e-6** for any pandas-ta indicator and **~1e-9** for streaming-engine indicators, unless `decimal` is carried end-to-end as a string or QuadPrecision type.

**Remediation options** (pick one):
- Accept the cap: document `numerical-rigor.md` as applying *inside Python only*, and set the cross-layer tolerance at `atol=1e-6`.
- Fix the cap: drop the `round(float(v), 6)` at `ta_service.py:144`, let Pydantic serialize full `float64`, accept the ~1e-16 IEEE artefact.
- Lift precision: change wire format to string-encoded decimals for indicator values; parse as `BigDecimal`/`decimal.js` on the frontend. Heavy lift, only worth it if reconciliation grade matters.

### 2.2 pandas-ta silently drops warmup rows

`ta_service.py:143`:
```python
if pd.notna(values.iloc[i]):
    points.append({"timestamp": ..., "value": round(float(values.iloc[i]), 6)})
```

Rows during the warmup window (`NaN` in pandas-ta output) **disappear** from the response. `.claude/rules/numerical-rigor.md:86-90` explicitly forbids this:

> **Don't silently drop warmup rows.** Tests explicitly assert the NaN region.
> **Warmup behavior documented in the module docstring** of every indicator...

The streaming engine in `app/engine/indicators/base.py:42` complies (emits `None` during warmup, consumer checks `is_ready`). The pandas-ta service does not.

**Concrete impact:** a frontend chart showing SMA(20) on 30 bars will render only 11 points, aligned to the wrong x-coordinates if the caller assumes one point per bar. Any reconciliation script that zips Python's output against the bar list will silently mis-align.

### 2.3 Missing `None`-guard in indicator dispatch (CRITICAL, reproduced live)

While running the empirical end-to-end trace, the live service returned:

```
HTTP 500: {"detail": "Failed to calculate indicators: 'NoneType' object has no attribute 'iloc'"}
```

Root cause:

- `ta_service.py:61-73`:
  ```python
  def _calc_sma(df, window):
      series = ta.sma(df["close"], length=window)   # can return None
      return TechnicalAnalysisService._series_to_points(df["timestamp"], series)
  ```
  When `window >= len(df)`, pandas-ta returns `None` rather than an empty Series.
- `ta_service.py:139-145` (`_series_to_points`) accesses `values.iloc[i]` without a None check.
- `ta_service.py:96-136` (`_calc_macd`, `_calc_bbands`, `_calc_stoch`) **do** guard with `if X is None or X.empty`. The SMA/EMA/RSI paths do not.

**Classic "some branches fixed, some forgotten" bug.** Fails open to HTTP 500 with a confusing stack trace instead of a 400 with a helpful message.

**Additionally**: `IndicatorConfig` (`requests.py:166`) uses `window: int = Field(14, ge=1, ...)`. Pydantic v2 defaults ignore extra keys — so a caller sending `{"name": "sma", "params": {"period": 5}}` gets `window=14` silently applied. Combined with the missing None-guard, this is a very user-hostile failure mode: the wrong key silently drops to default; if default window > bars, HTTP 500. It *should* be an early 400 with "unknown field `params`; did you mean `window`?".

**Fix (P0, small):**
```python
@staticmethod
def _calc_sma(df, window):
    series = ta.sma(df["close"], length=window)
    if series is None:
        raise HTTPException(400, f"SMA requires at least {window} bars; got {len(df)}")
    return TechnicalAnalysisService._series_to_points(df["timestamp"], series)
# same pattern for _calc_ema, _calc_rsi
```

### 2.4 Greeks: `rho` missing from two of three shapes

Three parallel Greeks types:

| Type | delta | gamma | theta | vega | **rho** | File |
|---|---|---|---|---|---|---|
| `GreeksSnapshot` (snapshot endpoint) | opt | opt | opt | opt | **missing** | `responses.py:78` |
| `GreeksResult` (strategy analyzer) | req | req | req | req | **missing** | `strategy.py:52` |
| `QuantLibGreeksResponse` (black-scholes pricer) | req | req | req | req | req | `quantlib_options.py:46` |

For options with maturity > 60-90 days `rho` is non-trivial (its absolute value for SPX 2026-06 calls is order 0.3-0.5). The Frontend displays `rho = 0` or `rho = null` depending on which endpoint it pulled from, with no type-level way to tell them apart.

**Fix (P2):** pick one canonical Greeks shape (probably `QuantLibGreeksResponse`'s 5-field + d1/d2), unify the three Python types, and delete the narrower two.

### 2.5 Naive-but-`Z`-suffixed timestamps (CRITICAL)

`PythonDataService/app/services/sanitizer.py:78`:

```python
ts = pd.to_datetime(ts, unit="ms")           # naive UTC-ish
row["timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # LIES ABOUT BEING UTC
```

The `Z` suffix is ISO 8601 for "UTC". The underlying pandas object is *timezone-naive*. `.NET`'s `DateTime.Parse` accepts it, drops the kind, and hands onward. Strategies that later `.ToLocalTime()` or `.ToUniversalTime()` get undefined behaviour.

`.claude/rules/numerical-rigor.md:77-78`:
> All stored timestamps are UTC, tz-aware. Naive datetimes are bugs.

This is a rule violation and a material risk. For backtest replay this is currently invisible (the LEAN data path at `app/engine/data/lean_format.py:40-65` reads ET bars directly and attaches `America/New_York`, bypassing `sanitizer`). For the pandas-ta / Polygon path — which is the production path — the lie rides through.

**Fix (P0, small):**
```python
ts = pd.to_datetime(ts, unit="ms", utc=True)          # tz-aware UTC
row["timestamp"] = ts.isoformat()                     # preserves +00:00
```

### 2.6 `error` → `message` rename silently drops backend errors

`CalculateIndicatorsResponse` on the Python side has `error: str | None`. The Frontend's GraphQL query at `Frontend/src/app/services/market-data.service.ts:86` reads `message`. Unless there is a GraphQL alias elsewhere in the query (spot-checked: there isn't), Python error strings **never reach the UI**. Any upstream failure becomes a blank chart with no user-facing explanation.

Confirmed by Agent 4. Low blast radius (only in the error path) but invisible today.

**Fix (P0, trivial):** rename the TS interface `message` → `error`, or add a GraphQL field alias on the query side.

### 2.7 6-decimal round at ta_service.py:144 is a silent contract

`ta_service.py:144`:
```python
points.append({"timestamp": int(...), "value": round(float(values.iloc[i]), 6)})
```

No docstring, no comment, no configuration. Every consumer of this endpoint gets values capped at 1e-6. The streaming engine at `engine.py:802` converts `Decimal` → `float` with no rounding — so **the two engines produce responses at different precisions**, silently. A reconciliation test that compares them will see a small drift that is not a bug but a contract mismatch.

**Fix (P2):** either document the cap in the module docstring and `numerical-rigor.md`, or remove it. I would remove it — the cap is not load-bearing for anything except display.

### 2.8 Accumulation-order preservation (engine path) — PASS

`portfolio.py:188-190`:
```python
pos.average_price = (
    pos.average_price * Decimal(pos.quantity) + fill_price * Decimal(fill_qty)
) / Decimal(new_qty)
```

Explicit left-to-right order: `(old_avg * old_qty + new_fill * new_qty) / new_qty`. This matches LEAN's `SecurityHolding.UpdateAveragePrice` and is the only safe order for positions where `new_qty` can be very large. No Kahan — unnecessary for `Decimal`. **Compliant with `numerical-rigor.md:93-97`.**

### 2.9 LEAN-scale encoding — PASS

`lean_format.py:37, 84-87`: LEAN's minute CSV stores prices as integer deci-cents (`price * 10000`). The engine divides by `Decimal(10000)` on read. Preserves `Decimal` precision across the data-layer boundary.

### 2.10 Commission and slippage — single models, documented, LEAN-parity defaults — PASS

- **Commission:** flat `Decimal("1.00")` per order; configurable via `ExecutionConfig.commission_per_order` (`execution_config.py:46`). Matches LEAN's `InteractiveBrokersFeeModel` default.
- **Slippage:** constant per-share (default `Decimal(0)`); direction-aware (`fill_model.py:85-89`). Matches LEAN's `ConstantSlippageModel(0)`.

These are single-model choices deliberately chosen for reference parity. Adding a tiered or bid-ask-spread model later will require explicit configuration and a fresh reconciliation.

### 2.11 Execution realism layer (PR #3, commit 3a9ddb2) — PASS

Commit added four phases, all with explicit tests:
1. ExecutionConfig dataclass + slippage/commission plumbing.
2. TP/SL bracket resolution with **pessimistic** intrabar rule (`intrabar_resolver.py:41-76`): when both triggers land in the same bar, the stop wins. The explicit comment at lines 14-16 notes "PR 5 will replay 1-minute data" — matching the user's memory that the bar-magnifier is deferred.
3. Session entry cutoff + `force_flat_at` handler (`engine.py:237-254`, `strategy/base.py:194-203`).
4. Resting limit orders with penetration model (`engine.py:335-369`, default penetration = `Decimal(0)`, i.e. touch-fill).

All 52 tests pass. Pre-existing SPY regression tests (`test_spy_validation.py`) continue to pass with ExecutionConfig defaults (backward-compatible). Insight timestamps (`insight.py:86`) use `datetime.utcnow()` for logging only — acceptable.

### 2.12 Warmup in streaming engine — PASS

`app/engine/indicators/base.py:42-56`: indicators return `None` until `samples >= period`, then emit. Consumers must check `is_ready` before reading. No forward-fill. No silent drop. No interpolation.

Compliant with `numerical-rigor.md:86-90`.

### 2.13 Transactions-as-decimal (minor)

`Backend/Models/DTOs/PolygonResponses/AggregateData.cs:16`: `decimal? Transactions`. A transaction count is a non-negative integer; `long?` is appropriate. Not harmful (all values fit) but type-wrong.

### 2.14 `unit="ms"` → `int64` conversion pitfall

The `PythonDataService/CLAUDE.md` already flags: *"`DatetimeIndex.astype("int64")` returns microseconds in pandas 3.0 (not nanoseconds)"*. This is a known hazard. Grep finds 4 call sites using `.astype("int64")` on datetime series; verify each one's divisor is 1e3/1e6/1e9 consistent with the pandas version actually installed. Not audited exhaustively here — flagged for the team to sweep.

---

## 3. Architectural Drift Report

Where the Angular, .NET, and Python layers disagree about what the system is.

### 3.1 Two parallel indicator engines (by design, badly documented)

Covered in §1. This is the single biggest source of future confusion. Make the dual-path explicit in CLAUDE.md.

### 3.2 Two parallel OHLCV shapes

| Shape | Where born | Timestamp | Example |
|---|---|---|---|
| **A (DB-persisted)** | Polygon aggregate → `StockAggregate.cs:7` (EF entity) → GraphQL `AggregateBar` type → TS `StockAggregate` (`graphql/types.ts:24`) | ISO string (naive UTC with `Z` lie, see §2.5) | Used by charts, data-quality, dataset export |
| **B (wire for indicator compute)** | Python `OhlcvBar` (`requests.py:61`, `timestamp: int` Unix ms) → .NET `OhlcvBarDto` (`IndicatorModels.cs:3`, `long Timestamp`) → TS `IndicatorTableRow` (`types.ts:511`, `time: number`) | Unix ms | Used by indicator calculate/generate-table endpoints |

Nothing in the type system enforces consistency when a caller converts one to the other. Shape A has id/timespan/multiplier fields that Shape B lacks; Shape B has Unix-ms timestamps that don't round-trip through Shape A's ISO formatter without explicit conversion.

**Fix (P2):** unify to a single internal shape. Suggestion: Shape B (Unix ms) is cheaper to compare and reconcile; add `id`/`timespan`/`multiplier` as optional.

### 3.3 Two parallel BacktestResult schemas

This is the most consequential drift.

**Python** `BacktestResponse` (`app/routers/backtest.py:145`) contains 25 fields including:
- `win_rate`, `avg_win_pct`, `avg_loss_pct`, `win_loss_ratio`, `profit_factor`, `expectancy_per_trade`, `total_pnl_pct`, `total_pnl_pts`
- `lean_statistics: LeanStatisticsResponse` (35+ LEAN-parity KPIs)
- `chart_bars`, `chart_indicators` (for UI rendering)
- Trades with `pnl`, `pnl_pct`, `cumulative_pnl_pct`, `indicator_snapshot` dict

**.NET** `BacktestResultType` (`Backend/GraphQL/Mutation.cs:817`) exposes ~10 of those fields. **Every one of the KPI percentages, the LEAN statistics block, the chart data, and the per-trade indicator snapshot is silently dropped.** `parameters: dict` → `parameters: string?` — structured data is flattened to a JSON string the consumer must re-parse.

**Impact:** the frontend displays the old schema's metrics; the newer LEAN-parity statistics never reach the UI. If the product advertises "we compute the same metrics LEAN does", that claim is true in Python and false at the GraphQL boundary.

**Fix (P1):** re-generate the `BacktestResultType` to mirror `BacktestResponse`, or add a new `LeanBacktestResultType` alongside.

### 3.4 `calculateIndicators` is a Query, not a Mutation

Not a bug — a convention choice worth noting for a reviewer. `Backend/GraphQL/Query.cs` exposes `calculateIndicators` as a Query. The Frontend at `market-data.service.ts:61-89` uses `query CalculateIndicators(...)`. Its signature is `(ticker, fromDate, toDate, indicators, timespan, multiplier) → CalculateIndicatorsResult`: the frontend passes **dates**, and .NET fetches bars itself from its cache/DB then calls Python's `/api/indicators/calculate` with the bars.

This is a reasonable design (bar transmission would be huge). But:
- It means the UI caches indicator results implicitly via the bar cache, not via explicit indicator memoisation. If bars are cached but indicator params change, Python still re-runs.
- The flow is `Angular → .NET → DB (bars) → .NET → Python (bars+params) → .NET → Angular`. Two extra hops vs. frontend talking directly to Python.
- Python's response rounds to 6dp (§2.7); .NET re-wraps in `decimal`; shipped to TS as `number`. See precision funnel §2.1.

Document this flow in CLAUDE.md or `docs/architecture/`. Currently neither mentions the indirection.

### 3.5 LEAN reference not on disk

The user said LEAN is at `../lean`. It is not there. The closest match is `/home/inkant/Documents/Lean/`, which contains only a `Data/` subfolder — no `Indicators/`, no `.csproj`, no `.git`. No git history, no VERSION file. **There is no LEAN source to reconcile against.**

The fixture extractor at `app/engine/tests/extract_lean_fixture.py:5` references a path `/sessions/ecstatic-hopeful-volta/mnt/Lean/...` — presumably a cloud-sandbox used to generate the SPY golden trade log. That session is gone. The golden `spy_lean_trades.csv` it produced is committed to the repo (good), but future regenerations are blocked.

**Fix (P3):** clone QuantConnect/Lean at a pinned commit into `references/Lean/` (or wherever the user prefers) and commit a `references/Lean.commit` file with the SHA. Then:
- regenerate the SPY trade log from a local LEAN run
- write per-indicator golden fixtures for SMA/EMA/RSI
- verify bit-exactness against the ported Python

### 3.6 Golden fixtures for ported indicators don't exist

`numerical-rigor.md:32-40`:

> Every ported indicator, strategy, or calculation ships with (a) a golden fixture derived from the reference, (b) a tolerance-pinned test...

Reality:
- `PythonDataService/tests/fixtures/golden/` does not exist.
- There is no `sma/`, `ema/`, `rsi/` subfolder anywhere.
- Existing tests (`test_indicator_parity.py:49-64`) compare against **pandas-ta**, not LEAN. Self-consistent, not equivalence-to-reference.
- `test_rsi_mean_reversion_parity.py:13-19` admits the reference is an "inline reimplementation" because pandas-ta "is not installable in every environment this test runs in."

Net: the existing "parity" tests are self-parity, not LEAN-parity.

### 3.7 `docs/references/` is empty (`.gitkeep` only)

Per CLAUDE.md:17 this directory should contain one note per port. It contains zero. `references/` (top-level) is also empty aside from `.gitkeep`.

### 3.8 Silent optional→required drift in Greeks

Already covered in §2.4. Three Greeks shapes with mixed optionality, one missing `rho`.

### 3.9 Two parallel "pnl" contracts (per trade)

`BacktestTradeResponse` (Python) carries both `pnl` (absolute) and `pnl_pct` (percent). `.NET` `BacktestTradeType` (`Mutation.cs:839`) carries only `PnL` exposed via `[GraphQLName("pnl")]` as `decimal`. TS `BacktestTrade` reads `pnl: number`. The percent form is silently dropped.

This is a unit-drift risk: a caller who expects a percent gets an absolute, and vice versa. Product features that display "trade gained X%" may be reading an absolute dollar value.

**Fix (P1, trivial):** add `pnlPct` to the .NET and TS types.

### 3.10 Field renames without `[GraphQLName]` audit

`vwap` is `Vwap` on the wire DTO and `VolumeWeightedAveragePrice` on the EF entity. `transactions` is `Transactions` on the wire and `TransactionCount` on the entity. Both renames work because there is explicit mapping in services — but they are hand-maintained and fragile. If anyone adds a third field with a rename and forgets the mapping, the value silently becomes 0/null.

---

## 4. Reactivity Graph Analysis (Angular 21)

### 4.1 Signal topology — mostly clean

State owned by services (`replay-engine.service.ts:17-19` signals, 6 `computed()` projections at lines 28-50). Components consume via `computed()` and template bindings. Subscribe leaks audited: **all 46 `.subscribe()` calls in components use `takeUntilDestroyed()`** (`takeUntilDestroyed` appears 57 times in the frontend; no leaks found).

`computed()` coverage is strong: 28+ across services and components (`replay-engine.service` 6, `strategy-builder.component` 16, `options-history.component` 6, `lean-engine.component` 8). No unmemoised derived state was flagged.

**No `.mutate()` usage anywhere.** Good.

### 4.2 Potential signal glitch (one, medium risk)

`strategy-builder.component.ts:449-451` computes `weightedIv` as:

```typescript
sum(iv_i * premium_i * quantity_i) / sum(premium_i * quantity_i)
```

across `enabledLegsParams()`. The numerator depends on three upstream signals (`legs`, `premium`, `quantity`, `iv`). During rapid leg-edit sequences (common during volatility), intermediate states can briefly surface if updates are not batched into a single microtask. This is the classic "diamond" signal glitch. Signals in Angular 21 are generally glitch-free for synchronous chains, but cross-signal sequences via `effect()` are not.

**Fix (P3):** wrap leg edits in a single `set()` on a composite signal, or assert in a test that `weightedIv` never emits a NaN/Infinity during a multi-step edit.

### 4.3 OnPush misses (3 components)

- `components/books/books.component.ts:6-33`
- `components/authors/authors.component.ts`
- `components/tickers/tickers.component.ts`

All three fetch-once reference-data components. Low real-world impact (they don't re-render during volatility), but CLAUDE.md's rule is "all components". **Fix (P2):** add `changeDetection: ChangeDetectionStrategy.OnPush` to all three.

### 4.4 Decorator violations (migration incomplete)

Rules forbid `@HostBinding`, `@HostListener`, `@Input()`, `@Output()` decorators. Audit found:
- **`@HostListener`**: 2 instances (`shell/app-sidebar.component.ts:485`, `shared/indicator-tooltip/indicator-tooltip.component.ts:83,95`)
- **`@Input()`**: 7 instances across 4 files (`strategy-lab/replay-chart`, `market-data/candlestick-chart`, `market-data/summary-stats`, `tickers/tradingview-widget`)
- `@HostBinding`, `@Output()`: none.

**Fix (P2):** migrate to `input()`, `output()`, and the `host: {...}` metadata object. Low-risk mechanical change.

### 4.5 Template violations (3 `[ngClass]`)

- `quality-modal.component.html:34`
- `chunk-queue.component.html:33`
- `readiness-score-card.component.html:31`

Rules forbid `[ngClass]`, `[ngStyle]`, `*ngIf`, `*ngFor`. The star-directives are fully migrated. `[ngClass]` is the last holdout.

**Fix (P3):** replace with `[class.foo]="cond"` or a `computed()` that returns a class string.

### 4.6 No use of `resource()`/`rxResource()` (v21 modern async)

Zero occurrences. Frontend uses `.subscribe() + takeUntilDestroyed()` and `firstValueFrom()` throughout. Safe (no leaks), but stale — the Angular 21 best practice is `resource()` for fetches.

**Fix (P3, optional):** migrate new fetches to `resource()`. Not worth a sweep of existing code.

### 4.7 Type safety leakage

22 `: any` / `as any` / `as unknown as` instances. Most are justifiable API-boundary adapters (`polygon.service.ts` for Polygon API, `strategy-builder.component.ts` for TradingView and QuantLib).

Two that are not justifiable:
- `data-lab-session.service.ts:22-24` — session state declared as `any[]` for bars, indicators, quality. Should have a typed `DataLabSession` interface.
- `data-lab.component.ts:315-317` — mirrors the untyped service.

**Fix (P2):** add a typed `DataLabSession` interface; propagate.

### 4.8 Math in the frontend (mostly acceptable)

Audit enumerates every numerical operation in services and components. Verdicts:
- **Acceptable (UI-transient)**: `strategy-builder.component.ts:414-507` (net cost, weighted IV, payoff curves, Greek curves at chart price points), `readiness-score.util.ts:95-103` (UI readiness heuristic), currency/percent formatting throughout.
- **Flagged**: `stock-analysis/chunk-detail.component.ts:74, 100` — volume aggregation (`reduce((sum, b) => sum + b.volume, 0)`). Pre-computable server-side; adds client CPU cost on large datasets.
- **None-critical**: No indicator math in the frontend. All EMA/RSI/MACD values come from backend. Good.

### 4.9 Display precision at the render boundary

Zero instances of lossy number pipes (`| number:'1.2-2'`) in indicator templates. Frontend renders values at full `number` precision from the GraphQL response. Precision loss, if any, happened upstream (see §2.1).

---

## 5. Containerized data throughput

### 5.1 Caching — **absent everywhere**

- **.NET:** no `IMemoryCache` / `IDistributedCache` registered. Every GraphQL query hits Python or the DB.
- **Python:** no `@lru_cache`, no Redis, no in-memory dict. Every `/api/indicators/calculate` and `/api/chart/data` request recomputes from scratch.
- **HTTP:** no `Cache-Control` / `ETag` / `Last-Modified` on any FastAPI route.

**Concrete cost:** a user scrolling a 500-bar chart and toggling four indicators re-fetches aggregates from Postgres and re-computes all four indicators in pandas-ta per toggle. Each round-trip is Angular → .NET → EF → DB → .NET → HTTP → Python → pandas-ta → HTTP → .NET → GraphQL → Angular. CPU-bound in pandas-ta.

**Fix (P2):** `IMemoryCache` in .NET keyed by `(ticker, timespan, indicator_name, window, from, to)` with 5-15 min TTL covers 80% of benefit. Python can add `functools.lru_cache(maxsize=1024)` on `_calc_sma` etc. keyed by a content hash of the bars.

### 5.2 N+1 risk in list resolvers — **unmitigated**

Hot Chocolate DataLoader is not used. Resolvers return `IQueryable<T>` (e.g., `Query.cs:27-38`, `PortfolioQuery.cs:55`). For list resolvers over entities with navigation properties (e.g., Portfolio → Positions → Lots → Trades) this is an N+1 landmine.

**Fix (P2):** migrate list resolvers to `DataLoader<TKey, TValue>`. Start with the portfolio graph — highest fanout.

### 5.3 `CancellationToken` drops — **3 call sites**

- `Backend/GraphQL/Query.cs:75-88` (`GetFetchProgress`) — synchronous, no CT.
- `Backend/Services/Implementation/PortfolioValuationService.cs:58-131` — CT not propagated to the internal EF query at lines 103-107.
- `Backend/GraphQL/Query.cs:122-123` (`GetOrFetchStockAggregates`) — context query doesn't receive the resolver's CT.

Long-running queries cannot be cancelled from the client. Not a correctness bug, a liveness/cost bug.

### 5.4 Silent price-fetch degradation — **HIGH**

`PortfolioValuationService.cs:151-154`:

```csharp
catch (Exception ex)
{
    _logger.LogWarning(ex, "[Valuation] Failed to fetch live prices, using empty price map");
}
return prices;  // empty dict → valuations compute with zero cost basis
```

If the Python service is briefly down, portfolio valuations silently report zero prices on all positions. This is a logged-warning path but produces completely wrong numbers without surfacing the failure to the user.

**Fix (P1):** propagate the failure to the GraphQL response as an error union type, or populate with stale prices from the DB snapshot with a `stale_since` field.

### 5.5 HTTP client hygiene — **PASS**

`Backend/Program.cs:44-85` registers typed `HttpClient`s with Polly retry (3× exponential) + circuit-breaker (15 events / 15s break), 300s timeout for QuantLib, 120s for sanitization/TA. `JsonNamingPolicy.SnakeCaseLower` applied at the client level. Good.

### 5.6 EF Core column types — **PASS**

`AppDbContext:82-87, 100, 113, 128-130, 156-174, 272-391`: all financial fields use `HasPrecision(18, 8)` → PostgreSQL `numeric(18, 8)`. 10-digit integer + 8-decimal gives ~1e-8 precision with headroom — fit for purpose.

### 5.7 Logging — **PASS**

All services use structured log templates (`LogInformation("[STEP X] {Var}", v)`). No string interpolation in log templates. Two `Console.WriteLine` calls in `Program.cs` at startup (acceptable). No `Console.WriteLine` in hot paths.

---

## 6. Cross-cutting: idempotency & state purity

| Subsystem | Verdict | Notes |
|---|---|---|
| pandas-ta `TechnicalAnalysisService` | Stateless | All methods `@staticmethod`; no module-level state; fresh DataFrame per call; byte-identical on re-run |
| Streaming `BaseIndicator` (SMA/EMA/RSI) | Stateful by design | Matches LEAN's live state machine; `.reset()` available; feeding the same bar twice is deduplicated by timestamp (`base.py:65-67`) |
| Backtest engine | Fully idempotent | Fresh `StrategyContext` / `Portfolio` / consolidators per `.run()`; no module-level state; no `datetime.now()` in fill/position logic; single-threaded; tests assert exact fill prices and quantities |
| .NET services | Pure proxy + aggregation | All numerical logic is aggregation (sum, cost-basis average) over stored state; no strategy or indicator computation |
| Angular signals | Pure | No `.mutate()`; signal-graph topology is a DAG rooted at service-owned signals |

No cross-cutting correctness concerns. The one identified risk is the signal-glitch pattern at §4.2.

---

## 7. The "35+ indicator" claim

The brief mentioned "35+ technical indicators defined in Python". That number is stale. Reality:

- `TechnicalAnalysisService.calculate_indicators` exposes **6** indicators (SMA, EMA, RSI, MACD, BBands, Stoch).
- `generate_indicator_table` produces a fixed set per `ta_service.py:151-275`: EMAs for 8 periods, BB, Supertrend, RSI, RSI-MA, MACD (fast/slow/signal), ADX — **16 columns** for a typical call.
- `pandas-ta`'s full catalog (`pandas_ta.Category`) is 151 indicators across 9 categories (momentum 43, overlap 36, trend 20, volume 19, volatility 16, statistics 10, candle 3, performance 2, cycle 2). Any of these is callable, but only the 6 are explicitly wrapped in the service.
- Streaming engine: 3.

So the project has **151 callable, 16 packaged, 6 wrapped, 3 ported**. Pick whichever is true to your audience.

Also worth noting: the dispatch at `ta_service.py:34-54` is an if-elif chain with a silent drop (`logger.warning` + `continue`) on unknown names. If the UI sends a typo'd indicator name, it silently returns an empty result list. **Fix (P2):** return 400 on unknown indicator name.

---

## 8. Recommended fixes, prioritised

### P0 — ship before the next demo (trivial, high value)

- **P0-A** (§2.3, §7): Add `None`-guard and unknown-name validation in `ta_service.py:61-73` + `:34-54`. Convert silent 500s into helpful 400s.
- **P0-B** (§2.5): `sanitizer.py:78` — emit tz-aware UTC timestamps. Fixes the "Z lie".
- **P0-C** (§2.6): Rename TS `message` → `error` on `CalculateIndicatorsResult` (`Frontend/.../market-data.service.ts:86` and `types.ts:121`) so Python errors surface in the UI. Or add a GraphQL alias.
- **P0-D** (§5.4): Propagate `PortfolioValuationService.cs:151-154` failures to the UI rather than returning an empty price dict.

### P1 — this sprint

- **P1-A** (§1, §3): Rewrite `CLAUDE.md` sections 1, 2, 3 to describe the dual indicator path honestly. Remove or qualify the "external deps eliminated" claim. List streaming-engine port status explicitly (3 ported, 0 golden fixtures, no LEAN checkout).
- **P1-B** (§3.3): Re-generate `BacktestResultType` and TS `BacktestResult` to carry the `lean_statistics`, `chart_bars`, `chart_indicators`, and per-trade percent/indicator-snapshot fields.
- **P1-C** (§3.9): Add `pnlPct` and `cumulativePnlPct` to `BacktestTradeType` and TS `BacktestTrade`. Unit-label fields explicitly.
- **P1-D** (§2.1, cap tolerances): Pick a number. Update `numerical-rigor.md` with separate Python-internal and cross-layer tolerances.

### P2 — this quarter

- **P2-A** (§5.1): `IMemoryCache` in .NET keyed by `(ticker, timespan, name, window, from, to)`; 10-minute TTL. Python `functools.lru_cache` on `_calc_*` keyed by a content hash.
- **P2-B** (§5.2): Migrate portfolio list resolvers to `DataLoader<TKey, TValue>`.
- **P2-C** (§4.3, §4.4, §4.5): Angular hygiene sweep — OnPush on the 3 missing components, migrate 7 `@Input()` → `input()`, replace 3 `[ngClass]` with `[class.x]`, type `data-lab-session.service.ts`.
- **P2-D** (§2.4): Unify the three Greeks shapes; surface `rho` in the snapshot and strategy paths.
- **P2-E** (§2.7): Remove or document the 6-decimal round at `ta_service.py:144`.
- **P2-F** (§3.2): Decide on one canonical OHLCV shape internally; collapse Shape A and Shape B.

### P3 — when the repo has more time than problems

- **P3-A** (§3.5, §3.6): Clone QuantConnect/Lean at a pinned SHA into `references/Lean/`, commit the SHA, regenerate per-indicator golden fixtures for SMA/EMA/RSI at `atol=1e-9, rtol=0`, populate `docs/references/`.
- **P3-B** (§4.6): Migrate frontend fetches to `resource()`/`rxResource()`.
- **P3-C** (§5.3): Propagate `CancellationToken` through the 3 identified drop sites.
- **P3-D** (§4.2): Cover the `weightedIv` signal-glitch window with a test during rapid leg edits.

---

## Appendix A: Documentation vs. reality (CLAUDE.md audit)

Because a CLAUDE.md that lies about invariants drives future agents to make wrong choices, verbatim:

| CLAUDE.md claim | Current reality | Drift |
|---|---|---|
| *"indicators... ported into this repo with strict numerical equivalence and vanishing external dependency"* (line 3) | 3 indicators ported; pandas-ta pinned in `requirements.txt:20`, `requirements-light.txt:20`, `requirements-lock.txt`; used in 8+ production modules | **FALSE** |
| *"Every ported indicator, strategy, or calculation ships with (a) a golden fixture derived from the reference, (b) a tolerance-pinned test, and (c) a citation in `docs/references/`"* (line 9) | None of the three ports ship with any of the three artefacts. `docs/references/` contains `.gitkeep` | **FALSE** |
| *"Reference code is studied, ported, and then the dependency is eliminated"* (line 10) | pandas-ta is not eliminated | **FALSE for service path, TRUE for engine path** |
| *"Vendored references in `references/` exist for audit, not for runtime use"* (line 11) | `references/` contains `.gitkeep` only | **FALSE** (nothing vendored) |
| *"Strict equivalence is the default."* (line 12) | 6-dp rounding in `ta_service.py:144` caps all pandas-ta indicators. No LEAN source to compare against for the streaming engine | **ASPIRATIONAL** |
| *"`.claude/rules/numerical-rigor.md`: `atol=1e-9, rtol=0` default for indicators"* | Achievable in Python for streaming engine; not achievable cross-layer (see §2.1) | **SCOPE UNDERSPECIFIED** |
| *"All stored timestamps are UTC, tz-aware. Naive datetimes are bugs."* (`numerical-rigor.md:77-78`) | `sanitizer.py:78` emits naive-with-`Z` timestamps | **VIOLATION** |

---

## Appendix B: Live empirical precision trace

**Input:** 12 synthetic OHLCV bars (closes: 100.75, 101.80, 102.90, 102.50, 102.10, 102.30, 103.10, 103.50, 103.20, 102.90, 103.20, 103.00).

### Hand-computed reference (Python arithmetic, float64)

| i | close | sma5 | ema5 (k=1/3) |
|---|---|---|---|
| 4 | 102.10 | 102.01 | 102.01 (seed = SMA) |
| 5 | 102.30 | 102.32000000000001 | 102.10666666666667 |
| 6 | 103.10 | 102.58 | 102.43777777777778 |
| 7 | 103.50 | 102.70 | 102.79185185185186 |
| 8 | 103.20 | 102.84 | 102.92790123456791 |
| 9 | 102.90 | 103.00 | 102.91860082304528 |
| 10 | 103.20 | 103.17999999999999 | 103.01240054869686 |
| 11 | 103.00 | 103.16 | 103.00826703246457 |

### FastAPI response (ta_service.py, pandas-ta, rounded to 6dp)

| i | sma5 | drift vs reference |
|---|---|---|
| 4 | 102.01 | 0 |
| 5 | 102.32 | `~1e-14` (rounded away) |
| 6 | 102.58 | 0 |
| 7 | 102.7 | 0 |
| 8 | 102.84 | 0 |
| 9 | 103.0 | 0 |
| 10 | 103.18 | `~1e-14` (rounded away) |
| 11 | 103.16 | 0 |

**Conclusion:** pandas-ta math is equivalent to textbook float64 to within 1e-14 (IEEE-754 noise). The 6-dp round at `ta_service.py:144` eliminates that noise at the cost of ~1e-14 meaningful precision — acceptable tradeoff for display, foreclosing for reconciliation.

### Failure mode reproduced

Request with schema mismatch (`{"params": {"period": 5}}` instead of `{"window": 5}`) returned `HTTP 500: 'NoneType' object has no attribute 'iloc'`. Root cause in §2.3. Confirmed live against `http://localhost:8000/api/indicators/calculate`.

### .NET GraphQL surface check

- Total GraphQL types: 216
- Total queries: 53; total mutations: 28
- `calculateIndicators` exists as a **Query** (not Mutation, despite taking an Input type); signature takes `(ticker, fromDate, toDate, indicators, timespan, multiplier)`. .NET fetches bars from its cache/DB then calls Python's bar-level indicator endpoint. See §3.4.

---

## Appendix C: File:line citation index

### Python

- `PythonDataService/app/services/ta_service.py:18-58` — indicator dispatch (if-elif, silent drop on unknown name)
- `PythonDataService/app/services/ta_service.py:61-73` — missing None-guard in SMA/EMA/RSI
- `PythonDataService/app/services/ta_service.py:96-136` — correct None-guard in MACD/BBands/Stoch
- `PythonDataService/app/services/ta_service.py:139-145` — 6-dp round and NaN-skip (warmup drop)
- `PythonDataService/app/services/sanitizer.py:78` — naive-UTC-with-`Z` timestamp emission
- `PythonDataService/app/models/requests.py:61` — `OhlcvBar` (Shape B with Unix ms)
- `PythonDataService/app/models/requests.py:166` — `IndicatorConfig` (`window: int = Field(14, ...)`)
- `PythonDataService/app/models/responses.py:28` — `IndicatorDataPoint` (`value: float | None`)
- `PythonDataService/app/routers/indicators.py:31-50` — `/api/indicators/calculate` route
- `PythonDataService/app/routers/backtest.py:53` — `BacktestTradeResponse` (with `pnl_pct`)
- `PythonDataService/app/routers/backtest.py:145` — `BacktestResponse` (the 25-field rich schema)
- `PythonDataService/app/engine/indicators/base.py:42-56` — compliant warmup (returns `None`)
- `PythonDataService/app/engine/execution/execution_config.py:31-75` — realism dataclass
- `PythonDataService/app/engine/execution/intrabar_resolver.py:14-16, 41-76` — pessimistic bracket rule, bar-magnifier deferral note
- `PythonDataService/app/engine/execution/portfolio.py:188-190` — accumulation-order-preserving average price
- `PythonDataService/app/engine/data/lean_format.py:37, 84-87` — Decimal(10000) scale decoding
- `PythonDataService/app/engine/engine.py:802` — `float(Decimal)` at response boundary
- `PythonDataService/app/engine/tests/extract_lean_fixture.py:5` — dangling path to a sandbox that no longer exists
- `PythonDataService/tests/test_indicator_parity.py:49-64` — self-parity test (not LEAN-parity)

### .NET

- `Backend/GraphQL/Query.cs:75-88, 122-123` — `CancellationToken` drops
- `Backend/GraphQL/Mutation.cs:817, 839` — older `BacktestResultType`/`BacktestTradeType` missing LEAN-parity fields
- `Backend/Models/DTOs/IndicatorModels.cs:3, 17` — `OhlcvBarDto`, `CalculateIndicatorsRequest`
- `Backend/Models/DTOs/PolygonResponses/AggregateData.cs:16` — `Transactions: decimal?` (should be `long?`)
- `Backend/Models/MarketData/StockAggregate.cs:7` — EF entity Shape A
- `Backend/Data/AppDbContext.cs:82-87, 100, 113, 128-130, 156-174, 272-391` — `HasPrecision(18,8)` throughout
- `Backend/Services/Implementation/PortfolioValuationService.cs:58-131` — CT drop
- `Backend/Services/Implementation/PortfolioValuationService.cs:151-154` — silent price-fetch degradation
- `Backend/Services/Implementation/PolygonService.cs:20-21` — `JsonNamingPolicy.SnakeCaseLower`
- `Backend/Program.cs:44-85` — typed HttpClient + Polly
- `Backend/Program.cs:129, 134` — acceptable startup `Console.WriteLine`

### Angular

- `Frontend/src/app/services/market-data.service.ts:60-89` — `CALCULATE_INDICATORS_QUERY` (with `message` instead of `error`)
- `Frontend/src/app/graphql/types.ts:24` — `StockAggregate` (Shape A)
- `Frontend/src/app/graphql/types.ts:117-121` — `CalculateIndicatorsResult` (`message: string | null`)
- `Frontend/src/app/graphql/types.ts:511` — `IndicatorTableRow` (Shape B time)
- `Frontend/src/app/services/data-lab-session.service.ts:22-24` — untyped session `any[]`
- `Frontend/src/app/components/books/books.component.ts:6-33` — missing OnPush
- `Frontend/src/app/components/strategy-lab/strategy-builder.component.ts:449-451` — `weightedIv` glitch candidate
- `Frontend/src/app/components/shell/app-sidebar.component.ts:485` — `@HostListener` violation
- `Frontend/src/app/components/shared/indicator-tooltip/indicator-tooltip.component.ts:83, 95` — `@HostListener` violations

### Docs / rules

- `CLAUDE.md:3, 8-12, 17, 21, 65` — the claims that no longer hold
- `.claude/rules/numerical-rigor.md:32-40` — golden-fixture / attribution requirement (unmet)
- `.claude/rules/numerical-rigor.md:62` — default indicator tolerance `atol=1e-9`
- `.claude/rules/numerical-rigor.md:77-78` — UTC/tz-aware rule (violated by sanitizer.py:78)
- `.claude/rules/numerical-rigor.md:86-90` — warmup rule (violated by ta_service.py:143)

---

## Closing

The repo's *engine* is the strongest part — strict, tested, state-isolated, with execution-realism knobs that preserve LEAN parity when left at defaults. The *docs* are the weakest part — they describe an aspirational world that the code does not ship. The *wire format* is the highest-leverage place to invest: a single round of P0 fixes (timestamp lie, error-field rename, None-guard, portfolio-valuation degradation) removes four silent-failure modes; a single round of P1 fixes (rewrite CLAUDE.md, re-wire BacktestResultType) removes two large categories of cross-layer drift.

The path forward isn't "finish porting 147 indicators from a LEAN checkout you don't have". It's "tell the truth in the docs, then invest in the wire format."
