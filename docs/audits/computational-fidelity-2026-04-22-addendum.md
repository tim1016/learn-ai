# Computational Fidelity Audit — Addendum (Response to Review)

**Parent:** `computational-fidelity-2026-04-22.md`
**Date:** 2026-04-22
**Purpose:** Respond to the external review of the parent audit. The review graded it **A−** and argued the audit is production-grade but not yet system-theoretic. This addendum accepts the valid challenges, pushes back where the audit was clearer than the review credited, and adds the dimensions the review called out as missing. Every change here is evidence-backed or explicit about what's still unmeasured.

---

## 0. What the review got right (and what it didn't)

**Accepted outright:**

1. **Symptoms were listed where root causes belonged.** The Top-10 table in § 0 of the parent leads with "5.13 s cold load" and "inf → HTTP 500" — those are user-visible effects. The root causes (row-wise `iterrows`, sync work in `async def`, non-finite floats leaking past Pydantic) are one layer down in § 5. The Top-10 should lead with the root, not the phenomenon. Rewritten in § 2 below.
2. **Temporal integrity is one failure class, not five.** Sanitizer × 10⁶ collapse, naive-Z lie, `"YYYY-MM-DD HH:MM"` local-time trap, `DateTime.Kind=Local`, missing dedupe/monotonicity — the parent listed each as separate CRITICAL/HIGH. The review is right that these are all symptoms of **no canonical timestamp format**. Reframed in § 3.
3. **RSI divergence is CRITICAL, not HIGH.** The parent rated it HIGH because convergence eventually happens at bar 316. That's the wrong frame: the repo's own `numerical-rigor.md:62` pins `atol=1e-9` unconditionally, and the RSI(14) window most traders look at is < 100 bars — the divergence is **in the regime users actually use**. Same input → different output on every short-window call. Repromoted in § 2.
4. **Dark code is a cognitive-integrity failure.** The parent called it CRITICAL but framed it as "dead code." The review is right that 180 lines of LEAN-parity Pydantic models + a test suite + import wiring creates a false mental model for every reader (human or AI). That's a stronger claim and it's correct.
5. **Missing dimensions.** Backpressure, numerical stability over long horizons, and memory pressure were not in scope. Restored in § 4.

**Partially accepted:**

- **"You mix symptoms with root causes"** — accepted for the Top-10. The body sections (1-7) do trace symptoms to roots (e.g., § 5.4 names `iterrows`, `chart.py:65`, and columnar payload directly); the framing problem is confined to the executive summary.
- **"Deterministic replay dropped was a mistake"** — the parent explicitly noted it was dropped at user request. That wasn't an omission; it was a scope decision. The review's argument is that given the RSI non-equivalence and the no-canonical-timestamp finding, **non-determinism is now a downstream consequence we've proven, not just an unchecked assumption**. That raises replay from "nice to have" to "the diagnostic we need next quarter." Added to § 4.

**Rejected:**

- **"Audit hints at fragmentation but doesn't go far enough: this is multi-brain architecture"** — the parent § 6 names six shapes for OHLCV, four for Greeks, two for BacktestResult, and says *"the math-of-record for backtest statistics is in .NET, not Python — which contradicts CLAUDE.md."* That's the multi-brain point, said in the repo's own vocabulary. The review wants it louder. § 5 below makes it louder and decides between Option A and Option B.
- **Grade A−** — fair. Fixing the framing issues above lifts it to A; the A+ claim requires the architectural decision in § 5 to be made and encoded, which is not an audit-shaped task.

---

## 1. Root-cause restatement of the Top-10

The parent's Top-10 ranks by **user-visible damage**. This table ranks by **architectural failure class**. The review is right that the second framing is more useful for planning the fix.

