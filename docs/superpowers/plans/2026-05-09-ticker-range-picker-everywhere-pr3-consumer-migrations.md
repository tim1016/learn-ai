# PR (iii) — Consumer migrations + cleanup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal (REVISED post-review):** Migrate **six** research-lab/explorer consumers off `<app-polygon-date-range>` and onto the picker family (canonical, multi, or date sibling); remove the Pydantic transitional aliases + the deprecated `hideResolution` input + the GraphQL `[GraphQLName]` schema aliases pinned in PR (ii); delete `<app-polygon-date-range>`. Land each consumer as its own commit (matching PR #198's cadence) so any single migration can be reverted without rewinding the others.

**Deferred from this PR:**
- `spec-strategy-runner` — `StrategySpec.symbols: list[str]` is a plural domain shape, not a UI state. **Own design follow-up.**
- `indicator-report` — never adopted `polygon-date-range`; needs a separate signals + OnPush refactor. **PR (iv) follow-up.**

**Architecture:** Each consumer's HTML drops the existing `<app-polygon-date-range>` + sibling inputs (ticker text input, multiplier int, timespan select, etc.) and replaces them with one of: `<app-ticker-range-picker>` (canonical), `<app-multi-ticker-range-picker>` (batch-runner), or `<app-ticker-date-picker>` (ticker-explorer). Consumer TS migrates from N separate signals (`ticker`, `fromDate`, `toDate`, `timespan`, `multiplier`) to a single `range = signal<TickerRange>(…)`. **Each consumer's `range` signal is initialized with that consumer's pre-migration defaults** (e.g. `signal-runner` initializes `multiplier: 15` to match `SignalEngineJobRequest`'s default; `feature-runner` keeps `multiplier: 1`). Service calls run through `tickerRangeToWire(range())` from PR (i)'s adapter.

**Tech Stack:** Angular 21 (standalone, OnPush, signals, `model()`, `@if`/`@for`), `tickerRangeToWire` adapter from PR (i), .NET DTOs from PR (ii), ESLint, Vitest, ruff (for the symbol-lift backend coordination), pytest, dotnet test/format.

**Spec reference:** `docs/superpowers/specs/2026-05-09-ticker-range-picker-everywhere-design.md` §"Migration order (inside PR iii)", §"Build sequence — PR (iii)".

**Predecessors that MUST be merged first:**
- PR (i) — picker enhancements + sibling components + `tickerRangeToWire` adapter
- PR (ii) — Python `TickerRequest` schema base + .NET DTO renames

---

## File structure

**Modified (Frontend consumers — one commit each, REVISED — six consumers):**
```
Frontend/src/app/components/research-lab/indicator-reliability/indicator-reliability.component.{ts,html,spec.ts}
Frontend/src/app/components/research-lab/strategy-preflight/strategy-preflight.component.{ts,html,spec.ts}
Frontend/src/app/components/research-lab/feature-runner/feature-runner.component.{ts,html,spec.ts}
Frontend/src/app/components/research-lab/signal-runner/signal-runner.component.{ts,html,spec.ts}
Frontend/src/app/components/research-lab/batch-runner/batch-runner.component.{ts,html,spec.ts}
Frontend/src/app/components/ticker-explorer/ticker-explorer.component.{ts,html,spec.ts}
```

**Excluded (deferred — see "Deferred" section above):**
```
Frontend/src/app/components/spec-strategy-runner/    (own design follow-up)
Frontend/src/app/components/indicator-report/        (PR iv follow-up)
```

**Deleted (final commits of the PR):**
```
Frontend/src/app/shared/polygon-date-range/           (entire directory)
Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.ts: hideResolution input (deprecated alias from PR i)
```

**Final cleanup commits:**
```
PythonDataService/app/schemas/ticker_request.py        (remove AliasChoices for legacy names — Pydantic-only transitional layer)
Backend/GraphQL/*.cs                                   (remove [GraphQLName] schema aliases pinned in PR ii Task 12 step 5)
```

**Untouched (no .NET DTO setter cleanup — there are no transitional .NET setters to remove; PR (ii) used canonical-only renames):**
```
Backend/Models/DTOs/*.cs    (already canonical-only after PR ii)
```

---

## Conventions for every task

- **Branch:** start fresh after PR (ii) merges. `git checkout master && git pull && git checkout -b feat/ticker-range-picker-everywhere-consumers`.
- **Commit cadence:** one commit per migrated consumer; one commit for the symbol-lift backend coordination; one commit for the alias removals; one commit for `polygon-date-range` deletion. Total ~12 commits.
- **TDD per consumer:** update the consumer's existing spec (or add new test) to reflect the new picker → run-fail → migrate the HTML/TS → run-pass → commit.
- **Per-consumer iteration:**
  ```bash
  podman exec my-frontend npx ng test --watch=false --include='src/app/components/<consumer-path>/**'
  ```
