# MarketScope - Quantitative Trading Research Platform

A full-stack quantitative trading research platform for US equity and options markets. Built for systematic alpha research, options analytics, strategy backtesting, and ML-based forecasting — all powered by Polygon.io market data.

## Architecture

```
┌──────────────┐     GraphQL      ┌──────────────┐     REST/HTTP     ┌──────────────────┐
│   Angular 21 │ ◄──────────────► │  .NET 10     │ ◄──────────────► │  Python FastAPI   │
│   Frontend   │     Apollo       │  Backend     │     Polly        │  Data Service     │
│              │                  │  Hot Choc v15│                  │  Polygon.io SDK   │
└──────────────┘                  └──────┬───────┘                  └──────────────────┘
                                         │ EF Core 10
                                  ┌──────┴───────┐
                                  │ PostgreSQL 16│
                                  └──────────────┘
```

**Deployment**: Podman Compose with 3 containers (PostgreSQL, Python data service, .NET backend). The Angular frontend runs locally via `ng serve`.

| Layer | Tech | Purpose |
|-------|------|---------|
| **Frontend** | Angular 21, Apollo Angular, PrimeNG, Tailwind CSS, TradingView lightweight-charts v5 | SPA with interactive charts, tables, and forms |
| **Backend** | .NET 10, Hot Chocolate v15 (GraphQL), EF Core 10, Polly | GraphQL API, data caching, backtesting engine |
| **Data Service** | Python FastAPI, Polygon.io REST client v1.12.5, pandas, pandas-ta, TensorFlow/Keras | Polygon.io proxy, indicator calculations, ML training |
| **Database** | PostgreSQL 16 | Persistent storage for tickers, OHLCV bars, indicators, research experiments |
| **Infrastructure** | Podman Compose | 3 containers: `db`, `python-service`, `backend` |

---

## Features

### Market Data & Visualization

**Aggregate Fetcher** (`/market-data`) — Fetch and cache OHLCV bars for any US ticker across multiple timeframes (minute, hour, day, week, month). The system checks PostgreSQL first and only calls Polygon.io on cache miss. Features include:

- Candlestick, volume, and closing price line charts (TradingView lightweight-charts v5)
- Sortable, paginated data table with VWAP calculations
- Gap detection — analyzes data coverage against the market calendar, flags missing dates and partial trading days
- Live market calendar with trading holidays
- Real-time fetch progress polling during large requests

**Stock Analysis** (`/stock-analysis`) — Bulk-fetch months of minute-level data using a monthly chunking algorithm:

- Cache-aware execution — pre-checks which months already have data
- Configurable inter-chunk delays (default 12s) to stay within API rate limits
- 0DTE options integration — detects trading days, calculates ATM strikes, fetches minute data for ATM +/- 2 ITM/OTM contracts
- Per-chunk progress tracking with abort capability
- Drill-down into chunk-level and day-level detail pages

### Technical Analysis

Calculate and overlay indicators (`/technical-analysis`) on cached price data, computed server-side via pandas-ta:

- **SMA** (Simple Moving Average) — configurable window (default 20)
- **EMA** (Exponential Moving Average) — configurable window (default 50)
- **RSI** (Relative Strength Index) — configurable period (default 14)
- **MACD** — with signal line and histogram
- **Bollinger Bands** — with upper/lower bands

Results are cached to prevent redundant calculations.

### Options Analytics

**Options Chain** (`/options-chain`) — Professional dark-themed options chain viewer:

- Expiration ribbon with horizontal scrolling grouped by month
- Symmetrical chain table: Calls (vega, theta, gamma, delta, price, OI, volume) | Strike + IV% | Puts (mirrored)
- ATM strike auto-detection with amber highlighting and auto-scroll
- ITM/OTM zone coloring (emerald for ITM calls, red for ITM puts, dimmed OTM)
- Volume bars with proportional colored fills
- Configurable strike range (5-50 strikes around ATM) with "Show All" toggle
- Contract detail drawer — click any cell to view candlestick charts, all Greeks, IV, OI, bid/ask, break-even price, and historical summary stats
- Smart price resolution: day close -> last trade -> quote midpoint -> bid/ask midpoint
- Stock snapshot header with real-time price and daily change

