# Polygon Date-Range Shared Component Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ad-hoc date-range inputs in six research-lab forms with a single shared `PolygonDateRangeComponent` that prevents the `2025-5-31` mis-format bug and brings Polygon Starter plan constraints (2-year history, T-1 max, weekend + market-holiday disable) to every research form.

**Architecture:** New standalone Angular component at `Frontend/src/app/shared/polygon-date-range/`, two-way `model.required<string>()` bindings for `fromDate`/`toDate`, self-fetched holidays via injected `MarketMonitorService`, PrimeNG `p-datepicker` with `[minDate]`/`[maxDate]`/`[disabledDays]`/`[disabledDates]`, inline `<p-message>` advisory powered by the existing `validateDateRange` util. Two helpers (`parseYmd` / `formatYmd`) lifted out of `data-lab.component.ts` into `utils/date-validation.ts` so the canonical YYYY-MM-DD↔Date conversion has one home.

**Tech Stack:** Angular 21 (standalone, signals, `model()`, OnPush), PrimeNG `DatePickerModule` + `MessageModule`, Vitest + Angular Testing Library, existing `MarketMonitorService.getHolidays(years)` Observable.

**Spec:** [`docs/superpowers/specs/2026-05-09-polygon-date-range-design.md`](../specs/2026-05-09-polygon-date-range-design.md)

---

## File Map

**Created:**
- `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.ts`
- `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.html`
- `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.scss`
- `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.spec.ts`
- `Frontend/src/app/shared/polygon-date-range/index.ts`

**Modified — utils:**
- `Frontend/src/app/utils/date-validation.ts` (+`parseYmd`, +`formatYmd`)
- `Frontend/src/app/utils/date-validation.spec.ts` (+tests)

**Modified — data-lab (helper migration):**
- `Frontend/src/app/components/data-lab/data-lab.component.ts` (delete static `parseDate`/`formatDate`, import + use `parseYmd`/`formatYmd`)

**Modified — six consumers (HTML swap; `.ts` import-only unless rename noted):**
- `Frontend/src/app/components/research-lab/feature-runner/feature-runner.component.{ts,html}`
- `Frontend/src/app/components/research-lab/signal-runner/signal-runner.component.{ts,html}`
- `Frontend/src/app/components/research-lab/batch-runner/batch-runner.component.{ts,html}`
- `Frontend/src/app/components/research-lab/indicator-reliability/indicator-reliability.component.{ts,html}`
- `Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.{ts,html}` *(plus `startDate`/`endDate` → `fromDate`/`toDate` rename)*
- `Frontend/src/app/components/research-lab/strategy-preflight/strategy-preflight.component.{ts,html}` *(plus the same rename)*

---

## Task 1: Add `parseYmd` / `formatYmd` to `utils/date-validation.ts`

**Files:**
- Modify: `Frontend/src/app/utils/date-validation.ts`
- Modify: `Frontend/src/app/utils/date-validation.spec.ts`

- [ ] **Step 1: Write the failing tests**

Append to `Frontend/src/app/utils/date-validation.spec.ts` (above the closing brace of the outer `describe`, or as a new top-level describe — pick the existing pattern in the file):

```ts
import { parseYmd, formatYmd } from './date-validation';

describe('parseYmd', () => {
  it('parses YYYY-MM-DD to a local-midnight Date', () => {
    const d = parseYmd('2025-05-31');
    expect(d).not.toBeNull();
    expect(d!.getFullYear()).toBe(2025);
    expect(d!.getMonth()).toBe(4); // May (0-indexed)
    expect(d!.getDate()).toBe(31);
    expect(d!.getHours()).toBe(0);
    expect(d!.getMinutes()).toBe(0);
  });

  it('returns null for empty string', () => {
    expect(parseYmd('')).toBeNull();
  });

  it('returns null for malformed strings', () => {
    expect(parseYmd('garbage')).toBeNull();
    expect(parseYmd('2025-5-31')).toBeNull(); // single-digit month — the original bug
    expect(parseYmd('2025/05/31')).toBeNull();
    expect(parseYmd('2025-13-01')).toBeNull(); // invalid month
    expect(parseYmd('2025-02-30')).toBeNull(); // Feb 30
  });
});

describe('formatYmd', () => {
  it('formats a Date as zero-padded YYYY-MM-DD', () => {
    expect(formatYmd(new Date(2025, 4, 31))).toBe('2025-05-31');
    expect(formatYmd(new Date(2025, 0, 1))).toBe('2025-01-01');
  });

  it('returns empty string for null', () => {
    expect(formatYmd(null)).toBe('');
  });

  it('round-trips with parseYmd', () => {
    const original = '2024-02-29'; // leap day
    const parsed = parseYmd(original);
    expect(parsed).not.toBeNull();
    expect(formatYmd(parsed)).toBe(original);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec my-frontend npx vitest run src/app/utils/date-validation.spec.ts`

