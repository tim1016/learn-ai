# Market Data Dashboard

A full-stack application with a .NET 10 GraphQL backend, Python FastAPI data service, and Angular 21 frontend for fetching, caching, and visualizing stock market data.

## Architecture

```
Frontend (Angular 21)  -->  Backend (.NET 10 GraphQL)  -->  Python FastAPI  -->  Polygon.io API
     :4200                       :5000                        :8000
                                    |
                                    v
                              PostgreSQL :5432
```

- **Frontend**: Angular 21 standalone components, Apollo Angular for GraphQL, TradingView lightweight-charts for visualizations
- **Backend**: .NET 10 with Hot Chocolate v15 GraphQL, Entity Framework Core 10, Polly for resilience
- **Python Service**: FastAPI microservice that proxies and sanitizes Polygon.io API data
- **Database**: PostgreSQL 16 for persistent storage

## Features

### Smart Caching
The backend implements a smart caching strategy for market data:
1. When a query is received, check PostgreSQL for cached data matching the ticker/timespan/date range
2. If found (cache hit), return immediately from DB
3. If not found (cache miss), fetch from Polygon.io via the Python service, persist to DB, then return
4. Data accumulates over time — subsequent requests for the same data are served from cache

### Market Data Dashboard (`/market-data`)
- Search by ticker symbol (AAPL, MSFT, etc.) with date range and timespan selectors
- **Candlestick Chart**: OHLC price data using TradingView lightweight-charts
- **Volume Chart**: Volume bars colored green/red based on price direction
- **Line Chart**: Closing price trend line
- **Summary Stats**: Period high/low, average volume, VWAP, price change with color coding

### Demo Pages
- **/books** — View all books with author information (seed data)
- **/authors** — View all authors with their books

## Prerequisites

- Podman or Docker
- Node.js 20+
- npm

## Quick Start

1. Start the backend stack (PostgreSQL, Python service, .NET backend):

   ```bash
   podman compose up -d
   # or: docker compose up -d
   ```

2. Wait for all services to be ready:

   ```bash
   podman logs -f my-backend
   # Look for: "Now listening on: http://[::]:8080"
   ```

3. In a new terminal, start the frontend:

   ```bash
   cd Frontend
   npm install
   npm start
   ```

4. Open http://localhost:4200 in your browser

## Endpoints

| Service         | URL                            | Description                     |
|-----------------|--------------------------------|---------------------------------|
| Frontend        | http://localhost:4200           | Angular dev server              |
| GraphQL API     | http://localhost:5000/graphql   | Hot Chocolate GraphQL endpoint  |
| Python Service  | http://localhost:8000           | FastAPI Polygon.io proxy        |
| PostgreSQL      | localhost:5432                  | Database (postgres/mysecretpassword) |

## Sample GraphQL Queries

```graphql
# Smart query: fetches from cache or Polygon.io
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

# Get all books (demo)
query {
  books {
    id title publishedYear
    author { name }
  }
}
```

## Project Structure

```
learn-ai/
├── Backend/                    # .NET 10 GraphQL API
│   ├── GraphQL/
│   │   ├── Query.cs            # GraphQL queries (books, market data, smart caching)
│   │   ├── Mutation.cs         # GraphQL mutations (CRUD, fetch from Polygon)
│   │   └── Types/              # GraphQL result types (DTOs)
│   ├── Services/
│   │   ├── Interfaces/         # IMarketDataService, IPolygonService
│   │   └── Implementation/     # MarketDataService (smart cache), PolygonService (HTTP client)
│   ├── Models/                 # EF Core entities (Ticker, StockAggregate, etc.)
│   ├── Data/                   # AppDbContext, seed data
│   ├── Dockerfile              # Multi-stage .NET 10 build
│   └── Backend.csproj
├── PythonDataService/          # FastAPI Polygon.io proxy
├── Frontend/                   # Angular 21 application
│   └── src/app/
│       ├── components/
│       │   └── market-data/    # Market data dashboard
│       │       ├── candlestick-chart/
│       │       ├── line-chart/
│       │       ├── volume-chart/
│       │       └── summary-stats/
│       ├── graphql/            # Queries and type definitions
│       └── services/           # Angular services (Apollo-based)
├── compose.yaml                # Podman/Docker compose (3 services + PostgreSQL)
└── README.md
```

## Environment Variables

| Variable         | Service        | Description                    |
|------------------|----------------|--------------------------------|
| `POLYGON_API_KEY`| Python service | Polygon.io API key (required)  |

Set in a `.env` file at the project root or export before running compose.

## Stopping the Stack

```bash
podman compose down
# To also remove the database volume (resets all data):
podman compose down -v
```
