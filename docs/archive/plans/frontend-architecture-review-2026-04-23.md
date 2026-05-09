> **Status:** Archived — stale architecture review (2026-04-23), superseded.
> **Do not use as implementation authority.**
> Current authority: `.claude/rules/angular.md` (Angular 21 conventions).
> Archived because: Angular 21 conventions now captured in .claude/rules/angular.md, which supersedes ad-hoc review notes.

# Frontend architecture review — 2026-04-23

Scope: Apollo orchestration, signal-driven data fetching, leak-proof routing, zoneless migration. The original brief was written against a more Apollo-heavy assumption — see "What changed and why" for what was actually applicable.

## Summary of changes

| Area | Before | After |
|---|---|---|
| GraphQL transport | `HttpClient.post` inline in every service, no central error surfacing | New `GqlClient` (imperative) and `gqlResource` (signal-driven) under `Frontend/src/app/shared/graphql/`, both auto-toast server errors |
| Toast surface | None | `MessageService` provided in `app.config.ts`, `<p-toast position="top-right" />` mounted in `AppComponent` |
| Change detection | Zone-based (`zone.js@~0.15.0` polyfill) | Zoneless (`provideZonelessChangeDetection()`); `zone.js` removed from `angular.json` polyfills (`polyfills: []`); shipped polyfill bundle dropped to ~95 B |
| Router | `provideRouter(routes, withInMemoryScrolling(...))` | `+ withExperimentalAutoCleanupInjectors()` so per-route environment injectors are torn down on route exit |
| OnPush coverage | 92 / 97 components | 97 / 97 (added: `authors`, `books`, `summary-stats`, `tickers`, `tradingview-widget`) |
| App config drift | `app.config.ts` hardcoded `http://localhost:5000/graphql` while every service used `environment.backendUrl` | Both sides now use `environment.backendUrl` |
| Date helpers | Inline `new Date().toISOString().slice(0,10)` and `Date.now() + 180 * 86400000` (DST-unsafe) in `market-data.service.ts` | Extracted to `todayDateString()` / `dateStringMonthsFromNow(months)` in `utils/date-validation.ts`; the `setMonth` form is DST-safe |
| Demo migration | All consumers used `firstValueFrom(service.method())` | Snapshots → Single-Ticker tab now consumes `gqlResource` end-to-end (signal params, signal value, signal isLoading, auto-toast errors) |

All 423 unit tests pass under zoneless. New tests: `gql-client.spec.ts` (5), `gql-resource.spec.ts` (4). Updated specs: `app.component.spec.ts`, `authors.component.spec.ts`, `books.component.spec.ts`, `summary-stats.component.spec.ts` (signal-input via `setInput`).

Production bundle builds cleanly. Frontend dev container serves `/snapshots` 200 OK. **Manual browser verification of the toast firing on a deliberate server error was not performed** — I have no browser automation in this environment. Recommend a 30-second smoke test: open `/snapshots`, mistype the URL of the Python backtest endpoint to force a 500, confirm the red toast lands at top-right.

## What changed and why — clarifying the original brief

The original review framed everything around Apollo cache integrity, normalization, `cache-first` vs `cache-and-network`, and `readQuery`/`writeQuery` atomicity. Reality on inspection: **Apollo was wired in `app.config.ts` but was not actually used by any market-data path** — every service (`market-data.service.ts`, `polygon.service.ts`, `portfolio.service.ts`, etc.) was posting raw GraphQL through `HttpClient`. The only Apollo consumers were `BookService` and `AuthorService`, which back the unrouted `BooksComponent`/`AuthorsComponent` demo pages.

The brief also asserted that `withExperimentalAutoCleanupInjectors()` was a v21 router feature. I doubted this initially because I couldn't find it in `@angular/core`. It is in `@angular/router` (`router.d.ts:780`, marked `@experimental 21.1`). It's now wired up, with a caveat below in "Notes on `withExperimentalAutoCleanupInjectors`".

## New surface API

### `gqlResource<TData, TVars>(query, params, options?)`

Signal-driven GraphQL fetcher. Wraps Angular's first-party `httpResource()`. Returns an `HttpResourceRef<TData | undefined>` exposing `.value()`, `.status()`, `.error()`, `.isLoading()`, `.reload()` as signals.

- `params` is reactive — when it returns `undefined`, the request is skipped (idle state), enabling "fetch on demand" patterns by toggling a sentinel signal.
- Server-side `errors[]` are mapped to a `GraphqlError` thrown out of `parse`, surfaced on `.error()`.
- An internal `effect()` toasts via `MessageService` whenever `.error()` flips truthy. Toast is suppressible via `options.suppressToast`.
- Must be called from an injection context (component field initializer, `inject()` call site).

Reference port: `snapshots.component.ts` Single-Ticker tab.

### `GqlClient` (imperative)

For call sites that need a `Promise<TData>` rather than a resource — primarily existing services we're not migrating in this pass. Same error → toast contract.

```ts
private gql = inject(GqlClient);
const data = await this.gql.post<MyData, MyVars>(QUERY, vars, { errorContext: 'My fetch' });
```

## Notes on `withExperimentalAutoCleanupInjectors`

It is wired in `app.config.ts`. Per its JSDoc at `@angular/router/types/router.d.ts:766-779`:

> When enabled, the router will automatically destroy `EnvironmentInjector`s associated with `Route`s that are no longer active or stored by the `RouteReuseStrategy`. **This feature is opt-in and requires `RouteReuseStrategy.shouldDestroyInjector` to return `true`** for the routes that should be destroyed.

