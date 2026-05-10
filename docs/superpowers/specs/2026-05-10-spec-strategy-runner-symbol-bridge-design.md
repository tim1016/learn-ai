# spec-strategy-runner picker adoption + polygon-date-range deletion — design

**Status:** approved (brainstorm), pending implementation plan
**Date:** 2026-05-10
**Author:** Claude (with Tim)
**Predecessors:**
- `docs/superpowers/specs/2026-05-09-ticker-range-picker-everywhere-design.md` (the parent initiative)
- PR #205 deferred this consumer + the `polygon-date-range` deletion that gates on it

## Goal

Migrate `spec-strategy-runner` off `<app-polygon-date-range>` onto the picker family, and delete `Frontend/src/app/shared/polygon-date-range/` once no consumer remains. **Frontend-only change.** No backend, no GraphQL, no fixtures, no schema migration.

The earlier audit memo treated this as "a domain-shape change" that warranted lifting `symbols` out of `StrategySpec`. On a closer read the domain shape is correct as-is: `StrategySpec.symbols: list[str]` is intentionally future-tense (multi-symbol Phase-2+); the `model_validator` enforces single-symbol Phase-1 today. The mismatch is in the form-binding layer, not the schema.

## Decisions

Distilled from a four-question brainstorm. Each row captures one of two or three options surfaced in conversation; the rationale is why this option won.

| # | Question | Decision | Rationale |
|---|---|---|---|
| Q1 | How should the picker's symbol bind to `spec.symbols`? | **Frontend adapter — TickerRange-shaped writable bridge.** Picker reads `symbol` from `spec().symbols[0]` (via the `range` signal); on change, the consumer mutates `spec.symbols = [next.symbol]`. **No backend touched.** | Domain rule "a strategy spec declares its symbols" stays. Lifting `symbol` to `SpecBacktestRequest` (Option B) inverts authority and creates a research/walk-forward fixture-migration cascade for no engine-capability gain. Multi-ticker UX today (Option C) lies about the engine's `len(symbols) != 1` rejection. |
| Q2 | Sampling card visibility | **`[hideSampling]="true"`** — same pattern as `indicator-reliability`. The spec already owns `resolution.period_minutes`. | "Sampling lives where it lives" carries cleanly from `indicator-reliability`. Bridging `period_minutes ↔ (resolution, multiplier)` introduces a lossy seam (e.g. `period_minutes=45` has no clean picker representation). |
| Q3 | Date signal shape | **Consolidate `(fromDate, toDate)` into one `range = signal<TickerRange>(...)`** matching every other PR (iii) consumer. Six call-site renames. | PR (iii) set the precedent. Diverging here for diff-size reasons would create a maintenance asymmetry that's hard to justify later. Tim: *"goal is a single UI/UX, spec-strategy-runner should follow the same shape as the other PR (iii) consumers."* |
| Q4 | PR scope | **Bundle in this PR**: spec-strategy-runner migration + delete `polygon-date-range/`. | The deletion is the gating dependency this work was created to resolve; splitting adds PR overhead with no review benefit. |

## Architecture

One layer touched: **Frontend.** Zero backend / GraphQL / Pydantic / fixture / .NET DTO changes. The migration is local to `spec-strategy-runner.component.{ts,html,spec.ts}` plus the deletion of the now-orphaned `polygon-date-range/` directory.

```
┌─────────────────────────────────────────────────────────┐
│ spec-strategy-runner.component.ts                       │
│                                                         │
│   range = signal<TickerRange>({                         │
│     symbol: spec().symbols[0],   ◀──── single source    │
│     from, to,                          of truth for     │
│     resolution: 'minute',              picker UI        │
│   })                                                    │
│                                                         │
│   onRangeChange(next):                                  │
│     this.range.set(next)                                │
│     if next.symbol !== spec.symbols[0]:                 │
│       this.spec.update(s => ({                          │
│         ...s, symbols: [next.symbol]                    │
│       }))                          ◀──── domain         │
│                                          authority      │
│                                          (spec.symbols  │
│                                          stays plural,  │
│                                          future-tense)  │
└─────────────────────────────────────────────────────────┘
```

## Components & files

**Modified:**

```
Frontend/src/app/components/spec-strategy-runner/
  spec-strategy-runner.component.ts
    - drop `fromDate` / `toDate` signals
    - drop `PolygonDateRangeComponent` import
    - add `TickerRangePickerComponent` + `TickerRange` type imports
    - add `TICKER_POOL` / `RECENT_TICKERS` from shared/ticker-catalog
    - add `range = signal<TickerRange>({...})`
    - add `onRangeChange(next)` bridge method
    - rename 6 call sites: `this.fromDate()` → `this.range().from`,
      `this.toDate()` → `this.range().to` at :182-184 (validation)
      and :646-647 (runBacktest)

  spec-strategy-runner.component.html
    - replace the `<app-polygon-date-range [(fromDate)] [(toDate)] ...>`
      block (lines 363-371 today) with:
        <app-ticker-range-picker
          [value]="range()"
          (valueChange)="onRangeChange($event)"
          [tickerPool]="tickerPool"
          [recent]="recentTickers"
          [hideSampling]="true"
          title="Backtest data" />

  spec-strategy-runner.component.spec.ts
    - add: symbol bridge round-trip test (set range.symbol, assert
      spec().symbols updates)
    - add: range.from/to flow into validation
    - update existing tests that reference fromDate/toDate signals
```

**Deleted (final commit of the PR):**

```
Frontend/src/app/shared/polygon-date-range/
  index.ts
  polygon-date-range.component.{ts,html,scss,spec.ts}
```

**Untouched:**

