---

# Architectural Review: Market Data Dashboard (learn-ai)

## 1. Overall System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    Angular 21 Frontend                            │
│   Apollo GraphQL · TradingView Charts · PrimeNG · Tailwind CSS   │
│                      Port 4200                                    │
└─────────────────────────┬────────────────────────────────────────┘
                          │ GraphQL over HTTP
┌─────────────────────────▼────────────────────────────────────────┐
│              .NET 10 GraphQL Backend (Hot Chocolate v15)          │
│   Query/Mutation · EF Core 10 · Polly Retry+CB · 14 Services     │
│                   Port 5000 → 8080 internal                      │
└───────┬────────────────────────────┬─────────────────────────────┘
        │ HTTP REST (Polly)          │ TCP (Npgsql)
┌───────▼───────────────┐    ┌───────▼──────────────────┐
│  Python FastAPI        │    │  PostgreSQL 16            │
│  Polygon.io SDK        │    │  47 entity tables         │
│  pandas-ta · scipy     │    │  512MB limit              │
│  40+ endpoints         │    │  Named pgdata volume      │
│  Port 8000             │    │  Port 5432                │
└───────┬───────────────┘    └──────────────────────────┘
        │ HTTPS
┌───────▼───────────────┐
│  Polygon.io REST API   │
│  (Starter plan)        │
└────────────────────────┘
```

**Technology rationale**: Each layer is well-chosen for its role:
- **Angular + Apollo**: Type-safe frontend with GraphQL introspection, signals for fine-grained reactivity, lazy-loaded routes for bundle splitting
- **.NET + Hot Chocolate**: Strongly-typed backend with projections/filtering/sorting pushed to EF Core, Polly for resilience against external service failures
- **Python + FastAPI**: Natural home for pandas-ta, scipy, and the Polygon Python SDK — numerical computing that would be painful in .NET
- **PostgreSQL**: ACID transactions for portfolio accounting, JSONB for flexible data lab sessions, strong indexing for time-series queries

**Communication flow**: Frontend → GraphQL (single endpoint) → .NET services → Python REST (compute-heavy) + PostgreSQL (persistence). The .NET layer acts as an orchestrator and cache, deciding whether to serve from DB or fetch fresh data.

---

## 2. Frontend Analysis

### Structure
- **22 feature components** organized by domain: market data, options, portfolio (8 sub-components), research lab (8 sub-components), data quality, strategy
- **13 services** for data access, all returning `Observable<T>` with `providedIn: 'root'`
- **26 lazy-loaded routes** via `loadComponent` — good code splitting
- **Standalone components** throughout (no NgModules) with `OnPush` change detection

### State Management
Angular Signals used extensively (~991 occurrences of signal/computed/effect/toSignal). Pattern: `signal()` for local state, `computed()` for derived values, `effect()` for triggering fetches when inputs change. RxJS (~142 `.subscribe/.pipe` occurrences) persists in services and some components — a transitional dual-pattern.

### GraphQL Integration
Two patterns coexist:
1. **Raw HTTP POST with string queries** — `market-data.service.ts` (948 lines of inline GraphQL strings)
2. **Helper `gql()` function** — `portfolio.service.ts` (cleaner, DRY)

Apollo Client is configured but several services bypass it for raw `HttpClient.post()`, losing cache benefits.

### Strengths
- Modern Angular 21 with signals, standalone, OnPush — excellent performance characteristics
- Comprehensive TypeScript types (492-line `types.ts`, 296-line `portfolio-types.ts`)
- Domain-rich components: Black-Scholes calculator, Greeks visualization, FIFO position replay
- PrimeNG + Tailwind + TradingView lightweight-charts is a strong UI toolkit

### Weaknesses
- **Inconsistent GraphQL client usage**: Some services use Apollo, others use raw HTTP, losing caching/dedup
- **Large components**: `options-strategy-lab` (822 lines), `strategy-builder` (870 lines) exceed the 80-line template guideline
- **No path aliases**: `tsconfig.json` has empty `paths: {}` — deep relative imports
- **Hardcoded API URL**: `http://localhost:5000/graphql` — no environment-based configuration

---

## 3. Backend Analysis (GraphQL/.NET)

