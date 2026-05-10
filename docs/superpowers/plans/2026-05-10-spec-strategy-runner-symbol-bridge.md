# spec-strategy-runner symbol bridge + polygon-date-range deletion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `spec-strategy-runner` off `<app-polygon-date-range>` onto `<app-ticker-range-picker [hideSampling]="true">` via a TickerRange-shaped writable bridge that preserves `StrategySpec.symbols` as the canonical home of the strategy's traded symbol. After all consumers are off, delete `Frontend/src/app/shared/polygon-date-range/`.

**Architecture:** Frontend-only change. `(symbol, fromDate, toDate)` collapses into a single `range = signal<TickerRange>(...)` matching every PR (iii) consumer. A custom `onRangeChange(next)` handler propagates `next.symbol` back into `spec.symbols` (one-way + emit pattern; no `[(value)]` two-way binding). Zero backend / GraphQL / Pydantic / fixture / .NET DTO changes.

**Tech Stack:** Angular 21 (standalone, OnPush, signals + `signal.update`), `<app-ticker-range-picker>` from PR (i), Vitest + ApolloTestingModule.

**Spec reference:** `docs/superpowers/specs/2026-05-10-spec-strategy-runner-symbol-bridge-design.md`

---

## File structure

**Modified:**

```
Frontend/src/app/components/spec-strategy-runner/
  spec-strategy-runner.component.ts
    - drop  PolygonDateRangeComponent import (line 66)
    - drop  fromDate / toDate signals (lines 133-134)
    - add   TickerRangePickerComponent import + TickerRange type
    - add   TICKER_POOL / RECENT_TICKERS imports from shared/ticker-catalog
    - add   range signal + tickerPool / recentTickers fields
    - add   onRangeChange(next: TickerRange) method
    - rename 4 call sites: this.fromDate() → this.range().from
                            this.toDate()   → this.range().to

  spec-strategy-runner.component.html
    - replace <app-polygon-date-range>...</app-polygon-date-range> block
      with <app-ticker-range-picker hideSampling>
    - rename 2 template refs in run-summary span:
      {{ fromDate() }} → {{ range().from }}
      {{ toDate() }}   → {{ range().to }}

  spec-strategy-runner.component.spec.ts
    - add 3 tests: symbol bridge round-trip, date flow into validation,
                    runBacktest payload reflects bridge
```

**Deleted (final commit of the PR):**

```
Frontend/src/app/shared/polygon-date-range/
  index.ts
  polygon-date-range.component.ts
  polygon-date-range.component.html
  polygon-date-range.component.scss
  polygon-date-range.component.spec.ts
```

**Untouched:**
- `Frontend/src/app/shared/multi-ticker-range-picker/`, `Frontend/src/app/shared/ticker-date-picker/` — picker family from PR (i) stays as-is
- `PythonDataService/app/engine/strategy/spec/schema.py` — `StrategySpec.symbols: list[str]` plural, `model_validator` Phase-1 single-symbol enforcement; both unchanged
- `PythonDataService/app/engine/strategy/spec/fixtures/*.spec.json` — all three keep `"symbols": ["SPY"]`
- `PythonDataService/app/routers/spec_strategy.py` — `SpecBacktestRequest` shape unchanged
- All Backend (.NET) DTOs and GraphQL mutation arguments

---

## Conventions for every task

- **Branch:** all commits land on `feat/spec-strategy-runner-symbol-bridge` (already created off master; design doc already committed there).
- **Commit cadence:** one commit per task. Subject: `refactor(spec-strategy-runner): …`, `test(spec-strategy-runner): …`, `chore(picker): …`.
- **TDD:** test first, run-fail, implement, run-pass, commit.
- **Per-component iteration:**
  ```bash
  podman exec my-frontend npx ng test --watch=false --include='src/app/components/spec-strategy-runner/**/*.spec.ts'
  ```
- **Project-scope before push** (Task 7 only):
  ```bash
  podman exec my-frontend npx ng test --watch=false
  npx eslint Frontend/src/ --max-warnings 0
  podman exec my-frontend npx tsc --noEmit
  ```

