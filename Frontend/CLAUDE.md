# Frontend — Angular 21 SPA

## Commands

| Action     | Command                                        |
|------------|------------------------------------------------|
| Run        | `podman compose up frontend` (localhost:4200)  |
| Test       | `podman exec my-frontend npx ng test`          |
| Build      | `podman exec my-frontend npx ng build`         |
| Type-check | `podman exec my-frontend npx tsc --noEmit`     |
| Lint       | `npx eslint Frontend/src/ --max-warnings 0`    |
| Logs       | `podman logs -f my-frontend`                   |

Frontend tests are **independent** — no backend or database needed.

## File Structure

```
src/app/
├── components/          # 23 feature directories
│   ├── market-data/     # OHLCV dashboard, candlestick charts
│   ├── portfolio/       # Event-sourced portfolio tracker
│   ├── options-chain-v2/        # Options chain viewer
│   ├── options-strategy-lab/    # Multi-leg strategy builder
│   ├── strategy-lab/            # Strategy backtesting UI
│   ├── technical-analysis/      # Indicator overlays
│   ├── pricing-lab/             # Black-Scholes pricer UI
│   ├── research-lab/            # Research experiment runner
│   ├── data-quality/            # Data validation dashboards
│   ├── lean-engine/             # Lean engine integration UI
│   └── ...                      # ticker-explorer, snapshots, etc.
├── services/            # 13 injectable services
│   ├── polygon.service.ts       # Polygon.io REST client
│   ├── market-data.service.ts   # GraphQL market data queries
│   ├── portfolio.service.ts     # GraphQL portfolio mutations/queries
│   ├── replay-engine.service.ts # Backtest replay orchestration
│   └── ...
├── graphql/             # Apollo Client type definitions
│   ├── queries.ts               # Market data GQL queries
│   ├── types.ts                 # Generated/manual GQL types
│   └── portfolio-types.ts       # Portfolio GQL types
├── models/              # Shared TypeScript interfaces
├── shared/              # Reusable directives, helpers
└── utils/               # Pure utility functions (black-scholes, date-validation)
```

## Key Patterns

- **Standalone components** with `ChangeDetectionStrategy.OnPush`
- **Signals** for state: `signal()`, `computed()`, `input()`, `output()`, `inject()`
- **Apollo Angular** for GraphQL — queries in `graphql/queries.ts`, types in `graphql/types.ts`
- **PrimeNG** for UI components + **Tailwind CSS** for utility styling
- **TradingView lightweight-charts v5** for OHLCV candlestick charts (`chart.addSeries(CandlestickSeries, options)`)
- Modern control flow: `@if`, `@for` (with `track`), `@switch`, `@let`
- API proxy: `/graphql` proxied to backend container via `proxy.conf.js`
- Receipt/evidence identifiers render through the shared `receiptLabel` pipe. Preserve opaque audit tokens such as intent/order IDs, paths, hashes, refs, and URLs exactly. Backend-authored trader/operator prose stays unpiped.

## Testing

- **Vitest** via `@angular/build:unit-test` builder (configured in `angular.json`)
- Setup file: `src/test-setup.ts` (stubs ResizeObserver, Canvas, matchMedia)
- Test behavior, not implementation — assert rendered output, not signal values
- Spec files co-located: `*.component.spec.ts`, `*.service.spec.ts`

## Gotchas

- `proxy.conf.js` routes `/graphql` to `http://backend:8080` inside the container network. It attaches the Python data-plane control header from `DATA_PLANE_CONTROL_SECRET` only for Angular-marked unsafe control mutations and protected broker-session reads with positive same-origin local-dev browser provenance; metadata-absent local clients are intentionally not given the proxy secret.
- Some components are large (options-strategy-lab, strategy-builder) — consider extracting child components
- `tsconfig.json` excludes spec files; `tsconfig.spec.json` includes them for test builds