- **Frontend payload migration pattern** — every consumer follows this same TS shape transformation:

  Before (typical post-PR-#198):
  ```ts
  ticker = signal<string>('AAPL');
  fromDate = signal<string>('2025-01-01');
  toDate = signal<string>('2025-04-30');
  timespan = signal<Timespan>('minute');
  multiplier = signal<number>(15);
  ```

  After (post-migration):
  ```ts
  range = signal<TickerRange>({
    symbol: 'AAPL',
    from: '2025-01-01',
    to: '2025-04-30',
    resolution: 'minute',
    multiplier: 15,
  });
  ```

  Service calls:
  ```ts
  // Before
  this.api.run({ ticker: this.ticker(), fromDate: this.fromDate(), ... });

  // After
  import { tickerRangeToWire } from '../../utils/ticker-wire';
  this.api.run(tickerRangeToWire(this.range()));
  ```

- **Smoke check for visual regression:** after each consumer migration, manually load `http://localhost:4200/<consumer-route>` and verify the picker renders, behaves correctly, and submits successfully. UI check is mandatory per CLAUDE.md ("for UI or frontend changes, start the dev server and use the feature in a browser before reporting the task as complete").

---

## Task 1: Migrate `indicator-reliability` (cleanest fit — uses `hideSampling`)

**Files:**
- Modify: `Frontend/src/app/components/research-lab/indicator-reliability/indicator-reliability.component.{ts,html,spec.ts}`

This consumer's "sampling" is the indicator's own timeframe — passed via the indicator config, not the picker. Smoke-tests `hideSampling=true`.

- [ ] **Step 1: Update the spec to expect the picker**

In `indicator-reliability.component.spec.ts`, find the existing test asserting `<app-polygon-date-range>` rendering. Replace the assertion:

```ts
// Before
expect(screen.queryByTestId('polygon-date-range')).toBeTruthy();

// After
expect(screen.queryByText(/^Instrument$/)).toBeTruthy();        // Instrument card
expect(screen.queryByLabelText(/from/i)).toBeTruthy();           // Time window card
expect(screen.queryByRole('radiogroup', { name: /Resolution/i })).toBeNull(); // hideSampling
```

- [ ] **Step 2: Verify failure**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/components/research-lab/indicator-reliability/**'
```
Expected: FAIL.

- [ ] **Step 3: Update the HTML**

In `indicator-reliability.component.html`, replace the existing `<div class="field field--date-range"> <app-polygon-date-range ... /> </div>` block (lines 70–78) with:

```html
<div class="field field--picker">
  <app-ticker-range-picker
    [(value)]="range"
    [tickerPool]="tickerPool()"
    [recent]="recentTickers()"
    [hideSampling]="true"
    title="Reliability data" />
</div>
```

Also remove the separate ticker `<input pInputText id="ticker" ...>` if one exists, since the picker owns the symbol now.

- [ ] **Step 4: Update the TS**

```ts
// indicator-reliability.component.ts
import { TickerRangePickerComponent } from '../../../shared/ticker-range-picker/ticker-range-picker.component';
import type { TickerRange } from '../../../shared/ticker-range-picker/ticker-range-picker.types';
import { tickerRangeToWire } from '../../../utils/ticker-wire';

// Replace existing ticker / fromDate / toDate signals with:
range = signal<TickerRange>({
  symbol: 'AAPL',
  from: this.defaultFromDate(),
  to: this.defaultToDate(),
  resolution: 'daily',
});

// Service call site — wherever the existing code constructs the request body:
const payload = {
  ...tickerRangeToWire(this.range()),
  // ...indicator-specific fields (indicator name, params, horizons, ...)
};
```

Add `TickerRangePickerComponent` to the component's `imports` array. Remove `PolygonDateRangeComponent` from imports.

- [ ] **Step 5: Run + verify pass**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/components/research-lab/indicator-reliability/**'
```
Expected: PASS.

- [ ] **Step 6: Manual UI smoke**

```bash
podman compose up -d frontend
# Navigate to http://localhost:4200/research-lab/indicator-reliability
# Verify: picker renders with Instrument + Time window cards; no Sampling card.
# Pick a ticker, change the date range, hit Run — verify the request lands on
# the Python service successfully (check polygon-data-service logs).
```

- [ ] **Step 7: Commit**

```bash
git add Frontend/src/app/components/research-lab/indicator-reliability/
git commit -m "refactor(indicator-reliability): adopt ticker-range-picker with hideSampling"
```

---

## Task 2: Migrate `strategy-preflight` — uses `availableMultipliers`

**Files:**
- Modify: `Frontend/src/app/components/research-lab/strategy-preflight/strategy-preflight.component.{ts,html,spec.ts}`

This consumer has its own `timeframe: '5m'|'15m'|'1h'` shape today; PR (iii) maps that to `{ resolution, multiplier }` via the picker.

- [ ] **Step 1: Update spec** — assert picker renders with multiplier dropdown (`availableMultipliers={[1,5,15,60]}`).

```ts
expect(screen.queryByLabelText(/multiplier/i)).toBeTruthy();
```

- [ ] **Step 2: Verify failure.**

- [ ] **Step 3: HTML migration**

Replace the existing date-range field + the separate `<select>` for `timeframe` (lines 23-30 + 31-39 of current HTML) with:

```html
<app-ticker-range-picker
  [(value)]="range"
  [tickerPool]="tickerPool()"
  [recent]="recentTickers()"
  [availableResolutions]="['minute','hour']"
  [availableMultipliers]="[1, 5, 15, 60]"
  title="Pre-flight data" />
```

Remove the separate `<input>` for symbol and the `<select>` for timeframe — the picker owns both.

- [ ] **Step 4: TS migration**

```ts
range = signal<TickerRange>({
  symbol: 'SPY',
  from: this.defaultFromDate(),
  to: this.defaultToDate(),
  resolution: 'minute',
  multiplier: 5,    // legacy timeframe='5m' default
});

// Replace the timeframe-string mapping:
private timeframeFromRange(r: TickerRange): string {
  // Used for any UI label that still wants a 'NM/h' string, e.g. tooltips
  if (r.resolution === 'hour' && (r.multiplier ?? 1) === 1) return '1h';
  return `${r.multiplier ?? 1}${r.resolution === 'minute' ? 'm' : 'h'}`;
}
```

Service call — use `tickerRangeToWire(this.range())`.

- [ ] **Step 5: Run + verify pass + manual smoke + commit**

```bash
git add Frontend/src/app/components/research-lab/strategy-preflight/
git commit -m "refactor(strategy-preflight): adopt ticker-range-picker with availableMultipliers"
```

---

## Task 3: Migrate `feature-runner` — full multiplier surface

**Files:**
- Modify: `Frontend/src/app/components/research-lab/feature-runner/feature-runner.component.{ts,html,spec.ts}`

Drops: standalone `<input pInputText id="ticker">`, `<p-select id="timespan">`, `<input id="multiplier" type="number">` (lines 28-48 of current HTML).

- [ ] **Step 1: Spec update** — assert picker renders with multiplier dropdown including `[1, 5, 15, 60, 240]`.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: HTML migration**

Replace lines 28-48 of current `feature-runner.component.html` with:

```html
<app-ticker-range-picker
  [(value)]="range"
  [tickerPool]="tickerPool()"
  [recent]="recentTickers()"
  [availableResolutions]="['minute','hour','daily']"
  [availableMultipliers]="[1, 5, 15, 60, 240]"
  title="Feature research data" />
```

The "Force re-run" checkbox stays as a separate `field--toggle` (it's not a picker concern).

- [ ] **Step 4: TS migration**

```ts
range = signal<TickerRange>({
  symbol: 'AAPL',
  from: this.defaultFromDate(),
  to: this.defaultToDate(),
  resolution: 'minute',
  multiplier: 15,    // existing default
});

// canRun computed unchanged in spirit; just reads from range() now
canRun = computed(() => this.range().symbol.length > 0 && /* ... */);