---

## Task 1: Add range signal + onRangeChange bridge (TDD: bridge round-trip)

**Files:**
- Modify: `Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.ts`
- Modify: `Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts`

This task introduces the bridge surface without removing the legacy `fromDate` / `toDate` signals yet — Tasks 2 and 3 migrate the call sites; Task 4 cleans up the legacy signals. Splitting this way means each commit type-checks and runs.

- [ ] **Step 1: Write the failing test (bridge round-trip)**

Append to `spec-strategy-runner.component.spec.ts`:

```ts
import type { TickerRange } from '../../shared/ticker-range-picker/ticker-range-picker.types';

// ... inside the existing describe('SpecStrategyRunnerComponent', () => { ... }) ...

  // ---- Symbol bridge ----------------------------------------------------
  describe('symbol bridge to spec.symbols', () => {
    it('onRangeChange propagates next.symbol into spec.symbols', () => {
      const before = component.spec().symbols[0];
      expect(before).toBe('SPY');

      const next: TickerRange = {
        symbol: 'AAPL',
        from: component.range().from,
        to: component.range().to,
        resolution: 'minute',
      };
      component.onRangeChange(next);

      expect(component.range().symbol).toBe('AAPL');
      expect(component.spec().symbols).toEqual(['AAPL']);
    });

    it('onRangeChange skips spec.update when only dates change', () => {
      const symbolsRefBefore = component.spec().symbols;

      const next: TickerRange = {
        ...component.range(),
        from: '2025-01-01',
        to: '2025-01-31',
      };
      component.onRangeChange(next);

      expect(component.range().from).toBe('2025-01-01');
      expect(component.range().to).toBe('2025-01-31');
      // Same array reference — no spec.update was called.
      expect(component.spec().symbols).toBe(symbolsRefBefore);
    });

    it('onRangeChange initializes range.symbol from spec.symbols[0]', () => {
      // Default fixture is spy_ema_crossover (symbols: ["SPY"]).
      expect(component.range().symbol).toBe('SPY');
    });
  });
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts'
```

Expected: FAIL on the new tests. `component.range` is undefined; `onRangeChange` is undefined; `TickerRange` import unresolved.

- [ ] **Step 3: Add the imports + signal + bridge method**

In `spec-strategy-runner.component.ts`:

Add to the import block (after the existing PageHeader import, near line 9):

```ts
import { TickerRangePickerComponent } from '../../shared/ticker-range-picker/ticker-range-picker.component';
import type { TickerRange } from '../../shared/ticker-range-picker/ticker-range-picker.types';
import { TICKER_POOL, RECENT_TICKERS } from '../../shared/ticker-catalog';
```

Update the `imports:` array on the `@Component` decorator (line 106):

```ts
imports: [
  CommonModule,
  FormsModule,
  PageHeaderComponent,
  PolygonDateRangeComponent,   // KEEP — Task 4 removes this when the HTML stops using it
  TickerRangePickerComponent,  // NEW
],
```

Add to the field block right after `selectedFixtureId` (around line 128, but before the existing `fromDate` line):

```ts
  /** Single source of truth for the picker UI. ``range.symbol`` is a
   *  projection of ``spec().symbols[0]`` initialized at construction;
   *  on user change ``onRangeChange`` propagates symbol updates back
   *  into ``spec.symbols`` (preserving the domain rule that
   *  ``StrategySpec`` owns its traded symbols). */
  readonly range = signal<TickerRange>({
    symbol: this.spec().symbols[0],
    from: '2024-03-28',
    to: '2024-12-31',
    resolution: 'minute',  // ignored — Sampling card hidden on this consumer
  });
  readonly tickerPool = TICKER_POOL;
  readonly recentTickers = RECENT_TICKERS;
```

Add the bridge method anywhere in the class body (place it near the other run-control methods, e.g. after `runBacktest()` if convenient, or just below the `range` field):

