# Testing Guide

This project has test coverage across all three layers: Angular frontend (Vitest), .NET backend (xUnit), and Python data service (pytest).

---

## Quick Start

```bash
# Angular (Vitest)
cd Frontend && npx vitest run

# .NET
cd Backend.Tests && dotnet test

# Python
cd PythonDataService && python -m pytest tests/ -v
```

---

## Angular (Vitest + @testing-library/angular)

### Configuration

| File | Purpose |
|------|---------|
| `Frontend/vitest.config.ts` | Vitest config — module mocks, coverage, test setup |
| `Frontend/src/test-setup.ts` | Test environment initialization, global stubs (ResizeObserver) |
| `Frontend/tsconfig.spec.json` | TypeScript config for test files |

### Mocks

| File | What it mocks |
|------|---------------|
| `Frontend/src/testing/mocks/lightweight-charts.mock.ts` | `createChart`, `CandlestickSeries`, `LineSeries`, `HistogramSeries` — jsdom has no Canvas API |
| `Frontend/src/testing/mocks/polygon-client.mock.ts` | `@polygon.io/client-js` — ESM-only package |

### Test Factories

`Frontend/src/testing/factories/market-data.factory.ts` provides:
- `createMockAggregate(overrides?)` — single OHLCV bar
- `createMockAggregates(count)` — array of bars with incrementing dates/prices
- `createMockAggregatesTimeSeries(count)` — minute-interval bars with sinusoidal prices
- `createMockSummary(overrides?)` — period statistics
- `createMockTicker(overrides?)` — ticker entity
- `createMockIndicatorSeries(overrides?)` — SMA/EMA/RSI series data

### Component Specs

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

### Service Specs

| Spec | Key tests |
|------|-----------|
| `market-data.service.spec.ts` | GraphQL query/variables mapping, response parsing, indicator queries, error handling |
| `market-monitor.service.spec.ts` | Python REST API integration, caching (shareReplay), clearCache(), error handling |
| `ticker.service.spec.ts` | GraphQL tickers query, aggregate stats mapping |
| `stock-aggregate-store.service.spec.ts` | Cache hit/miss, TTL expiration, invalidation, deduplication |
| `author.service.spec.ts` | Apollo `watchQuery`, response mapping, empty results |
| `book.service.spec.ts` | Apollo `watchQuery`, response mapping, empty results |
| `polygon.service.spec.ts` | Client initialization, environment-based config |

### Replay Engine Specs (extensive)

| Spec | Key tests |
|------|-----------|
| `replay-engine.service.spec.ts` | 40+ tests: playback lifecycle, no-lookahead guarantee, speed control, progress calculation, determinism |
| `replay-strategy.service.spec.ts` | Trade visibility by timestamp, active position detection, no-lookahead for trades |
| `replay-indicator.service.spec.ts` | Indicator filtering by timestamp, progressive revelation, multi-series independence |

### Utility Specs (NEW)

| Spec | Key tests |
|------|-----------|
| `black-scholes.spec.ts` | normCdf/normPdf accuracy, BS pricing with reference values, put-call parity, all 5 Greeks with expected ranges, expiry edge cases, strategy P&L, multi-leg Greeks |
| `date-validation.spec.ts` | 2-year lookback validation, date range errors, market holiday detection, weekend/non-trading day logic, disabled dates for PrimeNG |

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

**Signal-based components** (ta-chart, technical-analysis):
```typescript
fixture.componentRef.setInput('inputName', value);
fixture.detectChanges();
// Read computed values directly from component instance
```

**Replay services** — Use `vi.useFakeTimers()` for deterministic interval testing.

### Running

```bash
cd Frontend

# Run all tests
npx vitest run

# Watch mode
npx vitest

# Single file
npx vitest run src/app/utils/black-scholes.spec.ts

# With coverage
npx vitest run --coverage
```

---

## .NET Backend (xUnit + Moq)

### Project Structure

```
Backend.Tests/
├── Backend.Tests.csproj
├── Helpers/
│   └── TestDbContextFactory.cs           # In-memory EF Core database factory
└── Unit/
    ├── Models/
    │   └── StockAggregateTests.cs        # 5 tests — IsValid() method
    ├── Services/
    │   ├── MarketDataServiceTests.cs     # 11 tests — GetOrCreateTicker, cache hit/miss, upsert, force refresh
    │   ├── LstmServiceTests.cs           # 8 tests — training/validation submit, job status deserialization, models list
    │   ├── BacktestServiceTests.cs       # Strategy execution tests
    │   ├── PolygonServiceTests.cs        # HTTP client integration
    │   ├── TechnicalAnalysisServiceTests.cs  # TA service delegation
    │   ├── SanitizationServiceTests.cs   # Data sanitization logic
    │   └── ReplayDeterminismTests.cs     # Replay determinism guarantees
    └── GraphQL/
        ├── MutationTests.cs              # FetchStockAggregates success/error/empty
        ├── MutationSanitizeAndBacktestTests.cs  # Sanitize + backtest mutations
        └── QueryTests.cs                 # GraphQL query resolvers
```

### Dependencies

| Package | Purpose |
|---------|---------|
| `xunit` | Test framework |
| `Moq` | Mocking `IPolygonService`, `IMarketDataService`, `ILstmService` |
| `Microsoft.EntityFrameworkCore.InMemory` | In-memory database for service tests |
| `coverlet.collector` | Code coverage |

### Key Test Classes

**StockAggregateTests** (5 tests): OHLCV validation rules

**MarketDataServiceTests** (11 tests):
- `GetOrCreateTickerAsync` — new/existing/different markets
- `GetOrFetchAggregatesAsync` — cache hit, cache miss, force refresh
- `FetchAndStoreAggregatesAsync` — insert new, upsert existing, mixed new+existing, empty response, options market detection

