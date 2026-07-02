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
| **Data Service** | Python FastAPI, Polygon.io REST client v1.12.5, pandas, pandas-ta, scipy, statsmodels | Polygon.io proxy, indicator calculations, data quality pipeline |
| **Database** | PostgreSQL 16 | Persistent storage for tickers, OHLCV bars, indicators, research experiments |
| **Infrastructure** | Podman Compose | 3 containers: `db`, `python-service`, `backend` |

---

<!-- AUTO-UPDATED:FEATURES — managed by .claude/skills/auto-readme-tick. Edits between the fences will be overwritten. -->

## Features

### Live Paper Trading (IBKR)

End-to-end IBKR paper-trading runtime with safety-first design. Three coordinated surfaces (operator UI, live engine, reconciliation) make a single paper run reproducible, observable, and reversible.

**Broker surface** (`/broker/*`) — Operator UIs for live runs:

- **Bots** (`/broker/bots`) — Catalog of paper/live bots with links into each bot's control panel (`/broker/bots/:id`) for run state, readiness, controls, activity, audit evidence, and reconciliation receipts
- **Account Monitor** (`/broker/account-monitor`) — Live account balances and active positions
- **Orders** (`/broker/orders`) — Order ledger with status, fills, and exchange round-trip detail
- **Reconciliation** (`/broker/reconciliation`) — Daily three-way reconcile (decisions ⨯ fills ⨯ broker state) with category breakdown
- **Options Chain** (`/broker/options-chain`) — Live IBKR options chain for trade entry
- **Broker landing** (`/broker`) — Top-strip session health, IBKR connectivity, and run summary

**Live engine** (`PythonDataService/app/engine/live/`) — Paper-trading runtime ported from LEAN:

- **Halt detection** — `poisoned.flag` protocol, fatal-halt end-to-end pipeline, atomic flag I/O, and `cmd_start` refusal when flags are set
- **Emergency flatten** — force-flat on halt with --readonly safety gates (both CLI and env-driven signals must agree)
- **Indicator-state persistence** — EMA/RSI hydrate across runs (skip the ~3 h 45 m warmup) with policy tri-state (`require` / `optional` / `disabled`); per-run hydration receipt
- **Recovery flatten** — force-flat at 15:55 ET; canonical first-checkpoint write; graceful-shutdown finally with a "newer state wins" check
- **Run ledger** with pre-flight halt rules; artifact writers pinned to reconcile schemas; per-bar DecisionSnapshot publishing
- **Host runner daemon** (`host_daemon.py`) — local FastAPI control plane for start/stop of paper runs from the operator UI. Loopback-only bind, no auth (intentional design — local operator bridge, not a remotely exposed service)

**Parity reconciliation** (`PythonDataService/app/research/parity/`) — QC trade-level parity:

- Eight-category divergence taxonomy: `FIXTURE_INSUFFICIENT`, `DECISION_MISMATCH`, `DIRECTION_MISMATCH`, `QUANTITY_MISMATCH`, `FILL_PRICE_DRIFT`, `COMMISSION_DRIFT`, `PNL_DRIFT`, `ORDER_TYPE_MISMATCH`
- IBKR commission model port (`ibkr_commission.py`) with tier-aware fee computation
- Intraday-trigger fill mode for sub-bar fills (Phase 3.5 Path A)
- Fixture data reader with explicit captures for parity-pinned runs

### Spec Strategy Runner

**Spec Strategy** (`/spec-strategy`) — Plain-English strategy specification with deterministic execution:

- Compose trading conditions in plain English (e.g., "EMA(5) crosses above EMA(10) and RSI(14) < 70")
- Condition catalog with validated mutators — every spec is round-trip-stable through the canonical form
- Strategy store (versioned saved specs) with author attribution
- Canonical fixtures for parity-pinned reference runs
- Inline validation of operator-input specs before execution

### Data Lab

**Data Lab** (`/data-lab`, default landing route) — IDE-style data exploration workstation:

- **Active indicators panel** with per-indicator config modal (parameter grid, validation, preview)
- **Run dock** with bar-timeframe awareness and chunk-readout (knows when historical data fetch will run vs. complete instantly)
- **Quality modal** — inspect data quality (gaps, flat bars, OHLC integrity) before strategy use
- **Interactive chart** with TradingView lightweight-charts v5 (candles, overlays, marker tracks)
- **Past chain inspector** — historical options chain at a chosen date
- **Auto bar-timeframe** and chart-timeframe parsing — type "15m" / "1h" / "1d", the workstation reconfigures
- Built-in documentation pane (`/data-lab-docs`)

### Engine Lab

**Engine Lab** (`/engine`) — Backtesting engine with a Configure surface:

- Strategy execution against historical data with per-bar event replay
- Trade log with entry/exit timestamps, prices, per-trade and cumulative PnL
- Equity curve and drawdown rendering
- Built-in engine docs (`/engine/docs`)

**Lean Engine** (`/lean-engine`) — LEAN backtester integration surface for cross-validating ported strategies against the original framework

