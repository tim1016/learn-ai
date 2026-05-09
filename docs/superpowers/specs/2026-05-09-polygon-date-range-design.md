# Polygon-aware shared date-range input — design

**Status:** approved (brainstorm), pending implementation plan
**Date:** 2026-05-09
**Author:** Claude (with Tim)
**Triggering bug:** PythonDataService rejected `to_date: "2025-5-31"` from a feature-research request with `string_pattern_mismatch` against `^\d{4}-\d{2}-\d{2}$`. Trace landed at `feature-runner.component.html:34,38` and `signal-runner.component.html:35,39`, both of which use unvalidated `<input pInputText>` text fields whose contents flow straight into the API payload.

## Goal

Replace ad-hoc per-page date inputs across research-lab forms with a single shared component that:

1. Is impossible to mis-format (no `2025-5-31` paths into the API).
2. Knows the Polygon Starter plan's data window (2-year history, T-1 max).
3. Disables weekends and market holidays at the calendar level.
4. Reuses existing helpers in `Frontend/src/app/utils/date-validation.ts` rather than duplicating them.
5. Drops in to existing parents with a one-line HTML swap, no parent state-shape refactor.

Non-goals are documented in §10.

## Decisions

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| Q1 | Scope | Polygon-aware date range with PrimeNG `p-datepicker`, `minDate`/`maxDate`/weekend+holiday disable, optional inline warning | Matches data-lab's altitude without bringing in the cache-availability strip that's specific to that screen |
| Q2 | API surface | Two `model.required<string>()` signals, `[(fromDate)]` / `[(toDate)]` | Drop-in for every existing caller; `model()` is the Angular-21-native shape for paired controls |
| Q3 | Holiday source | Component self-fetches via injected `MarketMonitorService.getHolidays(20)`, swallows failures | Mirrors data-lab; consumers don't have to re-implement the fetch |
| Q4 | Migration scope | Six trivial migrations (the four research-lab runners plus `spec-strategy-runner` and `strategy-preflight`); skip `indicator-report` (template-driven) and richer pickers (`data-lab`, `ticker-range-picker`, `market-calendar`) | All six share the same pair-of-string-signals shape today; one consistent PR, all swaps are HTML-only |
| Q5 | Typing behaviour | Editable input + popup + inline `validateDateRange` advisory | `p-datepicker` already validates on blur and won't write a malformed string into the model, so the original bug can't recur. Advisory acts as belt-and-suspenders for paste / programmatic-set cases |

## Component

### Location & files

```
Frontend/src/app/shared/polygon-date-range/
  polygon-date-range.component.ts
  polygon-date-range.component.html
  polygon-date-range.component.scss
  polygon-date-range.component.spec.ts
  index.ts
```

Sibling to `Frontend/src/app/shared/ticker-range-picker/`, which is the precedent for "shared, opinionated for this app's data backend" naming.

### Public API

```ts
@Component({
  selector: 'app-polygon-date-range',
  imports: [DatePickerModule, FormsModule, MessageModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './polygon-date-range.component.html',
  styleUrls: ['./polygon-date-range.component.scss'],
})
export class PolygonDateRangeComponent {
  // Required two-way bindings — the only mandatory surface
  fromDate = model.required<string>();   // 'YYYY-MM-DD'
  toDate   = model.required<string>();   // 'YYYY-MM-DD'

  // Optional knobs
  fromLabel = input<string>('From');
  toLabel   = input<string>('To');
  idPrefix  = input<string>('pdr');      // for stable ARIA id wiring

  // Read-only outputs
  readonly warning = computed<string | null>(
    () => validateDateRange(this.fromDate(), this.toDate()),
  );
  readonly valid = computed<boolean>(() => this.warning() === null);
}
```

### Internals

```ts
private readonly marketMonitor = inject(MarketMonitorService);
private readonly holidays = signal<MarketHolidayEvent[]>([]);

protected readonly minDate = new Date(getMinAllowedDate() + 'T00:00:00');
protected readonly maxDate = (() => {
  const d = new Date(); d.setDate(d.getDate() - 1); return d;
})();
protected readonly disabledDays = [0, 6];
protected readonly disabledDates = computed(
  () => getDisabledHolidayDates(this.holidays()),
);

protected readonly fromDateValue = computed(() => parseYmd(this.fromDate()));
protected readonly toDateValue   = computed(() => parseYmd(this.toDate()));

protected onFromChange(d: Date | null): void { this.fromDate.set(formatYmd(d)); }
protected onToChange(d: Date | null): void   { this.toDate.set(formatYmd(d));   }

constructor() {
  firstValueFrom(this.marketMonitor.getHolidays(20))
    .then(events => this.holidays.set(events))
    .catch(() => { /* non-critical, matches data-lab */ });
}
```

