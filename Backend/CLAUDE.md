# Backend — .NET 10 GraphQL API

## Commands

| Action       | Command                                                                        |
|--------------|--------------------------------------------------------------------------------|
| Run          | `podman compose up backend` (localhost:5000)                               |
| Test (PR)    | `cd Backend.Tests && dotnet test --filter "Category!=PostgresIntegration"` |
| Test (daily) | `cd Backend.Tests && dotnet test`                                         |
| Build        | `podman exec my-backend dotnet build`                                      |
| Lint         | `dotnet format podman.sln --verify-no-changes`                             |
| Logs         | `podman logs -f my-backend`                                                |
| DB shell     | `podman exec -it my-postgres psql -U postgres`                             |

There is no `dotnet` on the owner's Mac: CI runs the test and lint commands above, and Build runs in the container.

Backend depends on **db** and **python-service** containers (health-checked).
Tests use InMemory EF Core — no containers needed.

## File Structure

```
Backend/
├── Program.cs                    # Composition root — service registration, middleware
├── GraphQL/
│   ├── Query.cs                  # Root market data queries
│   ├── Mutation.cs               # Root mutations
│   ├── PortfolioQuery.cs         # Portfolio queries (type extension)
│   ├── PortfolioMutation.cs      # Portfolio mutations (type extension)
│   ├── DataLabQuery.cs           # Data lab queries (type extension)
│   ├── DataLabMutation.cs        # Data lab mutations (type extension)
│   └── Types/                    # GraphQL result/payload types
├── Services/
│   ├── Interfaces/               # Service interfaces (IMarketDataService, IPolygonService, etc.)
│   └── Implementation/           # Their implementations
├── Models/
│   ├── MarketData/               # StockAggregate, Trade, Ticker, TechnicalIndicator, etc.
│   ├── Portfolio/                # Account, Position, PositionLot, Order, OptionContract, etc.
│   ├── DataLab/                  # DataLabSession
│   └── DTOs/                     # Request/response DTOs, PolygonResponses/
├── Data/
│   └── AppDbContext.cs           # EF Core DbContext (PostgreSQL 16)
└── Configuration/
    └── PolygonServiceOptions.cs  # IOptions<T> config model
```

## Key Patterns

- **Type extensions** for domain separation: `[ExtendObjectType(typeof(Query))]`
- **Interface-based DI** with scoped lifetime — all services registered in `Program.cs`
- **Polly** retry + circuit-breaker on HttpClient calls to Python service
- Container uses SDK image with `dotnet watch run` — `Dockerfile` is for production builds only

## Testing (Backend.Tests/)

- **Moq** for interface mocking
- `FakeHttpMessageHandler` for HTTP call mocking
- `TestDbContextFactory` for InMemory EF Core

## Gotchas

- HC v15 camelCase converts `PnL` → `pnL` (not `pnl`) — use explicit `[GraphQLName]`
- EF Core migrations run at backend startup; do not delete pgdata for routine schema changes. For a populated legacy `EnsureCreated()` database, follow `docs/runbooks/ef-migrations-adoption.md` after taking a backup.
- Backend maps port 5000 (host) → 8080 (container)
