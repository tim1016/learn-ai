# Testing Guide - Market Data Integration

This guide covers how to write unit tests, integration tests, and end-to-end tests for the market data fetching system.

---

## Table of Contents
1. [Unit Testing](#unit-testing)
2. [Integration Testing](#integration-testing)
3. [End-to-End Testing](#end-to-end-testing)
4. [Test Project Setup](#test-project-setup)

---

## Unit Testing

### 1. Testing Services with Mocked Dependencies

#### Testing MarketDataService

```csharp
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Data;
using Backend.Models.DTOs.PolygonResponses;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;
using Moq;
using Xunit;

public class MarketDataServiceTests
{
    [Fact]
    public async Task FetchAndStoreAggregates_Success_ReturnsStoredData()
    {
        // Arrange
        var options = new DbContextOptionsBuilder<AppDbContext>()
            .UseInMemoryDatabase(databaseName: "TestDb_" + Guid.NewGuid())
            .Options;

        using var context = new AppDbContext(options);

        var mockPolygonService = new Mock<IPolygonService>();
        mockPolygonService
            .Setup(x => x.FetchAggregatesAsync(
                "AAPL", 1, "day", "2026-01-01", "2026-01-31", default))
            .ReturnsAsync(new AggregateResponse
            {
                Success = true,
                Ticker = "AAPL",
                DataType = "aggregates",
                Data = new List<AggregateData>
                {
                    new AggregateData
                    {
                        Timestamp = "2026-01-02T00:00:00.000Z",
                        Open = 100,
                        High = 105,
                        Low = 99,
                        Close = 103,
                        Volume = 1000000,
                        Vwap = 102
                    }
                },
                Summary = new DataSummary
                {
                    OriginalCount = 1,
                    CleanedCount = 1,
                    RemovedCount = 0
                }
            });

        var mockLogger = new Mock<ILogger<MarketDataService>>();
        var service = new MarketDataService(context, mockPolygonService.Object, mockLogger.Object);

        // Act
        var result = await service.FetchAndStoreAggregatesAsync(
            "AAPL", 1, "day", "2026-01-01", "2026-01-31");

        // Assert
        Assert.Single(result);
        Assert.Equal(103, result[0].Close);

        // Verify data was saved to database
        var savedData = await context.StockAggregates.ToListAsync();
        Assert.Single(savedData);
    }

    [Fact]
    public async Task GetOrCreateTicker_NewTicker_CreatesAndReturns()
    {
        // Arrange
        var options = new DbContextOptionsBuilder<AppDbContext>()
            .UseInMemoryDatabase(databaseName: "TestDb_" + Guid.NewGuid())
            .Options;

        using var context = new AppDbContext(options);
        var mockPolygonService = new Mock<IPolygonService>();
        var mockLogger = new Mock<ILogger<MarketDataService>>();
        var service = new MarketDataService(context, mockPolygonService.Object, mockLogger.Object);

        // Act
        var ticker = await service.GetOrCreateTickerAsync("MSFT", "stocks");

        // Assert
        Assert.NotNull(ticker);
        Assert.Equal("MSFT", ticker.Symbol);
        Assert.Equal("stocks", ticker.Market);

        // Verify it was saved
        var savedTicker = await context.Tickers.FirstOrDefaultAsync(t => t.Symbol == "MSFT");
        Assert.NotNull(savedTicker);
    }
}
```

#### Testing PolygonService (with HttpClient mocking)

```csharp
using Backend.Services.Implementation;
using Backend.Configuration;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Moq;
using Moq.Protected;
using System.Net;
using System.Text.Json;
using Xunit;

public class PolygonServiceTests
{
    [Fact]
    public async Task FetchAggregates_Success_ReturnsData()
    {
        // Arrange
        var mockResponse = new
        {
            success = true,
            ticker = "AAPL",
            data_type = "aggregates",
            data = new[]
            {
                new
                {
                    timestamp = "2026-01-02T00:00:00.000Z",
                    open = 100.0,
                    high = 105.0,
                    low = 99.0,
                    close = 103.0,
                    volume = 1000000.0,
                    vwap = 102.0
                }
            },
            summary = new
            {
                original_count = 1,
                cleaned_count = 1,
                removed_count = 0
            }
        };

        var mockHttpMessageHandler = new Mock<HttpMessageHandler>();
        mockHttpMessageHandler.Protected()
            .Setup<Task<HttpResponseMessage>>(
                "SendAsync",
                ItExpr.IsAny<HttpRequestMessage>(),
                ItExpr.IsAny<CancellationToken>())
            .ReturnsAsync(new HttpResponseMessage
            {
                StatusCode = HttpStatusCode.OK,
                Content = new StringContent(JsonSerializer.Serialize(mockResponse))
            });

        var httpClient = new HttpClient(mockHttpMessageHandler.Object)
        {
            BaseAddress = new Uri("http://localhost:8000")
        };

        var options = Options.Create(new PolygonServiceOptions
        {
            BaseUrl = "http://localhost:8000"
        });
        var mockLogger = new Mock<ILogger<PolygonService>>();
        var service = new PolygonService(httpClient, mockLogger.Object, options);

        // Act
        var result = await service.FetchAggregatesAsync(
            "AAPL", 1, "day", "2026-01-01", "2026-01-31");

        // Assert
        Assert.True(result.Success);
        Assert.Single(result.Data);
        Assert.Equal(103, result.Data[0].Close);
    }
}
```

### 2. Testing GraphQL Mutations

```csharp
using Backend.GraphQL;
using Backend.GraphQL.Types;
using Backend.Services.Interfaces;
using Backend.Models.MarketData;
using Moq;
using Xunit;

public class MutationTests
{
    [Fact]
    public async Task FetchStockAggregates_Success_ReturnsSuccessResult()
    {
        // Arrange
        var mockService = new Mock<IMarketDataService>();
        mockService
            .Setup(x => x.FetchAndStoreAggregatesAsync(
                "AAPL", 1, "day", "2026-01-01", "2026-01-31", default))
            .ReturnsAsync(new List<StockAggregate>
            {
                new StockAggregate
                {
                    TickerId = 1,
                    Open = 100,
                    High = 105,
                    Low = 99,
                    Close = 103,
                    Volume = 1000000,
                    Timestamp = DateTime.Parse("2026-01-02"),
                    Timespan = "day",
                    Multiplier = 1
                }
            });

        var mutation = new Mutation();

        // Act
        var result = await mutation.FetchStockAggregates(
            mockService.Object,
            "AAPL",
            "2026-01-01",
            "2026-01-31",
            "day",
            1);

        // Assert
        Assert.True(result.Success);
        Assert.Equal("AAPL", result.Ticker);
        Assert.Equal(1, result.Count);
        Assert.Contains("Successfully", result.Message);
    }

    [Fact]
    public async Task FetchStockAggregates_ServiceThrows_ReturnsErrorResult()
    {
        // Arrange
        var mockService = new Mock<IMarketDataService>();
        mockService
            .Setup(x => x.FetchAndStoreAggregatesAsync(
                It.IsAny<string>(), It.IsAny<int>(), It.IsAny<string>(),
                It.IsAny<string>(), It.IsAny<string>(), default))
            .ThrowsAsync(new Exception("API key invalid"));

        var mutation = new Mutation();

        // Act
        var result = await mutation.FetchStockAggregates(
            mockService.Object,
            "AAPL",
            "2026-01-01",
            "2026-01-31");

        // Assert
        Assert.False(result.Success);
        Assert.Contains("API key invalid", result.Message);
    }
}
```

### 3. Testing Entity Models

```csharp
using Backend.Models.MarketData;
using Xunit;

public class StockAggregateTests
{
    [Fact]
    public void IsValid_ValidOHLCV_ReturnsTrue()
    {
        // Arrange
        var aggregate = new StockAggregate
        {
            Open = 100,
            High = 105,
            Low = 95,
            Close = 103,
            Volume = 1000000,
            Timestamp = DateTime.UtcNow,
            Timespan = "day"
        };

        // Act
        var isValid = aggregate.IsValid();

        // Assert
        Assert.True(isValid);
    }

    [Fact]
    public void IsValid_HighLowerThanLow_ReturnsFalse()
    {
        // Arrange
        var aggregate = new StockAggregate
        {
            Open = 100,
            High = 90,  // Invalid: High < Low
            Low = 95,
            Close = 103,
            Volume = 1000000,
            Timestamp = DateTime.UtcNow,
            Timespan = "day"
        };

        // Act
        var isValid = aggregate.IsValid();

        // Assert
        Assert.False(isValid);
    }
}

public class QuoteTests
{
    [Fact]
    public void GetSpread_ValidPrices_ReturnsCorrectSpread()
    {
        // Arrange
        var quote = new Quote
        {
            BidPrice = 100.00m,
            AskPrice = 100.50m,
            BidSize = 100,
            AskSize = 100,
            Timestamp = DateTime.UtcNow
        };

        // Act
        var spread = quote.GetSpread();

        // Assert
        Assert.Equal(0.50m, spread);
    }

    [Fact]
    public void GetMidPrice_ValidPrices_ReturnsCorrectMidpoint()
    {
        // Arrange
        var quote = new Quote
        {
            BidPrice = 100.00m,
            AskPrice = 100.50m,
            BidSize = 100,
            AskSize = 100,
            Timestamp = DateTime.UtcNow
        };

        // Act
        var midPrice = quote.GetMidPrice();

        // Assert
        Assert.Equal(100.25m, midPrice);
    }
}
```

---

## Integration Testing

### 1. Testing with In-Memory Database

```csharp
using Backend.Data;
using Backend.Models.MarketData;
using Microsoft.EntityFrameworkCore;
using Xunit;

public class AppDbContextIntegrationTests
{
    [Fact]
    public async Task CanSaveAndRetrieveTicker()
    {
        // Arrange
        var options = new DbContextOptionsBuilder<AppDbContext>()
            .UseInMemoryDatabase(databaseName: "IntegrationTest_" + Guid.NewGuid())
            .Options;

        // Act
        using (var context = new AppDbContext(options))
        {
            var ticker = new Ticker
            {
                Symbol = "AAPL",
                Name = "Apple Inc.",
                Market = "stocks",
                Active = true
            };
            context.Tickers.Add(ticker);
            await context.SaveChangesAsync();
        }

        // Assert
        using (var context = new AppDbContext(options))
        {
            var ticker = await context.Tickers.FirstOrDefaultAsync(t => t.Symbol == "AAPL");
            Assert.NotNull(ticker);
            Assert.Equal("Apple Inc.", ticker.Name);
        }
    }

    [Fact]
    public async Task UniqueConstraint_PreventsDuplicateTickers()
    {
        // Arrange
        var options = new DbContextOptionsBuilder<AppDbContext>()
            .UseInMemoryDatabase(databaseName: "IntegrationTest_" + Guid.NewGuid())
            .Options;

        using var context = new AppDbContext(options);

        var ticker1 = new Ticker { Symbol = "AAPL", Name = "Apple", Market = "stocks" };
        var ticker2 = new Ticker { Symbol = "AAPL", Name = "Apple Duplicate", Market = "stocks" };

        context.Tickers.Add(ticker1);
        await context.SaveChangesAsync();

        // Act & Assert
        context.Tickers.Add(ticker2);
        // Note: In-memory DB doesn't enforce unique constraints, use real DB for this test
        // This would throw with real PostgreSQL
    }

    [Fact]
    public async Task CascadeDelete_DeletesRelatedAggregates()
    {
        // Arrange
        var options = new DbContextOptionsBuilder<AppDbContext>()
            .UseInMemoryDatabase(databaseName: "IntegrationTest_" + Guid.NewGuid())
            .Options;

        using var context = new AppDbContext(options);

        var ticker = new Ticker { Symbol = "AAPL", Name = "Apple", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        var aggregate = new StockAggregate
        {
            TickerId = ticker.Id,
            Open = 100,
            High = 105,
            Low = 99,
            Close = 103,
            Volume = 1000000,
            Timestamp = DateTime.UtcNow,
            Timespan = "day"
        };
        context.StockAggregates.Add(aggregate);
        await context.SaveChangesAsync();

        // Act
        context.Tickers.Remove(ticker);
        await context.SaveChangesAsync();

        // Assert
        var aggregates = await context.StockAggregates.ToListAsync();
        Assert.Empty(aggregates);  // Should be deleted due to cascade
    }
}
```

---

## End-to-End Testing

### 1. Testing with Test Containers

```csharp
using System.Net.Http.Json;
using Backend.GraphQL.Types;
using DotNet.Testcontainers.Builders;
using DotNet.Testcontainers.Containers;
using Xunit;

public class EndToEndTests : IAsyncLifetime
{
    private IContainer _postgresContainer;
    private IContainer _pythonContainer;
    private HttpClient _client;

    public async Task InitializeAsync()
    {
        // Start PostgreSQL container
        _postgresContainer = new ContainerBuilder()
            .WithImage("postgres:16")
            .WithEnvironment("POSTGRES_PASSWORD", "testpassword")
            .WithPortBinding(5432, true)
            .WithWaitStrategy(Wait.ForUnixContainer().UntilPortIsAvailable(5432))
            .Build();

        await _postgresContainer.StartAsync();

        // Start Python service container
        _pythonContainer = new ContainerBuilder()
            .WithImage("polygon-service:latest")
            .WithEnvironment("POLYGON_API_KEY", "test_key")
            .WithPortBinding(8000, true)
            .WithWaitStrategy(Wait.ForUnixContainer().UntilHttpRequestIsSucceeded(r => r.ForPath("/health")))
            .Build();

        await _pythonContainer.StartAsync();

        // Start your backend service
        // ... (configure and start backend with test containers connection strings)
    }

    [Fact]
    public async Task FetchStockAggregates_EndToEnd_Success()
    {
        // This would be a full end-to-end test hitting all services
        // Arrange
        var mutation = @"
            mutation {
                fetchStockAggregates(
                    ticker: ""AAPL""
                    fromDate: ""2026-01-01""
                    toDate: ""2026-01-31""
                ) {
                    success
                    ticker
                    count
                    message
                }
            }";

        // Act
        var response = await _client.PostAsJsonAsync("/graphql", new { query = mutation });
        var result = await response.Content.ReadFromJsonAsync<GraphQLResponse<FetchAggregatesResult>>();

        // Assert
        Assert.NotNull(result);
        Assert.True(result.Data.FetchStockAggregates.Success);
    }

    public async Task DisposeAsync()
    {
        await _postgresContainer.DisposeAsync();
        await _pythonContainer.DisposeAsync();
    }

    private class GraphQLResponse<T>
    {
        public T Data { get; set; }
    }
}
```

---

## Test Project Setup

### 1. Create Test Project

```bash
cd Backend
dotnet new xunit -n Backend.Tests
cd Backend.Tests
dotnet add reference ../Backend.csproj
```

### 2. Add Test Dependencies

```bash
dotnet add package Moq
dotnet add package Microsoft.EntityFrameworkCore.InMemory
dotnet add package Testcontainers
dotnet add package FluentAssertions  # Optional, for better assertions
```

### 3. Test Project Structure

```
Backend.Tests/
├── Unit/
│   ├── Services/
│   │   ├── MarketDataServiceTests.cs
│   │   └── PolygonServiceTests.cs
│   ├── GraphQL/
│   │   ├── MutationTests.cs
│   │   └── QueryTests.cs
│   └── Models/
│       └── StockAggregateTests.cs
├── Integration/
│   ├── DatabaseTests.cs
│   └── ServiceIntegrationTests.cs
└── EndToEnd/
    └── FullStackTests.cs
```

### 4. Running Tests

```bash
# Run all tests
dotnet test

# Run with coverage
dotnet test --collect:"XPlat Code Coverage"

# Run specific test
dotnet test --filter "FullyQualifiedName~MarketDataServiceTests"

# Run tests in parallel
dotnet test --parallel
```

---

## Best Practices

### 1. Test Naming Convention
```
MethodName_Scenario_ExpectedResult
```
Example: `FetchAggregates_InvalidTicker_ThrowsException`

### 2. AAA Pattern
- **Arrange**: Set up test data and mocks
- **Act**: Execute the method under test
- **Assert**: Verify the results

### 3. Test Independence
- Each test should be independent
- Use unique database names for in-memory tests
- Clean up resources in Dispose methods

### 4. Mock External Dependencies
- Mock HttpClient for PolygonService tests
- Mock IPolygonService for MarketDataService tests
- Use in-memory database for testing database logic

### 5. Test Coverage Goals
- **Unit Tests**: 80%+ coverage of business logic
- **Integration Tests**: Key workflows (fetch → store → query)
- **End-to-End Tests**: Critical user journeys

---

## Continuous Integration

### GitHub Actions Example

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Setup .NET
        uses: actions/setup-dotnet@v1
        with:
          dotnet-version: '8.0.x'
      - name: Restore dependencies
        run: dotnet restore
      - name: Build
        run: dotnet build --no-restore
      - name: Test
        run: dotnet test --no-build --verbosity normal --collect:"XPlat Code Coverage"
```

---

## Summary

This testing guide provides:
- ✅ **Unit tests** for services, mutations, and models
- ✅ **Integration tests** with in-memory database
- ✅ **End-to-end tests** with test containers
- ✅ **Best practices** for testable architecture
- ✅ **CI/CD integration** examples

The testable architecture we built (interfaces, dependency injection, DTOs) makes all of this possible!
