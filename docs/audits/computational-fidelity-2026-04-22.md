# Computational Fidelity Audit

**Repo:** `learn-ai`
**Date:** 2026-04-22
**Auditor role:** Quant systems + reactive graph + contract enforcement specialists (5 parallel agents)
**Scope:** Six-dimension upgraded framework — mathematical stability, temporal integrity, reactive-graph correctness, cross-language contract enforcement + codegen proposal, throughput/latency. Computational-placement dimension was dropped at user request; deterministic bit-exact replay dropped at user request.
**Method:** Five parallel specialist agents, each running live against the stack (containers up throughout). Every finding has a file:line citation and a reproduced measurement or output. Computational-placement audit and bit-exact replay suite explicitly dropped per user direction.
**Relation to prior audit:** Companion to `docs/audits/structural-integrity-2026-04-22.md`. Each agent re-verified the prior audit's findings in its domain. The rollup is in § 7.

---

## 0. Executive summary

The prior audit found the stack was "more rigorous than it looked" at the engine level, but flagged a fragmented single-source-of-truth and a cross-layer precision funnel. **This audit finds the opposite pattern for the temporal, reactive, contract, and throughput dimensions: load-bearing bugs in the request/response path that quietly degrade correctness and UX, plus a contract surface so hand-maintained that drift is only a matter of time.** The math itself is clean at the engine level, but the two indicator paths disagree by up to 14 RSI points at warmup — a violation of the repo's own tolerance rules that happens on every short-history call.

**Severity legend (calibrated):**
- **CRITICAL** — active user-visible wrong data, data loss, or multi-second wasted compute
- **HIGH** — architectural precision/correctness loss, or high-probability near-future rot
- **MEDIUM** — latent, triggered by environmental flip or specific call shape
- **LOW** — hygiene

### Top-10 load-bearing flags

| # | Sev | Finding | Location |
|---|---|---|---|
| 1 | **CRITICAL** | `/api/sanitize` collapses ms-epoch timestamps by 10⁶ (1704067200000 → 1704067). Every bar lands on **1970-01-20** if re-interpreted as ms | `sanitizer.py:216` |
| 2 | **CRITICAL** | `_format_timestamp` emits `"2024-01-01 00:00"` (no T, no TZ). Browser `new Date(str)` parses as **local time** — 5-hour shift in ET browsers; active-vs-closed trade misclassification | `rule_based_backtest.py:252`, `strategies/common.py:115`, `replay-strategy.service.ts:18` |
| 3 | **CRITICAL** | Engine backtest returns **HTTP 500 `Out of range float values are not JSON compliant: inf`** after 3-5 s of compute. User waits, then sees generic error. Reproduced twice | `engine.py` / `engine/results/statistics.py` |
| 4 | **CRITICAL** | Data Lab cold chart load **5.13 s**; warm **717 ms**. 6 MB wire body per load. Event loop blocked during this window (availability check on a parallel page took 2.0 s) | `chart_service.py:878`, `chart.py:65` |
| 5 | **CRITICAL** | Two Python routers (`backtest.py`, `rule_based_backtest.py`) define full `LeanStatistics` response shapes but are **never registered** in `main.py`. `POST /api/backtest/run` → HTTP 404. The code is dark; any reader believes LEAN-parity stats exist when they do not | `app/main.py:61-78`, `backtest.py:145`, `rule_based_backtest.py` |
| 6 | **CRITICAL** | `IndicatorTableResult.rows` is `List<string>` of JSON-serialized row dicts. Frontend `JSON.parse`s and casts to a hopeful `IndicatorTableRow` type that is never actually returned. Structured-data contract collapsed to string at the .NET layer | `Types/IndicatorTableResult.cs:17`, `graphql/types.ts:502` |
| 7 | **CRITICAL** | Data Lab: `get selectedNames(): Set<string>` allocates a new Set on every template read. Called via `isSelected(ind.name)` in a category grid → **~1,040 Set allocations per keystroke** in a parameter field | `data-lab.component.ts:231`, `:607-649` |
| 8 | **HIGH** | Two RSI implementations diverge by **up to 14 RSI points** at warmup, converge to `atol=1e-9` only after **316 bars**. Violates `numerical-rigor.md:62` on every short-history call | `ta_service.py:71-73` vs `engine/indicators/rsi.py:29-88` |
| 9 | **HIGH** | Options `last_trade.sip_timestamp` and `last_quote.last_updated` dropped by .NET `LastTradeSnapshotDto`/`LastQuoteSnapshotDto`. **A 30-minute-old quote renders identical to a 2-second-old quote** | `Backend/Models/DTOs/PolygonResponses/OptionsChainSnapshotResponse.cs:53-69` |
| 10 | **HIGH** | Zero codegen. 209 hand-written TS interfaces, ~80 hand-written C# DTOs + GraphQL types, 30+ raw-string GraphQL queries that bypass even basic typing. Four Greeks shapes, two OHLCV shapes, two BacktestResult shapes, all hand-maintained | repo-wide |

**Underlying pattern:** the codebase has both the correct idiom and the broken counterpart living side by side in nearly every dimension — correct `Decimal` math in the streaming engine vs `round(,6)` truncation in `ta_service`; correct `utc=True` isoformat in `data_quality_service` vs naive-Z lie in `sanitizer`; correct `DateTimeOffset.FromUnixTimeMilliseconds` in `Mutation.cs:262` vs fragile `DateTime.Parse` in `MarketDataService.cs:450`. The team knows what good looks like; the violations are enforceable with targeted lint + codegen + two schema unifications.

---

## 1. Mathematical Red Flags

### 1.1 Prior-finding rollup (math domain)

| Prior finding | Location | Status at HEAD `14d66cf` |
|---|---|---|
| `_calc_sma/_calc_ema/_calc_rsi` missing None-guard → HTTP 500 when `window > len(bars)` | `ta_service.py:61-73, 139-145` | **CONFIRMED, unfixed.** Reproduced live: `curl` with 2 bars + `window=5` → `HTTP 500 {"detail":"...'NoneType' object has no attribute 'iloc'"}` on SMA/EMA/RSI. Sibling `macd/bbands/stoch` correctly guard. |
| `round(float(v), 6)` precision cap before wire | `ta_service.py:144` | **CONFIRMED.** Close `10.1234567891` → response `10.207819` (exact 6dp; true mean `10.207818929…`). |
| pandas-ta warmup rows silently dropped | `ta_service.py:143` | **CONFIRMED.** SMA(3) on 5 bars → 3 data points; warmup bars 1-2 vanish with no index. |

### 1.2 [HIGH] Two indicator paths disagree by up to 14 RSI points at warmup; convergence requires 316 bars

**Location:** `PythonDataService/app/services/ta_service.py:71-73` (pandas-ta `ta.rsi`) vs `PythonDataService/app/engine/indicators/rsi.py:29-88` (streaming Wilders).

**Evidence:** Seed 42, 2000-bar random walk, bar-by-bar comparison:

| bar | pandas-ta | streaming | drift |
|---|---|---|---|
| 14 | 34.210965 | 48.443493 | **1.42e+01** |
| 50 | 31.081603 | 32.161516 | 1.08e+00 |
| 100 | 36.311126 | 36.340303 | 2.92e-02 |
| 200 | 44.019961 | 44.019976 | 1.53e-05 |
| 300 | 54.018679 | 54.018679 | 6.48e-09 |
| 500 | 47.674722 | 47.674722 | -1.28e-13 |

First convergence to `atol=1e-6` at bar 234; to `atol=1e-9` at bar 316.

**Root cause:** `rsi.py:44-46` emits `is_ready=True` at `samples >= period + 1` and seeds `_avg_gain/_avg_loss` with SMA of the first `period` deltas (canonical Wilders seeding). `ta.rsi` uses `ewm` from bar 1 with no proper seed; early values are artefacts. `tests/test_indicator_parity.py:49-64` does not catch this because it compares pandas-ta against an inline re-implementation of pandas-ta, not against the streaming engine.

**Impact:** Any chart or backtest over a short window (<316 bars) silently shows two different RSI values for the same inputs — up to 14 RSI points apart, i.e. the difference between "overbought" and "neutral". Violates `.claude/rules/numerical-rigor.md:62` (`atol=1e-9` for indicator values) systematically.

**Remediation:** (a) document convergence-after-warmup semantics in both RSI module docstrings; (b) mask `ta.rsi` output for `i < ~3*period` — emit `None` until converged; (c) add cross-path golden fixture test that pins `atol=1e-9` at a documented first-valid-bar.

### 1.3 [HIGH] pandas-ta RSI is history-sensitive — slicing the input mid-history silently changes values at matched bars