### Schema Design
The schema is split across 6 type extensions — clean separation by domain:
- **Query** (market data): smart fetch, indicators, progress tracking
- **PortfolioQuery**: accounts, positions, trades, valuation, risk, metrics, attribution
- **DataLabQuery**: session management
- **Mutation**: fetch, sanitize, backtest
- **PortfolioMutation**: orders, trades, positions, snapshots, risk rules, scenarios
- **DataLabMutation**: session CRUD

All mutations return typed result objects (`{Success, Data, Error}`) instead of throwing — good for client error handling. The schema supports 50+ operations.

### Service Layer (14 services, ~8,500 lines)
Clean interface-based DI with Scoped lifetime. Key services:

| Service | Responsibility | Lines |
|---------|---------------|-------|
| **MarketDataService** | Fetch/cache orchestration with gap detection | 515 |
| **PolygonService** | HTTP client to Python with snake_case deserialization | 572 |
| **BacktestService** | In-memory strategy execution (SMA crossover, RSI, momentum) | 716 |
| **PositionEngine** | FIFO lot accounting, deterministic rebuild | 229 |
| **PortfolioService** | Orders, trades, cash management | 274 |
| **PortfolioValuationService** | Mark-to-market with Greeks aggregation | 158 |
| **PortfolioRiskService** | Dollar delta, vega, scenario analysis | 254 |
| **ResearchService** | Feature engineering via Python | 598 |
| **PortfolioValidationService** | 12-test integration suite (stress test, FIFO, invariants) | 790 |

### EF Core Usage
- **47 DbSets** across market data, portfolio, options, and research domains
- `EnsureCreated()` for initialization — **no migrations tracked** (known risk: adding entities requires volume reset)
- Decimal precision `(18,8)` for all monetary fields — correct for financial data
- Cascade deletes on FK relationships
- Composite indexes on `(TickerId, Timestamp, Timespan)` for aggregate queries
- Batch inserts with change tracker clearing every 1000 records — good memory management
- `AsNoTracking()` not consistently used for read-only queries

### Concerns
- **10-minute execution timeout** is very long — could mask slow queries
- **Exception details exposed** (`IncludeExceptionDetails = true`) — security risk in production
- **No input validation at GraphQL layer** — validation happens deep in service logic
- **No transaction scope management** — portfolio operations (order → trade → position) should be atomic

---

## 4. Python Data Service

### Role
The FastAPI service (40+ endpoints across 12 routers) serves as:
1. **Polygon.io proxy** — wraps the SDK, adds sanitization, handles pagination
2. **Compute engine** — pandas-ta for 50+ indicators, scipy for Black-Scholes, statsmodels for research
3. **Data pipeline** — sanitization (outlier clipping, null fill, OHLCV integrity), dataset generation with session filtering

### API Design
Clean router → service → polygon_client separation. Pydantic models validate all inputs (automatic 422 for bad requests). Responses follow `{success, data, error}` pattern consistently.

### Rate Limiting & Error Handling
- `MAX_REQUESTS_PER_MINUTE: 100` is **configured but not enforced** — the code relies on Polygon SDK internals
- ThreadPoolExecutor with 3x retries and exponential backoff for options expirations — good
- API key sanitized in logs (`***` replacement)
- Global exception handler returns structured JSON errors

### Concerns
- **No request-level caching** — every call hits Polygon.io. A short-lived in-memory or Redis cache would reduce API usage significantly
- **No observability** — no Prometheus metrics, request timing, or structured logging for production
- **Flat IV assumption** — strategy engine uses Black-Scholes without volatility surface interpolation (noted in `options-fragilities.md`)

---

## 5. Database & Caching

### Schema Design
PostgreSQL tables are well-indexed for the access patterns:
- **Time-series**: `(TickerId, Timestamp, Timespan)` composite index on StockAggregates
- **Portfolio**: `(AccountId, ExecutionTimestamp)` for trade history, `(AccountId, Timestamp)` for snapshots
- **Options**: `(UnderlyingTickerId, Strike, Expiration, OptionType)` composite + unique `Symbol` index
- **JSONB**: DataLabSession uses PostgreSQL-native JSON for flexible indicator research storage

### Caching Strategy
Two-tier approach:
- **L1 (PostgreSQL)**: `GetOrFetchAggregatesAsync()` checks DB first → cache hit returns with gap detection → cache miss triggers Polygon fetch → upsert to DB
- **L2 (In-memory)**: `ConcurrentDictionary` for fetch progress tracking (transient)
- **No TTL**: No time-based expiration. Historical data is treated as immutable; `forceRefresh=true` bypasses cache.

