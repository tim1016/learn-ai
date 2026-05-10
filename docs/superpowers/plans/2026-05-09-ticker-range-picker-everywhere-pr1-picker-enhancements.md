# PR (i) — Picker enhancements + new sibling components — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land additive changes on `<app-ticker-range-picker>` (multiplier, hideSampling, opt-in `availableMultipliers`), build two new sibling components (`<app-multi-ticker-range-picker>` and `<app-ticker-date-picker>`), and create the wire-format adapter (`utils/ticker-wire.ts`). No existing consumer changes — `data-lab` and `lean-engine` must remain byte-identical.

**Scope guardrail (post-review):** PR (i) **does not modernize the existing picker's legacy patterns** (`@HostListener`, `FormsModule`, `ngModel` — currently at `ticker-range-picker.component.ts:8,17,98,308`). Those are moved as-is into the new sub-components; they remain a known violation of `.claude/rules/angular.md` tracked in the spec's §"Out of scope". Modernizing them is unrelated to this initiative's goal and would balloon scope. The `hideResolution` input is **kept as a deprecated alias** for one PR cycle (removed in PR (iii)).

**Architecture:** Refactor the canonical picker's three semantic cards (Instrument / Time window / Sampling) into shared sub-components in `shared/ticker-range-picker/parts/`. The new sibling components reuse the appropriate sub-components. The wire adapter is a pure-function module that translates picker payloads to the snake_case JSON shape the Python service will expect (in PR ii).

**Tech Stack:** Angular 21 (standalone components, OnPush, signals, `model()`), PrimeNG (`p-select`, `p-datepicker`, `p-button`), Vitest + Angular Testing Library, ESLint, TypeScript strict mode.

**Spec reference:** `docs/superpowers/specs/2026-05-09-ticker-range-picker-everywhere-design.md` §"Components — full surface", §"Build sequence — PR (i)".

---

## File structure

**Created (new):**

```
Frontend/src/app/shared/ticker-range-picker/parts/
  instrument-card.component.ts            (extracted Instrument card UI + behavior)
  instrument-card.component.html
  instrument-card.component.scss          (only relocated rules; no new styles)
  time-window-card.component.ts           (extracted Time window card)
  time-window-card.component.html
  time-window-card.component.scss
  sampling-card.component.ts              (extracted Sampling card + new multiplier dropdown)
  sampling-card.component.html
  sampling-card.component.scss

Frontend/src/app/shared/multi-ticker-range-picker/
  multi-ticker-range-picker.types.ts
  multi-ticker-range-picker.component.ts
  multi-ticker-range-picker.component.html
  multi-ticker-range-picker.component.scss
  multi-ticker-range-picker.component.spec.ts
  multi-instrument-card.component.ts      (sibling-only Instrument card variant)
  multi-instrument-card.component.html
  multi-instrument-card.component.scss

Frontend/src/app/shared/ticker-date-picker/
  ticker-date-picker.types.ts
  ticker-date-picker.component.ts
  ticker-date-picker.component.html
  ticker-date-picker.component.scss
  ticker-date-picker.component.spec.ts

Frontend/src/app/utils/ticker-wire.ts
Frontend/src/app/utils/ticker-wire.spec.ts
```

**Modified:**

```
Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.types.ts
  - add `multiplier?: number` to TickerRange

Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.ts
  - ADD `hideSampling` input alongside the existing `hideResolution` (kept as deprecated alias)
  - add `availableMultipliers` input
  - reduce inline template logic by composing the three new parts/* sub-components
  - DO NOT touch the existing @HostListener / FormsModule / ngModel — those move into
    parts/instrument-card.component.ts as-is and remain known violations tracked in the spec

Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.html
  - replace inline Instrument / Time window / Sampling sections with
    <app-instrument-card>, <app-time-window-card>, <app-sampling-card>

Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.spec.ts
  - add: availableMultipliers renders dropdown
  - add: hideSampling collapses Sampling card
  - add: hideResolution=true ALSO collapses Sampling card (deprecated-alias test)
```

**Untouched:**
- All consumer components (consumers move in PR iii)
- `data-lab.component.ts`, `lean-engine.component.ts` and their HTMLs (verified by smoke tests below)
- `polygon-date-range/*` (deleted in PR iii)

---

## Conventions for every task in this plan

- **Branch:** all commits land on `feat/ticker-range-picker-everywhere` (already created off master).
- **Commit cadence:** one commit per task. Subject in conventional-commit style: `feat(picker): …`, `refactor(picker): …`, `test(picker): …`.
- **TDD:** test first, run-fail, implement, run-pass, commit.
- **Container commands:** the dev environment runs in `podman compose`. The frontend container is `my-frontend`.
- **Per-file iteration loop:**
  ```bash
  podman exec my-frontend npx ng test --watch=false --include='src/app/shared/ticker-range-picker/**' 
  ```
  Use this for fast feedback. The full project-scope run happens at the end (Task 16).
- **AT NO POINT push or open a PR mid-plan.** Push happens once after Task 16, then PR opens. PR-monitor handles review autonomously per workflow memory.

---

## Task 1: Extract `instrument-card.component` from canonical picker

**Files:**
- Create: `Frontend/src/app/shared/ticker-range-picker/parts/instrument-card.component.ts`
- Create: `Frontend/src/app/shared/ticker-range-picker/parts/instrument-card.component.html`
- Create: `Frontend/src/app/shared/ticker-range-picker/parts/instrument-card.component.scss`
- Modify: `Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.ts:103-405` (move Instrument-related state/methods)
- Modify: `Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.html:26-159` (replace with `<app-instrument-card>`)

- [ ] **Step 1: Write the smoke test for the extracted component**

Add to `Frontend/src/app/shared/ticker-range-picker/parts/instrument-card.component.spec.ts` (new file):

```ts
import { render, screen, fireEvent } from '@testing-library/angular';
import { signal } from '@angular/core';
import { InstrumentCardComponent } from './instrument-card.component';
import type { TickerOption, TickerRange } from '../ticker-range-picker.types';

describe('InstrumentCardComponent', () => {
  const baseValue: TickerRange = {
    symbol: 'SPY', from: '2025-04-01', to: '2025-04-30', resolution: 'minute',
  };
  const pool: TickerOption[] = [
    { symbol: 'SPY', name: 'SPDR S&P 500 ETF', exchange: 'ARCA', cache: 0.95, last: '2025-04-30' },
    { symbol: 'QQQ', name: 'Invesco QQQ',       exchange: 'NASDAQ', cache: 0.80, last: '2025-04-30' },
  ];

  it('renders the current symbol and exchange chip', async () => {
    await render(InstrumentCardComponent, {
      inputs: { value: signal(baseValue), tickerPool: pool, recent: [] },
    });
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByText('ARCA')).toBeTruthy();
  });

  it('opens the dropdown on click and shows the recent list when query is empty', async () => {
    await render(InstrumentCardComponent, {
      inputs: { value: signal(baseValue), tickerPool: pool, recent: ['QQQ'] },
    });
    const tickerBox = screen.getByRole('combobox');
    fireEvent.click(tickerBox);
    expect(screen.getByText('Recent')).toBeTruthy();
    expect(screen.getByText('Invesco QQQ')).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/ticker-range-picker/parts/instrument-card.component.spec.ts'
```
Expected: FAIL with "Cannot find module './instrument-card.component'".

- [ ] **Step 3: Create the component (TS file)**