**Location:** `ta_service.py:71-73`.

**Evidence:** Same synthetic, N=2000, value at absolute bar 999:

| slice start | value at bar 999 of full | value at same bar of slice | drift |
|---|---|---|---|
| 0 | 46.117507 | 46.117507 | 0 |
| 500 | 46.117507 | 46.117507 | 0 |
| 900 | 46.117507 | **46.079838** | **3.77e-02** |

At ~100 pre-target bars the drift is 5.7e-03; at 14 bars it's **31 RSI points**. Same shape on the streaming path — Wilders requires ~20·period samples to reach steady state, beyond the formal `is_ready` point.

**Impact:** A caller fetching 50 bars of minute data and asking for RSI(14) gets values with 0.3–30 RSI-point error at the front. Chart overlays and strategy entries that key off early RSI use noise.

**Remediation:** Either mask with `min_warmup_bars = 3 * period` or document the convergence boundary loudly in every consumer.

### 1.4 [MEDIUM] `rule_based_backtest` stores a running accumulator instead of recomputing from the trade list

**Location:** `rule_based_backtest.py:133, 166, 209`.

**Evidence:** `cum_pnl_pct += pnl_pct` accumulated and assigned to `result.total_pnl_pct`. At 10,000 trades the drift from `np.sum` is 3.5e-18 — well inside tolerance. The same function (line 210) correctly uses `sum(t.pnl for t in trades)` for `total_pnl_pts`, so the inconsistency is the real finding.

**Impact:** Latent violation of `numerical-rigor.md:93-97` (accumulation order). Idempotency under trade reordering is also broken.

**Remediation:** `result.total_pnl_pct = sum(t.pnl_pct for t in trades)` — one-line fix.

### 1.5 [MEDIUM] `SimpleMovingAverage._sum` subtractive maintenance safe only because `Decimal`

**Location:** `app/engine/indicators/sma.py:30-33`.

**Evidence:** Subtractive rolling sum. With closes ≈ 1e8, internal `Decimal` sum is exact at all tested lengths (10,000 bars); the observed ~1.5e-8 drift vs `pandas.rolling(20).mean()` is the `float(Decimal)` display-boundary conversion, not internal drift.

**Impact:** If anyone swaps `Decimal → float` for speed, error accumulates at magnitudes of 1e8 (equities prices).

**Remediation:** Comment at `sma.py:24` noting the load-bearing invariant.

### 1.6 [LOW] No Kahan / `math.fsum` anywhere — acceptable, but undocumented

`numerical-rigor.md` is silent on why Kahan isn't needed. Evidence-based justification: money-bearing accumulators (`portfolio.py:188-205`, `sma.py:30-33`, `ema.py:42`, `rsi.py:79-80`) are all `Decimal` (exact). Float accumulators operate at trade counts where `numpy.sum` (pairwise) is enough. Add an explicit rule: "Decimal-in-engine supersedes Kahan; float accumulators use `math.fsum`/`np.sum` (never `+=` in a loop) at trade counts > 1e4."

### 1.7 [LOW] Streaming EMA vs pandas-ta EMA agrees to ~1.4e-14 (ULP noise)

Two paths agree to a single ULP at magnitude 1e2 — well inside `atol=1e-9`. Documented so future auditors don't chase this as signal.

---

## 2. Temporal Consistency Report (NEW)

### 2.1 Prior-finding rollup (temporal domain)

| Prior finding | Status |
|---|---|
| `sanitizer.py:78` emits naive timestamps with `Z` suffix | **CONFIRMED.** `pd.to_datetime(..., unit="ms")` (no `utc=True`) + `.dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")` — the pandas object is tz-naive; the `Z` is a lie. Same pattern at `sanitizer.py:140` for trades. |

### 2.2 [CRITICAL] `/api/sanitize` collapses ms-epoch timestamps by a factor of 10⁶

**Location:** `PythonDataService/app/services/sanitizer.py:216`.

**Evidence:**
```
POST /api/sanitize with timestamp=1704067200000 (2024-01-01 UTC)
Response: "timestamp": 1704067
```

Reproduced via `curl -X POST http://localhost:8000/api/sanitize`. Root cause: pandas 3.0's `DatetimeIndex.astype("int64")` returns microseconds, not nanoseconds. The site at `dataset_service.py:322` has a comment acknowledging the hazard and uses `total_seconds() * 1000` correctly — `sanitizer.py:216` does not.

**Impact:** `Backend/Models/DTOs/SanitizeModels.cs:5` documents "timestamps are Unix milliseconds for lossless C# ↔ Python serialization". If consumers re-interpret the returned `1704067` as ms-epoch, every bar collapses to **1970-01-20**. Cross-series joins keyed on timestamp silently produce empty joins — no exception, no warning.

**Remediation:** Keep the original ms-epoch column unchanged; do not round-trip through `pd.to_datetime`. Regression test asserting `input_ms == output_ms`.

### 2.3 [CRITICAL] `_format_timestamp` emits ambiguous strings that parse as LOCAL time in non-UTC browsers

**Location:** `rule_based_backtest.py:252-260` and `strategies/common.py:115-123` (identical helper). Consumed in `Frontend/src/app/services/replay-strategy.service.ts:18-48` via GraphQL `runRuleBasedBacktest` (`Mutation.cs:785-802`).

**Evidence:**
```
Python:       _format_timestamp(1704067200000) → '2024-01-01 00:00'
Backend:      string pass-through (Mutation.cs:789)
Browser (ET): new Date('2024-01-01 00:00').getTime() = 1704085200000
Intended UTC:                                           1704067200000
Δ = 18,000,000 ms = 5 h
```

ECMAScript for date-time strings without ISO `T` and without tz designator is implementation-defined; Chrome/Safari → local time; Firefox historically → `Invalid Date`. The in-function comment claims "ISO 8601" — it is not.

**Impact:** `ReplayStrategyService` compares `new Date(t.entryTimestamp).getTime() <= new Date(currentBar.timestamp).getTime()`. `currentBar.timestamp` is numeric ms (UTC); `entryTimestamp` is string (local). In a US-East browser this mis-classifies any trade whose entry bar is within 5 h of `currentBar` — active vs closed positions flip. Currently latent (no component consumes `runRuleBasedBacktest` on a user-facing page), but service wiring and types are live; the first page to use it ships wrong results.

**Remediation:** Return `datetime.fromtimestamp(int(ts)/1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")`, or better return unix-ms `int` and change the TS side to `number`. Add a Vitest with `TZ=America/New_York` asserting trade classification survives the UTC/ET boundary.

### 2.4 [HIGH] Options snapshots lose all freshness signal at the .NET boundary

**Location:** `Backend/Models/DTOs/PolygonResponses/OptionsChainSnapshotResponse.cs:53-69` — `LastTradeSnapshotDto`, `LastQuoteSnapshotDto`.

**Evidence:** Python emits `sip_timestamp: <ns epoch>` on every last-trade and `last_updated: <ns epoch>` on every last-quote (`polygon_client.py:338, 350`). The .NET DTOs define only `Price, Size, Exchange, Timeframe` / `Bid, Ask, BidSize, AskSize, Midpoint, Timeframe`. `JsonNamingPolicy.SnakeCaseLower` silently drops unmapped fields. GraphQL output types expose no staleness field.

**Impact:** A 30-minute-old quote renders identical to a 2-second-old quote in `options-chain-v2/options-chain.component.ts:412-419` and `strategy-builder.component.ts:858-872`. Especially dangerous for expired contracts where Polygon returns stale `last_updated` long after the contract stopped trading.

**Remediation:** Add `long? SipTimestamp`, `long? LastUpdated` to the DTOs; surface through GraphQL; render "as of HH:MM:SS ET" in the UI.

### 2.5 [HIGH] `DateTime.Parse` on the sanitizer ISO string yields `Kind=Local`

**Location:** `Backend/Services/Implementation/MarketDataService.cs:450`.

**Evidence:** Reproduced inside `my-backend`:
```
DateTime.Parse("2024-01-01T00:00:00.000000Z") → Kind=Local
.ToUniversalTime() → Kind=Utc (correct wall-clock because container is UTC)
```

In the current UTC container the `.ToUniversalTime()` is a no-op so stored bars are correct. Flip the container to `TZ=America/New_York` (local dev, CI, operator override) and every stored bar shifts silently.

**Impact:** Correctness depends on the container TZ == UTC. No test captures the invariant.

**Remediation:** `DateTime.Parse(str, CultureInfo.InvariantCulture, DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal)`. Better: change the DTO to `long Timestamp` (ms) and use `DateTimeOffset.FromUnixTimeMilliseconds(...).UtcDateTime` as already done at `Mutation.cs:262`.