**Options Strategy Lab** (`/options-strategy-lab`) — Build and analyze multi-leg options strategies:

- **Pre-built templates**: Bull call spread, bear put spread, long straddle, iron condor, iron butterfly, covered call, protective put
- **Manual leg builder**: Up to 8 legs with per-leg controls (strike, type, position, premium, IV, quantity)
- **Live payoff curve**: Instantly updates as legs change
  - Expiration payoff (intrinsic value)
  - Current-time P&L (Black-Scholes priced, dashed blue line)
  - What-if scenarios: T+5d, IV +/- 10%, custom overlays
- **Greek curves**: Delta, gamma, theta, vega, rho plotted on secondary Y-axis
- **Probability of Profit (PoP)**: Lognormal terminal distribution with CDF
- **Break-even detection**: Linear interpolation across the payoff curve
- **Diagnostic table**: Full Black-Scholes breakdown (d1, d2, N(d1), price, delta, intrinsic, P&L) at sample prices
- Max profit / max loss / breakeven prices / aggregate Greeks / strategy cost (debit/credit)
- Configurable risk-free rate (default 4.3%) and price range (+/- 5% to +/- 50%)

**Strategy Builder** (`/strategy-builder`) — Quick options chain viewing with drag-to-build strategy construction and instant payoff updates.

**Options History** (`/options-history`) — Historical 0DTE options contract lookup for a specific date. Scans ATM +/- N strikes with per-contract daily bar data and relative strike positioning.

### Portfolio Management

**Portfolio Dashboard** (`/portfolio`) — Full event-sourced portfolio tracking system with FIFO lot accounting, risk management, and strategy attribution. Seven interactive tabs:

**Dashboard** — Account summary with cash balance, position count, performance metrics (Sharpe, Sortino, Calmar, max drawdown, win rate, profit factor), trade recording form, and recent trades table. Take point-in-time snapshots to build your equity curve.

**Positions** — View all open/closed positions with FIFO lot drill-down. Each position expands to show individual lots with entry price, remaining quantity, and per-lot realized PnL. Rebuild positions from the trade log to verify consistency.

**Equity Curve** — Interactive TradingView lightweight-charts visualization:

- Area chart of portfolio equity over time (built from snapshots)
- Histogram drawdown chart with color-coded severity (orange < 5%, red > 5%)
- Metrics summary bar: total return, Sharpe, Sortino, Calmar, max drawdown

**Risk Engine** — Configure and evaluate portfolio risk rules:

- **Rule types**: MaxDrawdown, MaxPositionSize, MaxVegaExposure, MaxDelta
- **Actions**: Warn or Block, with Low/Medium/High/Critical severity
- **Dollar Delta exposure**: Per-position delta × price × quantity × multiplier breakdown
- **Risk evaluation**: Real-time rule checks against current portfolio valuation
- Toggle rules on/off, view last-triggered timestamps

**Scenario Explorer** — Stress test the portfolio with configurable shocks:

- **Preset scenarios**: Market Crash (-20%), Correction (-10%), Rally (+10%), Vol Spike, Theta Decay
- **Custom inputs**: Price change %, IV change %, time forward (days)
- Per-position impact breakdown showing current vs. scenario value
- Options positions reflect vega (IV changes) and theta (time decay) impacts

**Reconciliation** — Detect and fix position drift:

- Compares cached position state vs. positions rebuilt from the trade log
- Reports drift type: Mismatch, ExtraInCache, MissingFromCache
- Shows quantity and PnL differences per position
- One-click Auto-Fix rebuilds positions from the authoritative trade log

**Strategy Attribution** — Connect backtest results to portfolio performance:

- Import trades from backtest strategy executions into the portfolio
- Per-strategy PnL breakdown with win rate and trade count
- Alpha contribution bar chart — see which strategies drive returns
- Contribution percentages showing each strategy's share of total PnL
- Links to the existing backtest system (`/strategy-lab`)

**Architecture**:

- **Event-sourced**: Trades are the single source of truth; positions are derived via FIFO lot matching
- **Multiplier-aware**: Options use contract multiplier (default 100) for correct market value, delta, and PnL
- **Position lifecycle**: Open → Closed (with ClosedAt timestamp when all lots are consumed)
- **Metrics**: Sharpe (√252 annualized, sample stddev), Sortino (downside deviation only), Calmar (return / max drawdown)
- **Full documentation**: See `docs/portfolio-system.md` for entity schemas, service APIs, GraphQL operations, and formulas