```ts
// Frontend/src/app/shared/ticker-range-picker/parts/instrument-card.component.ts
import {
  ChangeDetectionStrategy, Component, computed, DestroyRef, effect,
  ElementRef, HostListener, inject, input, model, signal, viewChild,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Tooltip } from 'primeng/tooltip';

import type { TickerOption, TickerRange } from '../ticker-range-picker.types';
import { isoDate } from '../ticker-range-picker.types';

const EXCHANGE_NAMES: Readonly<Record<string, string>> = {
  ARCA: 'NYSE Arca',
  NASDAQ: 'NASDAQ',
  NYSE: 'New York Stock Exchange',
  BATS: 'Cboe BZX',
  IEX: 'IEX',
  AMEX: 'NYSE American',
};

@Component({
  selector: 'app-instrument-card',
  imports: [CommonModule, FormsModule, Tooltip],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './instrument-card.component.html',
  styleUrls: ['./instrument-card.component.scss'],
})
export class InstrumentCardComponent {
  readonly value = model.required<TickerRange>();
  readonly tickerPool = input<readonly TickerOption[]>([]);
  readonly recent = input<readonly string[]>([]);

  private readonly rootEl = viewChild.required<ElementRef<HTMLElement>>('rootEl');
  private readonly searchInput = viewChild<ElementRef<HTMLInputElement>>('searchInput');

  readonly open = signal(false);
  readonly query = signal('');

  constructor() {
    effect(() => {
      if (this.open()) {
        const input = this.searchInput();
        if (input) queueMicrotask(() => input.nativeElement.focus());
      }
    });
  }

  readonly selectedTicker = computed<TickerOption | undefined>(() =>
    this.tickerPool().find((t) => t.symbol === this.value().symbol),
  );
  readonly selectedExchange = computed(() => this.selectedTicker()?.exchange ?? '—');
  readonly selectedExchangeTooltip = computed<string>(() => {
    const code = this.selectedExchange();
    const symbol = this.value().symbol;
    const name = EXCHANGE_NAMES[code];
    if (!name) return 'Listing exchange — where this instrument is primarily traded.';
    return `${name} — primary listing venue for ${symbol}.`;
  });

  readonly filteredTickers = computed<readonly TickerOption[]>(() => {
    const q = this.query().trim().toUpperCase();
    const pool = this.tickerPool();
    if (!q) return pool;
    return pool.filter((t) => t.symbol.includes(q) || t.name.toUpperCase().includes(q));
  });

  readonly recentTickers = computed<readonly TickerOption[]>(() => {
    const recent = this.recent();
    if (recent.length === 0) return [];
    const pool = this.tickerPool();
    return recent
      .map((s) => pool.find((t) => t.symbol === s))
      .filter((t): t is TickerOption => !!t);
  });

  trackBySymbol(_: number, t: TickerOption): string { return t.symbol; }

  openDropdown(): void {
    if (this.open()) return;
    this.open.set(true);
    this.query.set('');
  }
  closeDropdown(): void { this.open.set(false); }

  onTickerBoxEnter(event: Event): void {
    if (!this.open()) { this.openDropdown(); event.preventDefault(); }
  }
  onTickerBoxSpace(event: Event): void {
    if (!this.open()) { this.openDropdown(); event.preventDefault(); }
  }

  @HostListener('document:mousedown', ['$event'])
  onDocumentMouseDown(event: MouseEvent): void {
    const host = this.rootEl().nativeElement;
    if (!host.contains(event.target as Node)) this.closeDropdown();
  }

  onSearchInput(value: string): void { this.query.set(value); }

  pickTicker(t: TickerOption): void {
    const current = this.value();
    const patch: Partial<TickerRange> = { symbol: t.symbol };
    if (t.last) {
      const end = new Date(t.last);
      const start = new Date(end);
      start.setDate(start.getDate() - 30);
      patch.from = isoDate(start);
      patch.to = isoDate(end);
    }
    this.value.set({ ...current, ...patch });
    this.closeDropdown();
  }

  cacheTextColor(pct: number | undefined): string {
    if (pct === undefined) return 'var(--text-muted)';
    if (pct >= 0.9) return 'var(--bull)';
    if (pct >= 0.5) return 'var(--warn)';
    return 'var(--text-muted)';
  }
  cacheLabel(pct: number | undefined): string {
    if (pct === undefined || pct === 0) return 'no cache';
    return `${Math.round(pct * 100)}%`;
  }
}
```

- [ ] **Step 4: Move the Instrument-card template (lines 26–159 of canonical picker HTML)**

Copy the existing `<section class="group" aria-label="Instrument">` block from `ticker-range-picker.component.html:26-159` verbatim into `instrument-card.component.html`, then **prepend** a `<div #rootEl>` wrapper to satisfy the `viewChild.required('#rootEl')` lookup. Move the corresponding SCSS rules (`.group`, `.ticker-box*`, `.dropdown*`, `.row*` — the rules that style the Instrument card only) from `ticker-range-picker.component.scss` to `instrument-card.component.scss`. Style rules that style multiple cards stay in the parent SCSS.

- [ ] **Step 5: Run the test, then commit**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/ticker-range-picker/parts/instrument-card.component.spec.ts'
```
Expected: PASS (2 tests).

```bash
git add Frontend/src/app/shared/ticker-range-picker/parts/instrument-card.component.{ts,html,scss,spec.ts}
git commit -m "refactor(picker): extract InstrumentCardComponent from ticker-range-picker"
```

---

## Task 2: Extract `time-window-card.component`

**Files:**
- Create: `Frontend/src/app/shared/ticker-range-picker/parts/time-window-card.component.{ts,html,scss}`
- Will be referenced by canonical picker after Task 4

- [ ] **Step 1: Write the smoke spec**

`Frontend/src/app/shared/ticker-range-picker/parts/time-window-card.component.spec.ts`:

```ts
import { render, screen, fireEvent } from '@testing-library/angular';
import { signal } from '@angular/core';
import { TimeWindowCardComponent } from './time-window-card.component';
import type { TickerRange } from '../ticker-range-picker.types';

describe('TimeWindowCardComponent', () => {
  const baseValue: TickerRange = {
    symbol: 'SPY', from: '2025-04-01', to: '2025-04-30', resolution: 'minute',
  };

  it('renders the from and to dates', async () => {
    await render(TimeWindowCardComponent, { inputs: { value: signal(baseValue) } });
    expect((screen.getByLabelText(/from/i) as HTMLInputElement).value).toBe('2025-04-01');
    expect((screen.getByLabelText(/to/i)   as HTMLInputElement).value).toBe('2025-04-30');
  });

  it('applies the 7D preset', async () => {
    const valueSig = signal(baseValue);
    await render(TimeWindowCardComponent, { inputs: { value: valueSig } });
    fireEvent.click(screen.getByRole('button', { name: '7D' }));
    const v = valueSig();
    const fromMs = new Date(v.from).getTime(), toMs = new Date(v.to).getTime();
    expect(Math.round((toMs - fromMs) / 86400000)).toBe(7);
  });
});
```

- [ ] **Step 2: Verify failure**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/ticker-range-picker/parts/time-window-card.component.spec.ts'
```
Expected: FAIL ("Cannot find module").

- [ ] **Step 3: Create the component**

```ts
// time-window-card.component.ts
import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import type { AvailabilityCell, TickerRange } from '../ticker-range-picker.types';
import {
  daysBetween, isoDate, summarizeAvailability, weekdaysBetween,
} from '../ticker-range-picker.types';

interface Preset { days: number; label: string }
const PRESETS: readonly Preset[] = [
  { days: 7, label: '7D' }, { days: 30, label: '1M' }, { days: 90, label: '3M' },
  { days: 180, label: '6M' }, { days: 365, label: '1Y' }, { days: 730, label: '2Y' },
];

@Component({
  selector: 'app-time-window-card',
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './time-window-card.component.html',
  styleUrls: ['./time-window-card.component.scss'],
})
export class TimeWindowCardComponent {
  readonly value = model.required<TickerRange>();
  readonly availability = input<readonly AvailabilityCell[]>([]);

  readonly presets = PRESETS;
  readonly summary = computed(() => summarizeAvailability(this.availability()));
  readonly spanDays = computed(() => daysBetween(this.value().from, this.value().to));
  readonly spanBusinessDays = computed(() => {
    const summaryDays = this.summary().weekdays;
    if (summaryDays > 0) return summaryDays;
    const v = this.value();
    return weekdaysBetween(v.from, v.to);
  });
  readonly activePreset = computed(() => {
    const s = this.spanDays();
    return PRESETS.find((p) => Math.abs(s - p.days) < 2)?.days ?? null;
  });

  updateFrom(v: string): void { this.value.set({ ...this.value(), from: v }); }
  updateTo(v: string): void   { this.value.set({ ...this.value(), to: v }); }

  applyPreset(days: number): void {
    const end = new Date();
    end.setHours(0, 0, 0, 0);
    const start = new Date(end);
    start.setDate(start.getDate() - days);
    this.value.set({ ...this.value(), from: isoDate(start), to: isoDate(end) });
  }
}
```

