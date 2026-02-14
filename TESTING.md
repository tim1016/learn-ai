# Testing Guide

This project has test coverage across all three layers: Angular frontend (Jest), .NET backend (xUnit), and Python data service (pytest).

**Total: 23 suites, 115 tests**

---

## Quick Start

```bash
# Angular (17 suites, 91 tests)
cd Frontend && npx jest

# .NET (3 suites, 12 tests)
cd Backend.Tests && dotnet test

# Python (3 suites, 12 tests)
cd PythonDataService && python -m pytest tests/ -v
```

---

## Angular (Jest + jest-preset-angular)

### Configuration

| File | Purpose |
|------|---------|
| `Frontend/jest.config.ts` | Jest config — preset, module mocks, coverage, `maxWorkers: '50%'` for Windows stability |
| `Frontend/setup-jest.ts` | Zone.js import, `TestBed.initTestEnvironment()`, global `ResizeObserver` stub for jsdom |
| `Frontend/tsconfig.spec.json` | TypeScript config for test files — extends `tsconfig.json`, adds `jest` types |

### Mocks

| File | What it mocks |
|------|---------------|
| `Frontend/src/testing/mocks/lightweight-charts.mock.ts` | `createChart`, `CandlestickSeries`, `LineSeries`, `HistogramSeries` — jsdom has no Canvas API |
| `Frontend/src/testing/mocks/polygon-client.mock.ts` | `@polygon.io/client-js` — ESM-only package that Jest can't resolve natively |

Both are wired via `moduleNameMapper` in `jest.config.ts`.

### Test Factories

`Frontend/src/testing/factories/market-data.factory.ts` provides:
- `createMockAggregate(overrides?)` — single OHLCV bar
- `createMockAggregates(count)` — array of bars with incrementing dates/prices
- `createMockSummary(overrides?)` — period statistics
- `createMockTicker(overrides?)` — ticker entity
- `createMockIndicatorSeries(overrides?)` — SMA/EMA/RSI series data

### Component Specs (12)

| Spec | Key tests |
|------|-----------|
| `app.component.spec.ts` | Creates, renders navigation links, has router-outlet |
| `authors.component.spec.ts` | Creates, loading state, renders after Apollo query, error handling |
| `books.component.spec.ts` | Creates, loading state, renders after query, error handling |
| `market-data.component.spec.ts` | Creates, date range init, empty ticker validation, service call, data loaded on fetch |
| `candlestick-chart.component.spec.ts` | Creates, calls `createChart` on init, passes data to `setData`, cleanup on destroy |
| `line-chart.component.spec.ts` | Creates, chart init, data binding, cleanup |
| `volume-chart.component.spec.ts` | Creates, chart init, green/red color logic by price direction |
| `summary-stats.component.spec.ts` | Creates, renders nothing when null, renders stat cards, `.positive`/`.negative` CSS classes |
| `tickers.component.spec.ts` | Creates, loading state, `getExchange()` mapping (XNAS->NASDAQ, XNYS->NYSE) |
| `technical-analysis.component.spec.ts` | Creates, default signal values, empty ticker skip, computed defaults |
| `ta-chart.component.spec.ts` | Creates, empty candlestickData computed, overlayIndicators filter, rsiIndicator detection |
| `tradingview-widget.component.spec.ts` | Creates, renders without crashing |

### Service Specs (5)

| Spec | Key tests |
|------|-----------|
| `market-data.service.spec.ts` | Sends correct GraphQL query/variables to `/graphql`, maps response, throws on GraphQL errors |
| `ticker.service.spec.ts` | Sends correct query, maps tickers response, aggregate stats query |
| `author.service.spec.ts` | Apollo `watchQuery` with `GET_AUTHORS`, maps response, handles empty results |
| `book.service.spec.ts` | Apollo `watchQuery` with `GET_BOOKS`, maps response, handles empty results |
| `polygon.service.spec.ts` | Creates, basic structure verification |

### Testing Patterns

**HttpClient services** (market-data, ticker):
```typescript
providers: [
  MarketDataService,
  provideHttpClient(),
  provideHttpClientTesting(),
]
// Then use HttpTestingController to flush responses
```

**Apollo services** (author, book):
```typescript
imports: [ApolloTestingModule]
// Then use ApolloTestingController to flush queries
```

**Signal-based components** (ta-chart, technical-analysis):
```typescript
fixture.componentRef.setInput('inputName', value);
fixture.detectChanges();
// Read computed values directly from component instance
```

**Chart components** — The `lightweight-charts` mock tracks calls:
```typescript
expect(createChart).toHaveBeenCalled();
const series = chart.addSeries.mock.results[0].value;
expect(series.setData).toHaveBeenCalledWith(expectedData);
```

### Running

```bash
cd Frontend

# Run all tests
npx jest

# Verbose output
npx jest --verbose

# Single file
npx jest --testPathPattern="market-data.component"

# With coverage
npx jest --coverage
```