`parseYmd` and `formatYmd` are small string↔`Date` helpers currently inlined as private statics in `data-lab.component.ts`. The work moves them into `Frontend/src/app/utils/date-validation.ts` so the canonical YYYY-MM-DD↔`Date` conversion has one home, and updates `data-lab.component.ts` to import them. This is a deliberate, in-scope cleanup — not unrelated refactoring — because the new component would otherwise duplicate the logic.

### Template

```html
<div class="pdr">
  <div class="pdr__row">
    <div class="pdr__field">
      <label [for]="idPrefix() + '-from'" class="pdr__label">{{ fromLabel() }}</label>
      <p-datepicker
        [inputId]="idPrefix() + '-from'"
        [ngModel]="fromDateValue()"
        (ngModelChange)="onFromChange($event)"
        dateFormat="yy-mm-dd"
        [minDate]="minDate"
        [maxDate]="maxDate"
        [disabledDays]="disabledDays"
        [disabledDates]="disabledDates()"
        [showIcon]="true"
        appendTo="body"
      />
    </div>
    <div class="pdr__field">
      <label [for]="idPrefix() + '-to'" class="pdr__label">{{ toLabel() }}</label>
      <p-datepicker
        [inputId]="idPrefix() + '-to'"
        [ngModel]="toDateValue()"
        (ngModelChange)="onToChange($event)"
        dateFormat="yy-mm-dd"
        [minDate]="minDate"
        [maxDate]="maxDate"
        [disabledDays]="disabledDays"
        [disabledDates]="disabledDates()"
        [showIcon]="true"
        appendTo="body"
      />
    </div>
  </div>
  @if (warning(); as msg) {
    <p-message severity="warn" [text]="msg" class="pdr__warning" />
  }
</div>
```

`appendTo="body"` prevents the calendar from being clipped by parent `overflow:hidden` containers — every consumer sits inside a card.

### Styling

SCSS sets only `display:flex` + gap on `.pdr__row` and stacks the warning. No PrimeNG `::ng-deep` overrides — inherit ambient theme. Total SCSS expected ≤ 30 lines.

## Helpers added to `utils/date-validation.ts`

```ts
/** Parse 'YYYY-MM-DD' to a local Date at 00:00. Returns null for empty/invalid. */
export function parseYmd(s: string): Date | null;

/** Format a Date as 'YYYY-MM-DD' in local time, zero-padded. Returns '' for null. */
export function formatYmd(d: Date | null): string;
```

Both are pure, deterministic, and trivially unit-testable.

## Migration

All six callers receive a literal HTML swap.

| File | Lines today | After |
|---|---|---|
| `feature-runner/feature-runner.component.html` | 34, 38 | one `<app-polygon-date-range>` |
| `signal-runner/signal-runner.component.html` | 35, 39 | one `<app-polygon-date-range>` |
| `batch-runner/batch-runner.component.html` | 26, 30 | one `<app-polygon-date-range>` |
| `indicator-reliability/indicator-reliability.component.html` | 75–89 | one `<app-polygon-date-range>` |
| `spec-strategy-runner/spec-strategy-runner.component.html` | 365, 369 | one `<app-polygon-date-range>` (after `startDate`/`endDate` → `fromDate`/`toDate` rename) |
| `strategy-preflight/strategy-preflight.component.html` | 33, 38 | one `<app-polygon-date-range>` (after the same rename) |

The two callers using `startDate`/`endDate` get a TS-side rename to `fromDate`/`toDate` for consistency. No behavior change; surfaced explicitly in the PR description.

Each caller imports `PolygonDateRangeComponent` and adds it to its `imports` array. No additional service wiring (the component injects `MarketMonitorService` itself).

`canRun` computeds in each parent are **not** re-routed through the child component's `valid()` signal — parents continue to call `validateDateRange(fromDate(), toDate())` themselves where they already do (only data-lab does today; for the others, the calendar's `minDate`/`maxDate`/`disabledDays`/`disabledDates` already prevents picking invalid dates, and the inline warning catches the rare paste/programmatic-set case). Avoids `viewChild` lifecycle wiring at every callsite.