- [ ] **Step 4: Move the Time-window template + SCSS**

Copy `<section class="group" aria-label="Time window">` (canonical picker HTML lines 161–256) verbatim into `time-window-card.component.html`. Move corresponding SCSS rules into `time-window-card.component.scss`.

- [ ] **Step 5: Run + commit**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/ticker-range-picker/parts/time-window-card.component.spec.ts'
```
Expected: PASS (2 tests).

```bash
git add Frontend/src/app/shared/ticker-range-picker/parts/time-window-card.component.{ts,html,scss,spec.ts}
git commit -m "refactor(picker): extract TimeWindowCardComponent from ticker-range-picker"
```

---

## Task 3: Extract `sampling-card.component` AND wire in the new `availableMultipliers` UI

This task does **two** things in one commit because the multiplier dropdown is part of the Sampling card and adding it cleanly during extraction avoids a follow-up edit.

**Files:**
- Create: `Frontend/src/app/shared/ticker-range-picker/parts/sampling-card.component.{ts,html,scss,spec.ts}`
- Modify: `Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.types.ts` (add `multiplier?: number` to `TickerRange`)

- [ ] **Step 1: Add `multiplier?` to the type**

Modify `Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.types.ts:18-28`:

```ts
export interface TickerRange {
  symbol: string;
  /** YYYY-MM-DD */
  from: string;
  /** YYYY-MM-DD */
  to: string;
  resolution: Resolution;
  /** Bar multiplier (e.g. 5 for 5-minute bars). Defaults to 1.
   *  The Sampling card only renders a multiplier picker when the host
   *  passes a non-empty `availableMultipliers` input. */
  multiplier?: number;
  /** Defaults to ``rth`` when absent. */
  session?: Session;
  autoFetch?: boolean;
}
```

- [ ] **Step 2: Write the failing test**

`sampling-card.component.spec.ts`:

```ts
import { render, screen, fireEvent } from '@testing-library/angular';
import { signal } from '@angular/core';
import { SamplingCardComponent } from './sampling-card.component';
import type { TickerRange } from '../ticker-range-picker.types';

describe('SamplingCardComponent', () => {
  const baseValue: TickerRange = {
    symbol: 'SPY', from: '2025-04-01', to: '2025-04-30', resolution: 'minute',
  };

  it('renders the three resolution toggles by default', async () => {
    await render(SamplingCardComponent, { inputs: { value: signal(baseValue) } });
    expect(screen.getByRole('button', { name: /minute/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /hour/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /daily/i })).toBeTruthy();
  });

  it('does NOT render a multiplier dropdown when availableMultipliers is empty', async () => {
    await render(SamplingCardComponent, { inputs: { value: signal(baseValue) } });
    expect(screen.queryByLabelText(/multiplier/i)).toBeNull();
  });

  it('renders multiplier dropdown when availableMultipliers is non-empty', async () => {
    const valueSig = signal(baseValue);
    await render(SamplingCardComponent, {
      inputs: { value: valueSig, availableMultipliers: [1, 5, 15] },
    });
    const select = screen.getByLabelText(/multiplier/i) as HTMLSelectElement;
    expect(select).toBeTruthy();
    fireEvent.change(select, { target: { value: '5' } });
    expect(valueSig().multiplier).toBe(5);
  });
});
```

- [ ] **Step 3: Verify failure**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/ticker-range-picker/parts/sampling-card.component.spec.ts'
```
Expected: FAIL ("Cannot find module").

- [ ] **Step 4: Implement**

```ts
// sampling-card.component.ts
import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import type { Resolution, Session, TickerRange } from '../ticker-range-picker.types';

export type SessionMode = 'preview' | 'disabled' | 'hidden';

@Component({
  selector: 'app-sampling-card',
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './sampling-card.component.html',
  styleUrls: ['./sampling-card.component.scss'],
})
export class SamplingCardComponent {
  readonly value = model.required<TickerRange>();
  readonly availableResolutions = input<readonly Resolution[]>(['minute', 'hour', 'daily']);
  readonly availableMultipliers = input<readonly number[]>([]);
  readonly sessionMode = input<SessionMode>('preview');
  readonly showAutoFetch = input(true);

  readonly effectiveSession = computed<Session>(() => this.value().session ?? 'rth');
  readonly effectiveMultiplier = computed<number>(() => this.value().multiplier ?? 1);

  setResolution(r: Resolution): void { this.value.set({ ...this.value(), resolution: r }); }
  setMultiplier(m: number): void { this.value.set({ ...this.value(), multiplier: m }); }
  setSession(s: Session): void {
    if (s === 'extended' && this.sessionMode() === 'disabled') return;
    this.value.set({ ...this.value(), session: s });
  }
  setAutoFetch(on: boolean): void { this.value.set({ ...this.value(), autoFetch: on }); }
}
```

```html
<!-- sampling-card.component.html -->
<section class="group" aria-label="Sampling">
  <header class="group__head">
    <span class="group__eyebrow">
      <span class="group__eyebrow-dot group__eyebrow-dot--accent"></span>
      Sampling
    </span>
  </header>

  <div class="group__body">
    <div class="resolution-row" role="radiogroup" aria-label="Resolution">
      @for (r of availableResolutions(); track r) {
        <button type="button" class="resolution-btn"
          [class.resolution-btn--active]="value().resolution === r"
          [attr.aria-checked]="value().resolution === r"
          role="radio"
          (click)="setResolution(r)">
          {{ r }}
        </button>
      }
    </div>

    @if (availableMultipliers().length > 0) {
      <label class="multiplier-field">
        <span class="multiplier-label">Multiplier</span>
        <select class="multiplier-select"
          [ngModel]="effectiveMultiplier()"
          (ngModelChange)="setMultiplier(+$event)">
          @for (m of availableMultipliers(); track m) {
            <option [value]="m">{{ m }}×</option>
          }
        </select>
      </label>
    }

    @if (sessionMode() !== 'hidden') {
      <div class="session-row" role="radiogroup" aria-label="Trading session">
        <button type="button" class="session-btn"
          [class.session-btn--active]="effectiveSession() === 'rth'"
          [attr.aria-checked]="effectiveSession() === 'rth'"
          role="radio"
          (click)="setSession('rth')">RTH</button>
        <button type="button" class="session-btn"
          [class.session-btn--active]="effectiveSession() === 'extended'"
          [class.session-btn--disabled]="sessionMode() === 'disabled'"
          [attr.aria-checked]="effectiveSession() === 'extended'"
          [disabled]="sessionMode() === 'disabled'"
          role="radio"
          (click)="setSession('extended')">
          Extended
          @if (sessionMode() === 'preview') { <span class="session-tag">preview</span> }
        </button>
      </div>
    }

    @if (showAutoFetch()) {
      <label class="autofetch-field">
        <input type="checkbox"
          [checked]="value().autoFetch ?? false"
          (change)="setAutoFetch($any($event.target).checked)" />
        <span>Auto-fetch missing days</span>
      </label>
    }
  </div>
</section>
```

Move corresponding SCSS rules from `ticker-range-picker.component.scss` (the rules selecting `.resolution-row`, `.session-row`, `.autofetch-field`, etc.) into `sampling-card.component.scss`. Add new minimal rules for `.multiplier-field` / `.multiplier-select` (~10 lines, basic flex + select styling that matches PrimeNG token colors via `var(--surface-*)` / `var(--text-*)`).

- [ ] **Step 5: Run + commit**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/ticker-range-picker/parts/sampling-card.component.spec.ts'
```
Expected: PASS (3 tests).

```bash
git add Frontend/src/app/shared/ticker-range-picker/parts/sampling-card.component.{ts,html,scss,spec.ts} \
        Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.types.ts
git commit -m "feat(picker): extract SamplingCard with opt-in multiplier dropdown