Expected: FAIL with `parseYmd is not a function` / `formatYmd is not a function` (imports unresolved).

- [ ] **Step 3: Implement the helpers**

Append to `Frontend/src/app/utils/date-validation.ts` (above the `formatDateStr` private helper at the bottom):

```ts
/**
 * Parse a strict 'YYYY-MM-DD' string to a Date at local midnight.
 * Returns null for empty input, non-matching format (including
 * single-digit month/day like '2025-5-31'), or impossible calendar
 * dates like '2025-02-30'. Used as the canonical adapter between
 * the API string format and JS Date objects.
 */
export function parseYmd(s: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const [y, m, d] = s.split('-').map(Number);
  const date = new Date(y, m - 1, d, 0, 0, 0, 0);
  // Reject impossible dates: Date constructor rolls over (Feb 30 -> Mar 2),
  // so verify the round-trip matches the input.
  if (date.getFullYear() !== y || date.getMonth() !== m - 1 || date.getDate() !== d) {
    return null;
  }
  return date;
}

/**
 * Format a Date as 'YYYY-MM-DD' in local time, zero-padded.
 * Returns '' for null. Inverse of parseYmd.
 */
export function formatYmd(d: Date | null): string {
  if (d === null) return '';
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `podman exec my-frontend npx vitest run src/app/utils/date-validation.spec.ts`

Expected: PASS — all parseYmd / formatYmd cases plus the existing date-validation specs.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/utils/date-validation.ts Frontend/src/app/utils/date-validation.spec.ts
git commit -m "$(cat <<'EOF'
feat(frontend): add parseYmd/formatYmd helpers to date-validation

Lifts the YYYY-MM-DD<->Date conversion logic that's currently inlined
as private statics on DataLabComponent into shared helpers, in
preparation for the new PolygonDateRangeComponent. parseYmd rejects
single-digit-month formats like '2025-5-31' (the format that triggered
the upstream bug) and impossible dates via round-trip verification.
formatYmd mirrors the existing data-lab static.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Migrate data-lab to use the new helpers

**Files:**
- Modify: `Frontend/src/app/components/data-lab/data-lab.component.ts`

- [ ] **Step 1: Delete the static helpers and add the import**

In `data-lab.component.ts`:

Add to the existing import from `'../../utils/date-validation'` (or create the import if absent — the file already imports `validateDateRange`, `getDisabledHolidayDates`, `buildHolidayMap`, `getMinAllowedDate` from there):

```ts
import {
  validateDateRange,
  getDisabledHolidayDates,
  buildHolidayMap,
  getMinAllowedDate,
  parseYmd,
  formatYmd,
} from '../../utils/date-validation';
```

(Match the existing import style — single import or multi-line — at the top of the file.)

Delete the two private static methods around line 373–384:

```ts
// DELETE THIS BLOCK:
private static formatDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

private static parseDate(dateStr: string): Date {
  const [y, m, d] = dateStr.split('-').map(Number);
  const date = new Date(y, m - 1, d, 0, 0, 0, 0);
  return date;
}
```

Replace **every** `DataLabComponent.formatDate(x)` callsite with `formatYmd(x)`. Eight callsites, found at lines 388, 389, 451, 853, 854 (per spec inventory):

```ts
// Line 388:
fromDate = computed(() => formatYmd(this.fromDateValue()));
// Line 389:
toDate = computed(() => formatYmd(this.toDateValue()));
// Line 451 (within the rangeState init):
from: formatYmd(DataLabComponent.get30DaysAgo()),
to: formatYmd(DataLabComponent.getYesterday()),
// Lines 853-854 (inside the effect):
const fromIso = formatYmd(this.fromDateValue());
const toIso = formatYmd(this.toDateValue());
```

Replace **every** `DataLabComponent.parseDate(x)` callsite. Four callsites, lines 856, 859, 1128, 1129. Because `parseYmd` now returns `Date | null` (it can fail), wrap each with a non-null fallback that preserves existing behavior. The inputs at all four callsites come from already-stored YYYY-MM-DD strings (session config / picker state), so a `null` return signals corrupted persistence — fall back to the current value rather than throwing:

```ts
// Lines 856, 859 (inside the rangeState effect):
if (fromIso !== v.from) {
  const parsed = parseYmd(v.from);
  if (parsed) this.fromDateValue.set(parsed);
}
// (same shape for the toDate branch on line 859)