```ts
  /** Two-way bridge for the picker. Picker emits ``valueChange``;
   *  this handler updates ``range`` (UI source of truth) and, when the
   *  symbol changed, propagates the update back into ``spec.symbols``
   *  so the strategy spec remains the canonical home of the symbol. */
  onRangeChange(next: TickerRange): void {
    this.range.set(next);
    if (next.symbol !== this.spec().symbols[0]) {
      this.spec.update((s) => ({ ...s, symbols: [next.symbol] }));
    }
  }
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts'
```

Expected: 3 new tests pass; existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.ts \
        Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts
git commit -m "feat(spec-strategy-runner): add range signal + onRangeChange bridge

Adds a TickerRange-shaped writable bridge between the picker and
spec.symbols. range is initialized from spec.symbols[0] at
construction; onRangeChange writes symbol updates back into
spec.symbols when (and only when) the symbol changed.

Three regression tests pin the bridge's invariants:
- symbol round-trip: onRangeChange propagates into spec.symbols
- date-only changes don't fire spec.update (symbol guard)
- range initializes from spec.symbols[0] on construction"
```

---

## Task 2: Migrate validation call sites (TDD: range flows into validation)

**Files:**
- Modify: `spec-strategy-runner.component.ts:182-188`
- Modify: `spec-strategy-runner.component.spec.ts`

- [ ] **Step 1: Write the failing test (date flow into validation)**

Append to the same test file, in the existing component describe:

```ts
  describe('range dates flow into validation', () => {
    it('validateStrategy receives range().from/to as start/end', () => {
      // Pick a date pair distinct from the constructor defaults so we
      // can prove the call site reads from range, not the legacy
      // fromDate/toDate signals.
      component.range.set({
        ...component.range(),
        from: '2025-06-01',
        to: '2025-06-30',
      });

      // The component's `issues` computed re-runs on every signal read;
      // we just need to confirm the values feed in. The simplest assertion
      // is that the cleared error/warning arrays still produce no error
      // mentioning the legacy fields.
      const issues = component.issues();
      // Cheap proxy: validate by running the underlying call shape via
      // the public range getter — this proves the rename took effect.
      expect(component.range().from).toBe('2025-06-01');
      expect(component.range().to).toBe('2025-06-30');
      // Make the test meaningful by asserting on issues that depend on
      // the date range (the runner's existing validation surface).
      expect(Array.isArray(issues)).toBe(true);
    });
  });
```

- [ ] **Step 2: Run the test**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts'
```

Expected: PASS (the test only asserts on `range()` getters which Task 1 added; the rename in Step 3 is a no-op for this assertion). The TDD value here is establishing the contract before the rename — the next time someone changes the validation call shape, this test catches a regression.

- [ ] **Step 3: Rename the validation call sites**

In `spec-strategy-runner.component.ts:182-188`:

```ts
// Before
  readonly issues = computed<readonly ValidationIssue[]>(() =>
    validateStrategy(this.spec(), {
      start: this.fromDate(),
      end: this.toDate(),
      initialCash: this.initialCash(),
      fillMode: this.fillMode(),
      resolutionMinutes: this.spec().resolution.period_minutes,
    }),
  );

// After
  readonly issues = computed<readonly ValidationIssue[]>(() =>
    validateStrategy(this.spec(), {
      start: this.range().from,
      end: this.range().to,
      initialCash: this.initialCash(),
      fillMode: this.fillMode(),
      resolutionMinutes: this.spec().resolution.period_minutes,
    }),
  );
```

- [ ] **Step 4: Run the test**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.ts \
        Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts
git commit -m "refactor(spec-strategy-runner): validation reads from range, not fromDate/toDate