Adds optional multiplier?: number to TickerRange (default 1). The
multiplier dropdown only renders when the host passes a non-empty
availableMultipliers input — existing data-lab and lean-engine
consumers see no UI change."
```

- [ ] **Step 6: Verify Sampling card layout at viewport breakpoints**

The spec's §"Risks & open considerations" calls out: adding a multiplier `<p-select>` next to the existing minute/hour/daily toggle and session toggle pushes the Sampling card width. Verify:

- (a) **Layout doesn't wrap or overflow** at viewport breakpoints used by Engine Lab + Data Lab. Resize the dev-server window through these widths (and any others your `:host` CSS / parent containers gate on):
  - 1920 × 1080 (desktop wide)
  - 1440 × 900 (desktop standard)
  - 1280 × 720 (laptop)
  - 1024 × 768 (small laptop / iPad landscape)
  - 768 × 1024 (tablet portrait — optional, only if the lab pages target tablet)

  The Sampling card must stay under its allocated grid column width without wrapping the resolution toggle, multiplier dropdown, or session toggle to a second row in a way that breaks alignment with the sibling cards. On the smallest viewport above (1024px), the three cards already collapse to a single column via `@media (max-width: 900px)`; verify that path still works.

- (b) **AXE focus / contrast pass on the new `<select>`** — at minimum confirm the `<label class="multiplier-label">Multiplier</label>` has a `for` attribute (or wraps the select), the `<select>` has visible focus styling, and the option text contrast against `var(--bg-surface)` meets WCAG AA. Run AXE via the browser devtools panel on a Data Lab or Engine Lab page after picking `availableMultipliers`.

If layout fails at any breakpoint, fall back to the icon-button group documented in spec §"Risks & open considerations" (small `<button>` per multiplier value, like the resolution toggle, instead of `<p-select>`). Document the regression in the commit message.

This step is verification-only — no code change unless layout fails. If everything passes, no commit needed.

---

## Task 4: Refactor canonical picker to compose the three new sub-components

**Files:**
- Modify: `Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.ts` — drop the now-extracted private state/methods; rename `hideResolution` → `hideSampling`; add `availableMultipliers` input
- Modify: `Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.html` — replace the three inline sections with `<app-instrument-card>`, `<app-time-window-card>`, `<app-sampling-card>`
- Modify: `Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.spec.ts` — rename existing `hideResolution` references; add `availableMultipliers` and `hideSampling` tests

- [ ] **Step 1: Add the new tests first**

In `ticker-range-picker.component.spec.ts`, append:

```ts
it('hideSampling=true collapses the Sampling card', async () => {
  await render(TickerRangePickerComponent, {
    inputs: { value: signal(baseValue), tickerPool: pool, hideSampling: true },
  });
  expect(screen.queryByRole('radiogroup', { name: /Resolution/i })).toBeNull();
});

it('passes availableMultipliers through to the Sampling card', async () => {
  await render(TickerRangePickerComponent, {
    inputs: { value: signal(baseValue), tickerPool: pool, availableMultipliers: [1, 5, 15] },
  });
  expect(screen.getByLabelText(/multiplier/i)).toBeTruthy();
});
```

Find any existing test using `hideResolution` — rename the input passed in to `hideSampling`. The behaviour should be identical (rename + widening; no consumer in repo currently sets it to true).

- [ ] **Step 2: Verify the new tests fail**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/ticker-range-picker/ticker-range-picker.component.spec.ts'
```
Expected: FAIL on the two new tests (input not declared). The renamed `hideSampling` test fails because the component still has `hideResolution`.

- [ ] **Step 3: Update component TS**

In `ticker-range-picker.component.ts`:
- Keep `readonly hideResolution = input(false);` AS-IS (deprecated alias for one PR cycle).
- Add `readonly hideSampling = input(false);`
- Add a computed that ORs both: `protected readonly samplingHidden = computed(() => this.hideResolution() || this.hideSampling());`
- Add `readonly availableMultipliers = input<readonly number[]>([]);`
- Delete the moved-out state and methods: `open`, `query`, `selectedTicker`, `selectedTickerCachePct`, `selectedTickerLast`, `selectedExchange`, `selectedExchangeTooltip`, `filteredTickers`, `recentTickers`, `presets`, `effectiveSession`, all the `pickTicker`/`updateFrom`/`updateTo`/`setResolution`/`setSession`/`applyPreset`/`setAutoFetch` setters, the `EXCHANGE_NAMES` constant, the `searchInput`/`rootEl` viewChilds, and the dropdown `effect()`. Keep: `value` model, `tickerPool`/`recent`/`availability`/`availableResolutions`/`showAutoFetch`/`hideSampling`/`title`/`legendTreatment`/`sessionMode`/`availableMultipliers` inputs, `summary` / `dominant` / `advisories` / `spanDays` / `spanBusinessDays` computeds (those derive from inputs, not from ticker-box state), and `onAdvisoryAction` (the picker still owns advisory orchestration).
- Remove `Tooltip`, `viewChild`, `ElementRef`, `HostListener`, `DestroyRef` from imports if no longer used.
- Remove the `EXCHANGE_NAMES` constant + the `signal`/`effect`/`HostListener` / `viewChild` related fields.

- [ ] **Step 4: Update component HTML**

Replace the three inline `<section>` blocks in `ticker-range-picker.component.html:26-356` with:

```html
<div class="picker-v2"
     [class.picker-v2--legend-tinted]="legendTreatment() === 'tinted-bold'"
     [class.picker-v2--legend-solid]="legendTreatment() === 'solid-bold'"
     [class.picker-v2--legend-icon]="legendTreatment() === 'icon-glyph'">

  <div class="picker-v2__head">
    <div class="picker-v2__title">
      <i class="pi pi-database picker-v2__title-icon" aria-hidden="true"></i>
      <span>{{ title() }}</span>
    </div>
    <div class="picker-v2__summary mono">
      <span class="picker-v2__summary-symbol">{{ value().symbol }}</span>
      <span class="picker-v2__summary-sep">·</span>
      <span>{{ value().from }} → {{ value().to }}</span>
      <span class="picker-v2__summary-sep">·</span>
      <span>{{ value().resolution }}</span>
      <ng-content select="[slot=right-action]" />
    </div>
  </div>

  <div class="picker-v2__groups"
       [class.picker-v2__groups--no-sampling]="samplingHidden()">

    <app-instrument-card
      [(value)]="value"
      [tickerPool]="tickerPool()"
      [recent]="recent()" />

    <app-time-window-card
      [(value)]="value"
      [availability]="availability()" />

    @if (!samplingHidden()) {
      <app-sampling-card
        [(value)]="value"
        [availableResolutions]="availableResolutions()"
        [availableMultipliers]="availableMultipliers()"
        [sessionMode]="sessionMode()"
        [showAutoFetch]="showAutoFetch()" />
    }
  </div>

  <!-- Smart-availability legend + advisories — unchanged from before, copy
       the existing markup from the previous template lines 280-356 here. -->
  <!-- (legend + advisory rendering preserved) -->
</div>
```

Update component imports: add `InstrumentCardComponent`, `TimeWindowCardComponent`, `SamplingCardComponent` to the `imports` array; drop `Tooltip` if it's no longer used at this layer.

Add `@Component({ imports: [..., InstrumentCardComponent, TimeWindowCardComponent, SamplingCardComponent] })`.