### 2.6 [HIGH] No request sequencing in chart / backtest UIs — late responses overwrite fresh ones

**Location:** `data-lab-chart.component.ts:361-404`, `strategy-lab.component.ts:716-728`, `lean-engine.component.ts:334-344`.

**Evidence:** Every `fetchChartData()` / `runBacktest()` does `await firstValueFrom(http.post(...))` then unconditionally `.set(response)`. No request ID, no `AbortController`, no `switchMap`. Two clicks in close succession race — later response may arrive first.

**Impact:** User picks "5m", immediately picks "1D", the UI shows bars from whichever request the server happened to finish second. Same hazard on every backtest re-run.

**Remediation:** Convert to `rxResource({ request: ..., loader: ({request, abortSignal}) => ... })` (Angular v21 idiom per `.claude/rules/angular.md`). Alternative: attach a monotonic request ID and drop stale responses.

### 2.7 [MEDIUM] No response-level versioning / `generated_at` field on any response

**Location:** `Backend/GraphQL/Types/SmartAggregatesResult.cs`, `CalculateIndicatorsResult.cs`, `FetchAggregatesResult.cs`; `Frontend/src/app/graphql/types.ts:24-68`.

**Evidence:** The only `generated_at` in the repo is inside the CSV dataset metadata (`dataset_service.py:701, 897`) — not on any GraphQL response. No `version`, `etag`, `requestId`, `asOf`, or `refreshedAt`. Apollo `InMemoryCache` is configured at `app.config.ts:29` but unused (services bypass it with raw `HttpClient.post`). If anyone "refactors to use Apollo properly", `__typename + id` normalization will cache aggregates forever with no TTL — no version field lets the UI detect the staleness.

**Remediation:** Add `asOfUnixMs: Long!` to root result types. Emit from Python's `int(time.time() * 1000)` at build. Render "as of HH:MM:SS ET" in the UI; use as cache key if Apollo is ever adopted.

### 2.8 [MEDIUM] Seven deprecated `datetime.utcnow()` / `datetime.utcfromtimestamp()` sites

**Location:** `data_quality_service.py:37, 54, 461`; `dataset_service.py:617, 701, 897`; `validation_service.py:205`.

**Evidence:** Python 3.12 emits `DeprecationWarning: datetime.datetime.utcnow() is deprecated`; scheduled for removal. Two sites (`data_quality_service.py:54`, `dataset_service.py:617`) re-enact the sanitizer-78 naive-Z lie in the CSV `iso_time` column.

**Remediation:** Replace with `datetime.now(UTC)` / `datetime.fromtimestamp(x, tz=UTC)`. For the two CSV sites also fix the format string.

### 2.9 [MEDIUM] `rule_based_backtest` sorts input but doesn't dedupe

**Location:** `rule_based_backtest.py:108`.