## Tests

`polygon-date-range.component.spec.ts` (Vitest + Angular Testing Library), function-scoped fixture:

1. Renders both `p-datepicker`s with stable label-for / `inputId` wiring.
2. `model()` round-trip: parent sets `'2025-01-01'` → calendar shows that date → user picks May 15, 2025 → parent signal updates to `'2025-05-15'`.
3. Self-fetch on construct: `MarketMonitorService.getHolidays` called once with `20`. (Mock at the DI level via `providers`.)
4. Holiday disable: stub `getHolidays` to return `[Christmas 2025 (Closed)]` → `disabledDates()` includes that `Date`.
5. Holiday fetch failure: rejected promise → component renders without throwing, `disabledDates()` is `[]`.
6. Warning surfaces: setting `fromDate = '2010-01-01'` (older than the 2-year window) → inline `<p-message severity="warn">` appears with the `validateDateRange` text.
7. `valid` computed: `true` when `warning() === null`, `false` otherwise — covered through 6.

Helper tests in `date-validation.spec.ts`:
- `parseYmd('2025-05-31')` → `Date` at local midnight on May 31.
- `parseYmd('')`, `parseYmd('garbage')` → `null`.
- `formatYmd(new Date(2025, 4, 31))` → `'2025-05-31'` (zero-padded).
- `formatYmd(null)` → `''`.

Migration smoke tests: each consumer's existing spec re-runs against the new HTML; no new specs added per consumer.

## Accessibility

- Each `p-datepicker` has a `<label for="…">` association via `idPrefix`.
- Calendar popup is keyboard-navigable (PrimeNG default); `appendTo="body"` keeps focus management correct.
- Warning advisory uses `<p-message>` with `severity="warn"` (PrimeNG renders ARIA `role="alert"` semantics).
- AXE check expected to pass — no custom interactive controls introduced.

## Authority hierarchy notes

This work touches the Frontend stack only; numerical-rigor rules don't apply (no math is being ported). Per `.claude/rules/angular.md`:

- Standalone component (default; not setting `standalone: true`).
- `OnPush` change detection.
- Signals + `model()` + `input()` for state, no decorators.
- Modern control flow (`@if`).
- No `ngClass`/`ngStyle` (we use `[class]` bindings if needed in SCSS).

## Out of scope

Explicitly *not* in this PR:

- Quick-range presets ("Last 30d", "YTD"). Opt-in via an `@Input` later if a consumer asks.
- Timespan-aware range cap (feature-runner's `MAX_DAYS_BY_TIMESPAN`). That logic stays in feature-runner — it's a per-screen concern, not Polygon-plan-level.
- `indicator-report.component.html` migration. It uses template-driven `[(ngModel)]="fromDate"` against a non-signal field; converting it is its own PR.
- `data-lab` migration. It already has a richer picker with cache-availability and presets; downgrading to this component would lose features.
- Cache-availability strip / advisories beyond the date-range warning.
- Cross-engine reconciliation, IBKR plumbing, anything unrelated.

## Risks & open considerations

- **PrimeNG `dateFormat="yy-mm-dd"` quirk.** PrimeNG's `yy` is the four-digit year (jQuery-UI heritage). Verify in dev that the displayed format matches user expectation — both data-lab and `ticker-range-picker` rely on this without issue, so it's known-good in this repo.
- **Time zone.** `parseYmd` produces a *local-midnight* `Date`; `formatYmd` reads back local. This matches data-lab's existing helpers and avoids the `toISOString().split('T')[0]` gotcha that drops a day in negative UTC offsets. Documented in the helper docstrings.
- **Multiple instances on one page.** None of the migration targets host more than one date-range picker, so the per-instance holidays fetch is wasteful only if a future consumer mounts two. If/when that happens, lift the fetch into `MarketMonitorService` with `shareReplay(1)`. Out of scope here.

## Build sequence (for the implementation plan)

1. Add `parseYmd`/`formatYmd` to `utils/date-validation.ts` + tests.
2. Update `data-lab.component.ts` to import them (delete the private statics).
3. Create `PolygonDateRangeComponent` + spec.
4. Migrate consumers in this order: `feature-runner`, `signal-runner` (closes the bug), then `batch-runner`, `indicator-reliability`, `spec-strategy-runner`, `strategy-preflight`.
5. Project-scope lint + Vitest pass.
6. PR description includes: bug, Polygon-aware constraints adopted, six screens unified, what's deliberately deferred.