**Strategy Builder** (`/strategy-builder`) — Visual strategy construction with drag-to-build interface and instant payoff updates

### Technical Analysis & Indicator Reliability

**Indicator Report** (`/indicator-report`) — Drill-down indicator analysis:

- Compute SMA, EMA, RSI, MACD, Bollinger Bands server-side via pandas-ta with explicit warmup tracking
- **Indicator reliability scoring** (`app/research/indicator_reliability.py`) — stability scoring across volatility and trend regimes, documented in `/docs/indicator-reliability-methodology`
- Per-indicator parity tests against reference implementations (LEAN / TradingView CSV anchors)

### Options Analytics

**Options Lab** (`/options-lab/*`) — Multi-tab options workstation:

- **Chain** (`/options-lab/chain`) — Symmetric calls/puts grid with ATM auto-detection, ITM/OTM coloring, IV/Greeks per leg, expiration ribbon grouped by month
- **Strategy Builder** (`/options-lab/strategy-builder`) — Multi-leg payoff curves (expiration intrinsic + current-time Black-Scholes), Greek curves on a secondary Y-axis, Probability of Profit via lognormal terminal CDF, what-if scenarios (T+N days, ±IV)
- **Strategy Finder** (`/options-lab/strategy-finder`) — Pre-built templates (bull call spread, bear put spread, long straddle, iron condor, iron butterfly, covered call, protective put) with scenario-aware ranking
- **Volatility** (`/options-lab/volatility`) — IV surface analysis, ATM IV30 recorder, IV-skew metrics

**Pricing Lab** (`/pricing-lab`) — Black-Scholes pricing calculator and educational reference; configurable risk-free rate (default 4.3%); QuantLib-backed pricing comparison

### Portfolio Management

**Portfolio Dashboard** (`/portfolio`) — Full event-sourced portfolio tracking system with FIFO lot accounting, risk management, strategy attribution, and built-in system validation. Nine interactive tabs:

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

**Validation** — Frontend-piloted system validation suite that verifies the entire portfolio engine:

- One-click execution of automated tests against a temporary test account
- **FIFO Accounting** — lot closures, realized PnL, remaining quantities, average cost basis
- **Rebuild Determinism** — event sourcing guarantee: positions rebuild identically from trade log
- **Cash Accounting** — cash balance correctness after buys/sells including fees
- **Unrealized PnL** — mark-to-market valuation with explicit prices
- **Snapshot Stability** — Equity = Cash + MarketValue invariant at every snapshot
- **Drawdown Calculation** — peak tracking, drawdown amount and percentage
- **Risk Rule Triggering** — MaxPositionSize violation detection
- **Scenario Engine** — PnL impact under price shocks
- **Equity Invariant** — NetQuantity = sum(lot remaining), fundamental accounting identity
- **Stress Test** — 200 trades across 50 symbols, rebuild < 5 seconds, zero drift
- Per-assertion pass/fail detail with expected vs. actual values, timing, and category breakdown
- Automatic cleanup — test account and all related data are deleted after the suite completes

**Documentation** — Built-in reference with LaTeX-rendered formulas for FIFO algorithm, valuation, performance metrics (Sharpe, Sortino, Calmar), risk engine, scenario analysis, strategy attribution, position lifecycle, snapshot sampling, reconciliation process, notation glossary, and data model summary.

**Architecture**:

- **Event-sourced**: Trades are the single source of truth; positions are derived via FIFO lot matching
- **Multiplier-aware**: Options use contract multiplier (default 100) for correct market value, delta, and PnL
- **Position lifecycle**: Open → Closed (with ClosedAt timestamp when all lots are consumed)
- **Metrics**: Sharpe (√252 annualized, sample stddev), Sortino (downside deviation only), Calmar (return / max drawdown)
- **Self-validating**: Built-in validation suite tests all subsystems (accounting, valuation, risk, scenarios, rebuild) from the UI

### Research Lab (Alpha Validation)

A multi-tab experimental platform (`/research-lab`) for systematic alpha research, with Build Alpha-style functionality validation built in.

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

**Walk-forward validation** — OOS validation with rolling train/test windows (`/research-lab` + `app/routers/walk_forward.py`)

**Monte Carlo** — Simulation-based confidence-interval lab (`app/research/monte_carlo/` + `app/routers/monte_carlo.py`) — perturbation under noise, shifted-data, slippage, and cost grids

**Null baselines** — Random/shuffled/permuted reference distributions for overfitting detection (`app/research/baselines/` + `app/routers/baselines.py`)

**Parameter sensitivity** — Sensitivity grids with parsimony scoring across strategy hyperparameters

**ML predictions parity** — QuantConnect precomputed-predictions parity; predictions-as-data plumbing for LEAN compatibility (`app/research/ml/`)

**Batch Runner** — Run signals across multiple tickers in parallel for cross-sectional consistency testing

**Options Feature Research** — Options-specific alpha validation targeting directional or IV-based return forecasts (`app/research/options_runner.py`)