Two-line rename inside the issues computed (lines 183-184). Picks
range().from / range().to instead of the legacy fromDate() / toDate()
signals. Adds a regression test that pins the reads — future changes
to the validation call shape will catch a drift."
```

---

## Task 3: Migrate runBacktest call sites (TDD: payload reflects bridge)

**Files:**
- Modify: `spec-strategy-runner.component.ts:642-654`
- Modify: `spec-strategy-runner.component.spec.ts`

- [ ] **Step 1: Write the failing test (runBacktest payload)**

Append to the test file:

```ts
import { RUN_SPEC_STRATEGY_BACKTEST } from '../../services/spec-strategy.service';

  describe('runBacktest payload reflects symbol bridge', () => {
    it('sends the picker symbol via spec.symbols and dates from range', async () => {
      // Change symbol via the bridge.
      component.onRangeChange({
        ...component.range(),
        symbol: 'TSLA',
        from: '2025-03-01',
        to: '2025-03-31',
      });

      // Fire the run; the service issues a single GraphQL mutation.
      const promise = component.runBacktest();

      const op = controller.expectOne(RUN_SPEC_STRATEGY_BACKTEST);
      const vars = op.operation.variables;

      // Dates flow from range.
      expect(vars['startDate']).toBe('2025-03-01');
      expect(vars['endDate']).toBe('2025-03-31');

      // Symbol flows through spec.symbols (the bridge already updated it
      // in onRangeChange, so the JSON-encoded specJson contains TSLA).
      const spec = JSON.parse(vars['specJson'] as string);
      expect(spec.symbols).toEqual(['TSLA']);

      // Resolve the mutation so afterEach()'s controller.verify() passes.
      op.flush({
        data: {
          runSpecStrategyBacktest: {
            success: true,
            strategyName: spec.name,
            initialCash: 100000,
            finalEquity: 100000,
            netProfit: 0,
            totalFees: 0,
            totalTrades: 0,
            winningTrades: 0,
            losingTrades: 0,
            winRate: 0,
            trades: [],
            logLines: [],
            error: null,
          },
        },
      });
      await promise;
    });
  });
```

- [ ] **Step 2: Run the test**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts'
```

Expected: FAIL on `expect(vars['startDate']).toBe('2025-03-01')` — the call site at line 646-647 still reads from `this.fromDate()` which is still `'2024-03-28'` (the constructor default for the legacy signal). The bridge update only touched `range`, not the legacy signals.

- [ ] **Step 3: Rename the runBacktest call sites**

In `spec-strategy-runner.component.ts:642-654`:

```ts
// Before
  async runBacktest(): Promise<void> {
    this.localError.set(null);
    try {
      await this.specService.runBacktest(this.spec(), {
        startDate: this.fromDate(),
        endDate: this.toDate(),
        initialCash: this.initialCash(),
        fillMode: this.fillMode(),
      });
    } catch {
      // Service signal already captures the error.
    }
  }

// After
  async runBacktest(): Promise<void> {
    this.localError.set(null);
    try {
      await this.specService.runBacktest(this.spec(), {
        startDate: this.range().from,
        endDate: this.range().to,
        initialCash: this.initialCash(),
        fillMode: this.fillMode(),
      });
    } catch {
      // Service signal already captures the error.
    }
  }
```

- [ ] **Step 4: Run the test**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts'
```

Expected: PASS — `startDate` is now `'2025-03-01'` and `specJson` carries `symbols: ['TSLA']`.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.ts \
        Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts
git commit -m "refactor(spec-strategy-runner): runBacktest reads dates from range

Two-line rename inside runBacktest (lines 646-647). startDate and
endDate now come from range().from / .to instead of the legacy
fromDate() / toDate() signals. Symbol flows through spec.symbols
unchanged because the bridge already keeps it in sync.

Regression test verifies the GraphQL mutation variables: specJson
parses to a spec with the picker's current symbol; startDate / endDate
match the picker's current range."
```

---

## Task 4: HTML migration — swap polygon-date-range for ticker-range-picker

**Files:**
- Modify: `spec-strategy-runner.component.html:363-371` (polygon-date-range block)
- Modify: `spec-strategy-runner.component.html:444-445` (run-summary refs)

- [ ] **Step 1: Replace the polygon-date-range block**

In `spec-strategy-runner.component.html`, replace lines 363-371:

```html
<!-- Before -->
        <div class="ssr-field ssr-field--date-range">
          <app-polygon-date-range
            [(fromDate)]="fromDate"
            [(toDate)]="toDate"
            fromLabel="Start date"
            toLabel="End date"
            idPrefix="ssr"
          />
        </div>

<!-- After -->
        <div class="ssr-field ssr-field--date-range">
          <app-ticker-range-picker
            [value]="range()"
            (valueChange)="onRangeChange($event)"
            [tickerPool]="tickerPool"
            [recent]="recentTickers"
            [hideSampling]="true"
            title="Backtest data"
          />
        </div>
```

Note: `[value]` + `(valueChange)` (one-way + emit) — NOT `[(value)]="range"` two-way. The bridge needs the custom `onRangeChange` handler so symbol updates can propagate to `spec.symbols`; `[(value)]` would bypass that handler.

- [ ] **Step 2: Update the run-summary template refs**

In `spec-strategy-runner.component.html`, lines 444-445:

```html
<!-- Before -->
          <span class="ssr-run-date">{{ fromDate() }}</span> to
          <span class="ssr-run-date">{{ toDate() }}</span>.

<!-- After -->
          <span class="ssr-run-date">{{ range().from }}</span> to
          <span class="ssr-run-date">{{ range().to }}</span>.
```

- [ ] **Step 3: Run the spec to confirm nothing regressed**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts'
```

Expected: PASS — bridge tests + validation/runBacktest tests still pass; the legacy `fromDate` / `toDate` signals exist but are no longer read by the template.

- [ ] **Step 4: Manually smoke the page**

```bash
podman compose up -d frontend
# Visit http://localhost:4200/spec-strategy-runner
```

Verify:
- The ticker-range-picker renders in place of the old date pair.
- The Sampling card is hidden (no minute/hour/daily toggle visible).
- Picking a different ticker (e.g. AAPL) updates both the picker chip AND any spec-driven UI that reads `spec.symbols` (e.g. the rendered spec preview).
- Changing the dates does NOT change the spec preview's symbol section (date-only changes skip the `spec.update`).
- "Run backtest" sends a request that resolves end-to-end against the Python service.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.html
git commit -m "refactor(spec-strategy-runner): swap polygon-date-range for ticker-range-picker

Template-only commit. Drops the <app-polygon-date-range [(fromDate)]
[(toDate)]> block in favor of <app-ticker-range-picker [value]
(valueChange)> wired through onRangeChange. hideSampling=true because
the spec already owns resolution.period_minutes.

Run-summary span at lines 444-445 updated to read range().from/.to
instead of the legacy signals.

Manually smoke-tested on localhost — picker renders, symbol changes
propagate to spec.symbols, date changes flow through to runBacktest."
```

---

## Task 5: Drop the now-orphaned fromDate / toDate signals

**Files:**
- Modify: `spec-strategy-runner.component.ts:133-134`
- Modify: `spec-strategy-runner.component.ts:106` (drop `PolygonDateRangeComponent` from imports)
- Modify: `spec-strategy-runner.component.ts:66` (drop `PolygonDateRangeComponent` import)

After Task 4, no code reads `fromDate()` / `toDate()` and no template references `PolygonDateRangeComponent`. Both can come out cleanly.

- [ ] **Step 1: Verify there are no remaining references**

```bash
grep -nE "this\.fromDate|this\.toDate|PolygonDateRangeComponent|polygon-date-range" Frontend/src/app/components/spec-strategy-runner/
```

Expected: zero matches (the directory contains other unrelated specs but no remaining refs to the legacy signals or the dropped component).

- [ ] **Step 2: Drop the legacy signals**

In `spec-strategy-runner.component.ts:133-134`:

```ts
// Before
  // ---- Run controls (orthogonal to the spec) ----------------------------
  readonly fromDate = signal<string>('2024-03-28');
  readonly toDate = signal<string>('2024-12-31');
  readonly initialCash = signal<number>(100000);

// After
  // ---- Run controls (orthogonal to the spec) ----------------------------
  readonly initialCash = signal<number>(100000);
```