**Evidence:** `df = df.sort_values("timestamp").reset_index(drop=True)` — no dedupe, no monotonicity assertion. `sanitizer.py:56` and `data_quality_service.py` dedupe correctly. If upstream returns duplicate timestamps (CLAUDE.md explicitly warns about Polygon's 07:00 ET inflated-close issue), two trades can be emitted at the same ms in non-deterministic order.

**Impact:** Non-reproducible backtests on duplicate-timestamp input. Violates `numerical-rigor.md` strict-float requirement — same inputs, different outputs.

**Remediation:** `df = df.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)` + assert `df["timestamp"].is_monotonic_increasing`.

### 2.10 [LOW] Apollo `InMemoryCache` configured but unused — latent staleness trap

**Location:** `Frontend/src/app/app.config.ts:29`.

**Evidence:** `provideApollo` creates a full cache. Only demo services (`book.service.ts`, `author.service.ts`) use `apollo.watchQuery`. Production services (`market-data`, `portfolio`) bypass Apollo with raw `HttpClient.post`.

**Remediation:** Either configure per-type policies (`keyFields: false` on volatile types, `fetchPolicy: 'no-cache'` for market data) **or** drop Apollo entirely in favor of `httpResource` + Signal Forms as `angular.md` recommends.

### 2.11 [LOW] Unqualified `datetime.now()` in Polygon date-window construction

**Location:** `polygon_client.py:225, 228, 240, 276`; `fred_service.py:145`.

**Evidence:** `datetime.now().strftime("%Y-%m-%d")`. Correct only because the container runs UTC. A TZ flip shifts query windows by up to a day.

**Remediation:** `datetime.now(UTC).strftime(...)`.

---

## 3. Architectural Drift Report

### 3.1 Inventory

| Surface | Hand-written | Codegen |
|---|---|---|
| Frontend TS types (server-derived) | **209 interfaces** across `graphql/types.ts` (572 lines, ~60), `graphql/portfolio-types.ts` (295 lines, ~25), service-local view models (~120) | **0** |
| Frontend GraphQL operations | 3 queries use `gql` template tag; **30+ inline raw string queries** passed to Apollo as `query: string` (`market-data.service.ts`, `portfolio.service.ts`, etc.) | 0 |
| Backend C# DTOs | **~80** across `Backend/Models/DTOs/` (34 files), `Backend/Models/MarketData/` (EF entities), inline `*Result` types in `Query.cs`/`Mutation.cs` | 0 |
| Python Pydantic models | 141 schemas across `app/models/{requests,responses,strategy,research_models,indicator_reliability_models}.py` and scattered inline in routers | 0 (authoritative source) |

**Query definition style is the canary.** `market-data.service.ts` passes raw template-literal GraphQL strings to Apollo with no type binding. A misspelled field name compiles cleanly, becomes `undefined` at runtime, and is never caught by the TS compiler.

### 3.2 [CRITICAL] Dark Python routers define full LEAN-parity response shapes but are never registered

**Location:** `PythonDataService/app/routers/backtest.py:145` (`BacktestResponse` — 25 fields + 35-field `lean_statistics` block); `PythonDataService/app/routers/rule_based_backtest.py` (full shape).

**Evidence:** `app/main.py:61-78` enumerates every `include_router` call. Neither router is listed. Live probe:
```
curl -X POST http://localhost:8000/api/backtest/run  → HTTP 404
```

The active Python backtest path is `/api/engine/backtest` returning `EngineBacktestResponse` — a *different* 20-field shape with `final_equity`, `fill_mode`, `statistics`, `lean_statistics: null | null` [sic], `equity_curve`. Note that `.NET`'s `runBacktest` mutation (`Mutation.cs:99-220`) is in-process only — it does not call Python at all. So the prior audit's finding "Python-to-.NET drops `lean_statistics`" is subtler: **Python's `lean_statistics` producer is unreachable from every HTTP caller.**

**Impact:** 180+ lines of Pydantic models + pipeline code + test suite (`test_spy_validation.py`) suggest LEAN-parity stats exist in the hot path. **They do not.** Any reader (AI or human) trusting the code's structure will form a wrong mental model of what the system does.

**Remediation:** Either (a) register the routers and wire GraphQL through them, or (b) delete the dead code. **Preferred: delete.** Today's active path is `engine.py`, that's the canonical shape.

### 3.3 [CRITICAL] `CalculateIndicatorsResult.error` vs `.message` rename still present

| Field | Py (`responses.py:156`) | .NET (`Types/CalculateIndicatorsResult.cs:8`) | TS (`graphql/types.ts:121`) |
|---|---|---|---|
| error / message | `error: str \| None` | **`Message: string?`** | **`message: string \| null`** |

.NET deserializes Python's `error` correctly (via `SnakeCaseLower`) but the GraphQL output constructs `Message`. Frontend reads `message`. Python errors silently disappear from the UI. Prior audit flagged in §2.6; **unresolved**.

### 3.4 [CRITICAL] `IndicatorTableResult.rows` — structured data flattened to JSON strings

**Location:** `Types/IndicatorTableResult.cs:17`, `graphql/types.ts:502`.

| Field | Py | .NET | TS |
|---|---|---|---|
| rows | `list[dict[str, Any]]` | **`List<string>`** (JSON-serialized row dicts) | **`string[]`** with comment `// JSON-serialized row dicts` |

Frontend `JSON.parse`s per row and casts to a hopeful `IndicatorTableRow` interface (`types.ts:511`) that is never actually the type of any field the GraphQL query returns. The index signature `[key: string]: number | null` is an aspiration defeated by the `string` wire format.

**Remediation:** Structured `List<IndicatorTableRow>` at the C# layer; a GraphQL `JsonValue` / `AnyType` scalar for the truly-dynamic keys. Codegen will then pass the structure through end to end.

### 3.5 [HIGH] Four parallel Greeks shapes; TS `GreekType` union includes `'rho'` that no endpoint returns except QuantLib

| Source | delta | gamma | theta | vega | rho | d1 | d2 |
|---|---|---|---|---|---|---|---|
| Py `GreeksSnapshot` (chain snapshot) | opt | opt | opt | opt | — | — | — |
| Py `GreeksResult` (strategy analyze) | req | req | req | req | — | — | — |
| Py `QuantLibPriceResponse` (pricer) | req | req | req | req | req | opt | opt |
| .NET `GreeksSnapshotDto` | `decimal?` | `decimal?` | `decimal?` | `decimal?` | — | — | — |
| .NET `GreeksDto` | `decimal` | `decimal` | `decimal` | `decimal` | — | — | — |
| .NET `GreeksResult` (GraphQL, `Query.cs:1329`) | `decimal?` | `decimal?` | `decimal?` | `decimal?` | — | — | — |
| .NET `StrategyGreeksResult` (GraphQL, `Query.cs:1563`) | `decimal` | `decimal` | `decimal` | `decimal` | — | — | — |
| .NET `QuantLibPriceResult` (GraphQL, `Query.cs:1598`) | `decimal` | `decimal` | `decimal` | `decimal` | **`decimal`** | `decimal?` | `decimal?` |
| TS `GreeksSnapshot` (`types.ts:198`) | `number \| null` | ... | ... | ... | — | — | — |
| TS `GreeksResult` (`types.ts:371`) | `number` | ... | ... | ... | — | — | — |
| TS `QuantLibPriceResult` (`types.ts:406`) | `number` | ... | ... | ... | **`number`** | `number \| null` | `number \| null` |
| TS `GreekType` union (`types.ts:446`) | `'delta' \| 'gamma' \| 'theta' \| 'vega' \| 'rho'` | — it is a lie for every endpoint except `quantLibPrice` |

Selecting "rho" on any chart wired to snapshot or strategy Greeks renders `undefined`. Four separate `GreeksResult`-flavored classes is the repo's biggest contract smell.

### 3.6 [HIGH] OHLCV — two shapes, timestamp-type drift

| Field | Py `OhlcvBar` (req) | Py wire (sanitized) | .NET `OhlcvBarDto` | .NET `AggregateBar` (GraphQL) | TS `StockAggregate` | TS `IndicatorTableRow` |
|---|---|---|---|---|---|---|
| timestamp | `int` (ms) | ISO string (sanitizer) | `long` | `DateTime` | `string` | `number` |
| open/high/low/close/volume | `float` | `float` | `decimal` | `decimal` | `number` | `number \| null` |
| vwap | — | `float?` | — | `decimal? Vwap` | `number \| null` | — |
| transactions | — | `long?` | — | `long? TransactionCount` | `number \| null` | — |
| id | — | — | — | `long Id` (EF PK leak) | `number` (**required**) | — |
| multiplier/timespan | — | — | — | `int`, `string` | **required** | — |

`timestamp` is string in one shape, number in another; the string shape is the one that lies about UTC (§2.1). No type-level bridge between views.

### 3.7 [HIGH] `BacktestResult` missing ~10 fields vs the Python shape it claims to mirror

8 KPI fields (`winRate`, `avgWinPct`, `avgLossPct`, `winLossRatio`, `profitFactor`, `expectancyPerTrade`, `totalPnlPct`, `maxDrawdownPct`), plus `leanStatistics`, `chartBars`, `chartIndicators`, plus per-trade `pnlPct`, `cumulativePnlPct`, `indicatorSnapshot` — all absent in `BacktestResultType` (`Mutation.cs:817`) and `BacktestResult` (`types.ts:336`).

`BacktestResultType.Parameters: string?` — JSON dump of what's structured `dict[str, Any]` in Python. Frontend must `JSON.parse` and cast.

The `TotalPnL` field overrides HC v15's default camelCase via `[GraphQLName("totalPnL")]` — mid-word capital. A hand-typed client that guesses `totalPnl` (the default) silently gets `undefined`. Exactly the class of bug codegen eliminates.

### 3.8 [HIGH] `RuleBasedBacktestResult` duplicates `BacktestResult` with `double` instead of `decimal`

`Mutation.cs:894` and `types.ts:533` define a parallel `RuleBasedBacktestResultType` / `RuleBasedBacktestResult` with the KPI block that `BacktestResult` is missing. Same concept, two incompatible shapes — a user switching strategies gets a different wire shape. `RuleBasedBacktestResultType` uses `double` end to end — contradicting the prior audit's finding that the .NET layer is "type-consistent with `decimal(18,8)` end-to-end". Float ↔ decimal drift inside the same response family.

### 3.9 [HIGH] `SnapshotContractResult` — phantom field expansion

Python `LastTradeSnapshot` has `conditions: list[int] | None, sip_timestamp: int | None`; `LastQuoteSnapshot` has `last_updated: int`. All three are dropped at `.NET`'s `LastTradeSnapshotDto`/`LastQuoteSnapshotDto` and never reach GraphQL. See § 2.4 for the functional consequence.

### 3.10 [MEDIUM] `parameters` structural collapse

- Py `BacktestRequest.parameters: dict[str, Any]`, `BacktestResponse.parameters: dict[str, Any]`
- .NET `BacktestResultType.Parameters: string?` (JSON dump)
- TS `BacktestResult.parameters: string | null`
- `runRuleBasedBacktest` mutation: `parametersJson: string = "{}"` argument

Codegen can't fix `string` on both sides faithfully — the fix is a GraphQL `JsonValue`/`AnyType` scalar (HC v15 supports it). But once the schema changes, codegen preserves the fix forever.

### 3.11 [MEDIUM] `IndicatorConfig` name is a free string on the wire, validated only in Python

| | Py (`requests.py:72`) | .NET (`IndicatorModels.cs:12`) | .NET GraphQL input (`Query.cs:1404`) |
|---|---|---|---|
| name | `str` + whitelist `[sma,ema,rsi,macd,bbands,stoch]` | `string Name` | `required string Name` |

`ta_service.py:18-58` silently drops unknown names — request with `name: "RSI"` (uppercase) returns an empty indicator list with no error. Codegen alone can't fix this; enum-ified `IndicatorName` at the GraphQL layer (as an enum scalar) brings it to compile time.

### 3.12 [MEDIUM] `any` / `unknown` leaks in frontend graphql types

- `portfolio-types.ts:219`: `MutationResult<T = unknown>`
- `data-lab-session.service.ts:22-24` (prior audit §4.7): `any[]` for bars/indicators/quality
- `types.ts:529`: index signature `[key: string]: number | null` on `IndicatorTableRow` — broken by the JSON-string wire format

### 3.13 [LOW] `[GraphQLName]` naming inversions are codegen's exact sweet spot

`TotalPnL` (source `total_pnl_pts` — different name entirely!), materialized via `[GraphQLName("totalPnL")]` — rename upstream silently breaks TS without compile error because the TS side reads a string literal in the GraphQL query. Same class: `AverageVwap`, `Vwap` (entity-level `VolumeWeightedAveragePrice` → wire `Vwap` → TS `vwap`).

---

## 4. Reactive Graph Analysis

Scope: engine-lab (`/engine`) + data-lab (`/data-lab`) only, per user direction.

### 4.1 Engine Lab — DAG summary

**Primary signals (18):** `strategies`, `strategiesLoading`, `strategiesError`, `selectedStrategyName`, `paramValues`, `resolution`, `fillMode`, `startDate`, `endDate`, `initialCash`, `selectedTimezone`, `commissionPerOrder`, `activeTab`, `running`, `selectedStudyForReplay`, `preflight`, `result`, `runError`, `autoFetch`, `availability`, `availabilityLoading`, `availabilityError`.

**Computed (12):** `preflightBlocks[preflight]`, `strategyIndicators[selectedStrategyName]`, `preflightTimeframe[resolution]`, `chartBars[result]`, `chartTrades[result]`, `equityCurve[result]`, `insights[result]`, `insightSummary[result]`, `effectiveSymbol[paramValues]`, `availableStrategies[resolution,strategies]`, `selectedStrategy[selectedStrategyName,strategies]`, `paramEntries[selectedStrategy]`.

**Effects (2 parent + 2 tv-compat + 1 engine-chart + 1 engine-replay):**
- Parent A `[effectiveSymbol,startDate,endDate,resolution]` → `checkAvailability` → writes 3 signals
- Parent B `[availableStrategies,selectedStrategyName]` → `onStrategyChange` → writes 4 signals (**signal-writing effect, no `allowSignalWrites`**)
- TV-compat X `[tvCompatible]` → writes 6 signals (**signal-writing effect, no `allowSignalWrites`**)
- TV-compat Y `[strategyName,symbol,startDate,endDate,timeframe,indicators,sessionFilter,warmupDays,priceAdjustment]` → HTTP `runPreflight` → no abort, no debounce
- Engine-chart `[chartBars,trades,equityCurve]` → `setTimeout(renderAll)` — fires **3× per Run click** (once per input signal settling)
- Engine-replay `[study]` → hand-rolled RxJS `forkJoin` subscribe, no cleanup

### 4.2 Data Lab — DAG summary

**Primary signals (~35):** config (`ticker`, dates, session, indicators, …), loading/error states, session panel state, dataset/indicator registry.

**Computed (10):** `fromDate[fromDateValue]`, `toDate[toDateValue]`, `disabledDates[holidays]`, `holidayMap[holidays]`, `dateRangeWarning[fromDate,toDate]`, `entryCount[entries]`, `chartIndicators[entries]`, `currentTimeframeKey[multiplier,timespan]`, `timeframeWarnings[currentTimeframeKey,entries]`, `estimatedColumns[entries,indicatorMap]`.

**Effects: none in `DataLabComponent`.** All side effects driven by method handlers.

**Non-signal hot-path getter:** `selectedNames` rebuilds a `Set` on every DOM read — see § 4.3.

### 4.3 [CRITICAL] Data Lab `selectedNames` — ~1,040 Set allocations per keystroke

**Location:** `data-lab.component.ts:231` (`get selectedNames()`), `:607-613` (`updateEntryParam`).

**Evidence:** Template calls `isSelected(ind.name)` → getter `this.selectedNames` → `new Set(this.entries().map(e => e.name))`. Category grid has ~8 categories × ~10 indicators = 80 calls per template render, plus the parameter-panel cells. `updateEntryParam` rewrites the `entries` array on every keystroke in a numeric input → 4 downstream computeds invalidate + 80–130 getter calls × Set rebuild per CD pass.

**Impact:** Measurable UI stutter on parameter edits. Backend is fast; the slowness is ours.

**Remediation:** Promote to `computed<Set<string>>()`. One-line change, biggest UX win in this audit.

### 4.4 [HIGH] TV-compat preflight — signal-writing effect without `allowSignalWrites`

**Location:** `tv-compat-panel.component.ts:104-113`.

**Evidence:** Effect reads `tvCompatible()`, writes 6 locked-defaults signals. Angular v16+ throws `NG0600: Writing to signals is not allowed in a 'computed' or an 'effect' by default`. Either the effect is asserting and being swallowed, the check is not enabled, or the reactive path is muted. Either way it chains into effect Y (HTTP preflight) which reads 4 of the 6 written signals.

**Remediation:** Remove the effect; assign locked values directly in `toggleTvCompatible()`. Or make each locked field a `computed()` that returns canonical default when `tvCompatible()` is true, signal value otherwise.

### 4.5 [HIGH] TV-compat preflight — effect triggers HTTP with no abort, no debounce, no request ID

**Location:** `tv-compat-panel.component.ts:118-133`.

**Evidence:** 9 signal reads, fires `firstValueFrom(http.post(...))` per change. Typing a 10-char date fires ~10 requests. Server ordering is the only race guard. Late responses overwrite fresh ones → `preflightResult` stale → parent's `preflight` signal stale → `preflightBlocks` wrong → Run-button disabled state wrong.

**Remediation:** `rxResource({ request: ... , loader: ({request, abortSignal}) => ... })`.

### 4.6 [HIGH] Engine-Lab cross-component cascade: 5-hop preflight chain

Path: parent `selectedStrategyName` → `strategyIndicators` → input to TV-compat → effect Y → HTTP → `preflightResult` → `preflightStatus.emit` → parent `preflight` → `preflightBlocks` → Run button disabled state. Length 9 counting intra-child hops.

Add the effect-A availability check (parallel HTTP from `effectiveSymbol` edits) and a single form keystroke fires **two concurrent HTTP requests**.

**Remediation:** Debounce both effects (`rxResource` with `toObservable(...).pipe(debounceTime(200))`).

### 4.7 [HIGH] Data-Lab `entries` fan-out — 5 per-row template computations

`entries` feeds `entryCount`, `chartIndicators`, `timeframeWarnings`, `estimatedColumns`, and the `selectedNames` getter. Template reads `timeframeWarnings()[$index]` inside `@for (entry of entries())` → O(n) recomputation per edit. Acceptable at n ≤ 20, quadratic in disguise at higher counts.

### 4.8 [MEDIUM] Engine-chart effect re-renders 3× per Run click

**Location:** `engine-chart/engine-chart.component.ts:92-99`.

**Evidence:** Reads `chartBars()`, `trades()`, `equityCurve()`. All three derive from the same parent `result` signal but propagate independently. Effect fires 3× in sequence, each scheduling a `setTimeout(renderAll)`. `renderAll` does `.setData()` on all three series from scratch each time.

**Impact:** Per-render cost 50-200 ms on a 24k-point equity + 2k-bar candle → 100-600 ms wasted per Run.

**Remediation:** Change the child to read a single input (`result`) and derive locally; or compose one `computed` in the parent.

### 4.9 [MEDIUM] Engine replay per-tick cascade (~12 recomputes at 10× speed)

**Location:** `engine-replay-v2/services/replay-engine-v2.service.ts:92-260`.

`_currentIndex` bumps every ~100 ms / speed. Invalidates 12 computed reads — `currentBar`, `currentMs`, `progress`, `isAtStart`, `isAtEnd`, `renderWindow`, `windowTrades` (O(trades)), `hiddenSummary` (O(trades)), `activePosition`, `position`, `visibleIndicatorsWindow` (O(indicators × window)), `signalCards` (same). At 10× speed on long studies (thousands of trades, tens of thousands of indicator points), playback stutters.

**Remediation:** Precompute trade boundaries in a `computed` keyed on `_trades` only; bisect in per-tick work.

### 4.10 [MEDIUM] `fetchAllowedTimeframes` → `fetchChartData` race on double-click

**Location:** `data-lab-chart.component.ts:272, 326-353`.

Two quick clicks interleave two chain continuations; no request ID; chart renders wrong-timeframe data.

**Remediation:** Disable the button while `loading()`; or convert to `rxResource`.

### 4.11 [LOW] Zero `resource()` / `rxResource()` in either feature

`grep -rn "rxResource\|resource("` in both feature dirs → zero hits. Every async path hand-rolled, every one vulnerable to the race class above. The framework has the tool; the repo doesn't use it.

### 4.12 Safe-diamond inventory (no action needed, documented for future audits)

- Parent `result` → `{chartBars, chartTrades, equityCurve, insights, insightSummary}` → 9 EngineResults computeds. Width 14, glitch-free per microtask batching.
- Data-Lab `fromDateValue` → `{fromDate, dateRangeWarning}` → child `[fromDate]` input. Safe.
- Replay `_currentIndex` → 12 children through `currentMs` diamond. Safe but costly (see § 4.9).

---

## 5. Throughput & Latency Model (NEW)

Flag threshold: **> 500 ms**.

### 5.1 Engine Lab — "Run backtest" flow (ema_crossover, SPY, minute, 6-week)

| Hop | Measured | Method | Flag |
|---|---|---|---|
| Angular outbound (click → fetch) | ~1-3 ms (est) | static | — |
| Angular → Python wire | ~0.1 ms | curl `time_connect` | — |
| .NET resolver | **0 ms (not in path)** | static: direct POST to `/api/engine/backtest` | — |
| Python data-load + compute | ~1.82 s | curl `time_starttransfer ≈ time_total` | **HIGH** |
| Python → Angular serialize | bundled; body **2,459 KB** | curl `size_download` | **HIGH (size)** |
| Angular parse (JSON) | ~30-60 ms (est) | 2.5 MB at V8 ~40-80 MB/s | — |
| Angular computed cascade | ~10-25 ms (est) | 5 computeds; `equityCurve.map` over 24,376 pts | — |
| Angular render (engine-chart) | ~50-200 ms (est) | sort+`setData` ×3 on 24k equity + 2k candles; fires 3× (see § 4.8) | — |
| **Total perceived** | **~2.0-2.2 s** | | **HIGH** |

Adjacent measurements:
- 1-week SPY minute: **85 ms, 490 KB** — fine
- Same 6-week, 2nd run (cache hot): **1.82 s, 2.5 MB** — not a cache miss; it's serialization + RTH filter + indicators redoing work every time
- 2-year daily: 433 ms, 89 KB — fine
- 3-month minute backtest: **HTTP 500 after 5.3 s** (see § 5.3)

### 5.2 Data Lab — "loadChartData" flow (SPY, 1m, 1-month, 11 indicators)

| Hop | Measured | Method | Flag |
|---|---|---|---|
| Angular outbound | ~1-3 ms (est) | static | — |
| Angular → Python wire | ~0.1 ms | localhost | — |
| .NET resolver | **0 ms (not in path)** | direct POST | — |
| Python data-load (Polygon + preprocess) | ~1.66 s cold | server log 42.112→43.774 | **HIGH** |
| Python resample + session filter | ~2.56 s | log 43.774→46.330; RTH 18,873→7,800 + DataFrame copies | **CRITICAL** |
| Python indicator compute | ~0.9 s cold | log 46.330→~47.24 | **HIGH** |
| Python response build (`iterrows`) | bundled above | `chart_service.py:996` | **HIGH contributor** |
| Python → Angular serialize + send | body **3,523 KB**; warm **717 ms** | curl | **HIGH** |
| Angular parse (JSON) | ~75-150 ms (est) | 3.5 MB | — |
| Angular render | ~300-700 ms (est) | 16× `.filter().map().sort()` over 7,800 pts | **HIGH** |
| **Total cold** | **~5.1 s measured** | end-to-end curl | **CRITICAL** |
| **Total warm** | ~1.0-1.4 s | | **HIGH** |

### 5.3 [CRITICAL] Engine backtest emits non-finite floats → HTTP 500 after seconds of compute

**Location:** `app/routers/engine.py` + stats-builder in `engine/results/statistics.py`.

**Measured:** 3.80 s and 5.31 s runs both returned `HTTP 500 ValueError: Out of range float values are not JSON compliant: inf`. Reproduced twice. Occurs on zero-trade or lossless runs where `wins/0` or similar produces `inf`.

**Impact:** User waits multiple seconds, sees a generic error, has no recovery except narrowing the range.

**Remediation:** Sanitize stats in the response builder (`float("inf") → None`) before Pydantic. Add a regression test: zero-trade run must not 500.

### 5.4 [CRITICAL] Data Lab cold chart > 5 s for default parameter set

**Location:** `chart_service.py:878` + `chart.py:65`.

**Measured:** Cold 5.13 s, warm 717 ms (serialization alone).

**Remediation:**
1. Vectorize `chart_service.py:996` — replace `for _, row in df_resampled.iterrows()` with `df_resampled[["timestamp","open",...]].to_dict(orient='records')`. Expected 5-10× speedup on the bars-serialize step (~200-400 ms saved on 24k bars).
2. Columnar indicator format: response-level `timestamps: [...]` + `series: [{id, values: [...]}]`. Halves the 3.5 MB body.
3. Make handler sync (threadpool) or wrap internal call with `await asyncio.to_thread(...)`.

### 5.5 [HIGH] Engine backtest response is 2.5 MB dominated by redundant equity_curve

**Location:** `app/engine/engine.py:375`, `app/routers/engine.py:1251`.

**Measured:** 6-week SPY minute → 24,376 equity points, 2.459 MB. Flat-line (equity==100000 everywhere) — pure redundancy. Two of four per-point fields (`cash`, `holdings_value`) are never read by the UI (confirmed at `lean-engine.component.ts:268-273`).

**Remediation:**
1. Server-side downsample (the chart downsamples to 2,000 pts anyway at `engine-chart.component.ts:314`).
2. Drop `cash`, `holdings_value` unless a consumer needs them.
3. Columnar format saves ~40% of remaining body.

### 5.6 [HIGH] FastAPI event loop blocked by sync work in async handler

**Location:** `app/routers/chart.py:65`.

**Measured:** While a cold chart request was in flight, a normally-5-ms availability call served **2.003 s**. Live: `CHART TIME: 2.207 s / AVAIL TIME: 2.003 s` with the availability call starting 200 ms after chart start.

**Impact:** A single data-lab date change hangs every other Python endpoint on that worker — engine-lab availability checks, strategy list refreshes, any concurrent user.

**Remediation:** `def chart_data` (FastAPI threadpool) or `await asyncio.to_thread(get_chart_data, ...)`.

### 5.7 [HIGH] Data Lab chart response carries ~1.7 MB of redundant timestamp fields

**Location:** `chart_service.py:660-800` — `_format_indicator_results`.

**Measured:** 17 series × 7,800 points × 13-char `t` field ≈ 1.72 MB of duplication inside the 6.0 MB `indicators[]` payload.

**Remediation:** Columnar wire format (see § 5.4.2).

### 5.8 Top-5 latency contributors

1. **[PYTHON]** `chart_service.py:996` `iterrows` — serializing 7,800-24,000 bars via pandas `iterrows()` is 10-50× slower than `to_dict(orient='records')`.
2. **[PYTHON]** `chart.py:65` sync work in `async def` — blocks event loop for 3.5 s cold / 0.7 s warm; measured 2.0 s head-of-line block on a parallel availability call.
3. **[PYTHON]** `engine.py:375/1251` equity-curve emission — 24k points × 103 B when UI uses 2 fields × ~2k downsampled points.
4. **[PYTHON]** `chart_service.py:675-800` indicator output — `[{t, value}, ...]` with `t` duplicated across 16 series.
5. **[PYTHON/CRITICAL-BUG]** `inf` in statistics → HTTP 500 after seconds of compute (see § 5.3).

Runners-up:
- **[ANGULAR]** `engine-chart.component.ts:92` effect 3×-fire per Run.
- **[ANGULAR]** `data-lab-chart.component.ts:598-629 / 780-790` `.filter().map().sort()` on pre-sorted backend data.
- **[ANGULAR]** `lean-engine.component.ts:334-344` per-keystroke availability fetch, no debounce.

### 5.9 [LOW] ISO-string trade timestamps re-parsed with `new Date(...)` per trade

**Location:** `engine-chart.component.ts:280-281`. Trivial at 40 trades; matters at 1000+. Emit epoch-ms for consistency with `chart_bars`.

---

## 6. Single Source of Truth Verdict (updated)

Prior audit: **"Fragmented. Two parallel indicator engines, two parallel OHLCV shapes, two parallel BacktestResult schemas, two parallel Greeks shapes. The correct fix is to update CLAUDE.md to match reality, not to finish the port."**

This audit confirms and extends:

| Concept | Paths | Canonicality |
|---|---|---|
| Indicators | pandas-ta (batch, ~151, `ta_service.py`) + streaming LEAN-port (3: SMA, EMA, RSI, in `engine/indicators/`) | Both legitimate, but **diverge by up to 14 RSI points until 316 bars of history** (§1.2). Neither path documents the convergence boundary. |
| OHLCV | Py `OhlcvBar` (ms-int) + sanitizer wire (ISO string, lies about UTC) + .NET `OhlcvBarDto` (`long`) + .NET `AggregateBar` (`DateTime` + EF `Id` leak) + TS `StockAggregate` (`string`) + TS `IndicatorTableRow` (`number`) | **Six shapes**, two timestamp types, one documented UTC lie. |
| BacktestResult | Py `BacktestResponse` (**dark code, unregistered router**) + Py `EngineBacktestResponse` (live path) + .NET `BacktestResultType` (missing ~10 fields vs Py) + .NET `RuleBasedBacktestResultType` (`double`, not `decimal` — has the missing fields) + TS mirrors for both | **Two shapes actually on the wire** with incompatible field sets and different float types. |
| Greeks | Py × 3 + .NET × 4 + TS × 3 + TS `GreekType` union that lies about `'rho'` (§3.5) | **10 nominally-compatible shapes.** |
| Timestamps | 4 formats in flight: Py `int ms`, Py ISO-with-lying-`Z`, Py `"YYYY-MM-DD HH:MM"` (local-time trap, §2.3), .NET `DateTime` with Kind=Local-by-accident (§2.5) | **No canonical format.** |
| Math of record | `Decimal` in streaming engine, `float` in ta_service / rule_based, `decimal` in .NET, `number` in TS, mixed `double`/`decimal` in `RuleBasedBacktestResultType` | Honest answer: `Decimal` is canonical inside the engine; wire is lossy; TS is `float64`. **`atol=1e-9` cross-layer is architecturally impossible** without a string-encoded decimal scalar. |

**New verdict:** Fragmentation is worse than the structural audit implied because **two entire Python routers are dark code** (§3.2) — they exist, are imported, are tested, but are unreachable from any HTTP caller. Any reader trusting the code's structure forms a wrong mental model. **Delete or register** — either is fine; the dark state is the bug.

**% of logic outside the Python engine:** roughly — 3 of ~200 indicators are in the streaming engine; ~197 are pandas-ta (inside Python, but outside the "LEAN-shaped" engine). Backtest statistics: 35-field `LeanStatistics` exists in dark Python code; live backtest stats are assembled by .NET in-process with ~10 fields. So the math-of-record for backtest statistics is **in .NET, not Python** — which contradicts CLAUDE.md's "Python engine is canonical". Update CLAUDE.md.

---

## 7. Prior-audit re-verification rollup

Every prior CRITICAL/HIGH finding checked against HEAD `14d66cf`:

| # | Prior finding | Location | Status |
|---|---|---|---|
| 1 | None-guard on `_calc_sma/_calc_ema/_calc_rsi` → HTTP 500 | `ta_service.py:61-73, 139-145` | **CONFIRMED unfixed** (§1.1) |
| 2 | Naive timestamps with `Z` suffix (falsely claiming UTC) | `sanitizer.py:78, 140` | **CONFIRMED unfixed** (§2.1) |
| 3 | `calculateIndicators.error` vs `.message` — Python errors vanish from UI | `Backend/GraphQL/*`, `Frontend/.../market-data.service.ts:86`, `types.ts:121` | **CONFIRMED unfixed** (§3.3) |
| 4 | Precision funnel `float → decimal → number` forecloses `atol=1e-9` | every cross-layer numeric field | **CONFIRMED, unchanged** — inventory expanded (§6) |
| 5 | CLAUDE.md claims LEAN port + strict equivalence, reality is 3 indicators + 0 golden fixtures | `CLAUDE.md:3, 8-9, 21` | **CONFIRMED, also LEAN-stats routers are dark code** (§3.2) |

**Zero prior CRITICAL/HIGH findings have been fixed in the 0 commits since the prior audit.** (`git log --oneline master` shows only the audit-artifact commits.)

---

## 8. Codegen proposal

User approved this in the planning round. Two surfaces.

### 8.1 Angular ↔ .NET (GraphQL) — `@graphql-codegen/cli` + Apollo-Angular plugin

**Rationale:** Apollo-Angular is already in `package.json`. Graphql-code-generator's Apollo-Angular plugin generates injectable `*GQL` services with fully typed variables and response. Eliminates 30+ raw-string queries. Chose over `apollo-codegen` (deprecated, Apollo's own docs now point here).

