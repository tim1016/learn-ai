---
id: F-0034
severity: P2
status: deferred
area: timestamp
canonical_file: Frontend/src/app/components/lean-engine/engine-replay-v2/services/replay-engine-v2.service.ts; Frontend/src/app/services/replay-{engine,indicator,strategy}.service.ts; multiple lean-engine + research-lab + edge components
reference: docs/audits/computational-fidelity-2026-04-22.md (top-10 finding #2); .claude/rules/numerical-rigor.md (Timestamp rigor → Ban list)
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 3
---

## What

Phase 3 TS triage of `new Date(<variable>)` across the Frontend. **45 candidate files from F-0020**; per-file inspection classifies them into:

- **Tier 1 — Engine-replay services** (highest risk): `bar.timestamp` is treated as a string and parsed via `new Date(b.timestamp).getTime()` across ~17 occurrences in 4 service files. If `b.timestamp` is `int64 ms` (per the canonical wire format), `new Date(number).getTime()` is idempotent and safe. If it's a naive ISO string (per F-0009/F-0033 producer side), this is the **browser-shift bug from prior-audit top-10 #2**: Chrome/Safari interpret naive ISO as local time; Firefox returns Invalid Date. **Severity: P1**, contingent on Step 3.1 wire-format change closing this transitively.
- **Tier 2 — Date-only query parameters**: `new Date(this.fromDate())` and friends, where the signal returns `"YYYY-MM-DD"`. Date-only naive ISO is mostly benign in modern browsers (interpreted as UTC midnight) but explicit `Date.UTC(...)` would be safer. **Severity: P2.**
- **Tier 3 — Display, tooltip, formatter, chart axis**: `new Date(ms).toLocaleDateString(...)` etc. **Severity: P3 / display-only / not on ban list per `learn-ai-validation` escape hatches.**
- **Tier 4 — Defensive band-aids**: `engine-chart.component.ts:345` does `new Date(ts.includes('T') ? ts : ts.replace(' ', 'T') + ':00Z')` — explicitly compensates for naive producer output by appending `Z`. **This is the GOOD pattern**, not a bug; but it's evidence the producer is emitting naive strings. **Severity: P3** (works correctly; counts as informal proof of producer-side issue already filed in F-0009 + F-0033).

## Where

### Tier 1 — engine-replay services (P1)

```
Frontend/src/app/components/lean-engine/engine-replay-v2/services/replay-engine-v2.service.ts:98, 148, 149, 166, 167, 226, 277, 280, 284, 285, 393, 394   (12 occurrences)
Frontend/src/app/services/replay-engine.service.ts:59
Frontend/src/app/services/replay-indicator.service.ts:24
Frontend/src/app/services/replay-strategy.service.ts:18, 21, 30   (prior audit #2 explicitly named replay-strategy.service.ts:18)
Frontend/src/app/components/lean-engine/engine-replay-v2/engine-replay-v2.component.ts:119
Frontend/src/app/components/lean-engine/lean-engine.component.ts:419, 878, 937, 963
Frontend/src/app/components/lean-engine/engine-chart/engine-chart.component.ts:325, 333
Frontend/src/app/components/research-lab/feature-report/feature-report.component.ts:89, 90
Frontend/src/app/components/edge/services/edge-api.service.ts:226, 278
```

### Tier 2 — Date-only query parameters (P2)

```
Frontend/src/app/components/data-lab/data-lab.component.ts:534, 535
Frontend/src/app/components/data-lab/data-lab-chart/data-lab-chart.component.ts:558, 559
Frontend/src/app/utils/date-validation.ts:75
Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.types.ts:130, 151
Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.ts:330, 331, 369
```

### Tier 3 — Display/format/chart-axis (P3 / display-only)

`broker/format.ts`, `broker-options-chain`, `data-lab/data-lab.component.ts:1166`, `engine-history.component.ts:331`, `quality-modal`, `run-dock`, `run-progress-panel`, `spec-strategy-runner.component.ts:646`, `engine-results.component.ts:252`, `insight-panel.component.ts:167`, `volume/line/candlestick/equity` charts (`getTime() / 1000` for chart-library `UTCTimestamp`), `indicator-report.component.ts:413`. Also `replay-chart-v2.component.ts:159`.

### Tier 4 — Defensive band-aid (P3)

`engine-chart.component.ts:345` — `new Date(ts.includes('T') ? ts : ts.replace(' ', 'T') + ':00Z')` — defends against naive producer output. Good pattern, but evidence of producer bug.

### Tier 5 — Test factories / specs / mocks (skip)

`testing/factories/market-data.factory.ts`, `*.spec.ts` files, `edge-mock-data.service.ts`. Out of scope for this finding.

## Why this severity

P1 — The Tier 1 cases are the actual risk. They depend on what `bar.timestamp` is at runtime:

- **If wire format is `int64 ms` (the goal):** `new Date(number).getTime()` is idempotent. Safe.
- **If wire format is naive ISO** (F-0009/F-0033 reality): the parse is browser-dependent. Chrome/Safari treat as local; Firefox may fail. Under load this manifests as wrong PnL, missing trades, replay scrubber misalignment.

The prior audit's top-10 #2 explicitly named `replay-strategy.service.ts:18` as a CRITICAL finding for this reason. The fix is **producer-side** (Step 3.1 of §5 — `int64 ms` wire format) — once the wire is `int64 ms`, all Tier 1 occurrences become benign and `new Date(number).getTime()` is correct.

## Reproduction

```
grep -rnE 'new Date\([a-zA-Z_][a-zA-Z0-9_]*\)|new Date\([a-zA-Z_][a-zA-Z0-9_]*\.' Frontend/src/ --include='*.ts' | wc -l
# 80+ occurrences in 45 files (head_limit=80 hit during sweep)
```

## Suggested resolution (NOT auto-applied)

**The fix is producer-side** — Step 3.1 in §5 (Python wire format → `int64 ms UTC`). Once that lands:

- All Tier 1 sites become idempotent (`new Date(number).getTime()` returns the input number). No frontend edits needed.
- The Tier 4 band-aid in `engine-chart.component.ts:345` becomes dead code and can be removed.
- Tier 2 date-only inputs are unrelated to the wire-format change and need their own pass — convert to `Date.UTC(y, m-1, d)` for clarity.

**Tier 1 stays P1 until the producer fix lands.** No frontend-only fix is appropriate; greenwashing the consumers without fixing the producer would mask the real bug.

## Provenance of the finding itself

Phase 3 / cursor: per-file triage of `new Date(<var>)` across `Frontend/src/`. Output cap was 80 lines so the actual scope is slightly larger; the tail looked similar in pattern based on the high-suspicion file inspection in F-0028 work.

## Tier 1 resolution (2026-05-06)

**Tier 1 (P1) is resolved by the producer-side fix.** F-0009 and F-0033 changed the Python service to emit `int64 ms` timestamps on the wire (70-file commit, Stage E–I of remediation). With `bar.timestamp` now a number, `new Date(number).getTime()` is idempotent and safe — no browser-dependent parse occurs.

The Tier 4 band-aid in `engine-chart.component.ts:345` (appending `':00Z'` to naive ISO strings) is now dead code but harmless; it can be removed in a future cleanup PR.

**Remaining concern**: Tier 2 (date-only query parameters — `new Date("YYYY-MM-DD")`) is still present. `"YYYY-MM-DD"` without a timezone designator is interpreted as UTC midnight per the HTML spec, so modern browsers handle this consistently, but `Date.UTC(y, m-1, d)` would be more explicit. This is the residual **P2** concern. Tier 3/4 are display-only P3 and do not need fixes.

Severity downgraded from P1 → P2. Finding remains deferred (Tier 2 is a frontend-only cleanup, not a data-integrity issue).
