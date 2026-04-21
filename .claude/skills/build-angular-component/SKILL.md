---
name: build-angular-component
description: Build or modify an Angular 21 component in the Frontend. Use when user says "create a component", "add a chart", "build a page", "update the component", "make an Angular", or asks for any frontend UI work.
---

# Build Angular Component

Build or modify a component in `Frontend/src/`. Targets Angular 21: zoneless by default, signals-first, standalone-only, Vitest as the test runner, Signal Forms where appropriate.

## When to use

- Any new Angular component, directive, or pipe
- Modifications to existing components
- Chart components (typically using `lightweight-charts`)
- Form components (Signal Forms preferred for new work)

## Before starting

1. Read `Frontend/.claude/rules/angular.md` if present, or the root `.claude/rules/angular.md` fallback.
2. Search for an existing component that does something similar. Don't duplicate patterns.
3. Identify whether this is a **presentation** component (pure inputs → template) or a **container** component (injects services, manages state). Build the two separately if the logic is non-trivial.

## Angular 21 conventions (critical)

- **Zoneless by default**. New apps don't ship Zone.js. Any code relying on Zone.js side effects (e.g., `setTimeout` triggering change detection automatically) is a bug. Use signals.
- **`standalone: true` is implicit**. Do NOT set `standalone: true` in `@Component` — it's the default in v20+, setting it explicitly is noise.
- **Signals everywhere**: `signal()` for state, `computed()` for derived, `effect()` for side effects.
- **`input()` and `output()` functions**, not `@Input()` / `@Output()` decorators.
- **`model()` for two-way binding** instead of `input()` + `output()` pairs.
- **`resource()` / `rxResource()` for async** — prefer over manual `toSignal(httpClient...)` patterns.
- **`inject()` for DI**, not constructor injection.
- **Modern control flow**: `@if`, `@for`, `@switch`. Every `@for` must include `track` (prefer a stable ID over `$index`).
- **`@let` in templates** to avoid type narrowing awkwardness.
- **`ChangeDetectionStrategy.OnPush`** on every component.
- **No `@HostBinding` / `@HostListener` decorators**. Use the `host` object on `@Component` instead.
- **No `ngClass` / `ngStyle`**. Use `[class.foo]` and `[style.color]` bindings.
- **No `*ngIf` / `*ngFor` / `ngSwitch`** — use the `@if` / `@for` / `@switch` control flow.

## File organization

Co-located files per component:

```
feature-name/
  feature-name.component.ts
  feature-name.component.html
  feature-name.component.scss
  feature-name.component.spec.ts
```

Naming: `kebab-case.component.ts`. Services: `kebab-case.service.ts`. Models: in a sibling `models/` folder or co-located.

## Component template

```typescript
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { MarketDataService } from '../services/market-data.service';

@Component({
  selector: 'app-ema-chart',
  templateUrl: './ema-chart.component.html',
  styleUrl: './ema-chart.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class EmaChartComponent {
  private readonly marketData = inject(MarketDataService);

  readonly symbol = input.required<string>();
  readonly period = input<number>(10);

  private readonly rawData = signal<number[] | null>(null);

  readonly isLoading = computed(() => this.rawData() === null);
  readonly dataPoints = computed(() => this.rawData()?.length ?? 0);

  // effects, data loading, etc.
}
```

Template uses `@let`, `@if`, `@for` with `track`. Avoid embedding complex expressions; if type safety fights you in the template, extract a getter or a `computed()` in the TS file.

## Forms

- **Signal Forms** (new in v21) for new forms. These are signal-based, reactive, and simpler than `ReactiveFormsModule` for most cases.
- **Reactive Forms** (`ReactiveFormsModule`) still valid for complex, multi-field forms with heavy validation. Never use Template-driven forms.
- Never use Template-driven forms (`ngModel`, `FormsModule`).

## Charts

For trading charts, prefer `lightweight-charts`. Wrap it in a thin component that:

- Accepts data as an `input()` signal
- Uses an `effect()` to push data into the chart instance
- Tears down the chart in `ngOnDestroy` (or via `takeUntilDestroyed()` equivalent)
- Does NOT use `ChangeDetectionStrategy.Default`

## Accessibility

- Must pass all AXE checks
- WCAG AA minimums: focus management, color contrast, ARIA attributes
- Use `NgOptimizedImage` for static images (not for base64 inline images)
- Every interactive control has an accessible name

## Testing

- **Vitest** is the default test runner in v21.
- Use **Angular Testing Library** (`@testing-library/angular`): `render()` + `screen` queries.
- Test **behavior**, not implementation. Assert what the user sees, not internal signal values.
- Mock services at the DI level via `providers: [{ provide: MarketDataService, useValue: fakeService }]`.
- Name: `*.component.spec.ts`.

Example:

```typescript
import { render, screen } from '@testing-library/angular';
import { EmaChartComponent } from './ema-chart.component';
import { MarketDataService } from '../services/market-data.service';

it('shows a loading state while data is fetching', async () => {
  const fakeService = { getEma: () => signal(null) } as unknown as MarketDataService;
  await render(EmaChartComponent, {
    inputs: { symbol: 'SPY' },
    providers: [{ provide: MarketDataService, useValue: fakeService }],
  });
  expect(screen.getByText(/loading/i)).toBeInTheDocument();
});
```

## Routing (if the component is a page)

- **Lazy-loaded** via `loadComponent` in the route config.
- **Functional guards** and resolvers, not class-based.
- **Route data as signals** (v21 feature); prefer over `ActivatedRoute` subscribe patterns.

## Output

Report:

- Component path and selector
- Inputs, outputs, injected services
- Template size (if over ~80 lines, propose splitting)
- Tests added
- Any accessibility concerns found and how they were addressed

## Anti-patterns to avoid

- `*ngIf`, `*ngFor`, `ngSwitch` (use `@if`, `@for`, `@switch`)
- `ngClass`, `ngStyle` (use class/style bindings)
- `@Input()`, `@Output()` decorators (use `input()`, `output()` functions)
- Setting `standalone: true` explicitly (it's the default)
- `ChangeDetectionStrategy.Default` on new components
- `@HostBinding` / `@HostListener` (use the `host` object)
- Subscribing in components when `toSignal` or `resource` would work
- Mutating signals with `.mutate()` — use `.set()` or `.update()`
- Template-driven forms
- Type assertions in templates — fix with a getter or `@let`
