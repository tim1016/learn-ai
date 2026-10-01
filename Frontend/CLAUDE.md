# Frontend — Angular 22 SPA

## Commands

| Action     | Command                                        |
|------------|------------------------------------------------|
| Run        | `podman compose up frontend` (localhost:4200)  |
| Test       | `npx ng test --include='src/app/<path>/<name>.spec.ts'` (host, one exact spec) |
| Build      | `podman exec my-frontend npx ng build`         |
| Type-check | `podman exec my-frontend npx tsc --noEmit`     |
| Lint       | `npx eslint src/` (what CI runs)               |
| Logs       | `podman logs -f my-frontend`                   |

Frontend tests are **independent** — no backend or database needed.

## Key Patterns

- **Raw `HttpClient` POSTs** to the .NET GraphQL endpoint (no Apollo) — each service posts `{ query, variables }` itself; the canonical `GraphQLResponse<T>` / `GraphqlError` types are in `shared/graphql/graphql-error.ts`, response types in `graphql/types.ts`
- **PrimeNG** for UI components + **Tailwind CSS** for utility styling
- **TradingView lightweight-charts v5** for OHLCV candlestick charts (`chart.addSeries(CandlestickSeries, options)`)
- API proxy: `/graphql` proxied via the canonical `proxy.conf.js`; it defaults to host loopback targets and Compose overrides those targets with service names.

## Testing

- **Vitest** via `@angular/build:unit-test` builder (configured in `angular.json`)
- Setup file: `src/test-setup.ts` (stubs ResizeObserver, Canvas, matchMedia)
- Test behavior, not implementation — assert rendered output, not signal values
- Spec files co-located: `*.component.spec.ts`, `*.service.spec.ts`
- Locally, run one exact spec (`npx ng test --include='src/app/<path>/<name>.spec.ts'`); CI runs the rest. To reproduce one CI shard, set `TEST_SHARD_INDEX`/`TEST_SHARD_COUNT` to match `.github/workflows/ci.yml`'s `frontend-test-shard` matrix (e.g. `TEST_SHARD_INDEX=1 TEST_SHARD_COUNT=6 npm test`); `scripts/run-test-budget.cjs` then auto-appends `--runner-config=vitest.ci.config.ts`. Unsharded, `npm test` runs the whole suite and can OOM on a memory-capped container. `NG_BUILD_MAX_WORKERS` defaults to `2` (overridable) either way.

## Gotchas

- `proxy.conf.js` is the only approved dev proxy configuration. It routes host development to loopback ports by default; Compose sets `BACKEND_PROXY_TARGET` and `DATA_PLANE_PROXY_TARGET` to container service names. Do not replace it with a target-only JSON proxy: that bypasses the data-plane control-header hook. It attaches the Python data-plane control header from `DATA_PLANE_CONTROL_SECRET` only for Angular-marked unsafe control mutations and protected broker-session reads with positive same-origin local-dev browser provenance; metadata-absent local clients are intentionally not given the proxy secret.
- PrimeNG 22 uses PrimeUI licensing. Set `primeUiLicense` in the gitignored environment override before production use; the checked-in examples show the required field.
- `tsconfig.json` excludes spec files; `tsconfig.spec.json` includes them for test builds
