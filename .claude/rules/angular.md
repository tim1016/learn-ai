---
paths:
  - "Frontend/**"
---

# Angular rules

Targets Angular 22.

**Authoritative reference**: https://angular.dev (current version docs). When this file conflicts with angular.dev, angular.dev wins and this file should be updated.

## Non-negotiables

- Zoneless is the default. New code must not depend on Zone.js side effects.
- All components use `ChangeDetectionStrategy.OnPush`.
- All components are standalone (do NOT set `standalone: true` — it's the default and setting it is noise).
- Signals for state. `input()` and `output()` functions, not decorators.
- Modern control flow only: `@if`, `@for`, `@switch`.
- Every `@for` must have `track` — prefer a stable ID over `$index`.
- Use `inject()` for DI. No constructor injection.
- Use `[class.foo]` and `[style.color]` bindings. Never `ngClass` or `ngStyle`.
- Never use `*ngIf`, `*ngFor`, `ngSwitch`, `@Input()`, `@Output()`, `@HostBinding`, `@HostListener`, or Template-driven forms.
- Never use `mutate()` on signals. Use `set()` or `update()`.

## Async data

- Prefer `resource()` and `rxResource()` for async loading over manual patterns.
- Use `toSignal()` when you already have an observable.
- When a subscription is unavoidable in a component, use `takeUntilDestroyed()` inside an injection context, or pass a `DestroyRef`.
- Prefer `firstValueFrom()` over `.toPromise()`.
- Prefer the `async` pipe over `.subscribe()` in components.

## Forms

- **Signal Forms** for new forms.
- **Reactive Forms** still valid for complex existing forms. Don't migrate just to migrate.
- Never Template-driven forms.

## Shared renderers

- Raw backend identifiers in Frontend receipt/evidence UI (`reason_code`, `gate_id`, `source`, receipt labels, and known code-like receipt values such as `NO_LIVE_BINDING` or `broker.connection`) must render through the shared `receiptLabel` pipe. Preserve opaque audit tokens such as intent/order IDs, paths, hashes, refs, and URLs exactly. Do not pipe backend-authored trader/operator prose; that copy should arrive from the backend or from a closed operator-copy map.
- Tradeable-instrument symbols in Frontend display UI must render through shared `app-asset-identity`, the canonical symbol renderer. Use its compact size in dense rows; plain symbol text remains appropriate in controls such as `<option>` elements and ticker pickers. Do not duplicate logo, slug-resolution, or initials-fallback logic in feature components.

## Symbol picking (ADR 0066)

- Every symbol input uses the shared instrument card (`app-instrument-card`) or a picker
  wrapper (`app-ticker-range-picker`, `app-multi-ticker-range-picker`). Never hand-roll a
  ticker input, a suggestion list, or a membership map — `TICKER_LABELS` is display
  metadata, never membership.
- The default universe is the joined catalog (`SymbolCatalogService`): every listed
  US-equity symbol from the data-plane's Polygon reference catalog (`GET /api/tickers/catalog`),
  with per-row lake coverage (held span / "not held" / "delisted"). Rows render through
  `app-asset-identity`.
- An unheld pick is gated on its lake backfill inside the card (`EnsureCoverageService`);
  the selection emits only after the lake — re-read, not the job's word — confirms the
  bars landed. Hosts never implement their own backfill gating.
- A host-supplied `universe` input (single card) or typed `options` input (the multi-symbol
  card) is a closed list the host owns outright (no gate); use it only when membership
  genuinely is the host's — e.g. the backfill panel adapts the joined catalog through its
  own delisted policy, because the panel is the bulk path the gate defers to.
- When the vendor catalog is dark, the picker degrades visibly (banner + lake holdings +
  retry). Never substitute a canned symbol list for a failed read. The vendor catalog is
  read once per tab: `view.retryVendor()` re-fetches it; `view.reload()` refreshes only
  the lake's coverage on a dropdown open.
- A symbol-only host binds `app-symbol-picker` (`[(symbol)]`, `adjustmentMode`) instead of
  projecting a `TickerRange` by hand; a multi-symbol host whose runs read the lake passes
  the multi card an `adjustmentMode` so unheld picks are gated, while a host that owns its
  membership outright (the backfill panel; the Observatory's read-only coverage query)
  leaves the mode unset and the list closed.

## Routing

- Lazy-loaded routes via `loadComponent` / `loadChildren`.
- Functional guards and resolvers, not class-based.
- Use route data as signals.
- Component input binding: route params flow into `input()` signals directly.

## Templates

- Keep templates under ~80 lines. Extract child components when exceeded.
- Use `@let` to bind intermediate values and avoid awkward type narrowing.
- If type safety breaks in the template, fix it with a getter or `computed()` in the TS file, not a type assertion in the template.
- Inline templates for components under ~10 lines. External templates otherwise, with paths relative to the TS file.

## Styling

- SCSS per component, co-located.
- CSS custom properties for theming. Tailwind where it already exists; don't introduce it to files that don't use it.

## Accessibility

- All UI must pass AXE.
- WCAG AA minimums: focus management, color contrast, ARIA.
- `NgOptimizedImage` for static images (not base64 inline).
- Every interactive control has an accessible name.

## TypeScript

- Strict mode. No `any`. Use `unknown` when type is uncertain.
- Prefer inference when obvious; annotate when not.
- No type assertions without justification (`as X` is a smell; use type guards or narrowing).

## File naming

- Components: `kebab-case.component.ts` / `.html` / `.scss` / `.spec.ts`
- Services: `kebab-case.service.ts`
- Guards: `kebab-case.guard.ts`
- Resolvers: `kebab-case.resolver.ts`
- Models: co-located or in `models/` folder, `kebab-case.ts`

## Common pitfalls (v22-specific)

- Safe-navigation expressions that distinguish `null` from `undefined`; preserve the former behavior explicitly when Angular's migration flags them.