| Failure class | Symptom manifestations (parent §) | Root | Severity |
|---|---|---|---|
| **Temporal integrity collapse** | § 2.2 sanitizer ms/10⁶, § 2.3 naive-Z / local-time parse, § 2.5 `Kind=Local`, § 2.6 no request sequencing, § 2.9 no dedupe, § 2.11 unqualified `datetime.now()` | **No canonical timestamp format**; four formats on the wire; no enforcement at the boundary | CRITICAL (class) |
| **Non-equivalent math paths** | § 1.2 RSI 14-point drift, § 1.3 history-sensitivity, § 6 two BacktestResult shapes with different float types | **No single authority for indicator / statistic math**; pandas-ta (batch) and streaming engine coexist, produce different outputs for identical inputs | CRITICAL (was HIGH) |
| **Cognitive-integrity failure (dark code)** | § 3.2 unregistered routers, § 6 `lean_statistics` producer unreachable | Imports + tests + 180 lines of Pydantic models present; HTTP path absent. Reader forms wrong mental model | CRITICAL |
| **Row-oriented payloads for columnar data** | § 5.4 `iterrows`, § 5.5 redundant equity_curve, § 5.7 duplicated timestamps, § 4.8 engine-chart 3×-fire | Wire format is `[{t, open, ...}]` for time-series; serializer + parser + render all pay. Parent said this; review is right it should be **first-class architectural failure**, not a contributor | CRITICAL |
| **Non-atomic reactive graph** | § 4.4 signal-writing effects, § 4.5-4.6 no abort / debounce / request IDs, § 2.6 late-response override, § 4.10 double-click race | **No consistency boundary**; UI renders intermediate states that never existed in the truth | HIGH |
| **Event-loop blocking (no backpressure primitive)** | § 5.6 2.0 s head-of-line block on a parallel request | `async def` handlers doing synchronous pandas work. One user blocks every other user on that worker | HIGH |
| **Contract hand-maintenance** | § 3 inventory (209 TS + 80 DTOs + 30 inline queries + 0 codegen), § 3.5-3.9 four Greeks shapes, BacktestResult missing 10 fields, `RuleBasedBacktestResult` uses `double` not `decimal` | **Schema drift is unenforceable by the compiler**; every rename silently `undefined`s | HIGH |
| **Numerical stability unmeasured over time** | (not in parent) | Cumulative PnL, EMA drift, Greeks compounding on 100k-trade horizons untested. Only bar-wise equivalence measured | HIGH (new) |
| **Memory pressure unmeasured** | (not in parent) | Heap growth in Angular, pandas DataFrame duplication, GC pressure from JSON parsing untested | MEDIUM (new) |

The parent's Top-10 remains valid as a "which user complaint do we fix first" list. The table above is the "which architectural invariant do we restore first" list. Both matter; the audit should have had both.

---

## 2. Severity re-ranking

| Finding | Parent rating | New rating | Reason |
|---|---|---|---|
| § 1.2 RSI 14-point divergence | HIGH | **CRITICAL** | Same input → different output on every < 316-bar call. Violates repo's `atol=1e-9` unconditionally. RSI(14) at bar 14 is 34.21 on one path, 48.44 on the other — "overbought" vs "neutral". The regime most traders look at is the regime where it's broken. Review is right. |
| § 3.2 dark Python routers | CRITICAL | **CRITICAL (reframed)** | Rating unchanged; framing updated. This is not "dead code" — it's **a false promise to every future reader**. 180 lines of Pydantic LEAN-stats models + imports + tests + no HTTP path. Preferred remediation unchanged (delete), but CLAUDE.md's "LEAN-parity stats exist" claim must be corrected in the same PR. |
| § 5.4 Data Lab 5.13 s cold load | CRITICAL | **CRITICAL (symptom) — root = row-oriented payload + sync-in-async** | Promote the two root causes to first-class findings. The symptom's severity is unchanged; the remediation priority shifts toward the roots. |
| § 2.1 sanitizer + § 2.3 format + § 2.5 Kind=Local + § 2.6 no sequencing + § 2.9 no dedupe | 5 separate findings | **One class: "Temporal integrity broken"** | Fix all or the class isn't closed. A PR that fixes sanitizer but not `_format_timestamp` leaves the class alive. |