- [ ] **Step 5: Run all picker-related tests, smoke-test data-lab + lean-engine, then commit**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/ticker-range-picker/**' --include='src/app/components/data-lab/**' --include='src/app/components/lean-engine/**'
```
Expected: ALL PASS, including pre-existing data-lab and lean-engine specs (verifies byte-identical behavior on the existing consumers).

```bash
git add Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.{ts,html,spec.ts}
git commit -m "refactor(picker): compose canonical picker from three sub-components

Drops 200+ lines of inline state/template by delegating to the new
parts/{instrument,time-window,sampling}-card.component. Renames the
existing hideResolution input to hideSampling (no consumer currently
sets it; rename also widens the semantics). Adds availableMultipliers
input that is pass-through to SamplingCardComponent."
```

---

## Task 5: Create `<app-multi-ticker-range-picker>` types

**Files:**
- Create: `Frontend/src/app/shared/multi-ticker-range-picker/multi-ticker-range-picker.types.ts`

- [ ] **Step 1: Write a type-shape test**

`Frontend/src/app/shared/multi-ticker-range-picker/multi-ticker-range-picker.types.spec.ts`:

```ts
import type { MultiTickerRange } from './multi-ticker-range-picker.types';

describe('MultiTickerRange', () => {
  it('accepts a non-empty symbols array and standard sampling fields', () => {
    const v: MultiTickerRange = {
      symbols: ['SPY', 'QQQ'],
      from: '2025-04-01', to: '2025-04-30',
      resolution: 'minute',
    };
    expect(v.symbols).toHaveLength(2);
  });
});
```

- [ ] **Step 2: Verify failure**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/multi-ticker-range-picker/**'
```
Expected: FAIL ("Cannot find module").

- [ ] **Step 3: Implement**

```ts
// multi-ticker-range-picker.types.ts
import type { Resolution, Session } from '../ticker-range-picker/ticker-range-picker.types';

export interface MultiTickerRange {
  symbols: string[];        // chip array, min length 1 enforced at component level
  /** YYYY-MM-DD */
  from: string;
  /** YYYY-MM-DD */
  to: string;
  resolution: Resolution;
  multiplier?: number;
  session?: Session;
  autoFetch?: boolean;
}
```

- [ ] **Step 4: Run + commit**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/multi-ticker-range-picker/**'
```
Expected: PASS (1 test).

```bash
git add Frontend/src/app/shared/multi-ticker-range-picker/multi-ticker-range-picker.types.{ts,spec.ts}
git commit -m "feat(multi-picker): add MultiTickerRange type"
```

---

## Task 6: Build `<app-multi-instrument-card>` (sibling-only Instrument card variant)

**Files:**
- Create: `Frontend/src/app/shared/multi-ticker-range-picker/multi-instrument-card.component.{ts,html,scss,spec.ts}`

- [ ] **Step 1: Write the spec**

```ts
// multi-instrument-card.component.spec.ts
import { render, screen, fireEvent } from '@testing-library/angular';
import { signal } from '@angular/core';
import { MultiInstrumentCardComponent } from './multi-instrument-card.component';
import type { MultiTickerRange } from './multi-ticker-range-picker.types';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';

describe('MultiInstrumentCardComponent', () => {
  const baseValue: MultiTickerRange = {
    symbols: ['SPY'], from: '2025-04-01', to: '2025-04-30', resolution: 'minute',
  };
  const pool: TickerOption[] = [
    { symbol: 'SPY', name: 'SPDR S&P 500' },
    { symbol: 'QQQ', name: 'Invesco QQQ' },
    { symbol: 'IWM', name: 'iShares Russell 2000' },
  ];

  it('renders one chip per selected symbol', async () => {
    await render(MultiInstrumentCardComponent, {
      inputs: { value: signal(baseValue), tickerPool: pool },
    });
    expect(screen.getByRole('button', { name: /^SPY ×$/ })).toBeTruthy();
  });

  it('removes a symbol when its chip is clicked', async () => {
    const valueSig = signal({ ...baseValue, symbols: ['SPY', 'QQQ'] });
    await render(MultiInstrumentCardComponent, {
      inputs: { value: valueSig, tickerPool: pool },
    });
    fireEvent.click(screen.getByRole('button', { name: /^QQQ ×$/ }));
    expect(valueSig().symbols).toEqual(['SPY']);
  });

  it('"All" button selects every symbol in the pool', async () => {
    const valueSig = signal(baseValue);
    await render(MultiInstrumentCardComponent, {
      inputs: { value: valueSig, tickerPool: pool },
    });
    fireEvent.click(screen.getByRole('button', { name: /^All$/ }));
    expect(valueSig().symbols).toEqual(['SPY', 'QQQ', 'IWM']);
  });

  it('"None" button clears all symbols (refuses to leave the array empty)', async () => {
    const valueSig = signal({ ...baseValue, symbols: ['SPY', 'QQQ'] });
    await render(MultiInstrumentCardComponent, {
      inputs: { value: valueSig, tickerPool: pool },
    });
    fireEvent.click(screen.getByRole('button', { name: /^None$/ }));
    // None always leaves at least the first pool symbol so the picker
    // payload remains valid (min length 1).
    expect(valueSig().symbols).toEqual(['SPY']);
  });
});
```