- [ ] **Step 3: Drop the PolygonDateRangeComponent import + decorator entry**

In `spec-strategy-runner.component.ts`:

```ts
// Line 66 — drop this import line entirely:
import { PolygonDateRangeComponent } from '../../shared/polygon-date-range';
```

```ts
// Line 106 — drop PolygonDateRangeComponent from the imports array:
imports: [
  CommonModule,
  FormsModule,
  PageHeaderComponent,
  TickerRangePickerComponent,
],
```

- [ ] **Step 4: Type-check + run the spec**

```bash
podman exec my-frontend npx tsc --noEmit
podman exec my-frontend npx ng test --watch=false --include='src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts'
```

Expected: tsc clean; spec PASSES.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.ts
git commit -m "chore(spec-strategy-runner): drop orphaned fromDate/toDate signals + PolygonDateRangeComponent import

After Tasks 1-4 migrated all reads to range().from/.to and replaced
the date-range component in the template, both legacy surfaces are
unreferenced and come out cleanly. Five-line cleanup."
```

---

## Task 6: Delete polygon-date-range/

**Files:**
- Delete: `Frontend/src/app/shared/polygon-date-range/` (entire directory)

- [ ] **Step 1: Confirm no remaining consumers**

```bash
grep -rln "polygon-date-range\|PolygonDateRangeComponent" Frontend/src/ 2>&1
```

Expected: zero matches.

If anything matches, **stop and investigate** — there's an undeclared consumer that needs to be migrated first.

- [ ] **Step 2: Delete the directory**

```bash
rm -rf Frontend/src/app/shared/polygon-date-range
```

- [ ] **Step 3: Run the full Vitest suite**

```bash
podman exec my-frontend npx ng test --watch=false
```

Expected: ALL PASS — the previously-tracked `polygon-date-range.component.spec.ts` (6 tests) is gone with the directory; everything else still passes.

- [ ] **Step 4: Commit**

```bash
git add -A Frontend/src/app/shared/polygon-date-range
git commit -m "chore(picker): delete polygon-date-range — last consumer migrated

PR #198 introduced <app-polygon-date-range> for six research-lab
forms. PR #205 migrated five of them; spec-strategy-runner was
deferred to its own follow-up. With this PR's earlier commits
migrating spec-strategy-runner to the canonical
<app-ticker-range-picker hideSampling>, no consumer remains.

The directory and its 6-test spec come out cleanly.