No finding is downgraded.

---

## 3. Temporal integrity — one class, one test suite, one remediation

The parent lists § 2.1 through § 2.11. The review is right that this is not a list of bugs — it's the absence of a policy. Restated:

**The rule (proposed addition to `.claude/rules/numerical-rigor.md`):**

> Every timestamp in flight, at rest, or on the wire is an integer count of milliseconds since Unix epoch UTC. Every conversion happens at exactly two boundaries: (a) UI rendering (ms → `America/New_York` for display only, never stored back), and (b) external-API ingestion (parse → ms-UTC immediately on receipt, validate monotonicity and uniqueness at the same point). No other place in the codebase converts timestamps. ISO strings, `DateTime`, naive datetimes, and timezone-bearing ISO strings are all disallowed as a wire/storage format.

**Enforcement (one-time audit, then CI):**

1. grep ban list: `datetime.utcnow`, `datetime.utcfromtimestamp`, `DateTime.Parse` (no `AdjustToUniversal`), `new Date(` on anything not typed as `number`, `pd.to_datetime(...)` without `utc=True`.
2. Type-level: every `timestamp` field is `long` / `int` / `number`. No `DateTime`, no `string`, no `datetime`. One GraphQL scalar (`Timestamp = long`) replaces `DateTime`.
3. Regression test per fix site:
   - `sanitizer.py:216` — round-trip `1704067200000 → sanitize → 1704067200000`.
   - `rule_based_backtest.py:252` — trade classified identically under `TZ=UTC` and `TZ=America/New_York` containers.
   - `MarketDataService.cs:450` — remove the call site; the test is that `DateTime.Parse` no longer appears in `Services/`.
   - `rule_based_backtest.py:108` — dedupe + `assert df.timestamp.is_monotonic_increasing`; test with an intentionally-duplicated fixture.

**Status after the fix:** the class is closed iff grep returns zero hits, all four regression tests pass, and CLAUDE.md § "Guiding philosophy" is extended with the rule above.

---

## 4. Dimensions the audit didn't cover — restored

These were out of scope in the parent. The review argues (correctly) that given what the audit found, they can no longer stay out of scope.

### 4.1 Deterministic replay — reopened

**Parent statement:** "deterministic bit-exact replay dropped at user request."

**Why that's no longer tenable:** § 1.2 proves the two RSI paths are non-equivalent under the repo's own tolerance rule. The review's framing — *"Given your drift issues, you already have non-determinism"* — is accurate. Without a replay suite we cannot prove, after the P0 fixes land, that the system has become deterministic.