### Strategy Backtesting

**Backtest Engine** (`/strategy-lab`) — Execute trading strategies against historical data:

- **SMA Crossover**: Configurable short/long windows (default 10/30)
- **RSI Mean Reversion**: Configurable window (14), oversold (30), overbought (70) thresholds
- **Results**: Win/loss count, total PnL, max drawdown, Sharpe ratio
- **Equity curve**: Multiple chart type options (Lightweight Charts, SVG, PrimeNG/Chart.js)
- **Full trade log**: Entry/exit timestamps, prices, per-trade and cumulative PnL

**Replay Mode** — Step through historical price action bar-by-bar:

- Indicator overlays (SMA, EMA, RSI) rendered in real-time
- Trade entry/exit markers from backtests overlaid on chart
- Play, pause, step, and speed controls
- Live P&L tracking as trades execute

### Research Lab

A multi-tab experimental platform (`/research-lab`) for systematic alpha research:

**Feature Runner** — Validate alpha features (momentum, RSI, VWAP deviation, etc.) using Information Coefficient (IC) analysis:

- IC time series and t-statistics (Newey-West adjusted for autocorrelation)
- Stationarity tests (ADF + KPSS p-values)
- Quantile bin analysis with monotonicity check across return deciles
- Configurable ticker, date range, and timespan

**Signal Engine** — Full walk-forward backtesting and graduation pipeline:

- Z-score standardized signals with threshold/cost grid search
- Walk-forward validation with rolling train/test windows
- Regime gating — filters out trading in choppy markets (volatility + trend detection)
- Graduation criteria: OOS Sharpe, win rate, max drawdown, parameter stability
- Alpha decay analysis and signal diagnostics
- Effective sample size (autocorrelation-adjusted)

**Robustness Report** — Comprehensive diagnostics for signal stability:

- Monthly IC breakdown with mean, t-stat, and consistency metrics
- Volatility regime analysis (high/low vol performance)
- Trend regime analysis (trending vs. mean-reverting markets)
- Train/test split validation (overfit detection)
- Structural break detection (significant IC changes over time)
- Rolling t-statistic smoothing for stability assessment

**Batch Runner** — Run signals across multiple tickers in parallel for cross-sectional consistency testing.

**Options Feature Research** — Options-specific alpha validation targeting directional or IV-based return forecasts.

**Signal Report** (`/research-lab/signal-report/:id`) — Detailed experiment report with walk-forward equity curves, graduation scorecard, backtesting grid, and alpha decay metrics.

**Options Math Docs** — Educational reference for Black-Scholes pricing, Greeks definitions, and interactive examples.

### LSTM Predictions

Deep learning pipeline for price forecasting under `/lstm/*`:

**Training** (`/lstm/train`) — Submit background LSTM training jobs with configurable:

- Epochs (default 50), sequence length (default 60), feature selection (close, OHLC)
- Scaler type: standard, minmax, robust
- Preprocessing: log returns toggle, winsorization toggle
- Mock mode for quick testing
- Results: RMSE, MAE, improvement vs. naive baseline, loss curves, residual analysis, stationarity tests (ADF/KPSS)

**Validation** (`/lstm/validate`) — K-fold cross-validation with per-fold metrics: RMSE, MAE, R-squared, directional accuracy, Sharpe ratio.

**Predictions** (`/lstm/predictions`) — Generate forward forecasts from trained models with confidence intervals.

**Model History** (`/lstm/models`) — Browse, compare, and manage all trained model artifacts with hyperparameters and training performance.

### Snapshots & Market Data

**Snapshots** (`/snapshots`) — Four snapshot modes in a single tabbed view:

- **Single Ticker** — Detailed real-time snapshot (price, change, bid/ask, volume, VWAP)
- **Market Movers** — Top gainers/losers ranked by percentage change
- **Multi-Ticker** — Side-by-side comparison of multiple tickers
- **Unified** — Advanced query with configurable limits and structured output