### Gaps
- No Redis or distributed cache — scaling to multiple backend instances would cause duplicate fetches
- No cache warming — first request for any ticker/date always hits Polygon
- Gap detection is smart (computes missing dates, coverage %) but doesn't auto-fill gaps

---

## 6. DevOps & Deployment

### Containerization
3-service Podman Compose with health checks, resource limits, and dependency ordering:

| Service | Memory | CPU | Health Check |
|---------|--------|-----|--------------|
| PostgreSQL 16 | 512MB max | - | `pg_isready` every 5s |
| Python FastAPI | 2GB max / 512MB reserved | 2 cores | curl `/health` every 10s |
| .NET Backend | 1GB max | - | curl `/health` every 10s |

Dockerfiles are production-quality:
- .NET uses multi-stage build (SDK → runtime image)
- Python splits `requirements-heavy.txt` (scipy/pandas) from `requirements-light.txt` (fastapi/polygon) for layer caching
- Both use slim base images

### Build & Deploy
```bash
podman compose down && podman compose up -d --build  # Full rebuild
```
**No CI/CD pipeline exists** — no GitHub Actions, no automated testing, no image registry. Tests must be run manually.

### Scaling Limitations
- Single PostgreSQL instance, no replication
- No horizontal scaling configuration
- No load balancer or reverse proxy
- Frontend runs via `ng serve` in dev (no production containerization)

---

## 7. Testing Strategy

### Current Coverage

| Layer | Framework | Test Files | Patterns |
|-------|-----------|-----------|----------|
| **Frontend** | Vitest + jsdom | 56 `.spec.ts` | Component + service unit tests, `HttpTestingController` mocks |
| **Backend** | xUnit + Moq | 20+ test classes | In-memory EF Core, `FakeHttpMessageHandler`, Arrange-Act-Assert |
| **Python** | pytest + anyio | 20+ test files | AsyncClient + ASGITransport, sample data generators |

Additionally, `PortfolioValidationService` (790 lines) runs 12 integration tests as a GraphQL mutation — a creative in-app validation suite covering FIFO accounting, cash integrity, drawdown math, risk rules, and a 1000-trade stress test.

### Gaps & Recommendations
1. **No integration tests** that exercise the full stack (Frontend → GraphQL → Python → Polygon mock)
2. **No contract tests** between .NET and Python services — JSON schema drift could silently break
3. **No performance tests** — the backtest engine and indicator calculations should have benchmark tests
4. **No mutation testing** — coverage numbers don't measure test quality
5. **Add CI pipeline** with `dotnet test`, `pytest`, `ng test` as mandatory gates

---

## 8. Security Review

### Authentication/Authorization
**None implemented.** The system has no auth layer — all GraphQL operations are publicly accessible. This is acceptable for a local development/learning project but blocks any production deployment.

### Vulnerabilities & Risks

| Risk | Severity | Location |
|------|----------|----------|
| No authentication | **Critical** (for prod) | All endpoints |
| Exception details exposed in GraphQL | High | `IncludeExceptionDetails = true` in Program.cs |
| Hardcoded DB password in appsettings.json | Medium | `Backend/appsettings.json` |
| CORS allows any header/method | Medium | `Program.cs` CORS policy |
| No rate limiting on GraphQL | Medium | Backend accepts unlimited queries |
| API keys in `.env` (not rotated/vaulted) | Low | `.env` file (gitignored) |
| No input sanitization at GraphQL boundary | Low | Mutations accept raw strings |

### Positive Security Practices
- `.env` properly gitignored
- API keys sanitized in Python service logs
- Pydantic validates all Python service inputs
- Polly circuit breaker prevents cascading failures

---

## 9. Performance & Scalability

### Bottlenecks

1. **Polygon.io API** — Starter plan rate limits + 15-min delay. Every cache miss triggers external HTTP calls with up to 50k records. The Python service has no request caching.

2. **Large indicator calculations** — Generating a full indicator table (20+ indicators) on multi-year minute data hits pandas-ta hard. The 2GB Python container limit could be exceeded.

3. **EF Core change tracking** — Batch inserts clear tracker every 1000 records (good), but large `GetOrFetch` operations load entire result sets into memory.