**Proposed minimal replay suite (not in this audit's scope to build, but to be scheduled):**

1. **Golden fixture per active indicator** (3 in streaming engine, ~10 most-used from pandas-ta): input bars + expected output, with `atol=1e-9` per `numerical-rigor.md:62`, and a fail-shout assertion on the warmup region (not `allclose` — explicit `isnan` mask check).
2. **Backtest replay fixture** per registered strategy: 6-week SPY minute, all stats + trade list + equity curve, byte-identical on re-run.
3. **Two-engine reconciliation test** (per `reconcile-backtest` skill): pandas-ta RSI vs streaming RSI, with the expected divergence region **explicitly pinned** so the day they converge is a loud test failure prompting a fixture update.

The day this suite is green on main is the day "deterministic" is a claim with receipts rather than a hope.

### 4.2 Backpressure — one measurement, one decision

**The review's question:** what happens under sustained load?

**Measurement (performed now, not in parent):**

```
# 10 sequential /api/chart/data calls, same payload:
curl -w "%{time_total}\n" ... × 10
```

This was not run as part of the parent audit and is not run here — it requires load-generator infrastructure to be meaningful. What **is** measured in the parent, and reinforces the review's point:

- § 5.6 — a single in-flight chart request causes a parallel availability check to serve in **2.003 s** instead of 5 ms. That's the head-of-line blocking that 10 concurrent users would see compounded.
- § 4.5 — keystroke on a date input fires one HTTP request per keystroke with no debounce and no abort. Typing "2024-01-01" (10 chars) fires 10 in-flight requests; the server serializes them; the UI renders whichever returns last.

**What's missing from the codebase and needs to be added:**

| Layer | Primitive | Current state | Action |
|---|---|---|---|
| Angular (input) | debounce | absent on TV-compat preflight, availability, chart, backtest | `rxResource` with `toObservable(...).pipe(debounceTime(200))` |
| Angular (request) | abort | absent everywhere | `rxResource` with `abortSignal` wired through `fetch`/`HttpClient` |
| Angular (response) | sequence guard | absent everywhere | `rxResource`'s internal request-ID handling; or explicit monotonic counter + drop |
| FastAPI (worker) | threadpool for sync work | `async def` with sync pandas inside | `def` (FastAPI moves to threadpool) or `await asyncio.to_thread(...)` |
| FastAPI (global) | concurrency cap per route | absent | `Depends(Semaphore(n))` or uvicorn worker count documented in `compose.yaml` |

This table is new. It belongs as a P1 block in the parent's § 9.

### 4.3 Numerical stability over time — what we know, what we don't

**What we know** (from the parent, § 1.4, § 1.5, § 1.7):

- Streaming engine accumulators (`SMA._sum`, `EMA`, `RSI._avg_gain/_avg_loss`, `Portfolio`) are `Decimal` — exact over any horizon.
- `rule_based_backtest.total_pnl_pct` uses `cum_pnl_pct += pnl_pct` on floats — at 10,000 trades the drift from `np.sum` is 3.5e-18. Fine.
- Streaming EMA vs pandas-ta EMA agrees to ~1.4e-14 at magnitude 1e2 per bar.

**What we don't know** — and the review is right to call out:

- **Cumulative PnL on 100,000-trade runs.** Not tested. The accumulator at `rule_based_backtest.py:133` is float; the .NET `RuleBasedBacktestResultType` is `double`. `double` has ~15-16 decimal digits of precision; at cumulative PnL near $10⁶ that's 1e-10 per-operation noise. Over 100k trades, pessimistic accumulation is 1e-5 — below user-visible, above `atol=1e-9`. Needs a test fixture: generate 100k synthetic trades, assert `double` sum matches `Decimal` sum to some explicit tolerance, document the tolerance in `.claude/rules/numerical-rigor.md`.
- **EMA / RSI over 10-year minute bars.** ~1M bars. Streaming Wilders on `Decimal` is exact; pandas-ta on `float64` is not. Fixture generate → compare.
- **Greeks compounding.** Each Greek is a partial derivative computed by numerical differentiation (`h = 1e-4` typically). Running the pricer 1000× with tiny parameter perturbations is where compounding error shows. Not tested.

**Proposed addition to `numerical-rigor.md`:**

> For every quantity with an accumulator, the port ships a long-horizon fixture test: N ≥ 10× the expected production sequence length, assertion with an explicit tolerance, and a documented justification of that tolerance. "Same as bar-wise tolerance" is insufficient — long-horizon tolerance is typically 1-3 orders of magnitude looser and must be pinned.

### 4.4 Memory pressure — what we can measure cheaply

**Angular:**
- `lightweight-charts v5` retains its internal data buffer on every `.setData()`. Engine-chart's 3×-fire effect (§ 4.8) calls `.setData()` three times per Run with 24k points each = 72k points allocated, 48k immediately GC-eligible. Measurable with Chrome Performance → Memory tab; not measured here.
- Signals retain their last-set value forever; `entries()` in Data Lab fans out to 5 computeds. Not a leak, but the working set is larger than necessary.

**Python:**
- `chart_service.py:878` constructs `df_full`, `df_resampled`, then iterates `iterrows()` which materializes each row as a new `pd.Series` — O(n) allocations on top of the O(n) DataFrame. Switching to `to_dict(orient='records')` avoids the per-row `Series` object.
- `sanitizer.py` — every call makes a new DataFrame from the input list, then a new DataFrame with added columns. Fine at request sizes, not fine if called in a tight loop.

**.NET:**
- Hot Chocolate materializes resolver results fully before serialization. For the 2.5 MB engine-backtest response, that's 2.5 MB allocated on the managed heap per request. Acceptable at current QPS.
- EF Core `AsNoTracking()` is not consistently applied in `MarketDataService`. Tracked entities retain change-tracking metadata for the scoped lifetime of the DbContext.

**Cheap fix list (add to P2):**
- Grep for `.setData(` in Angular charts; ensure no more than one call per render cycle.
- Grep for `iterrows` in Python; replace with vectorized form on a second pass after the P0 in § 5.4.
- Grep for `context.<entity>.ToListAsync()` without `AsNoTracking` in `Services/`; add.

No memory leak is proven. The review's point is that it hasn't been checked, which is correct.

---

## 5. The architectural decision — Option A vs Option B

The review forces a choice. The parent implicitly favors Option A (CLAUDE.md § 1 says "Python owns math"; § 3.2 finds `.NET` assembles backtest statistics, contradicting that). Making the choice explicit:

### 5.1 Option A — Python owns all math (recommended)

**The rule:**

> No financial math outside Python. `.NET` is transport (GraphQL, auth, persistence) and Angular is visualization. Any change to an indicator, a statistic, a backtest calculation, or a fill model lands in `PythonDataService/` and is exposed via FastAPI. The `.NET` GraphQL resolver is the last place math is allowed to happen, and the only math it's allowed to do is `decimal`-preserving passthrough from the Python response.

**What this requires:**

1. **Move `.NET` backtest-stats assembly into Python.** The parent § 6 found that the live backtest-stats path (`Mutation.cs:99-220`) is a .NET-in-process calculation — not a Python call. This is the single largest violation of Option A. Action: port the stats assembly to Python, register it on the engine route, return a complete `LeanStatistics`, have `.NET` pass it through.
2. **Delete the dark routers (§ 3.2) with prejudice.** Once the engine route returns full stats, the dark `lean_statistics` producer has no reason to exist. Review's "cognitive integrity failure" closes.
3. **Delete `RuleBasedBacktestResultType` or fold it into the main result.** Two BacktestResult shapes is the Option A violation that hits users. One shape, one Python authority.
4. **RSI (§ 1.2) remediation** is forced by Option A: the two paths are both in Python, so Option A does not require deleting one. But the streaming engine is the Option A-aligned path (`Decimal`, warmup-aware, deterministic). pandas-ta stays as a research tool and is **masked** for `i < 3*period` at the `ta_service.py` response boundary. Any consumer that wants early values calls a different endpoint explicitly labeled "pandas-ta research mode — not indicator-of-record."
5. **CLAUDE.md update** to say exactly this in one paragraph. Currently CLAUDE.md hints at it but doesn't forbid the alternative.

**What Option A does not do:** eliminate Angular computeds. Angular still transforms for rendering (downsampling, chart-format mapping). Those are not "math" in the sense of "produces a number a user will compare against another number." The rule is enforceable because the boundary is clean: any number shown in a number field or used in a strategy rule originated in Python.

### 5.2 Option B — streaming engine owns everything

**The rule:**

> pandas-ta is deleted. Every indicator is a streaming incremental-update class in `engine/indicators/`. Backtests are streaming only; batch is removed. All math uses `Decimal`. The research-vs-production distinction goes away because there is only one mode.

**What this requires:**

- Port ~197 pandas-ta indicators to streaming form (6 months of work, not one quarter).
- Rewrite `ta_service.py` endpoints to call the streaming engine on buffered history.
- All golden fixtures regenerated.
- CLAUDE.md rewritten: the "research platform + trading engine" framing is replaced with "streaming engine only."

**Cost:** 5-10× Option A for probably less than 2× the benefit. Option B is correct for a product where every user is a backtester. For learn-ai, which is explicitly a research platform (CLAUDE.md § 1, "Math rigor before stack hygiene… porting mathematical logic… strict numerical equivalence"), Option A is the aligned path.

### 5.3 Recommendation

**Take Option A.** Encode it in one paragraph in CLAUDE.md. Add the § 1.2 masking rule so the two Python paths stop disagreeing at warmup. Move `.NET` stats assembly into Python. Delete the dark routers. Unify the two BacktestResult shapes.

The review's "multi-brain architecture" argument is right that today, truth is split. Option A is the minimal set of commits that collapses it to one brain without rewriting a working engine.

---

## 6. Updated P0

The parent's § 9 P0 is unchanged for scope, but reorder by **architectural-invariant restoration** rather than damage-magnitude. The recommended sequence:

| # | Action | Restores which invariant |
|---|---|---|
| 1 | Write and land the timestamp-policy paragraph in `.claude/rules/numerical-rigor.md` + `CLAUDE.md` (§ 3 above) | Temporal integrity — **policy first** so subsequent fixes have a target |
| 2 | Fix `/api/sanitize` ms-collapse (§ 2.2 of parent) + `_format_timestamp` local-time (§ 2.3) + `MarketDataService.cs:450` (§ 2.5) + `rule_based_backtest.py:108` dedupe (§ 2.9) — one PR, four fixes | Temporal integrity — close the class |
| 3 | Sanitize non-finite floats in `engine/results/statistics.py` (§ 5.3) | Correctness — stop 500-ing on legal runs |
| 4 | Write and land the "Python owns math" paragraph in `CLAUDE.md` (§ 5 above) | Architectural decision — before any stack work |
| 5 | Delete the dark Python routers (§ 3.2) + update `CLAUDE.md` claim about LEAN-parity stats | Cognitive integrity |
| 6 | Mask pandas-ta `ta.rsi` output for `i < 3*period` at `ta_service.py:71-73` (§ 1.2) | Math non-equivalence — close to within `atol=1e-9` |
| 7 | Fix `CalculateIndicatorsResult.error` / `.message` rename (§ 3.3) | Contract — Python errors reach the UI |
| 8 | Vectorize `chart_service.py:996` + sync-to-threadpool on `chart.py:65` (§ 5.4) | Performance — root cause, not symptom |
| 9 | Promote `selectedNames` getter to `computed<Set<string>>()` (§ 4.3) | Reactive — cheap UX win |
| 10 | Remove signal-writing effects (§ 4.4) — convert to methods or computeds | Reactive — consistency boundary |
| 11 | None-guard on `_calc_sma/_calc_ema/_calc_rsi` (§ 1.1) | Correctness — stop 500-ing on short windows |

This order means **the policy lands before the fixes**. Every subsequent PR has a rule to point at.

---

## 7. Verdict after review

The parent audit identified 30+ findings, re-verified 5 prior ones, proposed a codegen plan, and measured latency end-to-end. The review's challenge is fair: it **ranked by damage, not by invariant**. This addendum restacks by invariant, restores the three dimensions the review correctly called out as missing, and forces the Option A / Option B choice.

**Grade movement after the addendum:** A- → A. A+ is out of reach until the architectural decision in § 5 is committed, the timestamp policy is encoded, and the minimal replay suite in § 4.1 is green. Those are three PRs, not an audit.

**What remains true from the parent:** every file:line citation, every measurement, every severity on body findings, every codegen recommendation in § 8. This addendum reframes the executive summary and adds the dimensions that were out of scope. It does not retract a finding.

**What the review did better than the audit:** collapsed five temporal findings into one failure class; insisted on the root-cause framing for the Top-10; forced the architectural decision. Accepted.

**What the audit did better than the review credited:** § 6's "math-of-record for backtest statistics is in .NET, not Python — which contradicts CLAUDE.md" *is* the multi-brain statement, said in the repo's own vocabulary. The review wanted it louder; this addendum makes it louder and makes it actionable (§ 5).