// runResearch service call:
this.api.runFeature({
  ...tickerRangeToWire(this.range()),
  feature_name: this.selectedFeature(),
  force: this.forceRun(),
});
```

- [ ] **Step 5: Run + verify pass + manual smoke + commit**

```bash
git add Frontend/src/app/components/research-lab/feature-runner/
git commit -m "refactor(feature-runner): adopt ticker-range-picker with availableMultipliers"
```

---

## Task 4: Migrate `signal-runner` — same pattern as feature-runner, **but `multiplier: 15` default**

**Files:**
- Modify: `Frontend/src/app/components/research-lab/signal-runner/signal-runner.component.{ts,html,spec.ts}`

Pattern identical to Task 3 except for **default preservation**: `SignalEngineJobRequest`'s pre-migration default is `multiplier=15` (`jobs.py:162`). The frontend `range` signal must initialize with `multiplier: 15` to match — without this, signal-runner silently switches to 1-minute bars.

- [ ] **Steps 1–4** identical to Task 3, with `signal-runner` paths and the additional Options card retained verbatim (Flip Sign / Regime Gate / Force Re-run toggles stay).

  **Critical TS difference:**
  ```ts
  range = signal<TickerRange>({
    symbol: 'AAPL',
    from: this.defaultFromDate(),
    to: this.defaultToDate(),
    resolution: 'minute',
    multiplier: 15,    // PRESERVE pre-migration default — DO NOT default to 1
  });
  ```

  **Add a default-preservation test** to `signal-runner.component.spec.ts`:
  ```ts
  it('initializes range.multiplier to 15 to preserve pre-migration behavior', async () => {
    const { fixture } = await render(SignalRunnerComponent);
    expect(fixture.componentInstance.range().multiplier).toBe(15);
  });
  ```

- [ ] **Step 5: Commit** as `refactor(signal-runner): adopt ticker-range-picker with availableMultipliers`.

  Commit message must explicitly note the multiplier-default preservation:
  ```
  Preserves SignalEngineJobRequest's pre-migration default of multiplier=15
  by initializing the picker's range signal with that value. The base
  TickerRequest defaults to multiplier=1, which would silently switch
  signal-runner to 1-min bars; this PR's job-side override (PR ii) and
  this PR's frontend-side initializer pin the default to 15 explicitly.
  ```

---

## Task 5: Migrate `batch-runner` — uses `<app-multi-ticker-range-picker>`

**Files:**
- Modify: `Frontend/src/app/components/research-lab/batch-runner/batch-runner.component.{ts,html,spec.ts}`

This is the first consumer of the multi-ticker sibling. Drops the chip grid currently at lines 36-53 of `batch-runner.component.html`.

- [ ] **Step 1: Spec update**

```ts
expect(screen.queryByText(/^Instrument/)).toBeTruthy();        // Multi instrument card
expect(screen.queryByRole('button', { name: /^All$/ })).toBeTruthy();
expect(screen.queryByRole('button', { name: /^None$/ })).toBeTruthy();
```

- [ ] **Step 2: Verify failure.**

- [ ] **Step 3: HTML migration**

Replace the date-range field at lines 24-32 + the entire ticker chip grid at lines 35-53 with:

```html
<app-multi-ticker-range-picker
  [(value)]="range"
  [tickerPool]="tickerPool()"
  [availableResolutions]="['minute','hour','daily']"
  title="Cross-sectional data" />