**`Frontend/codegen.yml`:**
```yaml
overwrite: true
schema: "http://localhost:5000/graphql"
documents:
  - "src/app/**/*.graphql"
  - "src/app/**/*.ts"
generates:
  src/app/graphql/generated.ts:
    plugins:
      - typescript
      - typescript-operations
      - typescript-apollo-angular
    config:
      scalars:
        Decimal: number           # document the precision loss here
        DateTime: string
        Long: number
        JSON: "Record<string, unknown>"
      strictScalars: true
      enumsAsTypes: false
      avoidOptionals: { field: true, inputValue: false, object: true }
      apolloAngularVersion: 13
  src/app/graphql/schema.graphql:
    plugins: [schema-ast]
```

**`package.json` additions:**
```json
{
  "scripts": {
    "codegen": "graphql-codegen --config codegen.yml",
    "codegen:watch": "graphql-codegen --config codegen.yml --watch",
    "codegen:check": "graphql-codegen --config codegen.yml && git diff --exit-code -- src/app/graphql/generated.ts src/app/graphql/schema.graphql"
  },
  "devDependencies": {
    "@graphql-codegen/cli": "^5.0.0",
    "@graphql-codegen/typescript": "^4.0.0",
    "@graphql-codegen/typescript-operations": "^4.0.0",
    "@graphql-codegen/typescript-apollo-angular": "^4.0.0",
    "@graphql-codegen/schema-ast": "^4.0.0"
  }
}
```