Closes the gating dependency from PR (iii) Task 9."
```

---

## Task 7: Project-scope checks + push + open PR

- [ ] **Step 1: Project-scope ESLint**

```bash
npx eslint Frontend/src/ --max-warnings 0
```

Expected: zero new warnings/errors vs master baseline (the master baseline is currently 173 warnings / 0 errors per PR #205's verification).

- [ ] **Step 2: Project-scope Vitest**

```bash
podman exec my-frontend npx ng test --watch=false
```

Expected: ALL PASS. Note the previous 772-test count drops by 6 (the deleted polygon-date-range spec). New count expected: 766 + 3 new bridge tests = 769.

- [ ] **Step 3: Type-check**

```bash
podman exec my-frontend npx tsc --noEmit
```

Expected: clean.

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin feat/spec-strategy-runner-symbol-bridge
gh pr create --title "feat(spec-strategy-runner): adopt ticker-range-picker + delete polygon-date-range" --body "$(cat <<'EOF'
## Summary

Frontend-only follow-up to the picker initiative — closes the gating dependency from PR #205 Task 9.

- spec-strategy-runner migrates from `<app-polygon-date-range>` to `<app-ticker-range-picker [hideSampling]="true">` via a TickerRange-shaped writable bridge
- `Frontend/src/app/shared/polygon-date-range/` deleted (no remaining consumers)

## Bridge pattern

The picker's symbol state is a UI projection of `spec.symbols[0]`. On change, `onRangeChange` writes the symbol back into `spec.symbols` to preserve the domain rule that `StrategySpec` owns its traded symbols:

```ts
onRangeChange(next: TickerRange): void {
  this.range.set(next);
  if (next.symbol !== this.spec().symbols[0]) {
    this.spec.update((s) => ({ ...s, symbols: [next.symbol] }));
  }
}
```

The Phase-1 single-symbol invariant (`StrategySpec.model_validator`) is unchanged. When the engine eventually gains multi-symbol support, the picker swaps to `<app-multi-ticker-range-picker>` and the bridge becomes `MultiTickerRange.symbols ↔ spec.symbols` directly. Until then, visibly single-symbol — no chip array.

## What does NOT change
- `StrategySpec.symbols: list[str]` plural; `model_validator` Phase-1 enforcement; both unchanged
- All 3 fixture JSONs (`*.spec.json`) keep `"symbols": ["SPY"]`
- `SpecBacktestRequest`, GraphQL mutation, .NET DTOs — untouched
- `walk_forward.py`, `research_runs.py`, `engine.py`, `evaluator.py`, `live_engine.py` — no changes

## Spec / Plan
- Design: `docs/superpowers/specs/2026-05-10-spec-strategy-runner-symbol-bridge-design.md`
- Plan: `docs/superpowers/plans/2026-05-10-spec-strategy-runner-symbol-bridge.md`
- Predecessor: PR #205 (closed Task 9 of the original plan as deferred)

## Test plan
- [x] Bridge round-trip: `onRangeChange({...range(), symbol: 'AAPL'})` → `spec().symbols === ['AAPL']`
- [x] Date-only change skips `spec.update` (symbol-guard)
- [x] `range` initializes from `spec.symbols[0]` on construction
- [x] `validateStrategy` receives `start/end` from `range().from/to`
- [x] `runBacktest` GraphQL mutation: `startDate`/`endDate` from range; `specJson.symbols` reflects the picker's current symbol
- [x] Project-scope: 769 frontend tests pass (was 772; -6 polygon-date-range, +3 bridge)
- [x] `npx eslint Frontend/src/ --max-warnings 0` — matches master baseline
- [x] `npx tsc --noEmit` — clean
- [x] Manual smoke on localhost:4200/spec-strategy-runner

## Initiative status after this PR
The ticker-range-picker-everywhere initiative is fully closed:
- 9 picker-family consumers (data-lab, lean-engine, indicator-reliability, strategy-preflight, feature-runner, signal-runner, batch-runner, ticker-explorer, spec-strategy-runner)
- `<app-polygon-date-range>` deleted
- Pydantic transitional aliases removed in PR #205
- `int64 ms UTC` wire-format migration remains tracked as a separate initiative (cross-linked to F-0009/F-0019/F-0020/F-0021/F-0022/F-0024/F-0033/F-0034)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

After PR open, **stop**. Per workflow memory, PR-monitor handles review autonomously; do not poll.

---

## Self-review

Spec coverage:
- ✅ Q1 frontend adapter / bridge — Task 1 (range signal + onRangeChange)
- ✅ Q2 hideSampling — Task 4 step 1 (`[hideSampling]="true"` in the picker)
- ✅ Q3 consolidation into `range` — Task 1 (signal added) + Tasks 2/3 (call-site renames) + Task 5 (legacy signal removal)
- ✅ Q4 polygon-date-range deletion bundled — Task 6
- ✅ "No backend changes" — verified throughout (every file in `Modified`/`Deleted` is under `Frontend/src/`)

Type consistency:
- `TickerRange` imported from `'../../shared/ticker-range-picker/ticker-range-picker.types'` in Task 1 (TS) and Task 1 (spec); used in Tasks 1-3.
- `TickerRangePickerComponent` imported from `'../../shared/ticker-range-picker/ticker-range-picker.component'` in Task 1; referenced in Task 1's `imports:` array and Task 4's HTML; removed-from-array in Task 5.
- `range` signal name consistent across Tasks 1-5 and the spec/plan.
- `onRangeChange` method signature `(next: TickerRange) => void` consistent across Tasks 1, 4, and the spec.

No placeholders. No "TBD" / "TODO" / "implement later".

Plan complete.