**Tracked Instruments** (`/tracked-instruments`) — Curated watchlist of 50 major US stocks (NVDA, TSLA, AAPL, AMZN, MSFT, GOOGL, META, AMD, etc.) with expandable detail panels showing company info (description, IPO date, SIC code, address) and related tickers.

**Tickers** (`/tickers`) — Auto-populated inventory of all tracked symbols with TradingView mini-chart widgets, aggregate counts, date ranges, and data sanitization summaries.

---

## Data Pipeline

### Smart Caching

The system follows a **DB-first** approach for all market data:

1. Check PostgreSQL for cached data matching the requested ticker, timeframe, and date range
2. On cache miss, fetch from Polygon.io through the Python data service
3. Sanitize data (pandas-dq Fix_DQ) — removes duplicates, handles nulls, forward-fills gaps
4. Persist to PostgreSQL with deduplication (upsert)
5. Return the result with a sanitization summary (original count, cleaned count, removal %)

Data accumulates over time — subsequent requests for the same range are served instantly from cache.

### Windowed Fetching

Large date ranges are split into manageable windows (1-month for minute data, 3-month for hourly) to stay within Polygon.io's 50,000-bar pagination limit. Each window's progress is tracked independently via a `ConcurrentDictionary` and queryable in real-time from the frontend (polled every 2 seconds).

### Resilience

The .NET backend uses Polly policies for all outbound HTTP calls:

- **Retry**: 3 attempts with exponential backoff (2, 4, 8 seconds)
- **Circuit Breaker**: Opens after 5 consecutive failures, stays open for 30 seconds
- **Timeouts**: 120s standard, 300s for LSTM training, 600s for research operations

---

## Database Schema

| Entity | Purpose |
|--------|---------|
| **Ticker** | Reference symbol with metadata, market type, sanitization summary |
| **StockAggregate** | OHLCV bars with VWAP, transaction count, timespan, and multiplier |
| **Trade** | Individual trade records with exchange and condition codes |
| **Quote** | Bid/ask snapshots |
| **TechnicalIndicator** | Computed indicators with signal/histogram/band values |
| **StrategyExecution** | Backtest results with PnL, drawdown, Sharpe ratio |
| **BacktestTrade** | Individual trades from backtest executions |
| **ResearchExperiment** | Alpha validation results (IC, t-stat, stationarity, monotonicity) |
| **SignalExperiment** | Signal testing results (OOS Sharpe, threshold, cost) |
| **OptionsIvSnapshot** | Cached IV data (30d ATM/call/put) |
| **Account** | Portfolio account (Paper/Live/Backtest) with cash tracking |
| **Order** | Order lifecycle (Pending → Filled/Cancelled) |
| **PortfolioTrade** | Executed trade (source of truth for positions) |
| **Position** | Derived position state (NetQuantity, AvgCostBasis, RealizedPnL) |
| **PositionLot** | FIFO lot for cost basis and realized PnL tracking |
| **OptionContract** | Reusable option contract reference (strike, expiry, multiplier) |
| **OptionLeg** | Greeks snapshot at trade entry (IV, delta, gamma, theta, vega) |
| **PortfolioSnapshot** | Point-in-time equity, cash, Greeks, PnL capture |
| **RiskRule** | Configurable risk rule (MaxDrawdown, MaxPositionSize, etc.) |
| **StrategyAllocation** | Links account to strategy execution with capital allocated |
| **StrategyTradeLink** | Maps portfolio trades to backtest strategy executions |

Key indexes: composite `(TickerId, Timestamp, Timespan)` on StockAggregate for fast range queries. Unique constraint on `(Symbol, Market)` for Ticker. Composite `(AccountId, TickerId, Status)` on Position for portfolio queries.

---

## Getting Started

### Prerequisites

