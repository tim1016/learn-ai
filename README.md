# Market Data Dashboard

A full-stack market data analysis platform for fetching, caching, visualizing, and backtesting stock and options data from Polygon.io.

## Architecture

```
Frontend (Angular 21)  -->  Backend (.NET 10 / GraphQL)  -->  Python Data Service (FastAPI)
       :4200                        :5000                            :8000
                                      |                                |
                                 PostgreSQL 16                   Polygon.io API
                                    :5432
```

| Layer | Tech | Purpose |
|-------|------|---------|
| **Frontend** | Angular 21, Apollo Angular, PrimeNG, TradingView lightweight-charts v5 | SPA with charts, tables, and interactive forms |
| **Backend** | .NET 10, Hot Chocolate v15 (GraphQL), EF Core 10, Polly | GraphQL API, data caching, backtesting engine |
| **Data Service** | Python FastAPI, polygon-io client v1.12.5, pandas-ta | Proxy to Polygon.io API, technical indicator calculations |
| **Database** | PostgreSQL 16 | Persistent storage for tickers, OHLCV bars, and options data |
| **Infrastructure** | Podman Compose | 3 containers: `db`, `python-service`, `backend` |

### Smart Caching

The backend implements a cache-first strategy for all market data:

1. Check PostgreSQL for cached data matching the ticker, timespan, and date range
2. If found (cache hit), return immediately from DB
3. If not found (cache miss), fetch from Polygon.io via the Python service, persist to DB, then return
4. Data accumulates over time — subsequent requests for the same range are served instantly from cache

## Pages

### Market Data (`/market-data`)
Fetch OHLCV bars for any ticker across multiple timespans (minute, hourly, daily, weekly, monthly). Results are displayed as candlestick, volume, and line charts with a sortable data table. Each page has an expandable "How to use this page" guide.

### Stock Analysis (`/stock-analysis`)
Bulk-fetch months of minute-level data in automated monthly chunks. Features a chunk queue with live progress tracking, caching detection, and per-chunk refresh. Optionally fetches 0DTE options contracts for each trading day with ATM strike selection. Drill down into chunk-level and day-level detail pages.

### Technical Analysis (`/technical-analysis`)
Overlay SMA, EMA, and RSI indicators on previously fetched data. Powered by pandas-ta on the Python service. Supports intraday through weekly timespans with configurable lookback windows.

### Ticker Explorer (`/ticker-explorer`)
Live options chain viewer with Greeks (delta, theta), IV, open interest, and volume. Displays a traditional call/put chain layout with ATM highlighting and ITM/OTM tinting. Filters by expiration date (defaults to next Friday).

### Strategy Lab (`/strategy-lab`)
Run algorithmic backtests (SMA Crossover, RSI Mean Reversion) on cached data. Displays summary stats (P&L, win rate, Sharpe ratio, max drawdown), an equity curve with multiple chart type options (Lightweight Charts, SVG, PrimeNG/Chart.js), and a full trade log.

### Tickers (`/tickers`)
Inventory of all tracked ticker symbols with TradingView mini-charts, aggregate counts, date ranges, and data sanitization summaries. Tickers are added automatically when you fetch data from any page.

## Prerequisites

- [Podman](https://podman.io/) (or Docker) with Compose support
- [Node.js](https://nodejs.org/) 20+ and npm
- A [Polygon.io](https://polygon.io/) API key (Starter plan or higher)

## Getting Started

### 1. Clone and configure

```bash
git clone <repo-url>
cd learn-ai
```

Create a `.env` file in the project root:

```env
POLYGON_API_KEY=your_polygon_api_key_here
```

### 2. Start the backend services

```bash
podman compose up -d --build
```

This starts three containers:
- **db** (PostgreSQL 16) on port `5432`
- **python-service** (FastAPI) on port `8000`
- **backend** (.NET GraphQL) on port `5000`

Verify they're running:

```bash
podman compose ps
```

### 3. Start the frontend

```bash
cd Frontend
npm install
npx ng serve
```

Open [http://localhost:4200](http://localhost:4200) in your browser.

### 4. Fetch your first data

1. Navigate to **Market Data**
2. Enter a ticker (e.g. `AAPL`), pick a date range, choose "Daily", and click **Fetch Data**
3. Explore the candlestick chart, volume bars, and data table
4. Try **Stock Analysis** for bulk minute-level fetching, or **Strategy Lab** to run a backtest

## Endpoints

| Service | URL | Description |
|---------|-----|-------------|
| Frontend | http://localhost:4200 | Angular dev server |
| GraphQL API | http://localhost:5000/graphql | Hot Chocolate GraphQL endpoint |
| Python Service | http://localhost:8000 | FastAPI Polygon.io proxy |
| PostgreSQL | localhost:5432 | Database (postgres/mysecretpassword) |

## Data Limits (Polygon Starter Plan)

- **Historical data**: Up to 2 years from today
- **Delay**: 15-minute delayed (not real-time)
- **Options**: Greeks, IV, OI, and snapshots included
- **Rate limits**: Unlimited API calls, but the app adds configurable delays between requests

All date pickers enforce the 2-year limit and display a warning if you try to go beyond it.

## Project Structure

```
learn-ai/
  Backend/                        .NET 10 GraphQL API
    GraphQL/                        Query.cs, Mutation.cs
    Models/                         EF Core entities and DTOs
    Services/                       MarketDataService, PolygonService, BacktestService
    Program.cs                      App entry point and DI configuration
  Frontend/                       Angular 21 SPA
    src/app/
      components/
        market-data/                Candlestick, volume, line charts + data table
        stock-analysis/             Chunk queue, chunk detail, day detail
        tickers/                    Ticker inventory + TradingView widgets
          technical-analysis/       SMA, EMA, RSI indicator overlays
        ticker-explorer/            Options chain viewer
        strategy-lab/               Backtesting engine + equity curve charts
      graphql/                      TypeScript types matching GraphQL schema
      services/                     Apollo-based data services
      utils/                        Shared utilities (date validation, etc.)
  PythonDataService/              FastAPI proxy to Polygon.io
    app/
      routers/                      REST endpoints (aggregates, options, snapshot)
      services/                     polygon_client.py (Polygon SDK wrapper)
      models/                       Pydantic request/response models
  compose.yaml                    Podman/Docker Compose orchestration
```

## Sample GraphQL Query

```graphql
query {
  getOrFetchStockAggregates(
    ticker: "AAPL"
    fromDate: "2025-01-02"
    toDate: "2025-01-31"
    timespan: "day"
    multiplier: 1
  ) {
    ticker
    aggregates {
      open high low close volume
      timestamp
      volumeWeightedAveragePrice
    }
    summary {
      periodHigh periodLow
      averageVolume averageVwap
      openPrice closePrice
      priceChange priceChangePercent
      totalBars
    }
  }
}
```

## Rebuilding Services

When you change backend or Python service code:

```bash
# Always use down + up to ensure the container is recreated
podman compose down backend && podman compose up -d --build backend
podman compose down python-service && podman compose up -d --build python-service
```

To reset the database (e.g. after adding new EF Core entities):

```bash
podman compose down db
podman volume rm learn-ai_pgdata
podman compose up -d
```

## Running Tests

```bash
cd Frontend
npx jest

# If you get module resolution errors, clear the cache first:
npx jest --clearCache && npx jest
```

## Stopping the Stack

```bash
podman compose down

# To also remove the database volume (resets all data):
podman compose down -v
```