**LstmServiceTests** (8 tests):
- `StartTrainingAsync` / `StartValidationAsync` — job submission and error handling
- `GetJobStatusAsync` — training result deserialization (no `num_folds`), validation result deserialization (with `num_folds`), pending/failed states
- `GetModelsAsync` — model list and empty response

**BacktestServiceTests**: Strategy execution with mock aggregates

**ReplayDeterminismTests**: Same data produces identical replay sequences

### Running

```bash
cd Backend.Tests

# Run all tests
dotnet test

# Verbose output
dotnet test --verbosity normal

# Filter by test class
dotnet test --filter "FullyQualifiedName~LstmServiceTests"

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
├── conftest.py                    # Shared fixtures (AsyncClient, make_sample_bars)
├── test_health.py                 # 2 tests — GET /health, GET /
├── test_indicators.py             # 5 tests — POST /api/indicators/calculate
├── test_ta_service.py             # 5 tests — TechnicalAnalysisService unit tests
├── test_aggregates.py             # Aggregates endpoint tests
├── test_snapshot.py               # Snapshot endpoint tests
├── test_market_monitor.py         # Market status/holidays tests
├── test_sanitize_endpoint.py      # Sanitization endpoint tests
├── test_sanitizer.py              # Sanitizer service unit tests
├── test_strategy_engine.py        # 45+ tests — strategy engine (payoff, Greeks, POP, EV, edge cases)
├── test_strategy_endpoint.py      # 9 tests — /api/strategy/analyze endpoint integration + validation
└── ml/
    ├── test_protocols.py          # ML protocol compliance
    ├── test_lstm_model.py         # LSTM model architecture
    ├── test_baseline.py           # Baseline model comparison
    ├── test_walk_forward.py       # Walk-forward validation
    ├── test_prediction_service.py # Prediction service logic
    ├── test_stationarity.py       # ADF/KPSS stationarity tests
    ├── test_metrics.py            # RMSE, MAE, directional accuracy
    ├── test_preprocessing.py      # Feature engineering, scaling
    └── test_trainer.py            # Training pipeline
```

### Key Test Classes (NEW)

**test_strategy_engine.py** (45+ tests):
- `TestPayoffAtExpiry` — single leg, spreads, straddles
- `TestStrategyCost` — debit/credit detection
- `TestBreakevens` — single/double breakeven detection
- `TestMaxProfitLoss` — bounded profit/loss
- `TestWeightedIV` — premium-weighted IV calculation
- `TestInterpolateIV` — IV skew interpolation, iron condor 4-leg
- `TestD2` — Black-Scholes d2 with edge cases
- `TestPOP` — probability of profit ranges, deep ITM/OTM, at-expiry
- `TestExpectedValue` — finite EV, at-expiry EV
- `TestIronCondorPayoff` — max profit in middle, capped loss both sides, breakevens
- `TestNakedPut` — unlimited risk, credit strategy
- `TestCoveredCall` — capped upside
- `TestBearPutSpread` — full analysis
- `TestGreeks` — delta sign, straddle neutrality, gamma/theta signs, quantity scaling, at-expiry

**test_strategy_endpoint.py** (9 tests):
- `TestStrategyEndpoint` — bull call spread, iron condor (full response validation)
- `TestStrategyEndpointValidation` — empty legs, invalid option_type, invalid position, negative strike, zero spot, missing symbol, custom curve_points

### Running

```bash
cd PythonDataService

# Run all tests
python -m pytest tests/ -v

# Single file
python -m pytest tests/test_strategy_engine.py -v

# Only ML tests
python -m pytest tests/ml/ -v

# With coverage
python -m pytest tests/ --cov=app --cov-report=term-missing
```

---

## Known Gotchas

| Issue | Cause | Solution |
|-------|-------|----------|
| Chart spec failures in jsdom | No Canvas API | `lightweight-charts.mock.ts` via `moduleNameMapper` |
| `@polygon.io/client-js` import error | ESM-only package | `polygon-client.mock.ts` via `moduleNameMapper` |
| Apollo service `done()` called multiple times | `watchQuery().valueChanges` is a `BehaviorSubject` | Use `take(1)` for empty-result tests |
| PrimeNG p-table re-sorts component data | `[sortField]` mutates the backing array | Assert data presence, not specific order |
| Random parallel failures on Windows | Jest/Vitest worker contention | `maxWorkers: '50%'` in config |
| Python tests need local deps | Normally runs in Podman container | `pip install fastapi pydantic pandas-ta httpx pytest pytest-asyncio scipy numpy` |
| LstmService `num_folds` branching | Polymorphic JSON deserialization | Tested via FakeHttpMessageHandler in LstmServiceTests |

---

## Coverage Gaps (TODO)

Components still lacking tests:
- `OptionsChainComponent` — complex `visibleRows` computed signal
- `LstmTrainComponent` / `LstmValidateComponent` — ML training UI
- `StrategyBuilderComponent` / `OptionsStrategyLabComponent` — strategy analysis UI
- `SnapshotsComponent` / `TrackedInstrumentsComponent` — data display
- All LSTM chart components (PredictionChart, TrainingHistoryChart, ResidualsChart, FoldMetricsChart)
- `LstmService` (Angular) — async job polling via `interval` + `switchMap`

Backend:
- `Query.cs` LSTM queries (`lstmJobStatus`, `lstmModels`)
- `Mutation.cs` LSTM mutations (`startLstmTraining`, `startLstmValidation`)
- Integration tests (currently all tests mock external dependencies)

Python:
- `POST /api/options/contracts` and `POST /api/options/expirations` — endpoint tests
- `POST /api/predictions/*` — endpoint-level tests for job submission/polling
- Job manager concurrent access tests