4. **Single PostgreSQL instance** — No connection pooling configuration, no read replicas. Time-series queries on large aggregate tables could bottleneck under concurrent users.

5. **No WebSocket subscriptions** — Frontend polls via HTTP GraphQL. Real-time data (snapshots, portfolio updates) requires manual refresh.

### Scaling Recommendations

| Component | Recommendation |
|-----------|----------------|
| **Frontend** | Deploy as static assets behind CDN; bundle size is within budget (2-3MB) |
| **GraphQL Backend** | Add Redis for distributed cache; enable DataLoader for N+1 prevention in resolvers; add response caching middleware |
| **.NET → Python** | Add short-TTL in-memory cache (5-15 min) for repeated indicator/snapshot requests |
| **Python Service** | Add Gunicorn with multiple workers; implement request-level caching; enforce rate limits |
| **PostgreSQL** | Add connection pooling (PgBouncer), read replicas for analytics queries, TimescaleDB extension for time-series optimization |
| **Real-time** | Add GraphQL subscriptions (Hot Chocolate supports WebSocket) for live portfolio updates |

---

## 10. Architectural Recommendations

### High Priority

1. **Add EF Core Migrations** — Replace `EnsureCreated()` with tracked migrations. Current approach silently fails when adding new entities. This is the single highest-risk technical debt item.

2. **Add CI/CD Pipeline** — GitHub Actions running `dotnet test`, `pytest`, `ng test` on every PR. Block merges on test failure. This catches regressions before they land.

3. **Unify GraphQL Client Pattern** — Migrate all services from raw `HttpClient.post()` to Apollo Client or a single `gql()` helper. This enables client-side caching and deduplication.

4. **Implement Authentication** — Even for a learning project, adding JWT/cookie auth now prevents security debt. Hot Chocolate supports authorization directives natively.

### Medium Priority

5. **Add Redis Cache Layer** — Between .NET and Python, cache indicator results and snapshots with 5-15 minute TTL. Reduces Polygon API usage and Python compute load significantly.

6. **Extract Large Components** — `strategy-builder` (870 lines) and `options-strategy-lab` (822 lines) should be decomposed into sub-components following the portfolio pattern (which already has 8 clean sub-components).

7. **Add Service Contract Tests** — The .NET ↔ Python HTTP boundary has no schema validation. Add shared Pydantic/C# response models or use contract testing (Pact) to prevent silent breakage.

8. **Production Configuration** — Remove `IncludeExceptionDetails`, tighten CORS to specific origins, externalize all secrets to a vault, add structured logging (Serilog).

### Lower Priority / Future

9. **TimescaleDB** — For time-series queries at scale, TimescaleDB's hypertables and continuous aggregates would dramatically improve aggregate query performance.

10. **GraphQL Subscriptions** — Hot Chocolate supports WebSocket subscriptions. This would enable real-time portfolio updates, live price feeds, and backtest progress streaming without polling.

11. **Kubernetes Migration** — When scaling beyond a single machine, the compose.yaml maps cleanly to K8s Deployments + Services. The health checks and resource limits are already defined.

12. **Frontend Production Container** — Add an Nginx Dockerfile for the Angular build output. Currently only the dev server runs — no production-grade static serving.

---

### Summary Scorecard

| Dimension | Rating | Notes |
|-----------|--------|-------|
| **Architecture** | Strong | Clean 3-tier separation, well-chosen tech per layer |
| **Code Quality** | Strong | TypeScript strict, C# nullable refs, Pydantic validation |
| **Domain Modeling** | Excellent | FIFO accounting, Greeks aggregation, gap detection, 12-test validation suite |
| **Testing** | Moderate | Good unit test coverage but no integration/contract/CI pipeline |
| **Security** | Weak | No auth, exposed exceptions, permissive CORS |
| **DevOps** | Moderate | Good containerization, no CI/CD, no production config |
| **Performance** | Moderate | Smart caching patterns but no distributed cache, single DB instance |
| **Scalability** | Low | Single-instance everything, no horizontal scaling path |

This is a well-engineered learning/prototype system with strong domain logic and modern framework usage. The primary gaps are operational maturity (CI/CD, auth, migrations, observability) rather than architectural — the foundation is solid for progressive hardening toward production.