- `Frontend/src/app/shared/multi-ticker-range-picker/` and `Frontend/src/app/shared/ticker-date-picker/` — picker family stays as PR (i) shipped.
- `PythonDataService/app/engine/strategy/spec/schema.py` — `StrategySpec.symbols: list[str]` plural; `model_validator` Phase-1 single-symbol enforcement; both unchanged.
- `PythonDataService/app/engine/strategy/spec/fixtures/*.spec.json` — all three keep `"symbols": ["SPY"]`.
- `PythonDataService/app/routers/spec_strategy.py` — `SpecBacktestRequest` shape unchanged; route handler reads `spec.symbols[0]` as before.
- `walk_forward.py`, `research_runs.py`, `engine.py`, `evaluator.py`, `live_engine.py` — no changes (they read `spec.symbols` from the parsed `StrategySpec`; the spec shape is unchanged).
- All Backend (.NET) DTOs (`ResearchModels.cs`, `SignalModels.cs`, `BatchResearchModels.cs`, `GapDetectionModels.cs`, `SpecStrategyModels.cs`) and GraphQL mutation arguments — those target unmigrated Python routes (research, signal, walk-forward) whose response shapes still use legacy field names.
- `int64 ms UTC` wire-format migration — separate initiative cross-linked to F-0009/F-0019/F-0020/F-0021/F-0022/F-0024/F-0033/F-0034.

## Data flow

```
User picks a ticker / changes a date in <app-ticker-range-picker>
       │
       ▼ (valueChange)
onRangeChange(next: TickerRange)
       │
       ├──▶ this.range.set(next)
       │       (picker UI source of truth — drives validation reads
       │        of range().from / range().to)
       │
       └──▶ if next.symbol !== spec().symbols[0]:
              this.spec.update(s => ({ ...s, symbols: [next.symbol] }))
              (domain authority — StrategySpec.symbols stays the
               canonical home of the strategy's traded symbol; the
               picker is a UI projection of it)
```

`runBacktest()` reads from:
- `range().from` / `range().to` → GraphQL `startDate` / `endDate` mutation args
- `this.spec()` → GraphQL `specJson` (the picker's symbol changes are already inside `spec.symbols` via the bridge — nothing extra to wire at the service-call site)

`validateStrategy(spec, runOpts)` keeps its existing call shape: `start`/`end` come from `range().from/.to` instead of `fromDate()/toDate()`; `resolutionMinutes` continues from `spec.resolution.period_minutes`.

The flow is **uni-directional**: picker → range signal → spec mutation. If a future code path mutates `spec.symbols` directly without going through `onRangeChange`, the picker won't reflect that change until the next `range.set`. No such code path exists today; calling this out in case one is added.

## Error handling

No new error paths. The picker's existing date validation (PrimeNG `min`/`max` + Polygon-aware constraints inherited from PR (i)'s `TimeWindowCardComponent`) catches malformed dates before `validateStrategy` runs. Symbol non-emptiness/length is enforced by `_SymbolStr` (`Annotated[str, StringConstraints(min_length=1, max_length=20)]`) on the Python wire from PR (ii). `StrategySpec.model_validator` continues to reject `len(symbols) != 1` at the Python boundary — unchanged.

The `validateStrategy` Frontend-side checks (`spec-strategy-runner/validation.ts`) keep their existing semantics. Any error string that mentions "fromDate" / "toDate" gets rephrased to "range start" / "range end" if such strings exist; that's a minor copy-edit caught during implementation.

## Testing

`spec-strategy-runner.component.spec.ts` — three additions:

1. **Symbol bridge round-trip.** Set `component.onRangeChange({...range(), symbol: 'AAPL'})`, assert `component.spec().symbols` deep-equals `['AAPL']` AND `component.range().symbol === 'AAPL'`.
2. **Date flow into validation.** Set `component.range.set({...range(), from: '2025-01-01', to: '2025-12-31'})`, assert the next `validateStrategy` call receives `start: '2025-01-01'`, `end: '2025-12-31'`.
3. **runBacktest payload reflects bridge.** Set `range.symbol = 'TSLA'` via `onRangeChange`, call `runBacktest()`, assert the GraphQL mutation payload's `specJson` parses back to a spec with `symbols === ['TSLA']`.

Existing tests that reference `fromDate()` / `toDate()` are updated to read from `range()` instead.

`Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.spec.ts` (6 existing tests) is **deleted** along with the rest of the directory.

Project-scope checks before push:
- `podman exec my-frontend npx ng test --watch=false` — full Vitest
- `npx eslint Frontend/src/ --max-warnings 0` — clean against master baseline
- `podman exec my-frontend npx tsc --noEmit` — clean

## Out of scope (tracked follow-ups)

- **Multi-symbol UX.** When the engine gains multi-symbol support (and the `model_validator` relaxes from Phase-1 single-symbol enforcement), the picker swaps from `<app-ticker-range-picker>` to `<app-multi-ticker-range-picker>` and the bridge becomes `MultiTickerRange.symbols ↔ spec.symbols` directly. Until that day, the picker is visibly single-symbol — no chip array, no UI affordance for a capability the validator rejects.
- **`int64 ms UTC` wire-format migration.** The cross-stack timestamp-rigor initiative; separate from this work.
- **Other Polygon-aware niceties** (e.g. holiday-disable on the picker's date inputs) come for free from the canonical picker's existing TimeWindowCard logic — no extra design needed.

## Authority hierarchy notes

This work touches one stack:

- **Frontend** — `.claude/rules/angular.md`. New code is fully compliant: standalone, OnPush, signals + `model()` / `signal()`, modern control flow, no decorators, no `mutate()`. The bridge uses `signal.update` for the spec mutation (immutable patch).

No numerical-rigor or timestamp-rigor changes (no math is touched; the wire format on this consumer is unchanged from PR (iii)).
