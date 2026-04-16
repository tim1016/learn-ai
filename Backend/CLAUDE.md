# Backend — .NET 10 GraphQL API

## Commands

| Action     | Command                                              |
|------------|------------------------------------------------------|
| Run        | `podman compose up backend` (localhost:5000)         |
| Test       | `cd Backend.Tests && dotnet test`                    |
| Build      | `podman exec my-backend dotnet build`                |
| Lint       | `dotnet format podman.sln --verify-no-changes`       |
| Logs       | `podman logs -f my-backend`                          |
| DB shell   | `podman exec -it my-postgres psql -U postgres`       |

Backend depends on **db** and **python-service** containers (health-checked).
Tests run locally using InMemory EF Core — no containers needed.

## File Structure

```
Backend/
├── Program.cs                    # Composition root — service registration, middleware
├── StudiesApi.cs                 # Large minimal API file (REST endpoints alongside GraphQL)
├── GraphQL/
│   ├── Query.cs                  # Root market data queries
│   ├── Mutation.cs               # Root mutations
│   ├── PortfolioQuery.cs         # Portfolio queries (type extension)
│   ├── PortfolioMutation.cs      # Portfolio mutations (type extension)
│   ├── DataLabQuery.cs           # Data lab queries (type extension)
│   ├── DataLabMutation.cs        # Data lab mutations (type extension)
│   └── Types/                    # GraphQL result/payload types
├── Services/
│   ├── Interfaces/               # 14 service interfaces (IMarketDataService, IPolygonService, etc.)
│   └── Implementation/           # 14 implementations
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

- **Hot Chocolate v15** GraphQL — always use `[GraphQLName("fieldName")]` (HC strips "Get" prefix)
- **Type extensions** for domain separation: `[ExtendObjectType(typeof(Query))]`
- **Interface-based DI** with scoped lifetime — all services registered in `Program.cs`
- **Polly** retry + circuit-breaker on HttpClient calls to Python service
- **Structured logging**: `logger.LogInformation("[STEP X] ...")`
- **EF Core 10** with PostgreSQL — `AsNoTracking()` for read-only queries
- **`JsonNamingPolicy.SnakeCaseLower`** when deserializing Python service responses
- Container uses SDK image with `dotnet watch run` — `Dockerfile` is for production builds only

## Testing (Backend.Tests/)

- **xUnit** with `[Fact]` and `[Theory, InlineData(...)]`
- **Moq** for interface mocking
- Arrange / Act / Assert pattern
- `FakeHttpMessageHandler` for HTTP call mocking
- `TestDbContextFactory` for InMemory EF Core
- Name pattern: `MethodName_Scenario_ExpectedResult`

## Gotchas

- HC v15 camelCase converts `PnL` → `pnL` (not `pnl`) — use explicit `[GraphQLName]`
- `EnsureCreated()` does nothing if ANY tables exist — delete pgdata volume for new entities
- Backend maps port 5000 (host) → 8080 (container)
