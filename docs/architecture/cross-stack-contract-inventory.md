# Active Cross-Stack Contract Inventory

**Status:** Current as of issue #1126 (2026-07-20). This is an ownership
inventory, not a replacement for the generated schemas. The complete endpoint
set is in `contracts/openapi/python-data-service.openapi.json`; the complete
GraphQL surface is in `contracts/graphql/backend.schema.graphql`.

## Route selection

`Frontend/proxy.conf.js` makes the production topology explicit:

- `/graphql` → .NET Backend.
- `/api/jobs` → .NET Backend's job API (which may dispatch work to Python).
- all other `/api` requests → PythonDataService, with the control-plane proxy
  authorization hook applied where required.

Therefore the browser has two normal data paths. The route, rather than a
preference for one stack, determines which generated contract applies.

## Angular → GraphQL/.NET → FastAPI

| Browser owner | GraphQL operation / .NET owner | Python dependency | Contract status |
| --- | --- | --- | --- |
| `spec-strategy.service.ts` | `runSpecStrategyBacktest` / `SpecStrategyMutation` | `/api/spec-strategy/backtest` | Generated GraphQL operation result plus generated OpenAPI strategy input aliases; shared fixture verifies the Python → .NET response and GraphQL projection. |
| `graphql/queries.ts` consumers | `getOrFetchStockAggregates` / `Query` + `PolygonService` | `/api/aggregates/fetch` | Python response is typed at the Pydantic boundary; shared fixture verifies Python → .NET deserialization and `int64 ms UTC` bars. |
| `backtest-runs.query.ts`, Engine Lab history/report components | Backtest run queries and notes mutation | Backend persistence/projections; no live Python request on the read path | GraphQL-only today; listed so it is not misclassified as a FastAPI relay. |

## Angular → FastAPI, direct

| Browser owner(s) | Python route family | Contract status |
| --- | --- | --- |
| `broker.service.ts`, `broker-health.service.ts`, broker control components | `/api/broker/**`, `/api/accounts/**` | OpenAPI source of truth. `DataPlaneHealth` is now a generated alias; broker SSE streams remain the documented non-OpenAPI exception. |
| `broker-session-mirror.service.ts` | `/api/broker/session-mirror/**` | Direct REST/SSE; REST is OpenAPI-generated, streamed payloads are hand-owned by the Python SSE models. |
| `live-runs.service.ts`, bot-control/account-desk stores | `/api/live-runs/**`, `/api/live-instances/**`, `/api/lifecycle-projection/**`, `/api/accounts/**` | Direct FastAPI control-plane surface, protected by the proxy intent/secret policy. |
| `strategy-validation.service.ts`, `live-runs.service.ts` | `/api/strategy-validation/**`, `/api/engine/strategies`, `/api/spec-strategy/fixtures/**` | Direct FastAPI; generated schema is available for the next typed migration slices. |
| `strategy-runs.service.ts`, `baselines.service.ts`, `monte-carlo.service.ts`, `walk-forward.service.ts` | `/api/research/strategy-runs/**` | Direct FastAPI research-run contracts. |
| `lean-sidecar.service.ts` | `/api/lean-sidecar/**` | Direct FastAPI comparison boundary. |
| `market-monitor.service.ts`, `golden-fixtures.service.ts` | `/api/market/**`, `/api/golden-fixtures/**` | Direct FastAPI read boundaries. |
| Edge and research-lab API services | `/api/edge/**`, `/api/research/**`, `/api/data-quality/**`, `/api/dataset/**` | Direct FastAPI analysis boundaries. |

## Angular → .NET jobs → FastAPI

`jobs.service.ts` and `run-session.service.ts` use `/api/jobs/**`, which is a
.NET job-control API. Its dispatch to Python is an internal backend-to-service
boundary, not a browser-to-FastAPI direct route. It remains in the inventory
because a future GraphQL or OpenAPI migration must not bypass its job identity,
authorization, event stream, or download semantics.

## Exceptions and next migrations

- `broker-models.ts` still contains handwritten SSE event envelopes and some
  historical REST shapes. Each REST type should move to a generated alias when
  its owning slice is touched; SSE must retain a named Python-model reference
  and a fixture/test.
- Existing handwritten `gql` documents in `queries.ts` and
  `backtest-runs.query.ts` remain functional legacy operations. New operations
  use `Frontend/src/app/graphql/*.graphql`; migrating the legacy documents is
  incremental work, not a silent behavior change in issue #1126.
- No browser-facing API was moved between direct FastAPI and GraphQL in this
  issue. ADR 0031 makes that boundary choice explicit.
