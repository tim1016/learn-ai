# .NET rules

Targets .NET 10 with Hot Chocolate v15 and EF Core. Read when writing or editing code under `Backend/` or `Backend.Tests/`.

**Authoritative references**:
- https://learn.microsoft.com/dotnet (framework, language, EF Core)
- https://chillicream.com/docs/hotchocolate (Hot Chocolate)

## Naming

- Classes, methods, properties, public fields: `PascalCase`
- Private fields: `_camelCase` with underscore prefix
- Async methods: suffix `Async`
- Interfaces: `I` prefix (`IMarketDataService`)
- Constants / `static readonly`: `PascalCase`

## Structural patterns

- **Async/await for all I/O.** Pass `CancellationToken` through every call chain — do not create and ignore one.
- **Interface-based services.** Register with the narrowest lifetime that works (prefer `Scoped` > `Singleton` > `Transient`).
- **`IOptions<T>` / `IOptionsSnapshot<T>`** for config. Do not read `IConfiguration` directly throughout the code.
- **Guard clauses and early returns** over deep nesting.
- **Structured logging** via `ILogger<T>`. Use message templates with named placeholders: `logger.LogInformation("Fetched {Count} bars for {Symbol}", count, symbol)`. Never interpolate into the message.
- **`[STEP X]` prefix** in logs for multi-step operations to make traces scannable.
- **No `Console.WriteLine` in committed code.** Use `ILogger`.
- **No silent catches** (`catch {}`). Handle explicitly or let it propagate with context added.

## Hot Chocolate v15

- **Always use `[GraphQLName("...")]`** to control exposed field names. HC v15 strips the `Get` prefix; don't rely on inference.
- **Static resolver classes** with `[QueryType]` / `[MutationType]` attributes.
- **`[Service]` attribute** for resolver service injection (not constructor injection).
- **`DataLoader` for N+1** prevention in list resolvers.
- **Union result types** for domain errors instead of throwing exceptions. Throw only for unexpected conditions.
- **`CancellationToken`** as the last parameter of resolvers.

## EF Core

- **`AsNoTracking()`** for read-only queries.
- **Batch operations** over N+1 patterns. Project with `Select` where possible to avoid hydrating unused fields.
- **Migrations** are version-controlled. Every schema change has a matching migration; review before applying.
- **Don't leak `IQueryable`** out of services. Services return materialized results; `IQueryable` belongs inside the service boundary.
- **Use `IDbContextFactory<T>`** for long-running or parallel operations.

## Data deserialization

- **`JsonNamingPolicy.SnakeCaseLower`** for deserializing Python service responses (PythonDataService uses snake_case).
- **Strongly-typed clients** (typed `HttpClient` via `IHttpClientFactory`) for calling internal services.

## Testing (see testing.md for cross-stack standards)

- **xUnit** with `[Fact]` and `[Theory]`. Prefer `[Theory, InlineData(...)]` for parameterized tests.
- **Arrange → Act → Assert** with blank-line separation.
- **NSubstitute or Moq** for mocking interfaces. Mock interfaces, not concrete classes.
- **Async test methods** return `Task`. Never `.Result` or `.Wait()`.
- **Name pattern**: `MethodName_Scenario_ExpectedResult`.
- **GraphQL tests**: use `IRequestExecutor` to run raw queries against the schema.

## Common pitfalls

- Relying on HC's `Get` prefix stripping without `[GraphQLName]` — schema becomes fragile to rename refactors
- Constructor injection on resolver classes (they're static; use `[Service]`)
- Throwing for domain errors (use union result types)
- N+1 in list resolvers (use DataLoader)
- Raw `IConfiguration` reads scattered through code (use `IOptions<T>`)
- Message-template interpolation in logs: `logger.LogInformation($"Fetched {count}")` — breaks structured logging
- `.Result` / `.Wait()` in async code paths (deadlock risk)