```

- [ ] **Step 4: TS migration**

```ts
import { MultiTickerRangePickerComponent } from '../../../shared/multi-ticker-range-picker/multi-ticker-range-picker.component';
import type { MultiTickerRange } from '../../../shared/multi-ticker-range-picker/multi-ticker-range-picker.types';
import { multiTickerRangeToWire } from '../../../utils/ticker-wire';

range = signal<MultiTickerRange>({
  symbols: ['AAPL'],
  from: this.defaultFromDate(),
  to: this.defaultToDate(),
  resolution: 'daily',
});

runBatch(): void {
  this.api.runBatch({
    ...multiTickerRangeToWire(this.range()),
    feature_name: this.featureName(),
    target_type: this.targetType(),
  });
}

selectAll(): void {  /* now lives inside the multi-instrument card; remove */ }
deselectAll(): void { /* same; remove */ }
toggleTicker(t: string): void { /* same; remove */ }
```

Drop `selectedTickers: signal<string[]>` (the multi-picker owns it now). Drop `allTickers: string[]` if it was only used for the chip grid.

- [ ] **Step 5: Run + verify pass + manual smoke + commit**

```bash
git add Frontend/src/app/components/research-lab/batch-runner/
git commit -m "refactor(batch-runner): adopt multi-ticker-range-picker

Drops the inline chip grid + All/None buttons + ticker universe state
in favor of <app-multi-ticker-range-picker>. Service calls go through
multiTickerRangeToWire."
```

---

## Task 6: Migrate `ticker-explorer` — uses `<app-ticker-date-picker>`

**Files:**
- Modify: `Frontend/src/app/components/ticker-explorer/ticker-explorer.component.{ts,html,spec.ts}`

This consumer's "date" is an option expiration date — must be a future Friday by default. Use `<app-ticker-date-picker>` with consumer-supplied `minDate` (today) and a default value of next Friday.

- [ ] **Step 1: Spec update** — assert picker renders the symbol + a single date input.

- [ ] **Step 2: Verify failure.**

- [ ] **Step 3: HTML migration**

Replace lines 47-59 (the search-form `<input>` for ticker + `<input type="date">` for expiration) with:

```html
<div class="search-form">
  <app-ticker-date-picker
    [(value)]="snapshot"
    [tickerPool]="tickerPool()"
    [recent]="recentTickers()"
    [minDate]="todayDate"
    title="Options snapshot"
    dateLabel="Expiration date" />
  <button (click)="fetchSnapshot()" [disabled]="loading()">Fetch Chain</button>
</div>
```

- [ ] **Step 4: TS migration**

```ts
import { TickerDatePickerComponent } from '../../shared/ticker-date-picker/ticker-date-picker.component';
import type { TickerSnapshot } from '../../shared/ticker-date-picker/ticker-date-picker.types';

snapshot = signal<TickerSnapshot>({
  symbol: 'AAPL',
  date: this.nextFriday(),
});

protected readonly todayDate = new Date(); // for minDate input

// fetchSnapshot:
fetchSnapshot(): void {
  const snap = this.snapshot();
  this.api.snapshot({ symbol: snap.symbol, expiration: snap.date });
}