**What gets deleted:** `graphql/types.ts` (572 lines), `graphql/portfolio-types.ts` (295 lines), every inline raw-string query in services.
**What gets written:** co-located `*.graphql` files next to services; services use injected `*GQL` classes.

### 8.2 .NET ↔ Python (OpenAPI) — Microsoft Kiota

**Rationale:** Official MS tooling for .NET 10. Handles OpenAPI 3.1 natively (FastAPI emits 3.1 since 0.100+; NSwag lags here). Uses `System.Text.Json`, already the backend serializer. Fluent request-builder client: `await _pythonClient.Api.Indicators.Calculate.PostAsync(request, ct)`. Chose over NSwag (3.1 lag) and `dotnet-openapi` (NSwag.MSBuild wrapper, same 3.1 problem).

**Install:**
```bash
dotnet tool install -g Microsoft.OpenApi.Kiota
```

**`Backend/kiota-config.json`:**
```json
{
  "version": "1.0.0",
  "clients": {
    "PythonData": {
      "descriptionLocation": "http://localhost:8000/openapi.json",
      "language": "CSharp",
      "outputPath": "./Generated/PythonClient",
      "clientNamespaceName": "Backend.Generated.PythonClient",
      "clientClassName": "PythonDataClient",
      "structuredMimeTypes": ["application/json"],
      "excludeBackwardCompatible": true
    }
  }
}
```