We have no custom `RouteReuseStrategy` registered — the default strategy returns `false` for `shouldReuseRoute`/`shouldDetach` and `true` for `shouldDestroyInjector` (verify against the active default). This means with the default strategy, leaving a route should drop its injector. **If a custom `RouteReuseStrategy` is added later, override `shouldDestroyInjector` to keep this guarantee.**

## Pre-existing issues found while working

- **`Frontend/src/app/components/data-lab/data-lab.component.ts:127`** — was missing `PageHeaderComponent` from the `imports` array while using `<app-page-header>` in its template. Pre-existing on master; broke the dev-server build. Fixed as a one-line patch (added the import) to unblock verification.
- **`tap(...)` blocks throw inside `tap` to signal errors throughout `MarketDataService`** (e.g. lines 614-618, 646-650, etc.). The throws do reach the consumer's error channel, but it's a fragile idiom — consumers must wrap every call. The new `GqlClient` handles this correctly. Migrating MarketDataService onto `GqlClient` is the recommended cleanup.
- **313 pre-existing eslint warnings** (mostly `@typescript-eslint/no-explicit-any` and forbidden non-null assertions in spec files). The repo's lint script is `eslint src/ --max-warnings 0`, so the CI lint is currently broken regardless of this branch. Out of scope for this work.

## Recommendations / next steps

In rough priority order:

### 1. Migrate the rest of `MarketDataService` onto `GqlClient`
Each method becomes one line: `return this.gql.post<T>(QUERY, variables, { errorContext: 'Aggregates' })`. Removes the `tap-to-throw` pattern, removes per-method `GraphQLResponse<...>` interfaces, removes per-call try/catch in consumers. Estimated 1–2 hours, covered by existing service specs once they're updated to inject `GqlClient` instead of `HttpClient`.

### 2. Centralize GraphQL queries
Today, query strings live both inline in `MarketDataService` and (for the demo) inline in `snapshots.component.ts`. Move all to `Frontend/src/app/graphql/queries.ts` (file already exists for the demo book/author queries). Tag with `gql\`...\`` if you keep apollo-angular as a transport later, otherwise plain template strings work.

### 3. Decide on Apollo
Two options:
- **Delete it.** `BookService`/`AuthorService` and the demo `BooksComponent`/`AuthorsComponent` (unrouted) get migrated to `GqlClient` and deleted respectively. Drop `apollo-angular`, `@apollo/client`, `provideApollo`, `BookService`/`AuthorService`. Smaller bundle. Cleaner.
- **Adopt it.** If we want a normalized cache for cross-component data sharing (e.g. an OptionContract showing in both the chain view and the strategy lab), invest. Migration plan:
  - Wire every service onto `Apollo.watchQuery` instead of `HttpClient.post`.
  - Replace `gqlResource` with a `cacheResource` that subscribes to `watchQuery.valueChanges` and exposes the same signal contract.
  - Introduce `keyFields` policies on `OptionContract`, `Ticker`, `Aggregate` so cache hits work across queries.
  - Define the freshness contract: snapshots/quotes → `network-only`; historical aggregates on closed days → `cache-first`; intraday → `cache-and-network`; mutations → no cache.
  - Add the deferred test: cross-query normalization (Greeks under `OptionContract` returned by both `getOptionsContracts` and `getOptionsChainSnapshot` must merge).

Recommendation: **Delete.** The current per-page-fetch model works for a research tool, and we save ~80 kB. Revisit when shared real-time state actually appears.

### 4. Memory-leak structural test
Skipped per scoping. When wanted, the cheap-and-reliable form:

```ts
it('does not retain components after navigation', async () => {
  const refs: WeakRef<unknown>[] = [];
  for (let i = 0; i < 50; i++) {
    const fixture = TestBed.createComponent(SnapshotsComponent);
    refs.push(new WeakRef(fixture.componentInstance));
    fixture.destroy();
  }
  await new Promise(r => setTimeout(r, 50));
  if (typeof globalThis.gc === 'function') globalThis.gc();
  const retained = refs.filter(r => r.deref() !== undefined).length;
  expect(retained).toBeLessThan(5); // GC is non-deterministic; allow slack
});
```

This catches obvious retention (subscriptions held by long-lived services, missing `takeUntilDestroyed`). It will not catch DOM-level leaks; for those you need real browser tooling (Playwright + heap snapshots in CI).

### 5. Cross-query normalization test
Only meaningful once Apollo (or some normalized cache) is the data layer. Then:

```ts
// Both queries return the same option contract; assert it's a single object reference in cache.
await client.query({ query: GET_OPTIONS_CONTRACTS, variables: { underlyingTicker: 'AAPL' } });
await client.query({ query: GET_OPTIONS_CHAIN_SNAPSHOT, variables: { underlyingTicker: 'AAPL' } });
const fromContracts = client.cache.readFragment({ id: 'OptionContract:AAPL241115C00200000', fragment: ContractFragment });
const fromChain = client.cache.readFragment({ id: 'OptionContract:AAPL241115C00200000', fragment: ContractFragment });
expect(fromContracts).toBe(fromChain); // referential equality => single normalized entity
```

### 6. `@Input()` decorator → `input()` function migration
~25 components still use the legacy decorator. The Angular CLI has a schematic: `ng generate @angular/core:signal-input-migration`. Run, review, commit per-component.

### 7. Lint cleanup pass
313 warnings, mostly typed-`any` and forbidden non-null in spec files. Either fix or move `--max-warnings 0` off CI temporarily — the current state means lint CI must already be skipped.

### 8. Delete unrouted demo components
`AuthorsComponent`/`BooksComponent` and their services exist for learning but are not wired into routes. Either route them under `/dev/` for documentation or delete. They currently pull `apollo-angular` into the bundle.