// Lines 1128, 1129 (inside session restore):
const parsedFrom = parseYmd(session.config.fromDate);
if (parsedFrom) this.fromDateValue.set(parsedFrom);
const parsedTo = parseYmd(session.config.toDate);
if (parsedTo) this.toDateValue.set(parsedTo);
```

`getYesterday()` and `get30DaysAgo()` stay as private statics — they're not in scope for this lift, and their semantics (return Date, not string) don't fit the helper module.

- [ ] **Step 2: Run data-lab tests to verify nothing regressed**

Run: `podman exec my-frontend npx vitest run src/app/components/data-lab`

Expected: PASS — all existing data-lab specs (`data-lab.auto-bar-timeframe.spec.ts`, `data-lab.auto-chunk-readout.spec.ts`, `data-lab.parse-chart-timeframe.spec.ts`).

- [ ] **Step 3: Type-check the project**

Run: `podman exec my-frontend npx tsc --noEmit`

Expected: PASS — no type errors anywhere.

- [ ] **Step 4: Commit**

```bash
git add Frontend/src/app/components/data-lab/data-lab.component.ts
git commit -m "$(cat <<'EOF'
refactor(frontend): data-lab uses shared parseYmd/formatYmd helpers

Removes the inlined DataLabComponent.parseDate / formatDate statics
in favor of the shared utils added in the previous commit. Behavior
is preserved: the new parseYmd is null-safe (rejects malformed input)
and the four data-lab callsites guard with a 'keep current value if
parse fails' fallback, since the inputs come from already-stored
state where a parse failure indicates corrupted persistence.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Create `PolygonDateRangeComponent` skeleton with model round-trip test

**Files:**
- Create: `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.ts`
- Create: `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.html`
- Create: `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.scss`
- Create: `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.spec.ts`
- Create: `Frontend/src/app/shared/polygon-date-range/index.ts`

- [ ] **Step 1: Write the failing render + model-binding test**

Create `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.spec.ts`:

```ts
import { Component, signal } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { describe, it, expect, vi } from 'vitest';
import { of } from 'rxjs';

import { PolygonDateRangeComponent } from './polygon-date-range.component';
import { MarketMonitorService } from '../../services/market-monitor.service';

@Component({
  imports: [PolygonDateRangeComponent],
  template: `
    <app-polygon-date-range
      [(fromDate)]="from"
      [(toDate)]="to"
      idPrefix="test"
    />
  `,
})
class HostComponent {
  from = signal('2025-01-01');
  to = signal('2025-03-31');
}

function fakeMarketMonitor() {
  return {
    getHolidays: vi.fn().mockReturnValue(of([])),
  };
}

describe('PolygonDateRangeComponent', () => {
  it('renders both date inputs with stable label-for wiring', async () => {
    await render(HostComponent, {
      providers: [{ provide: MarketMonitorService, useValue: fakeMarketMonitor() }],
    });

    expect(screen.getByLabelText('From')).toHaveAttribute('id', 'test-from');
    expect(screen.getByLabelText('To')).toHaveAttribute('id', 'test-to');
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `podman exec my-frontend npx vitest run src/app/shared/polygon-date-range`

Expected: FAIL — `Cannot find module './polygon-date-range.component'`.

- [ ] **Step 3: Create the component skeleton (minimal — no holidays, no warning yet)**

Create `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.ts`:

```ts
import {
  Component,
  ChangeDetectionStrategy,
  inject,
  computed,
  signal,
  model,
  input,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { DatePickerModule } from 'primeng/datepicker';
import { MessageModule } from 'primeng/message';

import { MarketMonitorService } from '../../services/market-monitor.service';
import {
  parseYmd,
  formatYmd,
  validateDateRange,
  getDisabledHolidayDates,
  getMinAllowedDate,
} from '../../utils/date-validation';
import type { MarketHolidayEvent } from '../../models/market-monitor';

@Component({
  selector: 'app-polygon-date-range',
  imports: [DatePickerModule, MessageModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './polygon-date-range.component.html',
  styleUrls: ['./polygon-date-range.component.scss'],
})
export class PolygonDateRangeComponent {
  fromDate = model.required<string>();
  toDate = model.required<string>();

  fromLabel = input<string>('From');
  toLabel = input<string>('To');
  idPrefix = input<string>('pdr');

  private readonly marketMonitor = inject(MarketMonitorService);
  private readonly holidays = signal<MarketHolidayEvent[]>([]);

  protected readonly minDate = new Date(getMinAllowedDate() + 'T00:00:00');
  protected readonly maxDate = (() => {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    return d;
  })();
  protected readonly disabledDays = [0, 6];
  protected readonly disabledDates = computed(() =>
    getDisabledHolidayDates(this.holidays()),
  );

  protected readonly fromDateValue = computed(() => parseYmd(this.fromDate()));
  protected readonly toDateValue = computed(() => parseYmd(this.toDate()));

  readonly warning = computed<string | null>(() =>
    validateDateRange(this.fromDate(), this.toDate()),
  );
  readonly valid = computed<boolean>(() => this.warning() === null);

  protected onFromChange(d: Date | null): void {
    this.fromDate.set(formatYmd(d));
  }
  protected onToChange(d: Date | null): void {
    this.toDate.set(formatYmd(d));
  }

  constructor() {
    firstValueFrom(this.marketMonitor.getHolidays(20))
      .then((events) => this.holidays.set(events))
      .catch(() => {
        /* non-critical, matches data-lab */
      });
  }
}
```

Create `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.html`:

```html
<div class="pdr">
  <div class="pdr__row">
    <div class="pdr__field">
      <label [attr.for]="idPrefix() + '-from'" class="pdr__label">{{ fromLabel() }}</label>
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
      <label [attr.for]="idPrefix() + '-to'" class="pdr__label">{{ toLabel() }}</label>
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

Create `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.scss`:

```scss
.pdr {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.pdr__row {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem;
}

.pdr__field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  min-width: 0;
}

.pdr__label {
  font-size: 0.875rem;
  font-weight: 500;
}

.pdr__warning {
  display: block;
}
```

Create `Frontend/src/app/shared/polygon-date-range/index.ts`:

```ts
export { PolygonDateRangeComponent } from './polygon-date-range.component';
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `podman exec my-frontend npx vitest run src/app/shared/polygon-date-range`

Expected: PASS — both inputs render with the correct `id` / label association.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/shared/polygon-date-range/
git commit -m "$(cat <<'EOF'
feat(frontend): add PolygonDateRangeComponent skeleton

Standalone Angular 21 component with model.required<string> two-way
bindings for fromDate/toDate, PrimeNG p-datepicker with Polygon
Starter constraints (minDate=2yr ago, maxDate=yesterday, disabledDays
weekend, disabledDates self-fetched holidays), inline p-message
advisory powered by validateDateRange.

This commit lays the skeleton; the holidays-fetch and warning-render
tests come in the next two commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Test holiday self-fetch behavior

**Files:**
- Modify: `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.spec.ts`

- [ ] **Step 1: Write the failing tests**

Append to the `describe('PolygonDateRangeComponent', ...)` block:

```ts
it('fetches holidays from MarketMonitorService once on construct', async () => {
  const monitor = fakeMarketMonitor();
  await render(HostComponent, {
    providers: [{ provide: MarketMonitorService, useValue: monitor }],
  });

  expect(monitor.getHolidays).toHaveBeenCalledTimes(1);
  expect(monitor.getHolidays).toHaveBeenCalledWith(20);
});

it('disables holiday dates returned by the service', async () => {
  const christmas: MarketHolidayEvent = {
    date: '2025-12-25',
    name: 'Christmas',
    status: 'Closed',
  };
  const monitor = {
    getHolidays: vi.fn().mockReturnValue(of([christmas])),
  };

  const { fixture } = await render(HostComponent, {
    providers: [{ provide: MarketMonitorService, useValue: monitor }],
  });
  await fixture.whenStable();

  const componentInstance = fixture.debugElement.children[0].componentInstance as PolygonDateRangeComponent;
  const disabled = (componentInstance as unknown as { disabledDates: () => Date[] }).disabledDates();
  expect(disabled.length).toBe(1);
  expect(disabled[0].getFullYear()).toBe(2025);
  expect(disabled[0].getMonth()).toBe(11); // December
  expect(disabled[0].getDate()).toBe(25);
});

it('renders without throwing when getHolidays rejects', async () => {
  const monitor = {
    getHolidays: vi.fn().mockReturnValue(throwError(() => new Error('network down'))),
  };

  await render(HostComponent, {
    providers: [{ provide: MarketMonitorService, useValue: monitor }],
  });

  // If construct didn't catch the rejection, render would have thrown.
  expect(screen.getByLabelText('From')).toBeInTheDocument();
});
```

Add the missing imports at the top of the spec file:

```ts
import { throwError } from 'rxjs';
import type { MarketHolidayEvent } from '../../models/market-monitor';
```

- [ ] **Step 2: Run the tests to verify they pass**

Run: `podman exec my-frontend npx vitest run src/app/shared/polygon-date-range`

Expected: PASS — these tests should pass against the existing component (the fetch logic was implemented in Task 3). If any fails, the bug is in the component, not the test.

- [ ] **Step 3: Commit**

```bash
git add Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.spec.ts
git commit -m "$(cat <<'EOF'
test(frontend): cover PolygonDateRangeComponent holiday fetch

Verifies the component calls MarketMonitorService.getHolidays(20) once
on construct, exposes the resolved events through disabledDates(), and
renders without throwing when the fetch rejects (matching the silent
failure pattern data-lab uses).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Test inline warning advisory

**Files:**
- Modify: `Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.spec.ts`

- [ ] **Step 1: Write the failing tests**

Append to the `describe('PolygonDateRangeComponent', ...)` block:

```ts
it('shows the warning advisory when fromDate is before the 2-year window', async () => {
  @Component({
    imports: [PolygonDateRangeComponent],
    template: `<app-polygon-date-range [(fromDate)]="from" [(toDate)]="to" idPrefix="warn" />`,
  })
  class WarnHost {
    from = signal('2010-01-01'); // far older than the 2-year limit
    to = signal('2025-03-31');
  }

  await render(WarnHost, {
    imports: [PolygonDateRangeComponent],
    providers: [{ provide: MarketMonitorService, useValue: fakeMarketMonitor() }],
  });

  const warning = await screen.findByText(/2-year historical data limit/);
  expect(warning).toBeInTheDocument();
});

it('hides the warning advisory when the range is valid', async () => {
  await render(HostComponent, {
    providers: [{ provide: MarketMonitorService, useValue: fakeMarketMonitor() }],
  });

  expect(screen.queryByText(/2-year historical data limit/)).toBeNull();
  expect(screen.queryByText(/From date must be before/)).toBeNull();
});
```

- [ ] **Step 2: Run the tests to verify they pass**

Run: `podman exec my-frontend npx vitest run src/app/shared/polygon-date-range`

Expected: PASS — `warning()` already returns the `validateDateRange` text from Task 3, the template renders `<p-message>` when truthy.

- [ ] **Step 3: Commit**

```bash
git add Frontend/src/app/shared/polygon-date-range/polygon-date-range.component.spec.ts
git commit -m "$(cat <<'EOF'
test(frontend): cover PolygonDateRangeComponent warning advisory

Asserts the inline <p-message severity='warn'> renders when
validateDateRange returns a non-null string (e.g. fromDate older than
the 2-year Polygon Starter window) and stays hidden for valid ranges.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Migrate `feature-runner` (closes the original bug)

**Files:**
- Modify: `Frontend/src/app/components/research-lab/feature-runner/feature-runner.component.ts`
- Modify: `Frontend/src/app/components/research-lab/feature-runner/feature-runner.component.html`

- [ ] **Step 1: Add the import to the component TS**

In `feature-runner.component.ts`, add to the existing imports near the top:

```ts
import { PolygonDateRangeComponent } from '../../../shared/polygon-date-range';
```

In the `@Component` decorator's `imports` array, add `PolygonDateRangeComponent`. Remove `InputText` from `imports` if no other element in the template still uses it (search the HTML to confirm — keep it if anything else uses `pInputText`).

- [ ] **Step 2: Replace the two text inputs in the HTML**

In `feature-runner.component.html`, replace lines 32–39 (the two `<div class="field">` blocks for fromDate / toDate):

```html
<!-- BEFORE -->
<div class="field">
  <label class="field-label" for="fromDate">From Date</label>
  <input pInputText id="fromDate" [ngModel]="fromDate()" (ngModelChange)="fromDate.set($event)" placeholder="YYYY-MM-DD" />
</div>
<div class="field">
  <label class="field-label" for="toDate">To Date</label>
  <input pInputText id="toDate" [ngModel]="toDate()" (ngModelChange)="toDate.set($event)" placeholder="YYYY-MM-DD" />
</div>

<!-- AFTER -->
<div class="field field--date-range">
  <app-polygon-date-range
    [(fromDate)]="fromDate"
    [(toDate)]="toDate"
    fromLabel="From Date"
    toLabel="To Date"
    idPrefix="feat"
  />
</div>
```

- [ ] **Step 3: Run the feature-runner spec to confirm no regression**

Run: `podman exec my-frontend npx vitest run src/app/components/research-lab/feature-runner`

Expected: PASS. If the existing spec (`feature-runner.component.spec.ts`) asserts on `screen.getByLabelText('From Date')` or sets `component.fromDate.set('2024-01-01')`, both should keep working — the new component still surfaces those labels and signals.

If a spec test fails because it queried for `<input pInputText>` directly: update the spec to query by label (`screen.getByLabelText('From Date')`) so behavior is asserted, not DOM structure. (Per `.claude/rules/angular.md`: assert on rendered output, not DOM structure.)

- [ ] **Step 4: Manual smoke check** (skip if running headless / no UI)

Start dev server (`podman compose up frontend` or use existing) and navigate to the feature-runner page. Confirm: (a) calendar opens, (b) cannot pick a date older than 2 years ago, (c) cannot pick yesterday + 1, (d) weekends are dimmed, (e) entering a 2010 date via paste shows the inline warning.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/research-lab/feature-runner/
git commit -m "$(cat <<'EOF'
fix(frontend): feature-runner uses PolygonDateRangeComponent

Closes the 422 from PythonDataService when from_date / to_date land in
the API as 'YYYY-M-D' instead of 'YYYY-MM-DD'. Replaces the plain
pInputText text fields (which had no format validation) with the new
shared component, which renders a PrimeNG p-datepicker bounded by the
Polygon Starter 2-year window, weekend + holiday disable, and an
inline validation advisory for paste / programmatic-set cases.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Migrate `signal-runner` (same bug)

**Files:**
- Modify: `Frontend/src/app/components/research-lab/signal-runner/signal-runner.component.ts`
- Modify: `Frontend/src/app/components/research-lab/signal-runner/signal-runner.component.html`

- [ ] **Step 1: Add the import to the component TS**

In `signal-runner.component.ts`:

```ts
import { PolygonDateRangeComponent } from '../../../shared/polygon-date-range';
```

Add `PolygonDateRangeComponent` to the `@Component.imports` array. Remove `InputText` if unused elsewhere in the template.

- [ ] **Step 2: Replace the two text inputs in the HTML**

In `signal-runner.component.html`, replace lines 33–40 (the two `<div class="field">` blocks for fromDate / toDate). The existing block uses `id="sig-from"` / `id="sig-to"`:

```html
<!-- BEFORE -->
<div class="field">
  <label class="field-label" for="sig-from">From Date</label>
  <input pInputText id="sig-from" [ngModel]="fromDate()" (ngModelChange)="fromDate.set($event)" placeholder="YYYY-MM-DD" />
</div>
<div class="field">
  <label class="field-label" for="sig-to">To Date</label>
  <input pInputText id="sig-to" [ngModel]="toDate()" (ngModelChange)="toDate.set($event)" placeholder="YYYY-MM-DD" />
</div>

<!-- AFTER -->
<div class="field field--date-range">
  <app-polygon-date-range
    [(fromDate)]="fromDate"
    [(toDate)]="toDate"
    fromLabel="From Date"
    toLabel="To Date"
    idPrefix="sig"
  />
</div>
```

- [ ] **Step 3: Run signal-runner specs**

Run: `podman exec my-frontend npx vitest run src/app/components/research-lab/signal-runner`

Expected: PASS. Update any spec that queries for the raw `<input>` to use `screen.getByLabelText('From Date')` / `'To Date'` instead.

- [ ] **Step 4: Commit**

```bash
git add Frontend/src/app/components/research-lab/signal-runner/
git commit -m "$(cat <<'EOF'
fix(frontend): signal-runner uses PolygonDateRangeComponent

Same fix as feature-runner: replaces plain pInputText text inputs
with the shared component to prevent 'YYYY-M-D' from reaching the
PythonDataService and to bring Polygon Starter date constraints to
the signal-research form.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Migrate `batch-runner` (cross-sectional)

**Files:**
- Modify: `Frontend/src/app/components/research-lab/batch-runner/batch-runner.component.ts`
- Modify: `Frontend/src/app/components/research-lab/batch-runner/batch-runner.component.html`

Existing inputs use `[(ngModel)]="fromDate"` (banana-box on the signal directly, which works in Angular 21).

- [ ] **Step 1: Add the import**

In `batch-runner.component.ts`:

```ts
import { PolygonDateRangeComponent } from '../../../shared/polygon-date-range';
```

Add `PolygonDateRangeComponent` to the `@Component.imports` array.

- [ ] **Step 2: Replace the two date inputs in the HTML**

In `batch-runner.component.html`, replace lines 24–32 (the two `<div>` field blocks at lines 26 and 30):

```html
<!-- BEFORE (approximate — preserve the surrounding structure) -->
<div class="field">
  <label>From Date</label>
  <input pInputText type="date" [(ngModel)]="fromDate" class="w-full" />
</div>
<div class="field">
  <label>To Date</label>
  <input pInputText type="date" [(ngModel)]="toDate" class="w-full" />
</div>

<!-- AFTER -->
<div class="field field--date-range">
  <app-polygon-date-range
    [(fromDate)]="fromDate"
    [(toDate)]="toDate"
    fromLabel="From Date"
    toLabel="To Date"
    idPrefix="batch"
  />
</div>
```

- [ ] **Step 3: Run batch-runner specs**

Run: `podman exec my-frontend npx vitest run src/app/components/research-lab/batch-runner`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add Frontend/src/app/components/research-lab/batch-runner/
git commit -m "$(cat <<'EOF'
feat(frontend): batch-runner uses PolygonDateRangeComponent

Brings Polygon Starter constraints (2-year window, weekend + holiday
disable) and the inline validation advisory to the cross-sectional
batch research form. Functional behavior unchanged — same fromDate /
toDate signals flow into the existing GraphQL mutation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Migrate `indicator-reliability`

**Files:**
- Modify: `Frontend/src/app/components/research-lab/indicator-reliability/indicator-reliability.component.ts`
- Modify: `Frontend/src/app/components/research-lab/indicator-reliability/indicator-reliability.component.html`

- [ ] **Step 1: Add the import**

In `indicator-reliability.component.ts`:

```ts
import { PolygonDateRangeComponent } from '../../../shared/polygon-date-range';
```

Add `PolygonDateRangeComponent` to the `@Component.imports` array.

- [ ] **Step 2: Replace the two date inputs in the HTML**

In `indicator-reliability.component.html`, replace the two date-input blocks at lines 70–91:

```html
<!-- AFTER (preserves surrounding wrapper / grid layout from the file) -->
<div class="field field--date-range">
  <app-polygon-date-range
    [(fromDate)]="fromDate"
    [(toDate)]="toDate"
    fromLabel="From Date"
    toLabel="To Date"
    idPrefix="ireliab"
  />
</div>
```

- [ ] **Step 3: Run the spec**

Run: `podman exec my-frontend npx vitest run src/app/components/research-lab/indicator-reliability`

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add Frontend/src/app/components/research-lab/indicator-reliability/
git commit -m "$(cat <<'EOF'
feat(frontend): indicator-reliability uses PolygonDateRangeComponent

Adopts the shared component for parity with feature-runner /
signal-runner / batch-runner. Same Polygon Starter constraints,
same inline validation advisory.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Migrate `spec-strategy-runner` (rename + swap)

**Files:**
- Modify: `Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.ts`
- Modify: `Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.html`
- Modify: `Frontend/src/app/components/spec-strategy-runner/spec-strategy-runner.component.spec.ts` (if it asserts on the renamed signals)

The existing component uses `startDate`/`endDate` (per spec §4 inventory). Rename to `fromDate`/`toDate` for repo-wide consistency.

- [ ] **Step 1: Rename the signals in the component TS**

In `spec-strategy-runner.component.ts`, rename:

```ts
// startDate → fromDate
// endDate   → toDate
```

Use a project-wide search inside the component file: every `startDate(`, `endDate(`, `startDate.set(`, `endDate.set(`, `this.startDate`, `this.endDate` reference becomes `fromDate` / `toDate`. Do **not** rename anywhere else — keep changes scoped to this component's TS, HTML, SCSS, and spec.

If the component sends these to an API as `start_date` / `end_date` snake-case fields, **leave the API field names alone** — only the local signal names change. Verify by searching for `start_date` or `startDate:` (object literal key) inside the file and preserving those keys.

- [ ] **Step 2: Update the HTML — rename usages and replace inputs**

In `spec-strategy-runner.component.html`, first replace `startDate()` → `fromDate()` and `endDate()` → `toDate()` everywhere they appear (likely just lines 365, 369). Then replace lines ~363–371:

```html
<!-- AFTER -->
<div class="ssr-field ssr-field--date-range">
  <app-polygon-date-range
    [(fromDate)]="fromDate"
    [(toDate)]="toDate"
    fromLabel="From Date"
    toLabel="To Date"
    idPrefix="ssr"
  />
</div>
```

Add the import + `imports` entry as in previous tasks.

- [ ] **Step 3: Update the spec file if it references the old names**

Run: `podman exec my-frontend npx vitest run src/app/components/spec-strategy-runner`

If specs fail with `undefined is not a function` on `startDate.set` etc., update the spec to use the renamed signals. Otherwise leave it.

- [ ] **Step 4: Type-check whole project to catch any missed callsite**

Run: `podman exec my-frontend npx tsc --noEmit`

Expected: PASS. If anything outside this component referenced `SpecStrategyRunnerComponent.startDate` it'll surface here.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/spec-strategy-runner/
git commit -m "$(cat <<'EOF'
refactor(frontend): spec-strategy-runner standardizes on fromDate/toDate

Renames startDate/endDate to fromDate/toDate for consistency with the
five other research forms now sharing PolygonDateRangeComponent. The
external API field names (start_date / end_date) are unchanged — this
is only the in-component signal naming.

Then adopts PolygonDateRangeComponent for the same Polygon Starter
constraint coverage as the rest of the research-lab forms.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Migrate `strategy-preflight` (rename + swap)

**Files:**
- Modify: `Frontend/src/app/components/research-lab/strategy-preflight/strategy-preflight.component.ts`
- Modify: `Frontend/src/app/components/research-lab/strategy-preflight/strategy-preflight.component.html`
- Modify: `Frontend/src/app/components/research-lab/strategy-preflight/strategy-preflight.component.spec.ts` (if applicable)

Same shape as Task 10.

- [ ] **Step 1: Rename `startDate`/`endDate` → `fromDate`/`toDate` in the component TS, HTML, and spec**

Same procedure as Task 10. Preserve API-payload key names (`start_date` / `end_date`) if they exist.

- [ ] **Step 2: Replace the two date inputs in the HTML**

In `strategy-preflight.component.html`, replace lines 31–41:

```html
<!-- AFTER -->
<div class="field field--date-range">
  <app-polygon-date-range
    [(fromDate)]="fromDate"
    [(toDate)]="toDate"
    fromLabel="From Date"
    toLabel="To Date"
    idPrefix="preflight"
  />
</div>
```

Add the component import and `imports` entry.

- [ ] **Step 3: Run the spec + project-wide type-check**

Run: `podman exec my-frontend npx vitest run src/app/components/research-lab/strategy-preflight`
Run: `podman exec my-frontend npx tsc --noEmit`

Expected: PASS for both.

- [ ] **Step 4: Commit**

```bash
git add Frontend/src/app/components/research-lab/strategy-preflight/
git commit -m "$(cat <<'EOF'
refactor(frontend): strategy-preflight standardizes on fromDate/toDate

Same rename + adoption as spec-strategy-runner: startDate/endDate →
fromDate/toDate (signal naming only, API payload unchanged), then
adopt PolygonDateRangeComponent for parity with the other five
research forms.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Project-scope lint + full Vitest + push + PR

- [ ] **Step 1: Project-scope lint**

Run: `npx eslint Frontend/src/ --max-warnings 0`

Expected: PASS — zero warnings, zero errors. Per `.claude/rules/angular.md`: warnings break the build.

If failures appear in files **not** touched by this branch, they're pre-existing — note them in the PR description and proceed. If failures appear in files this branch touched, fix them before continuing.

- [ ] **Step 2: Full frontend Vitest**

Run: `podman exec my-frontend npx ng test --watch=false`

Expected: PASS — all specs across the project. Same baseline rule as Step 1: pre-existing failures get noted, regressions get fixed.

- [ ] **Step 3: Project-wide type-check**

Run: `podman exec my-frontend npx tsc --noEmit`

Expected: PASS.

- [ ] **Step 4: Push branch**

```bash
git push -u origin feat/polygon-date-range-shared-component
```

- [ ] **Step 5: Open PR**

```bash
gh pr create --title "feat(frontend): shared PolygonDateRangeComponent + six form migrations" --body "$(cat <<'EOF'
## Summary

- Closes a 422 from PythonDataService that surfaced when feature-runner / signal-runner sent `to_date: "2025-5-31"` to the API — the plain `<input pInputText>` had no format validation, allowing a single-digit month to flow through.
- Adds `PolygonDateRangeComponent` at `Frontend/src/app/shared/polygon-date-range/` — PrimeNG `p-datepicker` bounded by the Polygon Starter 2-year window with weekend + market-holiday disable, inline `validateDateRange` advisory.
- Migrates six research forms to the new component: `feature-runner`, `signal-runner`, `batch-runner` (cross-sectional), `indicator-reliability`, `spec-strategy-runner`, `strategy-preflight`. Latter two get a `startDate`/`endDate` → `fromDate`/`toDate` rename for repo-wide consistency (signal names only — API payload field names are unchanged).
- Lifts `parseYmd` / `formatYmd` out of `data-lab.component.ts` private statics into `utils/date-validation.ts` so the canonical YYYY-MM-DD↔Date conversion has one home; updates data-lab to use them.

## Out of scope (deferred)

- `indicator-report` migration — uses template-driven `[(ngModel)]` on a non-signal field; needs its own signal refactor PR.
- `data-lab` migration — already has a richer picker with cache-availability and presets; downgrading would lose features.
- Quick-range presets ("Last 30d", "YTD") — opt-in via an `@Input` later if asked.
- Timespan-aware range cap (feature-runner's minute=180d / hour=1095d) — that warning stays in feature-runner; it's a per-screen concern.

## Test plan

- [x] `parseYmd` rejects `'2025-5-31'`, `'garbage'`, `'2025-02-30'`, `''`; round-trips with `formatYmd` (unit)
- [x] `PolygonDateRangeComponent` renders both date inputs with stable label-for wiring (component spec)
- [x] Holiday self-fetch: `MarketMonitorService.getHolidays(20)` called once on construct (component spec)
- [x] Holiday disable: stubbed Christmas → `disabledDates()` includes Dec 25 (component spec)
- [x] Holiday fetch failure: rejected promise → renders without throwing (component spec)
- [x] Warning advisory: `fromDate='2010-01-01'` → inline `<p-message>` appears (component spec)
- [x] All six migrated component specs still pass
- [x] Project-scope `npx eslint Frontend/src/ --max-warnings 0`
- [x] Project-scope `npx tsc --noEmit`
- [ ] Manual smoke: every migrated screen — calendar opens, can't pick out-of-range dates, paste of a 2010 date triggers warning

## Spec

`docs/superpowers/specs/2026-05-09-polygon-date-range-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Final commit if any docs / lint follow-ups landed**

If steps 1–3 surfaced follow-ups, commit them on the same branch and push. Otherwise nothing to commit here.

---

## Self-review notes (already applied)

**Spec coverage:**
- Q1 (PrimeNG `p-datepicker` + Polygon constraints): Tasks 3, 4, 5
- Q2 (`model.required<string>`): Task 3
- Q3 (self-fetch holidays): Tasks 3, 4
- Q4 (six migrations): Tasks 6–11
- Q5 (editable input + inline advisory): Tasks 3, 5
- Helper lift (`parseYmd`/`formatYmd`): Tasks 1, 2
- Out-of-scope items: not implemented; surfaced in PR description (Task 12 step 5)

**Type consistency:** `fromDate`/`toDate` as `model.required<string>` everywhere; `Date | null` from `parseYmd` (not `Date`); `formatYmd(null) === ''`; `validateDateRange` returns `string | null`; `valid` is the `=== null` of `warning`.

**Placeholders:** None.

**Migration order rationale:** Tasks 6–7 close the original bug first (highest priority). Tasks 8–9 are the same shape, low risk. Tasks 10–11 also do a TS rename and go last so the rename can land in isolation if review wants to revert it.