**DI wiring (`Backend/Program.cs`):**
```csharp
builder.Services.AddHttpClient<PythonDataClient>(c =>
{
    c.BaseAddress = new Uri(builder.Configuration["PolygonService:BaseUrl"]!);
    c.Timeout = TimeSpan.FromSeconds(300);
})
    .AddPolicyHandler(PollyPolicies.GetRetryPolicy())
    .AddPolicyHandler(PollyPolicies.GetCircuitBreakerPolicy());

builder.Services.AddScoped<IRequestAdapter>(sp =>
{
    var http = sp.GetRequiredService<IHttpClientFactory>().CreateClient(nameof(PythonDataClient));
    return new HttpClientRequestAdapter(new AnonymousAuthenticationProvider(), httpClient: http);
});
```

**What gets deleted:** `Backend/Models/DTOs/IndicatorModels.cs`, `BatchResearchModels.cs`, `GapDetectionModels.cs`, `ResearchModels.cs`, `SanitizeModels.cs`, `SignalModels.cs`, all of `Backend/Models/DTOs/PolygonResponses/`.
**What stays:** `Backend/GraphQL/Types/*` (these are the frontend contract, generated from the frontend side). Service-layer code becomes the one explicit `Generated.X → GraphQL.X` mapping boundary.

### 8.3 Migration phases

| Phase | Scope | Tree stays green |
|---|---|---|
| 1 (1 PR) | Introduce GraphQL codegen; rename `types.ts → types.legacy.ts`; commit first `generated.ts`; add `codegen:check` to CI as informational | ✓ |
| 2 (N PRs) | Migrate services feature-by-feature (`market-data`, `portfolio`, `options`, `strategy`, `quantlib`). Delete slices of `types.legacy.ts` as imports fade | ✓ |
| 3 (1 PR) | Fix drift codegen surfaced: rename `Message` → `Error` (§3.3), add missing `BacktestResult` fields (§3.7), unify Greeks into one shape (§3.5), structured `IndicatorTableRow` (§3.4), delete dead Python routers (§3.2) | ✓ |
| 4 (1 PR) | Introduce Kiota for `.NET ↔ Python`. Flip `TechnicalAnalysisService` to generated client. Commit `kiota-config.json` + first generated tree | ✓ |
| 5 (N PRs) | Migrate remaining Backend services one at a time. Delete hand-written DTOs as replaced | ✓ |

### 8.4 CI enforcement

```bash
# Frontend
cd Frontend && npm ci && npm run codegen \
  && git diff --exit-code -- src/app/graphql/generated.ts src/app/graphql/schema.graphql

# Backend
cd Backend && dotnet tool restore \
  && dotnet kiota update --config-file kiota-config.json \
  && git diff --exit-code -- Generated/PythonClient/

# Schema snapshot guardrail
curl -s http://localhost:5000/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{ __schema { types { name fields { name type { name kind ofType { name kind } } } } } }"}' \
  | jq -S '.' > /tmp/schema-live.json
diff Frontend/schema.snapshot.json /tmp/schema-live.json || \
  (echo "GraphQL schema drifted; regenerate with: npm run codegen && git add src/app/graphql/generated.ts" && exit 1)
```

All three fit in one CI job (<30 s wall time).

### 8.5 Rejected alternatives