private nextFriday(): string {
  const d = new Date();
  const day = d.getDay();
  const offset = (5 - day + 7) % 7 || 7;
  d.setDate(d.getDate() + offset);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
```

- [ ] **Step 5: Run + verify pass + manual smoke + commit**

```bash
git add Frontend/src/app/components/ticker-explorer/
git commit -m "refactor(ticker-explorer): adopt ticker-date-picker for snapshot tools"
```

---

## ~~Task 7: Migrate `spec-strategy-runner`~~ — REMOVED

`StrategySpec.symbols: list[str]` (`engine/strategy/spec/schema.py:365`) is plural and load-bearing inside the domain spec object. The current Phase-1 evaluator picks `spec.symbols[0]` because it's single-symbol-only, but the type permits multi-symbol strategies. Lifting `symbols` (or `symbol`) to a top-level form field touches the strategy domain shape, not just UI. **Own design**, separate initiative — see spec §"Out of scope".

**Skip to Task 8.**

---

## Task 7-original-removed: spec-strategy-runner content (kept for reference, NOT EXECUTED)

**Files:**
- Modify: `Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.{ts,html,spec.ts}`
- Modify: `PythonDataService/app/routers/spec_strategy.py` (make `symbol` required at top level; remove from spec body)
- Modify: `Backend/Models/DTOs/SpecStrategyModels.cs` (DTO mirrors the lift)
- Modify: `Backend/GraphQL/SpecStrategyMutation.cs` (resolver mirrors the lift)

This is the largest single commit of the PR. Frontend + backend must land atomically — splitting risks runtime failures mid-deploy.

- [ ] **Step 1: Update Python test** — `tests/routers/test_spec_strategy.py` (or analog) — post a body with top-level `symbol` and assert the route uses it.

- [ ] **Step 2: Update .NET test** — `Backend.Tests/Models/SpecStrategyTests.cs` — assert `Symbol` is a top-level required property on the DTO.

- [ ] **Step 3: Frontend spec update**

```ts
expect(screen.queryByText(/^Instrument$/)).toBeTruthy();   // picker renders
// Verify ticker is no longer pulled from `spec.symbol` template:
expect(component.spec().symbol).toBeUndefined();
```

- [ ] **Step 4: Verify all three layers fail.**

- [ ] **Step 5: Backend changes (Python + .NET)**

Python — `PythonDataService/app/routers/spec_strategy.py`:

```python
# Before (post-PR ii — symbol was an optional alias-prep field)
class SpecBacktestRequest(BaseModel):
    spec: dict[str, Any] = Field(...)
    symbol: str | None = Field(None, validation_alias=AliasChoices("symbol", "ticker"))

# After — symbol becomes required at top level; remove from spec body in the handler
class SpecBacktestRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    spec: dict[str, Any] = Field(...)
    symbol: str = Field(..., min_length=1, max_length=20,
                        validation_alias=AliasChoices("symbol", "ticker"))
    # ...other fields

# Route handler:
async def run_spec_backtest(req: SpecBacktestRequest, ...):
    symbol = req.symbol  # always top-level now; legacy spec.symbol path removed
    spec = {**req.spec}
    spec.pop("symbol", None)  # tolerate stale frontend during transition
    ...
```

.NET — `Backend/Models/DTOs/SpecStrategyModels.cs`: add `Symbol` as a required top-level property mirroring the Python field. .NET resolver in `SpecStrategyMutation.cs` reads `Symbol` from the request and forwards it.

- [ ] **Step 6: Frontend changes**

`spec-strategy-runner.component.html` — replace lines 363-371 (the date-range field + sibling fields that read from `spec.symbol`) with:

```html
<app-ticker-range-picker
  [(value)]="range"
  [tickerPool]="tickerPool()"
  [recent]="recentTickers()"
  [availableResolutions]="['minute','hour','daily']"
  [availableMultipliers]="[1, 5, 15, 60]"
  [hideSampling]="false"
  title="Backtest data" />
```

`spec-strategy-runner.component.ts`:

```ts
range = signal<TickerRange>({
  symbol: 'SPY',
  from: this.defaultFromDate(),
  to: this.defaultToDate(),
  resolution: 'minute',
  multiplier: 5,
});

// spec object — remove `symbol` from its body
spec = signal<StrategySpec>({
  // ...spec fields excluding symbol
});

// Run:
runBacktest(): void {
  this.api.runSpecBacktest({
    ...tickerRangeToWire(this.range()),
    spec: this.spec(),  // no longer carries symbol
    initial_cash: this.initialCash(),
    fill_mode: this.fillMode(),
  });
}
```

- [ ] **Step 7: Run all three layers' tests + manual UI smoke**

```bash
podman exec polygon-data-service python -m pytest tests/routers/test_spec_strategy.py -v
cd Backend.Tests && dotnet test --filter "SpecStrategy"
podman exec my-frontend npx ng test --watch=false --include='src/app/components/spec-strategy-runner/**'
# Manual: load http://localhost:4200/spec-strategy-runner, run a backtest end-to-end
```

Expected: ALL PASS.

- [ ] **Step 8: Commit (single atomic commit across three stacks)**

```bash
git add Frontend/src/app/components/spec-strategy-runner/ \
        PythonDataService/app/routers/spec_strategy.py \
        PythonDataService/tests/routers/test_spec_strategy.py \
        Backend/Models/DTOs/SpecStrategyModels.cs \
        Backend/GraphQL/SpecStrategyMutation.cs \
        Backend.Tests/Models/SpecStrategyTests.cs
git commit -m "refactor(spec-strategy-runner): adopt ticker-range-picker; lift symbol out of spec

Frontend + backend coordinated change:
- Frontend: spec-strategy-runner uses <app-ticker-range-picker>; symbol
  is now a top-level form field, not a property of the strategy spec.
- Backend (Python): SpecBacktestRequest.symbol is now required at top
  level; route handler reads req.symbol instead of req.spec['symbol'].
  Stale frontend payloads still carrying spec.symbol have it stripped.
- Backend (.NET): SpecStrategyModels DTO and SpecStrategyMutation
  resolver mirror the lift.

Atomic — splitting risks runtime failures mid-deploy."
```

---

## ~~Task 8: Migrate `indicator-report`~~ — REMOVED

`indicator-report` was never a `polygon-date-range` consumer (template-driven `[(ngModel)]` against non-signal fields; was deferred from PR #198). Migrating it requires a `signals + OnPush` refactor independent of the picker work. **PR (iv) follow-up** — see spec §"Out of scope".

**Skip to Task 9.**

---

## Task 8-original-removed: indicator-report content (kept for reference, NOT EXECUTED)

**Files:**
- Modify: `Frontend/src/app/components/indicator-report/indicator-report.component.{ts,html,spec.ts}`

This consumer was deferred in PR #198 because it uses template-driven `[(ngModel)]="fromDate"` against non-signal fields. Two halves: refactor to signals, then adopt the picker.

**Split rule (from the spec):** if the signal refactor touches more than the consumer's own files (modifies a service or parent route component), or the PR exceeds ~25 changed files total at this point, **split this consumer to PR (iv)** and skip the rest of this task.

- [ ] **Step 1: Audit scope** before starting.

```bash
grep -rln "indicator-report\|indicatorReport\|IndicatorReport" Frontend/src/app/services/ Frontend/src/app/components/ --include="*.ts"
```

If this returns more than the consumer's own directory, **stop and split**:

```bash
git log --oneline --all | head
# Note the commit hash before this task
git status   # should be clean
```

Then create a new branch `feat/ticker-range-picker-everywhere-indicator-report-pr4` off the current branch's HEAD, document the split in PR (iii)'s description, and continue with Task 9 (skip Task 8).

If the audit returns only `indicator-report` files, proceed with steps 2–7.

- [ ] **Step 2: Update spec** — assert picker renders + `fromDate`/`toDate` signals exist.

- [ ] **Step 3: TS refactor — template-driven → signal**

```ts
// Before: bare class fields with template-driven ngModel
fromDate: string = '2025-01-01';
toDate:   string = '2025-04-30';

// After: signal-based + new range
range = signal<TickerRange>({
  symbol: 'AAPL',
  from: '2025-01-01',
  to: '2025-04-30',
  resolution: 'daily',
});
```

Anywhere the class fields were read (e.g. `this.fromDate`), replace with `this.range().from`. Anywhere they were written, replace with `this.range.set({ ...this.range(), from: ... })` — but those write paths only existed because of `[(ngModel)]`, which now goes away.

Add `ChangeDetectionStrategy.OnPush` to the component decorator if it isn't there.

- [ ] **Step 4: HTML migration**

Replace the existing `[(ngModel)]="fromDate"` + `[(ngModel)]="toDate"` inputs with:

```html
<app-ticker-range-picker
  [(value)]="range"
  [tickerPool]="tickerPool()"
  [hideSampling]="true"
  title="Indicator report data" />
```

Remove `FormsModule` from the component's `imports` array if it's no longer used after the refactor.

- [ ] **Step 5: Run + verify pass + manual smoke + commit**

```bash
git add Frontend/src/app/components/indicator-report/
git commit -m "refactor(indicator-report): convert to signals + adopt ticker-range-picker

Drops template-driven ngModel in favor of a single signal<TickerRange>.
Consumer is now standalone OnPush, like every other modern component
in the app."
```

---

## Task 9: Remove `[GraphQLName]` schema aliases pinned in PR (ii)

**Files:**
- Modify: `Backend/GraphQL/*.cs` (resolvers that had `[GraphQLName("ticker")]` / `[GraphQLName("startDate")]` / `[GraphQLName("endDate")]` on arguments)

PR (ii) Task 12 step 5 pinned legacy GraphQL schema field names so in-flight frontend GraphQL queries didn't break. With every consumer now migrated (Tasks 1-6), no query is sending the legacy names anymore — remove the aliases.

**Note:** there are NO transitional `[JsonPropertyName]` setters to remove because PR (ii) used canonical-only DTO renames (no .NET-side compatibility layer). The .NET part of this cleanup is GraphQL-only.

- [ ] **Step 1: Find every pinned schema alias**

```bash
grep -rnE 'GraphQLName\("(ticker|startDate|endDate)"\)' Backend/GraphQL/ --include="*.cs"
```

- [ ] **Step 2: Update one resolver test to confirm legacy GraphQL names now error**

`Backend.Tests/GraphQL/SchemaAliasRemovalTests.cs` (or analogous file):

```csharp
[Fact]
public async Task LegacyTickerArgumentNoLongerAcceptedAtSchema()
{
    var query = "query { runResearch(ticker: \"SPY\", startDate: \"2025-01-01\", endDate: \"2025-01-31\") { ... } }";
    var result = await ExecuteAsync(query);
    Assert.NotNull(result.Errors);
    Assert.Contains(result.Errors!, e => e.Message.Contains("ticker"));
}
```

- [ ] **Step 3: Verify failure.**

```bash
cd Backend.Tests && dotnet test --filter "SchemaAliasRemovalTests"
```
Expected: FAIL — the schema still exposes `ticker` / `startDate` / `endDate` argument names.

- [ ] **Step 4: Remove the `[GraphQLName]` overrides**

For each resolver argument the grep found, remove the `[GraphQLName("...")]` attribute, leaving the canonical argument name as the schema field name:

```csharp
// Before
public Task<ResearchResult> RunResearch(
    [GraphQLName("ticker")] string symbol,
    [GraphQLName("startDate")] string fromDate,
    [GraphQLName("endDate")] string toDate, ...);

// After
public Task<ResearchResult> RunResearch(string symbol, string fromDate, string toDate, ...);
```

- [ ] **Step 5: Run all .NET tests + format check.**

```bash
cd Backend.Tests && dotnet test
dotnet format podman.sln --verify-no-changes
```
Expected: ALL PASS, format clean.

- [ ] **Step 6: Commit**

```bash
git add Backend/ Backend.Tests/
git commit -m "refactor(backend): remove transitional [GraphQLName] resolver aliases

PR (iii)'s consumer migrations are complete — every Frontend GraphQL
query sends the canonical argument names (symbol, fromDate, toDate).
Removing the [GraphQLName] overrides pinned in PR (ii) Task 12 step 5;
the canonical argument names are now authoritative on the schema.

NOTE: No DTO setter cleanup needed — PR (ii) used canonical-only DTO
renames (no transitional [JsonPropertyName] aliases to remove)."
```

---

## Task 10: Remove transitional Pydantic aliases

**Files:**
- Modify: `PythonDataService/app/schemas/ticker_request.py` (drop `AliasChoices` for legacy names)

- [ ] **Step 1: Update the base test**

In `PythonDataService/tests/schemas/test_ticker_request.py`, flip the legacy-alias tests:

```python
def test_legacy_ticker_field_no_longer_accepted(self) -> None:
    with pytest.raises(ValidationError):
        TickerRequest.model_validate({
            "ticker": "SPY", "from_date": "2025-01-01", "to_date": "2025-01-31",
        })

def test_legacy_start_end_dates_no_longer_accepted(self) -> None:
    with pytest.raises(ValidationError):
        TickerRequest.model_validate({
            "symbol": "SPY", "start_date": "2025-01-01", "end_date": "2025-01-31",
        })
```

Same flip for `MultiTickerRequest` (`tickers` no longer accepted).

- [ ] **Step 2: Verify the new tests fail.**

```bash
podman exec polygon-data-service python -m pytest tests/schemas/test_ticker_request.py -v
```
Expected: FAIL on the new "no longer accepted" tests.

- [ ] **Step 3: Update the schema base**

In `PythonDataService/app/schemas/ticker_request.py`:

```python
# Before
from_date: str = Field(..., pattern=DATE_PATTERN, validation_alias=AliasChoices("from_date", "start_date"))
to_date:   str = Field(..., pattern=DATE_PATTERN, validation_alias=AliasChoices("to_date", "end_date"))
symbol:    str = Field(..., min_length=1, max_length=20, validation_alias=AliasChoices("symbol", "ticker"))
symbols:   list[str] = Field(..., min_length=1, validation_alias=AliasChoices("symbols", "tickers"))

# After
from_date: str = Field(..., pattern=DATE_PATTERN)
to_date:   str = Field(..., pattern=DATE_PATTERN)
symbol:    str = Field(..., min_length=1, max_length=20)
symbols:   list[str] = Field(..., min_length=1)
```

Remove the `AliasChoices` import if no longer used.

Update the module docstring to drop the "Transitional aliases" paragraph (now historical).

- [ ] **Step 4: Run all Python tests**

```bash
podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"
ruff check PythonDataService/app/ PythonDataService/tests/
```
Expected: ALL PASS, ruff clean.

If any router-specific test still references legacy field names (the ones added in PR ii's "legacy-alias" tests), update those tests to assert the `ValidationError` shape — same flip as Step 1.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/schemas/ticker_request.py \
        PythonDataService/tests/
git commit -m "refactor(schema): remove transitional Pydantic aliases

Frontend + .NET have all migrated to the canonical (symbol, from_date,
to_date, symbols) shape. AliasChoices for legacy names is removed;
any client still sending 'ticker', 'tickers', 'start_date', or
'end_date' will now fail Pydantic validation with a clear error."
```

---

## Task 10b: Remove deprecated `hideResolution` input from canonical picker

PR (i) kept `hideResolution` as a deprecated alias for one PR cycle. With every consumer migrated and none passing `hideResolution`, the alias comes off.

**Files:**
- Modify: `Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.{ts,spec.ts}`

- [ ] **Step 1: Confirm no consumer passes `hideResolution`.**

```bash
grep -rn "hideResolution" Frontend/src/ --include="*.ts" --include="*.html"
```
Expected: only the `ticker-range-picker.component.ts` declaration itself + its spec. If any consumer match: stop and migrate that consumer first.

- [ ] **Step 2: Update spec to remove the deprecated-alias test**, keeping the canonical `hideSampling` test.

- [ ] **Step 3: Remove the `hideResolution` input + the `samplingHidden` computed (revert it to plain `hideSampling()` reference).**

```ts
// Before (from PR i)
readonly hideResolution = input(false);
readonly hideSampling = input(false);
protected readonly samplingHidden = computed(() => this.hideResolution() || this.hideSampling());

// After
readonly hideSampling = input(false);
// (no computed; HTML uses hideSampling() directly)
```

Update the HTML to use `hideSampling()` directly instead of `samplingHidden()`.

- [ ] **Step 4: Run + commit**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/ticker-range-picker/**'
git add Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.{ts,html,spec.ts}
git commit -m "refactor(picker): remove deprecated hideResolution input

One-PR-cycle deprecation expires here. hideSampling is the canonical
name; no in-tree consumer passes hideResolution=true."
```

---

## Task 11: Delete `polygon-date-range`

**Files:**
- Delete: `Frontend/src/app/shared/polygon-date-range/` (entire directory)

- [ ] **Step 1: Confirm no remaining imports**

```bash
grep -rln "polygon-date-range\|PolygonDateRangeComponent" Frontend/src/ --include="*.ts" --include="*.html"
```
Expected: zero matches. If any match, return to whichever Task 1–8 missed the migration and finish it before deleting.

- [ ] **Step 2: Delete the directory**

```bash
rm -rf Frontend/src/app/shared/polygon-date-range
```

- [ ] **Step 3: Run full Vitest**

```bash
podman exec my-frontend npx ng test --watch=false
```
Expected: ALL PASS — no consumer should fail because every consumer was migrated.

- [ ] **Step 4: Commit**

```bash
git add -A Frontend/src/app/shared/polygon-date-range
git commit -m "chore(picker): delete polygon-date-range — superseded by ticker-range-picker family

All eight consumers have migrated to the picker family
(<app-ticker-range-picker>, <app-multi-ticker-range-picker>, or
<app-ticker-date-picker>). The narrow polygon-date-range component
shipped two days ago in PR #198 is no longer used; deleting it
completes the consolidation."
```

---

## Task 12: Project-scope checks + push

- [ ] **Step 1: Frontend project-scope tests + lint**

```bash
podman exec my-frontend npx ng test --watch=false
npx eslint Frontend/src/ --max-warnings 0
podman exec my-frontend npx tsc --noEmit
```
Expected: ALL PASS, ESLint clean, TypeScript clean.

- [ ] **Step 2: Python project-scope**

```bash
podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"
ruff check PythonDataService/app/ PythonDataService/tests/
```
Expected: ALL PASS, ruff clean.

- [ ] **Step 3: .NET project-scope**

```bash
cd Backend.Tests && dotnet test
dotnet format podman.sln --verify-no-changes
```
Expected: ALL PASS, format clean.

- [ ] **Step 4: Manual end-to-end smoke**

Visit each migrated consumer in the browser and run one full request through the UI:
- `/research-lab/indicator-reliability`
- `/research-lab/strategy-preflight`
- `/research-lab/feature-runner`
- `/research-lab/signal-runner`
- `/research-lab/batch-runner`
- `/ticker-explorer`
- `/spec-strategy-runner`
- `/indicator-report` (if not split)

For each, watch `podman logs -f polygon-data-service` to confirm the request lands with the canonical field names — no `ticker` / `start_date` / `end_date` should appear.

- [ ] **Step 5: Push and open PR**

```bash
git push -u origin feat/ticker-range-picker-everywhere-consumers
gh pr create --title "refactor(consumers): migrate eight forms to picker family + delete polygon-date-range (PR iii of iii)" --body "$(cat <<'EOF'
## Summary

Final PR of the three-PR initiative. Migrates every remaining consumer of `<app-polygon-date-range>` (and the two consumers that never adopted it) onto the picker family from PR (i):

- `indicator-reliability` → `<app-ticker-range-picker hideSampling>`
- `strategy-preflight` → `<app-ticker-range-picker availableMultipliers>`
- `feature-runner` → `<app-ticker-range-picker availableMultipliers>`
- `signal-runner` → `<app-ticker-range-picker availableMultipliers>`
- `batch-runner` → `<app-multi-ticker-range-picker>`
- `ticker-explorer` → `<app-ticker-date-picker>`
- `spec-strategy-runner` → `<app-ticker-range-picker>` + **lifts `symbol` out of strategy spec** (Frontend + Python + .NET coordinated commit)
- `indicator-report` → `<app-ticker-range-picker hideSampling>` + signal refactor [SPLIT TO PR (iv) IF MARKED]

Then:
- Removes the transitional `[JsonPropertyName]` aliases from .NET DTOs
- Removes the transitional `AliasChoices` from Python `TickerRequest` schema
- **Deletes** `Frontend/src/app/shared/polygon-date-range/` (PR #198's component, superseded)

After this PR merges, every ticker-bar request in the codebase has exactly one path: picker → `tickerRangeToWire` adapter → `TickerRequest` (or `MultiTickerRequest`) Pydantic-validated body.

## Spec
- Design: `docs/superpowers/specs/2026-05-09-ticker-range-picker-everywhere-design.md`
- Plan: `docs/superpowers/plans/2026-05-09-ticker-range-picker-everywhere-pr3-consumer-migrations.md`
- Predecessors: PR (i) (picker enhancements + sibling components), PR (ii) (schema base + .NET DTO renames)

## Test plan
- [x] Each migrated consumer's existing spec re-runs against new HTML
- [x] `ticker-range-picker` / `multi-ticker-range-picker` / `ticker-date-picker` specs unchanged from PR (i)
- [x] `spec_strategy` Python + .NET tests for the symbol-lift
- [x] .NET serialization test flipped to assert legacy names rejected
- [x] Python schema test flipped to assert legacy names rejected
- [x] Project-scope: Vitest + ESLint + tsc — clean
- [x] Project-scope: pytest + ruff — clean
- [x] Project-scope: dotnet test + dotnet format — clean
- [x] Manual end-to-end smoke on each migrated consumer route

## Risks
- Legacy field names (`ticker`, `start_date`, `end_date`, `tickers`) are no longer accepted after this PR. Any out-of-tree client (e.g. a Postman collection still using legacy names) will start receiving 422s. Acceptable — internal-only.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

After PR open, **stop**.

---

## Self-review

Spec coverage:
- ✅ Eight consumer migrations in the spec's order (`indicator-reliability` → `strategy-preflight` → `feature-runner` → `signal-runner` → `batch-runner` → `ticker-explorer` → `spec-strategy-runner` → `indicator-report`) — Tasks 1–8
- ✅ `spec-strategy-runner` symbol-lift coordinated atomically across three stacks — Task 7
- ✅ `indicator-report` template-driven → signal refactor + split rule — Task 8
- ✅ .NET transitional alias removal — Task 9
- ✅ Pydantic transitional alias removal — Task 10
- ✅ `polygon-date-range` deletion — Task 11
- ✅ Project-scope checks + manual smoke — Task 12

Type consistency:
- `TickerRange` (from Task 1, PR (i)) consumed by Tasks 1–4, 7, 8 — same type, same import path
- `MultiTickerRange` (from PR (i)) consumed by Task 5 — consistent
- `TickerSnapshot` (from PR (i)) consumed by Task 6 — consistent
- `tickerRangeToWire` / `multiTickerRangeToWire` (from PR (i)) consumed by every consumer migration — consistent
- Backend `Symbol` / `FromDate` / `ToDate` properties (from PR (ii)) used in Task 7's spec-strategy DTO and removed alias setters in Task 9 — consistent

No placeholders. No "TBD" / "implement later".

The split rule for Task 8 is concrete (>25 files OR cross-component changes) — engineer can audit and decide deterministically.

Plan complete.