---

## .NET Backend (xUnit + Moq)

### Project Structure

```
Backend.Tests/
├── Backend.Tests.csproj
├── Helpers/
│   └── TestDbContextFactory.cs      # In-memory EF Core database factory
└── Unit/
    ├── Models/
    │   └── StockAggregateTests.cs   # 5 tests — IsValid() method
    ├── Services/
    │   └── MarketDataServiceTests.cs # 4 tests — GetOrCreateTicker, GetOrFetchAggregates
    └── GraphQL/
        └── MutationTests.cs         # 3 tests — FetchStockAggregates success/error/empty
```

### Dependencies

| Package | Purpose |
|---------|---------|
| `xunit` | Test framework |
| `Moq` | Mocking `IPolygonService`, `IMarketDataService` |
| `Microsoft.EntityFrameworkCore.InMemory` | In-memory database for service tests |
| `coverlet.collector` | Code coverage |

### Test Helper

`TestDbContextFactory.Create()` returns an `AppDbContext` backed by a unique in-memory database:

```csharp
var context = TestDbContextFactory.Create();
// Each call gets an isolated database (Guid-based name)
```

### Tests

**StockAggregateTests** (5 tests):
- Valid OHLCV returns true
- High < Low returns false
- Negative volume returns false
- All prices equal returns true
- Low > Open still valid if High >= Low

**MarketDataServiceTests** (4 tests):
- `GetOrCreateTickerAsync` — creates new ticker
- `GetOrCreateTickerAsync` — returns existing ticker
- `GetOrCreateTickerAsync` — handles different market types
- `GetOrFetchAggregatesAsync` — returns cached data from database

**MutationTests** (3 tests):
- `FetchStockAggregates` — success returns count and message
- `FetchStockAggregates` — service throws returns error result
- `FetchStockAggregates` — empty result returns zero count

### Running

```bash
cd Backend.Tests

# Run all tests
dotnet test

# Verbose output
dotnet test --verbosity normal

# Filter by test class
dotnet test --filter "FullyQualifiedName~StockAggregateTests"

# With coverage
dotnet test --collect:"XPlat Code Coverage"
```

---

## Python Data Service (pytest + pytest-asyncio)

### Configuration

`PythonDataService/pytest.ini`:
```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

### Project Structure

```
PythonDataService/tests/
├── __init__.py
├── conftest.py           # Shared fixtures and helpers
├── test_health.py        # 2 tests — GET /health, GET /
├── test_indicators.py    # 5 tests — POST /api/indicators/calculate
└── test_ta_service.py    # 5 tests — TechnicalAnalysisService unit tests
```

### Shared Fixtures (`conftest.py`)

- **`client`** — `httpx.AsyncClient` with `ASGITransport` for testing FastAPI endpoints without a running server
- **`make_sample_bars(count)`** — Generates OHLCV bar data with incrementing prices/timestamps

```python
# Environment patched before app import
os.environ.setdefault("POLYGON_API_KEY", "test-key-for-testing")
```

### Tests

**test_health.py** (2 tests):
- `GET /health` returns 200 with `{"status": "healthy"}`
- `GET /` returns service info with version and doc links

**test_indicators.py** (5 tests):
- SMA calculation returns success with correct structure
- Multiple indicators (SMA + EMA + RSI) in single request
- Invalid indicator name returns 422
- Empty bars list returns 422
- Empty indicators list returns 422

**test_ta_service.py** (5 tests):
- SMA produces correct structure with name/window/data fields
- EMA produces correct structure
- RSI values are in 0-100 range
- Unknown indicator name is silently skipped
- Multiple indicators calculated in single call

### Running

```bash
cd PythonDataService

# Run all tests
python -m pytest tests/ -v

# Single file
python -m pytest tests/test_indicators.py -v

# With coverage (requires pytest-cov)
python -m pytest tests/ --cov=app --cov-report=term-missing
```

---

## Known Gotchas

| Issue | Cause | Solution |
|-------|-------|----------|
| Chart spec failures in jsdom | No Canvas API | `lightweight-charts.mock.ts` via `moduleNameMapper` |
| `@polygon.io/client-js` import error | ESM-only package | `polygon-client.mock.ts` via `moduleNameMapper` |
| Apollo service `done()` called multiple times | `watchQuery().valueChanges` is a `BehaviorSubject` that emits initial value + flushed data | Use `take(1)` for empty-result tests; guard clause (`if (arr.length === 0) return`) for data tests |
| PrimeNG p-table re-sorts component data | `[sortField]` + `[sortOrder]` mutates the backing array on `detectChanges()` | Assert data presence, not specific order |
| Random parallel failures on Windows | Jest worker contention | `maxWorkers: '50%'` in `jest.config.ts` |
| Python tests need local deps | Normally runs in Podman container | `pip install fastapi pydantic pandas-ta httpx pytest pytest-asyncio` |