- **`openapi-typescript` on frontend** — frontend never calls Python directly; everything goes through GraphQL.
- **NSwag** — OpenAPI 3.1 lag; Kiota is the MS-recommended successor.
- **Apollo-codegen** — deprecated.
- **Roll-your-own introspection** — `[GraphQLName]` complexity (`totalPnL` vs `totalPnl`), nullable-vs-optional nuance, scalars — justifies the tool.
- **GraphQL federation (Python subgraph via Strawberry)** — eliminates surface A entirely; out of scope for this quarter.
- **Keep GraphQL + add `JsonValue` scalar** — recommended as part of Phase 3 for the `parameters`/`rows` fields; codegen emits `unknown`/`Record<string, unknown>` on either side.

---

## 9. Remediation priorities

### P0 — ship this sprint

| # | Finding | Location | 1-line fix |
|---|---|---|---|
| 1 | `/api/sanitize` 10⁶ timestamp collapse | `sanitizer.py:216` | Keep original ms column; don't round-trip through `pd.to_datetime` |
| 2 | `_format_timestamp` local-time parse | `rule_based_backtest.py:252`, `strategies/common.py:115` | Return `datetime.fromtimestamp(ts/1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")` |
| 3 | Engine backtest `inf` → 500 | `engine/results/statistics.py` | Coerce non-finite floats → `None` before Pydantic |
| 4 | Data Lab cold load > 5 s | `chart_service.py:878, 996`, `chart.py:65` | (a) `to_dict(orient='records')` for bars; (b) `asyncio.to_thread`; (c) columnar indicators |
| 5 | Dead Python routers | `app/main.py:61-78`, `backtest.py`, `rule_based_backtest.py` | Delete or register — do not leave as dark code |
| 6 | None-guard missing | `ta_service.py:61-73, 139-145` | `if X is None or X.empty: return None` on SMA/EMA/RSI (copy the sibling pattern) |
| 7 | `CalculateIndicatorsResult` error/message rename | `Backend/GraphQL/Types/CalculateIndicatorsResult.cs:8` | Rename `Message` → `Error`; update TS query |
| 8 | `selectedNames` reallocating Set per read | `data-lab.component.ts:231` | Promote getter to `computed<Set<string>>()` |
| 9 | Signal-writing effects without `allowSignalWrites` | `tv-compat-panel.component.ts:104`, `lean-engine.component.ts` effect B | Convert to computed / method call |

### P1 — ship this quarter

| # | Finding | Location |
|---|---|---|
| 10 | RSI cross-path divergence up to 14 points | `ta_service.py:71-73`, `engine/indicators/rsi.py` — mask or document |
| 11 | Options snapshots lose freshness | `OptionsChainSnapshotResponse.cs:53-69` |
| 12 | `DateTime.Parse` Kind=Local fragility | `MarketDataService.cs:450` |
| 13 | No request sequencing on chart/backtest | `data-lab-chart.component.ts:361`, `strategy-lab.component.ts:716`, `lean-engine.component.ts:334` — move to `rxResource` |
| 14 | TV-compat HTTP effect race | `tv-compat-panel.component.ts:118` — `rxResource` with `abortSignal` |
| 15 | Engine backtest payload 2.5 MB / redundant equity_curve | `engine.py:375`, `engine.py:1251` |
| 16 | Chart payload 1.7 MB of duplicated timestamps | `chart_service.py:660-800` |
| 17 | FastAPI event loop blocked in `async def chart_data` | `chart.py:65` |
| 18 | 7 deprecated `datetime.utcnow()` sites | `data_quality_service.py`, `dataset_service.py`, `validation_service.py` |
| 19 | `IndicatorTableResult.rows` as JSON strings | `Types/IndicatorTableResult.cs:17` |
| 20 | Begin GraphQL codegen (Phase 1) | `Frontend/codegen.yml` |

### P2 — ship this half

| # | Finding | Location |
|---|---|---|
| 21 | Four Greeks shapes; TS `GreekType` includes phantom `rho` | Unify into single nullable `GreeksResult` |
| 22 | `BacktestResult` missing 10 fields vs Py | `Mutation.cs:817` + `types.ts:336` |
| 23 | `RuleBasedBacktestResult` uses `double` not `decimal` | `Mutation.cs:894` |
| 24 | `rule_based_backtest` no dedupe | `rule_based_backtest.py:108` |
| 25 | No response-level `asOfUnixMs` | cross-layer |
| 26 | Apollo cache configured but unused | `app.config.ts:29` — decide policy |
| 27 | Engine-chart 3×-fire effect | `engine-chart.component.ts:92` |
| 28 | Engine-replay per-tick O(trades+indicators) | `replay-engine-v2.service.ts:92-260` |
| 29 | Accumulator vs recompute inconsistency | `rule_based_backtest.py:133, 166, 209` |
| 30 | CLAUDE.md claims vs reality | `CLAUDE.md:3, 8-9, 21` — rewrite to match |
| 31 | Begin Kiota for .NET ↔ Python | `Backend/kiota-config.json` |

---

## Appendix A — Audit method

Five specialist agents ran in parallel against live containers (`podman compose ps` showed 4 services up throughout). Each agent was given a scoped brief, a re-verification list for its domain's prior findings, the `> 500 ms` flag threshold where applicable, and a hard instruction to produce evidence with file:line or measurement per finding. No agent wrote files or committed code. The throughput agent added no persistent instrumentation (`git status` clean at session end). Agent outputs were structured reports synthesized here verbatim on findings, consolidated on severity, and unified into the P0/P1/P2 priority list.

## Appendix B — Evidence files (for follow-on PRs)

- Python models / routers: `PythonDataService/app/models/responses.py:{28,39,78,121,146,156,404}`; `PythonDataService/app/models/requests.py:{61,72,166}`; `PythonDataService/app/routers/backtest.py:{40,53,145,181}`; `PythonDataService/app/routers/rule_based_backtest.py`; `PythonDataService/app/main.py:61-78`.
- Sanitizer / timestamp: `PythonDataService/app/services/sanitizer.py:{78,140,216}`; `PythonDataService/app/services/rule_based_backtest.py:{108,133,166,209,252-260}`; `PythonDataService/app/services/strategies/common.py:115-123`.
- Chart / engine: `PythonDataService/app/services/chart_service.py:{660-800,878,996}`; `PythonDataService/app/routers/chart.py:65`; `PythonDataService/app/routers/engine.py:{375,1251}`; `PythonDataService/app/engine/indicators/rsi.py:{29-88}`, `ema.py:42`, `sma.py:{30-33}`; `PythonDataService/app/engine/portfolio.py:188-205`.
- Backend DTOs: `Backend/Models/DTOs/IndicatorModels.cs:{3,12,17,38}`; `Backend/Models/DTOs/PolygonResponses/OptionsChainSnapshotResponse.cs:{20,35,53-69}`; `Backend/Services/Implementation/MarketDataService.cs:450`.
- Backend GraphQL types: `Backend/GraphQL/Types/CalculateIndicatorsResult.cs:8`; `Backend/GraphQL/Types/IndicatorTableResult.cs:17`; `Backend/GraphQL/Types/SmartAggregatesResult.cs:28`; `Backend/GraphQL/Query.cs:{1314,1329,1404,1563,1598}`; `Backend/GraphQL/Mutation.cs:{99,201,262,705,785-802,817,894}`.
- Frontend: `Frontend/src/app/graphql/types.ts:{24,117,121,198,336,371,406,446,502,511,529,533}`; `Frontend/src/app/graphql/portfolio-types.ts:219`; `Frontend/src/app/services/market-data.service.ts:{60,86,726}`; `Frontend/src/app/services/replay-strategy.service.ts:18-48`; `Frontend/src/app/components/lean-engine/lean-engine.component.ts:{168-181,246-248,268-273,334-344,486}`; `Frontend/src/app/components/lean-engine/tv-compat-panel/tv-compat-panel.component.ts:{104-113,118-133,143}`; `Frontend/src/app/components/lean-engine/engine-chart/engine-chart.component.ts:{92-99,107,280-281,314}`; `Frontend/src/app/components/lean-engine/engine-replay-v2/services/replay-engine-v2.service.ts:92-260`; `Frontend/src/app/components/data-lab/data-lab.component.ts:{181-193,231,250-263,314-355,420,607-649}`; `Frontend/src/app/components/data-lab/data-lab-chart/data-lab-chart.component.ts:{272,300,326-353,361-404,535-568,598-629,727-790,853-863}`; `Frontend/src/app/app.config.ts:29`.
- Live probes: `curl -s http://localhost:8000/openapi.json` → 141 schemas / 62 paths; `curl http://localhost:5000/graphql` introspection → 224 types (133 OBJECT); `curl -X POST http://localhost:8000/api/backtest/run` → HTTP 404; `curl -X POST http://localhost:8000/api/indicators/calculate` with `window > bars` → HTTP 500; chart/backtest wall-clock timings via `curl -w`.