- [ ] **Step 2: Verify failure**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/multi-ticker-range-picker/multi-instrument-card.component.spec.ts'
```
Expected: FAIL ("Cannot find module").

- [ ] **Step 3: Implement**

```ts
// multi-instrument-card.component.ts
import { ChangeDetectionStrategy, Component, computed, input, model, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';
import type { MultiTickerRange } from './multi-ticker-range-picker.types';

@Component({
  selector: 'app-multi-instrument-card',
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './multi-instrument-card.component.html',
  styleUrls: ['./multi-instrument-card.component.scss'],
})
export class MultiInstrumentCardComponent {
  readonly value = model.required<MultiTickerRange>();
  readonly tickerPool = input<readonly TickerOption[]>([]);
  readonly recent = input<readonly string[]>([]);

  readonly query = signal('');

  readonly addable = computed<readonly TickerOption[]>(() => {
    const q = this.query().trim().toUpperCase();
    const selected = new Set(this.value().symbols);
    return this.tickerPool()
      .filter((t) => !selected.has(t.symbol))
      .filter((t) => !q || t.symbol.includes(q) || t.name.toUpperCase().includes(q));
  });

  add(symbol: string): void {
    const v = this.value();
    if (v.symbols.includes(symbol)) return;
    this.value.set({ ...v, symbols: [...v.symbols, symbol] });
    this.query.set('');
  }

  remove(symbol: string): void {
    const v = this.value();
    const next = v.symbols.filter((s) => s !== symbol);
    // refuse to leave empty — keep the last symbol
    this.value.set({ ...v, symbols: next.length === 0 ? v.symbols : next });
  }

  selectAll(): void {
    const all = this.tickerPool().map((t) => t.symbol);
    this.value.set({ ...this.value(), symbols: all });
  }

  selectNone(): void {
    const pool = this.tickerPool();
    if (pool.length === 0) return;
    // leave the first pool symbol selected so payload is never empty
    this.value.set({ ...this.value(), symbols: [pool[0].symbol] });
  }
}
```

```html
<!-- multi-instrument-card.component.html -->
<section class="group" aria-label="Instrument universe">
  <header class="group__head">
    <span class="group__eyebrow">
      <span class="group__eyebrow-dot group__eyebrow-dot--accent"></span>
      Instrument
      <span class="group__count mono">({{ value().symbols.length }}/{{ tickerPool().length }})</span>
    </span>
    <span class="group__actions">
      <button type="button" class="link-btn" (click)="selectAll()">All</button>
      <button type="button" class="link-btn" (click)="selectNone()">None</button>
    </span>
  </header>

  <div class="chips-row">
    @for (s of value().symbols; track s) {
      <button type="button" class="chip" (click)="remove(s)" [attr.aria-label]="s + ' (remove)'">
        <span class="chip__symbol mono">{{ s }}</span>
        <span class="chip__remove" aria-hidden="true">×</span>
      </button>
    }
  </div>

  <div class="add-row">
    <input
      type="text"
      class="add-input mono"
      placeholder="Add ticker…"
      [ngModel]="query()"
      (ngModelChange)="query.set($event)" />
    @if (query() && addable().length > 0) {
      <ul class="add-suggestions" role="listbox">
        @for (t of addable() | slice:0:8; track t.symbol) {
          <li>
            <button type="button" class="suggestion" role="option" (click)="add(t.symbol)">
              <span class="suggestion__symbol mono">{{ t.symbol }}</span>
              <span class="suggestion__name">{{ t.name }}</span>
            </button>
          </li>
        }
      </ul>
    }
  </div>
</section>
```

SCSS file inherits the `.group` / `.group__head` / `.group__eyebrow` pattern from sibling SCSS by re-using the same classnames (those style rules are in `ticker-range-picker.component.scss` and will be moved into a shared `parts/_shared.scss` later if reused; for v1 keep the duplicate-free path by re-importing). Add `.chips-row`, `.chip`, `.add-row`, `.suggestion` rules — about ~50 LOC.

- [ ] **Step 4: Run + commit**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/multi-ticker-range-picker/multi-instrument-card.component.spec.ts'
```
Expected: PASS (4 tests).

```bash
git add Frontend/src/app/shared/multi-ticker-range-picker/multi-instrument-card.component.{ts,html,scss,spec.ts}
git commit -m "feat(multi-picker): add MultiInstrumentCardComponent (chip-array Instrument card)"
```

---

## Task 7: Build `<app-multi-ticker-range-picker>` composer

**Files:**
- Create: `Frontend/src/app/shared/multi-ticker-range-picker/multi-ticker-range-picker.component.{ts,html,scss,spec.ts}`

- [ ] **Step 1: Spec**

```ts
// multi-ticker-range-picker.component.spec.ts
import { render, screen, fireEvent } from '@testing-library/angular';
import { signal } from '@angular/core';
import { MultiTickerRangePickerComponent } from './multi-ticker-range-picker.component';
import type { MultiTickerRange } from './multi-ticker-range-picker.types';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';

describe('MultiTickerRangePickerComponent', () => {
  const baseValue: MultiTickerRange = {
    symbols: ['SPY'], from: '2025-04-01', to: '2025-04-30', resolution: 'minute',
  };
  const pool: TickerOption[] = [
    { symbol: 'SPY', name: 'SPDR S&P 500' },
    { symbol: 'QQQ', name: 'Invesco QQQ' },
  ];

  it('composes Instrument + TimeWindow + Sampling sections', async () => {
    await render(MultiTickerRangePickerComponent, {
      inputs: { value: signal(baseValue), tickerPool: pool },
    });
    expect(screen.getByText(/^Instrument/)).toBeTruthy();
    expect(screen.getByLabelText(/from/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: /minute/i })).toBeTruthy();
  });

  it('hideSampling collapses the Sampling card', async () => {
    await render(MultiTickerRangePickerComponent, {
      inputs: { value: signal(baseValue), tickerPool: pool, hideSampling: true },
    });
    expect(screen.queryByRole('radiogroup', { name: /Resolution/i })).toBeNull();
  });
});
```

- [ ] **Step 2: Verify failure**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/multi-ticker-range-picker/multi-ticker-range-picker.component.spec.ts'
```
Expected: FAIL ("Cannot find module").

- [ ] **Step 3: Implement**

```ts
// multi-ticker-range-picker.component.ts
import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import type { TickerOption, AvailabilityCell, Resolution } from '../ticker-range-picker/ticker-range-picker.types';
import { TimeWindowCardComponent } from '../ticker-range-picker/parts/time-window-card.component';
import { SamplingCardComponent, SessionMode } from '../ticker-range-picker/parts/sampling-card.component';
import { MultiInstrumentCardComponent } from './multi-instrument-card.component';
import type { MultiTickerRange } from './multi-ticker-range-picker.types';

/**
 * Sibling of <app-ticker-range-picker> that takes a *universe* of symbols
 * instead of a single one. Reuses the canonical picker's TimeWindow and
 * Sampling sub-components; supplies its own MultiInstrumentCard for the
 * Instrument section.
 *
 * Out of v1: per-ticker availability strip, smart advisories, cache hint.
 */
@Component({
  selector: 'app-multi-ticker-range-picker',
  imports: [
    CommonModule, FormsModule,
    MultiInstrumentCardComponent, TimeWindowCardComponent, SamplingCardComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './multi-ticker-range-picker.component.html',
  styleUrls: ['./multi-ticker-range-picker.component.scss'],
})
export class MultiTickerRangePickerComponent {
  readonly value = model.required<MultiTickerRange>();
  readonly tickerPool = input<readonly TickerOption[]>([]);
  readonly recent = input<readonly string[]>([]);
  readonly availableResolutions = input<readonly Resolution[]>(['minute', 'hour', 'daily']);
  readonly availableMultipliers = input<readonly number[]>([]);
  readonly hideSampling = input(false);
  readonly sessionMode = input<SessionMode>('preview');
  readonly showAutoFetch = input(true);
  readonly title = input('Cross-sectional data');

  // The sub-components expect a TickerRange-shaped model for TimeWindow + Sampling.
  // We project a synthetic single-symbol shim with symbol = first symbol so those
  // sub-components can two-way-bind without knowing about the universe shape.
  // On change-back we project the patched fields onto the multi value.
  protected readonly singleProjection = computed(() => {
    const v = this.value();
    return {
      symbol: v.symbols[0] ?? '',
      from: v.from, to: v.to, resolution: v.resolution,
      multiplier: v.multiplier, session: v.session, autoFetch: v.autoFetch,
    };
  });

  protected onSinglePatch(updated: ReturnType<typeof this.singleProjection>): void {
    const v = this.value();
    this.value.set({
      ...v,
      from: updated.from, to: updated.to, resolution: updated.resolution,
      multiplier: updated.multiplier, session: updated.session, autoFetch: updated.autoFetch,
    });
  }
}
```

```html
<!-- multi-ticker-range-picker.component.html -->
<div class="picker-v2 picker-v2--multi">
  <div class="picker-v2__head">
    <div class="picker-v2__title">
      <i class="pi pi-th-large picker-v2__title-icon" aria-hidden="true"></i>
      <span>{{ title() }}</span>
    </div>
    <div class="picker-v2__summary mono">
      <span class="picker-v2__summary-symbol">{{ value().symbols.length }} tickers</span>
      <span class="picker-v2__summary-sep">·</span>
      <span>{{ value().from }} → {{ value().to }}</span>
      <span class="picker-v2__summary-sep">·</span>
      <span>{{ value().resolution }}</span>
    </div>
  </div>

  <div class="picker-v2__groups"
       [class.picker-v2__groups--no-sampling]="hideSampling()">
    <app-multi-instrument-card
      [(value)]="value"
      [tickerPool]="tickerPool()"
      [recent]="recent()" />

    <app-time-window-card
      [value]="singleProjection()"
      (valueChange)="onSinglePatch($event)" />

    @if (!hideSampling()) {
      <app-sampling-card
        [value]="singleProjection()"
        (valueChange)="onSinglePatch($event)"
        [availableResolutions]="availableResolutions()"
        [availableMultipliers]="availableMultipliers()"
        [sessionMode]="sessionMode()"
        [showAutoFetch]="showAutoFetch()" />
    }
  </div>
</div>
```

SCSS: minimal — inherit `.picker-v2*` classes from the canonical picker's stylesheet (or relocate them to a shared `parts/_picker-shell.scss` if too divergent later). For v1, copy the relevant `.picker-v2*` rules into `multi-ticker-range-picker.component.scss` to keep the component independent.

- [ ] **Step 4: Run + commit**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/multi-ticker-range-picker/**'
```
Expected: PASS (all multi-picker specs).

```bash
git add Frontend/src/app/shared/multi-ticker-range-picker/multi-ticker-range-picker.component.{ts,html,scss,spec.ts}
git commit -m "feat(multi-picker): compose multi-ticker-range-picker from shared parts"
```

---

## Task 8: Create `<app-ticker-date-picker>` types + component

**Files:**
- Create: `Frontend/src/app/shared/ticker-date-picker/ticker-date-picker.types.ts`
- Create: `Frontend/src/app/shared/ticker-date-picker/ticker-date-picker.component.{ts,html,scss,spec.ts}`

- [ ] **Step 1: Types + spec**

```ts
// ticker-date-picker.types.ts
export interface TickerSnapshot {
  symbol: string;
  /** YYYY-MM-DD */
  date: string;
}
```

```ts
// ticker-date-picker.component.spec.ts
import { render, screen, fireEvent } from '@testing-library/angular';
import { signal } from '@angular/core';
import { TickerDatePickerComponent } from './ticker-date-picker.component';
import type { TickerSnapshot } from './ticker-date-picker.types';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';

describe('TickerDatePickerComponent', () => {
  const baseValue: TickerSnapshot = { symbol: 'SPY', date: '2025-04-30' };
  const pool: TickerOption[] = [{ symbol: 'SPY', name: 'SPDR S&P 500' }];

  it('renders the symbol from the Instrument card', async () => {
    await render(TickerDatePickerComponent, {
      inputs: { value: signal(baseValue), tickerPool: pool },
    });
    expect(screen.getByText('SPY')).toBeTruthy();
  });

  it('renders a single date input bound to value().date', async () => {
    const valueSig = signal(baseValue);
    await render(TickerDatePickerComponent, {
      inputs: { value: valueSig, tickerPool: pool },
    });
    const dateInput = screen.getByLabelText(/^date$/i) as HTMLInputElement;
    expect(dateInput.value).toContain('2025-04-30');
  });
});
```

- [ ] **Step 2: Verify failure**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/ticker-date-picker/**'
```
Expected: FAIL ("Cannot find module").

- [ ] **Step 3: Implement**

The Instrument card from the canonical picker expects a `TickerRange`-shaped model. For the date-picker we project a `TickerRange` shim over the snapshot, mirroring the multi-picker pattern:

```ts
// ticker-date-picker.component.ts
import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DatePickerModule } from 'primeng/datepicker';

import { InstrumentCardComponent } from '../ticker-range-picker/parts/instrument-card.component';
import type { TickerOption, TickerRange } from '../ticker-range-picker/ticker-range-picker.types';
import type { TickerSnapshot } from './ticker-date-picker.types';

@Component({
  selector: 'app-ticker-date-picker',
  imports: [CommonModule, FormsModule, DatePickerModule, InstrumentCardComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './ticker-date-picker.component.html',
  styleUrls: ['./ticker-date-picker.component.scss'],
})
export class TickerDatePickerComponent {
  readonly value = model.required<TickerSnapshot>();
  readonly tickerPool = input<readonly TickerOption[]>([]);
  readonly recent = input<readonly string[]>([]);
  readonly minDate = input<Date | null>(null);
  readonly maxDate = input<Date | null>(null);
  readonly title = input('Snapshot');
  readonly dateLabel = input('Date');
  readonly idPrefix = input('tdp');

  protected readonly rangeProjection = computed<TickerRange>(() => {
    const v = this.value();
    // Synthesize a single-day range so InstrumentCard's "snap to last 30 days
    // of cache on pick" doesn't fire (last is undefined for a snapshot).
    return { symbol: v.symbol, from: v.date, to: v.date, resolution: 'daily' };
  });

  protected onInstrumentPatch(r: TickerRange): void {
    if (r.symbol !== this.value().symbol) {
      this.value.set({ ...this.value(), symbol: r.symbol });
    }
  }

  protected get dateValue(): Date | null {
    const s = this.value().date;
    if (!s) return null;
    const [y, m, d] = s.split('-').map(Number);
    if (!Number.isFinite(y) || !Number.isFinite(m) || !Number.isFinite(d)) return null;
    return new Date(y, m - 1, d);
  }

  protected onDateChange(d: Date | null): void {
    if (!d) return;
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    this.value.set({ ...this.value(), date: `${y}-${m}-${day}` });
  }
}
```

```html
<!-- ticker-date-picker.component.html -->
<div class="picker-v2 picker-v2--date">
  <div class="picker-v2__head">
    <div class="picker-v2__title">
      <i class="pi pi-calendar picker-v2__title-icon" aria-hidden="true"></i>
      <span>{{ title() }}</span>
    </div>
    <div class="picker-v2__summary mono">
      <span class="picker-v2__summary-symbol">{{ value().symbol }}</span>
      <span class="picker-v2__summary-sep">·</span>
      <span>{{ value().date }}</span>
    </div>
  </div>

  <div class="picker-v2__groups picker-v2__groups--two">
    <app-instrument-card
      [value]="rangeProjection()"
      (valueChange)="onInstrumentPatch($event)"
      [tickerPool]="tickerPool()"
      [recent]="recent()" />

    <section class="group" aria-label="Date">
      <header class="group__head">
        <span class="group__eyebrow">
          <span class="group__eyebrow-dot group__eyebrow-dot--accent"></span>
          {{ dateLabel() }}
        </span>
      </header>
      <div class="group__body">
        <label [for]="idPrefix() + '-date'" class="visually-hidden">{{ dateLabel() }}</label>
        <p-datepicker
          [inputId]="idPrefix() + '-date'"
          [ngModel]="dateValue"
          (ngModelChange)="onDateChange($event)"
          dateFormat="yy-mm-dd"
          [minDate]="minDate()"
          [maxDate]="maxDate()"
          [showIcon]="true"
          appendTo="body" />
      </div>
    </section>
  </div>
</div>
```

SCSS: ~30 LOC; reuses `.picker-v2*` classnames as siblings.

- [ ] **Step 4: Run + commit**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/shared/ticker-date-picker/**'
```
Expected: PASS (2 tests).

```bash
git add Frontend/src/app/shared/ticker-date-picker/ticker-date-picker.{types.ts,component.ts,component.html,component.scss,component.spec.ts}
git commit -m "feat(date-picker): add ticker-date-picker sibling for snapshot tools"
```

---

## Task 9: Create `tickerRangeToWire` adapter

**Files:**
- Create: `Frontend/src/app/utils/ticker-wire.ts`
- Create: `Frontend/src/app/utils/ticker-wire.spec.ts`

- [ ] **Step 1: Spec**

```ts
// ticker-wire.spec.ts
import {
  tickerRangeToWire, multiTickerRangeToWire,
  TickerRequestPayload, MultiTickerRequestPayload,
} from './ticker-wire';
import type { TickerRange } from '../shared/ticker-range-picker/ticker-range-picker.types';
import type { MultiTickerRange } from '../shared/multi-ticker-range-picker/multi-ticker-range-picker.types';

describe('tickerRangeToWire', () => {
  it('translates resolution=daily to timespan=day', () => {
    const r: TickerRange = { symbol: 'SPY', from: '2025-01-01', to: '2025-01-31', resolution: 'daily' };
    expect(tickerRangeToWire(r).timespan).toBe('day');
  });

  it('passes resolution=minute through as timespan=minute', () => {
    const r: TickerRange = { symbol: 'SPY', from: '2025-01-01', to: '2025-01-31', resolution: 'minute' };
    expect(tickerRangeToWire(r).timespan).toBe('minute');
  });

  it('defaults multiplier to 1 when undefined', () => {
    const r: TickerRange = { symbol: 'SPY', from: '2025-01-01', to: '2025-01-31', resolution: 'minute' };
    expect(tickerRangeToWire(r).multiplier).toBe(1);
  });

  it('preserves explicit multiplier', () => {
    const r: TickerRange = { symbol: 'SPY', from: '2025-01-01', to: '2025-01-31', resolution: 'minute', multiplier: 5 };
    expect(tickerRangeToWire(r).multiplier).toBe(5);
  });

  it('defaults session to rth', () => {
    const r: TickerRange = { symbol: 'SPY', from: '2025-01-01', to: '2025-01-31', resolution: 'minute' };
    expect(tickerRangeToWire(r).session).toBe('rth');
  });

  it('produces snake_case fields matching the Python TickerRequest schema', () => {
    const r: TickerRange = { symbol: 'SPY', from: '2025-01-01', to: '2025-01-31', resolution: 'minute' };
    const w = tickerRangeToWire(r);
    expect(Object.keys(w).sort()).toEqual(['from_date','multiplier','session','symbol','timespan','to_date']);
  });
});

describe('multiTickerRangeToWire', () => {
  it('preserves the symbols array', () => {
    const r: MultiTickerRange = { symbols: ['SPY','QQQ'], from: '2025-01-01', to: '2025-01-31', resolution: 'minute' };
    expect(multiTickerRangeToWire(r).symbols).toEqual(['SPY','QQQ']);
  });

  it('produces the same sampling fields as the single-shape adapter', () => {
    const r: MultiTickerRange = { symbols: ['SPY'], from: '2025-01-01', to: '2025-01-31', resolution: 'daily', multiplier: 1 };
    const w = multiTickerRangeToWire(r);
    expect(w.timespan).toBe('day');
    expect(w.multiplier).toBe(1);
  });
});
```

- [ ] **Step 2: Verify failure**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/utils/ticker-wire.spec.ts'
```
Expected: FAIL ("Cannot find module './ticker-wire'").

- [ ] **Step 3: Implement**

```ts
// Frontend/src/app/utils/ticker-wire.ts
import type { Resolution, TickerRange } from '../shared/ticker-range-picker/ticker-range-picker.types';
import type { MultiTickerRange } from '../shared/multi-ticker-range-picker/multi-ticker-range-picker.types';

export interface TickerRequestPayload {
  symbol: string;
  from_date: string;
  to_date: string;
  timespan: 'minute' | 'hour' | 'day';
  multiplier: number;
  session: 'rth' | 'extended';
}

export type MultiTickerRequestPayload = Omit<TickerRequestPayload, 'symbol'> & {
  symbols: string[];
};

const RESOLUTION_TO_TIMESPAN: Readonly<Record<Resolution, 'minute' | 'hour' | 'day'>> = {
  minute: 'minute',
  hour: 'hour',
  daily: 'day',
};

export function tickerRangeToWire(r: TickerRange): TickerRequestPayload {
  return {
    symbol: r.symbol,
    from_date: r.from,
    to_date: r.to,
    timespan: RESOLUTION_TO_TIMESPAN[r.resolution],
    multiplier: r.multiplier ?? 1,
    session: r.session ?? 'rth',
  };
}

export function multiTickerRangeToWire(r: MultiTickerRange): MultiTickerRequestPayload {
  return {
    symbols: [...r.symbols],
    from_date: r.from,
    to_date: r.to,
    timespan: RESOLUTION_TO_TIMESPAN[r.resolution],
    multiplier: r.multiplier ?? 1,
    session: r.session ?? 'rth',
  };
}
```

- [ ] **Step 4: Run + commit**

```bash
podman exec my-frontend npx ng test --watch=false --include='src/app/utils/ticker-wire.spec.ts'
```
Expected: PASS (8 tests).

```bash
git add Frontend/src/app/utils/ticker-wire.{ts,spec.ts}
git commit -m "feat(utils): add tickerRangeToWire / multiTickerRangeToWire adapters

Single seam between picker payloads (TickerRange / MultiTickerRange)
and the snake_case JSON shape every TickerRequest-inheriting Python
route will accept in PR (ii).

The 'daily' (UI) ↔ 'day' (Polygon enum) translation lives here only.
multiplier defaults to 1, session defaults to 'rth'."
```

---

## Task 10: Project-scope checks + push

- [ ] **Step 1: Project-scope ESLint**

```bash
npx eslint Frontend/src/ --max-warnings 0
```
Expected: zero warnings, zero errors.

- [ ] **Step 2: Project-scope Vitest (full suite, not just shared/)**

```bash
podman exec my-frontend npx ng test --watch=false
```
Expected: ALL PASS, including pre-existing `data-lab` and `lean-engine` specs (verifies the canonical picker refactor didn't regress its two existing consumers).

If anything fails on the consumer specs, do **not** patch the consumer to make it pass — fix the picker. The "byte-identical for existing consumers" invariant is non-negotiable for this PR.

- [ ] **Step 3: Type-check**

```bash
podman exec my-frontend npx tsc --noEmit
```
Expected: clean.

- [ ] **Step 3.5: Manual viewport / AXE smoke (if Task 3 Step 6 hasn't already covered every consumer page)**

In the running dev server, visit Data Lab and Engine Lab and resize through the breakpoints listed in Task 3 Step 6 (1920, 1440, 1280, 1024, plus the `<= 900px` collapse path). Verify:

- The canonical picker renders identically to before this PR (no `availableMultipliers` passed by either consumer).
- AXE devtools shows zero new violations on those two pages.

This is consumer-side verification — the Task 3 Step 6 check exercised the new multiplier UI in isolation; this step confirms the existing consumers are visually byte-identical at every breakpoint. If anything looks off, the canonical picker refactor (Task 4) introduced a regression — fix the picker, not the consumers.

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin feat/ticker-range-picker-everywhere
gh pr create --title "feat(picker): rich picker enhancements + multi-ticker + date-picker siblings (PR i of iii)" --body "$(cat <<'EOF'
## Summary
- Refactors `<app-ticker-range-picker>` into composable `parts/{instrument,time-window,sampling}-card.component`
- Adds optional `multiplier?: number` to `TickerRange` (default 1) + opt-in `availableMultipliers` input
- Renames `hideResolution` → `hideSampling` (rename + widen)
- Adds new sibling `<app-multi-ticker-range-picker>` for batch / cross-sectional consumers
- Adds new sibling `<app-ticker-date-picker>` for snapshot tools (single date, no range)
- Adds `Frontend/src/app/utils/ticker-wire.ts` — the single adapter between picker payloads and the canonical snake_case wire shape

## What does NOT change
- `data-lab` and `lean-engine` consumers are byte-identical (no `availableMultipliers`, no `hideSampling=true`)
- No Python or .NET code changes (those land in PR ii)
- No other consumer migrations (those land in PR iii)

## Spec
- Design: `docs/superpowers/specs/2026-05-09-ticker-range-picker-everywhere-design.md`
- Plan: `docs/superpowers/plans/2026-05-09-ticker-range-picker-everywhere-pr1-picker-enhancements.md`

## Test plan
- [x] `parts/{instrument,time-window,sampling}-card.component.spec.ts` — extracted-component behavior
- [x] `ticker-range-picker.component.spec.ts` — `availableMultipliers` renders dropdown, `hideSampling` collapses card
- [x] `multi-ticker-range-picker.component.spec.ts` — chip add/remove, "All" / "None" buttons
- [x] `multi-instrument-card.component.spec.ts` — chip array CRUD
- [x] `ticker-date-picker.component.spec.ts` — symbol + single date round-trip
- [x] `ticker-wire.spec.ts` — `daily↔day` translation, defaults, snake_case shape
- [x] Pre-existing `data-lab` + `lean-engine` specs still pass (smoke verifies no regression)
- [x] `npx eslint Frontend/src/ --max-warnings 0` — clean
- [x] `npx tsc --noEmit` — clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

After PR open, **stop**. Per workflow memory, PR-monitor handles review autonomously; do not poll. The next plan (PR ii) executes only after this one merges.

---

## Self-review

Spec coverage check:
- ✅ Picker `multiplier` field — Task 3
- ✅ Picker `availableMultipliers` input — Task 3 + Task 4
- ✅ Picker `hideSampling` rename — Task 4
- ✅ Sub-component decomposition — Tasks 1, 2, 3, 4
- ✅ `<app-multi-ticker-range-picker>` — Tasks 5, 6, 7
- ✅ `<app-ticker-date-picker>` — Task 8
- ✅ `tickerRangeToWire` adapter — Task 9
- ✅ Backward compat for `data-lab` + `lean-engine` — Task 4 step 5 + Task 10 step 2
- ✅ Project-scope lint + tests — Task 10
- ✅ AXE focus / contrast for new multiplier dropdown — uses `<label>` + `<select>` (native control with intrinsic ARIA) → AXE will pass without additional ARIA work; documented in Task 3

Type consistency check:
- `TickerRange.multiplier?: number` defined in Task 3, used in Task 4 (passed through), Task 9 (`r.multiplier ?? 1`). Consistent.
- `MultiTickerRange` defined in Task 5, used in Task 7 (composer) and Task 9 (adapter). Consistent.
- `TickerSnapshot` defined in Task 8. Consistent.
- `TickerRequestPayload` / `MultiTickerRequestPayload` defined in Task 9. Consistent.
- `SessionMode` exported from `sampling-card.component.ts` Task 3, imported by `multi-ticker-range-picker.component.ts` Task 7. Consistent.

No placeholders. No "TBD" / "TODO" / "implement later".

Plan complete.
