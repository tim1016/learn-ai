---
name: write-graphql-resolver
description: Write or debug a Hot Chocolate v15 GraphQL resolver in the .NET Backend. Use when user says "add a resolver", "expose this in GraphQL", "add a query for", "debug this resolver", or asks to modify the GraphQL schema.
---

# Write GraphQL Resolver

Write or debug a Hot Chocolate v15 resolver in `Backend/`. Covers query resolvers, mutation resolvers, field resolvers, and DataLoader integration.

## When to use

- User wants a new GraphQL query or mutation
- User wants to expose existing data over GraphQL
- User reports a resolver isn't returning expected data
- User mentions "N+1", "DataLoader", or "resolver performance"

## Prerequisites

Before writing a resolver:

1. Is the underlying data or service already implemented? If the resolver is fronting a Python endpoint, confirm `add-fastapi-endpoint` has been run. If it's fronting EF Core, confirm the entity and DbContext setup exist.
2. Does the schema already have the type? Check `Backend/Schema/` or wherever types are defined.
3. Is this a new top-level field on `Query`/`Mutation`, or a field resolver on an existing type?

## Hot Chocolate v15 specifics

**Critical gotcha**: HC v15 strips the `Get` prefix from method names when inferring field names. `GetUserById` becomes `userById` on the schema. To control this, use `[GraphQLName("...")]` explicitly on every resolver method. Do not rely on inference.

```csharp
[QueryType]
public static class IndicatorQueries
{
    [GraphQLName("ema")]
    public static async Task<EmaResult> GetEma(
        string symbol,
        int period,
        DateTime start,
        DateTime end,
        [Service] IIndicatorClient client,
        CancellationToken ct)
    {
        return await client.ComputeEmaAsync(symbol, period, start, end, ct);
    }
}
```

## Execution

### 1. Choose the resolver shape

- **Query resolver**: reads data. Idempotent. Place in `Backend/GraphQL/Queries/<Domain>Queries.cs`.
- **Mutation resolver**: writes data. Side-effecting. Place in `Backend/GraphQL/Mutations/<Domain>Mutations.cs`.
- **Field resolver**: computes a field on an existing type (e.g., `User.fullName` derived from `firstName` + `lastName`). Place next to the type definition.

### 2. Write the resolver

- `static` methods on `static` classes marked with `[QueryType]` or `[MutationType]` attributes.
- `[Service]` attribute for service injection (not constructor injection; resolvers are static).
- Always accept and pass `CancellationToken` through to downstream calls.
- Return concrete types or union results for domain errors; throw only for unexpected conditions.
- `[GraphQLName("fieldName")]` on every method for stable schema naming.

### 3. Handle N+1 with DataLoader

If the resolver will be called in a list (e.g., each `User` resolving its `Company`), use a `DataLoader`.

```csharp
public sealed class CompanyBatchDataLoader(
    ICompanyRepository repo,
    IBatchScheduler scheduler,
    DataLoaderOptions options)
    : BatchDataLoader<int, Company>(scheduler, options)
{
    protected override async Task<IReadOnlyDictionary<int, Company>> LoadBatchAsync(
        IReadOnlyList<int> ids, CancellationToken ct)
    {
        var companies = await repo.GetByIdsAsync(ids, ct);
        return companies.ToDictionary(c => c.Id);
    }
}
```

Register the DataLoader in `Program.cs` via `.AddDataLoader<CompanyBatchDataLoader>()`.

In the field resolver, inject and use it:

```csharp
[GraphQLName("company")]
public static Task<Company> GetCompanyAsync(
    [Parent] User user,
    CompanyBatchDataLoader loader,
    CancellationToken ct)
    => loader.LoadAsync(user.CompanyId, ct);
```

### 4. Handle domain errors properly

Don't throw `Exception` for expected conditions like "user not found". Use a union or error type:

```csharp
[UnionType]
public abstract record EmaResult;
public sealed record EmaSuccess(decimal[] Values, DateTime[] Timestamps) : EmaResult;
public sealed record EmaError(string Code, string Message) : EmaResult;
```

The resolver returns the appropriate case; clients discriminate with a `__typename` check.

### 5. Test the resolver

Use `IRequestExecutor` to run the schema against raw GraphQL queries. Place tests in `Backend.Tests/GraphQL/`.

```csharp
[Fact]
public async Task Ema_ValidSymbol_ReturnsSuccess()
{
    // Arrange
    var executor = await CreateTestExecutorAsync();

    // Act
    var result = await executor.ExecuteAsync(
        """
        query {
            ema(symbol: "SPY", period: 10, start: "2024-01-01", end: "2024-01-31") {
                __typename
                ... on EmaSuccess { values }
            }
        }
        """);

    // Assert
    result.ToJson().Should().Contain("EmaSuccess");
}
```

Name pattern: `Field_Scenario_ExpectedResult`.

### 6. Confirm the schema

After adding, verify the schema by running the GraphQL endpoint locally (`http://localhost:5000/graphql`) and checking the introspection. The field should appear with the name you specified in `[GraphQLName]`.

## Output

Report:

- Resolver method and schema field name (they differ — state both)
- Files created or modified
- DataLoader added, if any
- Error/union types added, if any
- Tests added
- How to test it manually (sample GraphQL query to paste into the Banana Cake Pop UI at `/graphql`)

## Anti-patterns to avoid

- Relying on HC's `Get` prefix stripping (use explicit `[GraphQLName]`)
- Constructor injection on resolver classes (resolvers are static; use `[Service]` parameter injection)
- Throwing exceptions for domain errors — use union result types
- N+1 queries in list resolvers (use DataLoader)
- Forgetting `CancellationToken` in the call chain
- Using `AsTracking()` EF Core queries for read-only resolvers (use `AsNoTracking()`)
- Writing logic in the resolver; the resolver calls a service that does the work