**Research Divergence** — Compare two research runs and classify the divergence (`app/research/divergence/`)

**Signal Report** (`/research-lab/signal-report/:id`) — Detailed experiment report with walk-forward equity curves, graduation scorecard, backtesting grid, and alpha decay metrics

**Methodology docs** — `/docs/signal-engine-methodology` and `/docs/indicator-reliability-methodology`

### Volatility & Edge Research

**Edge Lab** (`/edge/*`) — Volatility-anchored research surface:

- **Realized vs IV** (`/edge/realized-vs-iv`) — Realized vol vs implied vol divergence and term-structure analysis
- **Cross-Asset** (`/edge/cross-asset`) — Cross-asset implications, correlations, and lead-lag relationships
- **Regimes** (`/edge/regimes`) — Volatility regime detection and persistence

### Tracked Instruments & Reference Data

**Tracked Instruments** (`/tracked-instruments`) — Curated watchlist with expandable detail panels showing company info, related tickers, and inline data sanitization summaries

**Golden Fixtures** (`/golden-fixtures`) — Browse the canonical reference fixtures used for numerical parity tests (see `PythonDataService/tests/fixtures/golden/`); attribution and regeneration commands per fixture

### Data Quality & Validation

**Data Quality Pipeline** (`/data-quality`) — 7-step automated cleanup pipeline for minute-level OHLCV data with before/after reporting:

- **Step 1 — Session Filter**: NYSE calendar-aware filtering using `pandas_market_calendars` (handles early-close days)
- **Step 2 — Fix Volume**: Corrects zero/missing volume bars
- **Step 3 — Recompute VWAP**: Recalculates VWAP from OHLCV when source data is suspect
- **Step 4 — Remove Flat Bars**: Detects and removes bars where O=H=L=C (no price movement)
- **Step 5 — OHLC Integrity**: Validates high >= low, all prices > 0
- **Step 6 — Normalize Timezone**: Ensures consistent US/Eastern timestamps
- **Step 7 — Recompute Indicators**: Recalculates technical indicators on cleaned data
- Per-step summary statistics (rows removed, percentage impact)
- CSV download of cleaned data for offline analysis
- Built-in pipeline documentation page (`/data-quality-docs`)

**Validation Study** (`app/routers/validation_study.py`) — Methodology validation across multiple external sources, with documented references

<!-- /AUTO-UPDATED:FEATURES -->

---

## Data Pipeline

### Smart Caching

The system follows a **DB-first** approach for all market data:

1. Check PostgreSQL for cached data matching the requested ticker, timeframe, and date range
2. On cache miss, fetch from Polygon.io through the Python data service
3. Sanitize data (native pandas/numpy) — removes duplicates, handles nulls, clips outliers by quantile
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
| PostgreSQL | localhost:5432 | Database (credentials in `.env`) |

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

## Container Operations

### Dev vs. Production containers

`compose.yaml` is the **dev** config — it mounts source code, uses the full .NET SDK image, and enables hot-reload (`dotnet watch`, `uvicorn --reload`). The Dockerfiles under `Backend/Dockerfile` and `PythonDataService/Dockerfile` produce lean **production** runtime images (multi-stage builds). To validate the production Dockerfiles still build:

```bash
podman build -t marketscope-backend ./Backend
podman build -t marketscope-python ./PythonDataService
```

### Debugging containers

```bash
# View live logs
podman compose logs -f python-service

# Shell into a running container
podman compose exec python-service bash

# Check healthcheck status
podman inspect --format='{{json .State.Health}}' polygon-data-service
```

### Data persistence

PostgreSQL data lives in the `pgdata` named volume and survives `podman compose down`. To backup or reset:

```bash
# Backup
podman exec my-postgres pg_dump -U postgres postgres > backup.sql

# Full reset (destroys all data)
podman compose down -v
```

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
                                    PortfolioRiskService, PortfolioReconciliationService, StrategyAttributionService,
                                    PortfolioValidationService
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
        data-quality/               Data quality 7-step pipeline with before/after reporting
        data-lab/                   Indicator validation report (pandas-ta vs TradingView CSV)
      graphql/                      TypeScript types matching GraphQL schema
        portfolio/                    Dashboard, positions, equity chart, risk, scenarios, reconciliation, attribution, validation
      services/                     Angular services (MarketData, Research, LSTM, Replay, Portfolio)
      utils/                        Shared utilities (Black-Scholes calculator, date validation)
    src/testing/                  Test factories for mock data
  PythonDataService/              FastAPI proxy to Polygon.io
    app/
      routers/                      REST endpoints (aggregates, options, snapshots, indicators, research, predictions, strategy, market, tickers, trades, data_quality, dataset)
      services/                     PolygonClient, DataSanitizer, DataQualityService, TechnicalAnalysis, StrategyEngine, LSTM, SignalEngine
      models/                       Pydantic request/response models
    tests/                          pytest test suite
  compose.yaml                    Podman/Docker Compose (3 services)
```