- [Podman](https://podman.io/) (or Docker) with Compose support
- [Node.js](https://nodejs.org/) 20+ and npm
- A [Polygon.io](https://polygon.io/) API key (Starter plan or higher)
- Optional: [FRED](https://fred.stlouisfed.org/) API key for risk-free rate curves

### 1. Clone and configure

```bash
git clone <repo-url>
cd learn-ai
```

Create a `.env` file in the project root:

```env
POLYGON_API_KEY=your_polygon_api_key_here
FRED_API_KEY=your_fred_api_key       # optional
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

### Endpoints

| Service | URL | Description |
|---------|-----|-------------|
| Frontend | http://localhost:4200 | Angular dev server |
| GraphQL API | http://localhost:5000/graphql | Hot Chocolate endpoint (Banana Cake Pop playground) |
| Python Service | http://localhost:8000 | FastAPI data service (health check at `/health`) |
| PostgreSQL | localhost:5432 | Database (postgres/mysecretpassword) |

---

## Data Limits (Polygon Starter Plan)

- **Historical data**: Up to 2 years from today
- **Delay**: 15-minute delayed (not real-time)
- **Options**: Greeks, IV, OI, and snapshots included (live/unexpired contracts only)
- **Rate limits**: Unlimited API calls, but the app adds configurable delays between bulk requests

All date pickers enforce the 2-year limit and display a warning if you try to go beyond it.

---

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

---

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

> **Note**: EF Core's `EnsureCreated()` does nothing if any tables already exist. New entities require a volume reset or switching to EF migrations.

## Running Tests

```bash
# Frontend (Vitest via Angular CLI)
cd Frontend && npx ng test

# Backend (.NET xUnit)
cd Backend.Tests && dotnet test

# Python (pytest)
cd PythonDataService && python -m pytest tests/ -v
```

## Stopping the Stack

```bash
podman compose down

# To also remove the database volume (resets all data):
podman compose down -v
```

---

## Project Structure

```
learn-ai/
  Backend/                        .NET 10 GraphQL API
    GraphQL/                        Query.cs, Mutation.cs (Hot Chocolate resolvers)
    Models/
      MarketData/                   EF Core entities (Ticker, StockAggregate, SignalExperiment...)
      Portfolio/                    Account, Order, PortfolioTrade, Position, PositionLot, OptionContract, RiskRule, etc.
      DTOs/                         Data transfer objects (ResearchReportDto, SignalModels...)
    Services/
      Implementation/               MarketDataService, PolygonService, BacktestService, ResearchService, LstmService,
                                    PositionEngine, PortfolioService, PortfolioValuationService, SnapshotService,
                                    PortfolioRiskService, PortfolioReconciliationService, StrategyAttributionService
      Interfaces/                   Service contracts (IMarketDataService, IResearchService, IPositionEngine, etc.)
    Data/                           AppDbContext (EF Core)
    Program.cs                      App entry point and DI configuration
  Backend.Tests/                  xUnit + Moq test suite
    Unit/Services/                  Service-level unit tests
    Unit/GraphQL/                   GraphQL resolver integration tests
    Helpers/                        TestDbContextFactory, FakeHttpMessageHandler
  Frontend/                       Angular 21 SPA
    src/app/
      components/
        market-data/                Candlestick, volume, line charts + data table
        stock-analysis/             Chunk queue, chunk detail, day detail
        tickers/                    Ticker inventory + TradingView widgets
        technical-analysis/         SMA, EMA, RSI indicator overlays
        options-chain-v2/           TradingView-style options chain (dark theme)
        options-strategy-lab/       Multi-leg strategy payoff analysis
        strategy-lab/               Backtesting engine + replay mode
        strategy-builder/           Visual strategy construction
        research-lab/               Feature research + signal engine pipeline
          signal-report-page/         Walk-forward, graduation, backtesting grid
        lstm/                       LSTM train, validate, predictions, models
        snapshots/                  Market movers and multi-ticker snapshots
        tracked-instruments/        Watchlist with unified snapshots
        options-history/            Historical 0DTE contract lookup
      graphql/                      TypeScript types matching GraphQL schema
        portfolio/                    Dashboard, positions, equity chart, risk, scenarios, reconciliation, attribution
      services/                     Angular services (MarketData, Research, LSTM, Replay, Portfolio)
      utils/                        Shared utilities (Black-Scholes calculator, date validation)
    src/testing/                  Test factories for mock data
  PythonDataService/              FastAPI proxy to Polygon.io
    app/
      routers/                      REST endpoints (aggregates, options, snapshots, indicators, research, predictions, strategy, market, tickers, trades)
      services/                     PolygonClient, DataSanitizer, TechnicalAnalysis, StrategyEngine, LSTM, SignalEngine
      models/                       Pydantic request/response models
    tests/                          pytest test suite
  compose.yaml                    Podman/Docker Compose (3 services)
```
