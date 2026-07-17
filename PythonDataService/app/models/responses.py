"""Pydantic response schemas"""

from typing import Any

from pydantic import BaseModel, Field


class SanitizedDataResponse(BaseModel):
    """Standard response schema for sanitized data"""

    success: bool
    data: list[dict[str, Any]]
    summary: dict[str, Any]
    ticker: str
    data_type: str
    error: str | None = None


class SanitizeResponse(BaseModel):
    """Response schema for the standalone /api/sanitize endpoint"""

    success: bool
    data: list[dict[str, Any]]
    summary: dict[str, Any]
    error: str | None = None


class IndicatorDataPoint(BaseModel):
    """A single indicator value at a timestamp"""

    timestamp: int
    value: float | None = None
    signal: float | None = None
    histogram: float | None = None
    upper: float | None = None
    lower: float | None = None


class IndicatorResult(BaseModel):
    """Result for a single indicator calculation"""

    name: str
    window: int
    data: list[IndicatorDataPoint]


class OptionsContractItem(BaseModel):
    """A single options contract"""

    ticker: str
    underlying_ticker: str | None = None
    contract_type: str | None = None
    strike_price: float | None = None
    expiration_date: str | None = None
    exercise_style: str | None = None
    shares_per_contract: float | None = None
    primary_exchange: str | None = None


class OptionsContractsResponse(BaseModel):
    """Response schema for options contracts listing"""

    success: bool
    contracts: list[OptionsContractItem] = []
    count: int = 0
    error: str | None = None


class OptionsExpirationsResponse(BaseModel):
    """Response schema for unique options expiration dates"""

    success: bool
    expirations: list[str] = []
    count: int = 0
    error: str | None = None


class GreeksSnapshot(BaseModel):
    """Greeks for an options contract snapshot"""

    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


class DaySnapshot(BaseModel):
    """Day OHLCV for an options contract snapshot"""

    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    vwap: float | None = None


class LastTradeSnapshot(BaseModel):
    """Last trade for an options contract snapshot"""

    price: float | None = None
    size: float | None = None
    exchange: int | None = None
    conditions: list[int] | None = None
    sip_timestamp: int | None = None
    timeframe: str | None = None


class LastQuoteSnapshot(BaseModel):
    """Last quote (bid/ask) for an options contract snapshot"""

    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    midpoint: float | None = None
    timeframe: str | None = None
    last_updated: int | None = None


class OptionsContractSnapshotItem(BaseModel):
    """A single options contract snapshot with greeks and day data"""

    ticker: str | None = None
    contract_type: str | None = None
    strike_price: float | None = None
    expiration_date: str | None = None
    break_even_price: float | None = None
    implied_volatility: float | None = None
    open_interest: float | None = None
    greeks: GreeksSnapshot | None = None
    day: DaySnapshot | None = None
    last_trade: LastTradeSnapshot | None = None
    last_quote: LastQuoteSnapshot | None = None


class UnderlyingSnapshot(BaseModel):
    """Underlying asset info from snapshot"""

    ticker: str
    price: float | None = 0
    change: float | None = 0
    change_percent: float | None = 0


class OptionsChainSnapshotResponse(BaseModel):
    """Response schema for options chain snapshot.

    The ``risk_free_rate`` and ``dividend_yield`` fields are sourced from FRED
    (DGS1MO interpolated) and Polygon TTM dividends respectively (Step 8 of
    IV-RV alignment). They replace the historical hardcoded `r=0.043, q=0`
    defaults used by pricing-lab, options-strategy-lab, strategy-builder.
    """

    success: bool
    underlying: UnderlyingSnapshot | None = None
    contracts: list[OptionsContractSnapshotItem] = []
    count: int = 0
    risk_free_rate: float | None = None
    dividend_yield: float | None = None
    rate_source: str | None = None
    dividend_source: str | None = None
    error: str | None = None


class CalculateIndicatorsResponse(BaseModel):
    """Response from indicator calculation"""

    success: bool
    ticker: str
    indicators: list[IndicatorResult] = []
    error: str | None = None


# ------------------------------------------------------------------
# Stock Snapshot responses (v2 & v3)
# ------------------------------------------------------------------


class SnapshotBar(BaseModel):
    """OHLCV bar from a snapshot (day or prev_day)"""

    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    vwap: float | None = None


class MinuteBar(SnapshotBar):
    """Most recent minute bar with accumulated volume and timestamp"""

    accumulated_volume: float | None = None
    timestamp: int | None = None


class StockTickerSnapshot(BaseModel):
    """Snapshot data for a single stock ticker (v2 API)"""

    ticker: str | None = None
    day: SnapshotBar | None = None
    prev_day: SnapshotBar | None = None
    min: MinuteBar | None = None
    todays_change: float | None = None
    todays_change_percent: float | None = None
    updated: int | None = None


class StockSnapshotResponse(BaseModel):
    """Response for a single stock ticker snapshot"""

    success: bool
    snapshot: StockTickerSnapshot | None = None
    error: str | None = None


class StockSnapshotsResponse(BaseModel):
    """Response for multiple stock ticker snapshots"""

    success: bool
    snapshots: list[StockTickerSnapshot] = []
    count: int = 0
    error: str | None = None


class MarketMoversResponse(BaseModel):
    """Response for top market movers (gainers/losers)"""

    success: bool
    tickers: list[StockTickerSnapshot] = []
    count: int = 0
    error: str | None = None


class UnifiedSnapshotSession(BaseModel):
    """Session data from unified v3 snapshot"""

    price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    open: float | None = None
    close: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    volume: float | None = None


class UnifiedSnapshotItem(BaseModel):
    """A single item from the unified v3 snapshot"""

    ticker: str | None = None
    type: str | None = None
    market_status: str | None = None
    name: str | None = None
    session: UnifiedSnapshotSession | None = None


class UnifiedSnapshotResponse(BaseModel):
    """Response for unified v3 snapshots"""

    success: bool
    results: list[UnifiedSnapshotItem] = []
    count: int = 0
    error: str | None = None


# ------------------------------------------------------------------
# Market Monitor responses
# ------------------------------------------------------------------


class ExchangeStatus(BaseModel):
    """Status of individual exchanges"""

    nyse: str | None = None
    nasdaq: str | None = None
    otc: str | None = None


class MarketStatusResponse(BaseModel):
    """Current market status response"""

    success: bool
    market: str = "unknown"
    exchanges: ExchangeStatus = ExchangeStatus()
    early_hours: bool = False
    after_hours: bool = False
    server_time: str = ""
    server_time_readable: str = "N/A"
    error: str | None = None


class MarketHolidayEvent(BaseModel):
    """A single upcoming market holiday event"""

    date: str | None = None
    name: str | None = None
    status: str | None = None
    open: str | None = None
    close: str | None = None
    exchanges: list[str] = []


class MarketHolidaysResponse(BaseModel):
    """Upcoming market holidays response"""

    success: bool
    events: list[MarketHolidayEvent] = []
    count: int = 0
    error: str | None = None


class MarketDashboardResponse(BaseModel):
    """Combined market status + holidays for the dashboard"""

    success: bool
    status: MarketStatusResponse | None = None
    holidays: MarketHolidaysResponse | None = None
    error: str | None = None


# ------------------------------------------------------------------
# Ticker Reference responses
# ------------------------------------------------------------------


class TickerInfo(BaseModel):
    """Basic ticker info from the reference API"""

    ticker: str
    name: str = ""
    market: str = ""
    type: str = ""
    active: bool = True
    primary_exchange: str | None = None
    currency_name: str | None = None


class TickerListResponse(BaseModel):
    """Response for batch ticker info lookup"""

    success: bool
    tickers: list[TickerInfo] = []
    count: int = 0
    error: str | None = None


class TickerAddress(BaseModel):
    """Company address from ticker details"""

    address1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None


class TickerDetailResponse(BaseModel):
    """Response for detailed ticker overview"""

    success: bool
    ticker: str = ""
    name: str = ""
    description: str | None = None
    market_cap: float | None = None
    homepage_url: str | None = None
    total_employees: int | None = None
    list_date: str | None = None
    sic_description: str | None = None
    primary_exchange: str | None = None
    type: str | None = None
    weighted_shares_outstanding: float | None = None
    address: TickerAddress | None = None
    error: str | None = None


class RelatedTickersResponse(BaseModel):
    """Response for related companies lookup"""

    success: bool
    ticker: str = ""
    related: list[str] = []
    error: str | None = None


# ------------------------------------------------------------------
# Indicator Table responses (TradingView-style)
# ------------------------------------------------------------------


class IndicatorTableRow(BaseModel):
    """A single row from the full indicator table"""

    time: int
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    bb_basis: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None
    supertrend_up: float | None = None
    supertrend_down: float | None = None
    rsi: float | None = None
    rsi_ma: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    adx: float | None = None


class IndicatorTableResponse(BaseModel):
    """Response containing the full indicator table"""

    success: bool
    ticker: str
    row_count: int = 0
    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    error: str | None = None


# ------------------------------------------------------------------
# Available Indicators & Dataset Generation
# ------------------------------------------------------------------


class IndicatorInfo(BaseModel):
    """Metadata for a single pandas-ta indicator"""

    name: str
    category: str
    description: str


class AvailableIndicatorsResponse(BaseModel):
    """All available pandas-ta indicators grouped by category"""

    success: bool
    categories: dict[str, list[IndicatorInfo]] = {}
    total: int = 0
    error: str | None = None


class DatasetGenerationResponse(BaseModel):
    """Response for dataset generation (non-CSV JSON mode)"""

    success: bool
    ticker: str
    row_count: int = 0
    bar_count: int = 0
    columns: list[str] = []
    indicators_calculated: list[str] = []
    error: str | None = None


# ---------------------------------------------------------------------------
# LEAN-parity statistics response models
#
# Moved from app/routers/backtest.py (deleted as dark code — PR 4) because
# app/routers/engine.py (the live backtest path) depends on these shapes.
# ---------------------------------------------------------------------------
class LeanPortfolioStatsResponse(BaseModel):
    """LEAN PortfolioStatistics — 25 fields matching PS.cs exactly."""

    average_win_rate: float = 0.0
    average_loss_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    win_rate: float = 0.0
    loss_rate: float = 0.0
    expectancy: float = 0.0
    start_equity: float = 0.0
    end_equity: float = 0.0
    total_net_profit: float = 0.0
    compounding_annual_return: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    probabilistic_sharpe_ratio: float = 0.0
    annual_standard_deviation: float = 0.0
    annual_variance: float = 0.0
    alpha: float = 0.0
    beta: float = 0.0
    information_ratio: float = 0.0
    tracking_error: float = 0.0
    treynor_ratio: float = 0.0
    drawdown: float = 0.0
    drawdown_recovery: int = 0
    value_at_risk_99: float = 0.0
    value_at_risk_95: float = 0.0
    portfolio_turnover: float = 0.0


class LeanTradeStatsResponse(BaseModel):
    """LEAN TradeStatistics — key fields matching TS.cs."""

    start_date_time: int | None = None
    end_date_time: int | None = None
    total_number_of_trades: int = 0
    number_of_winning_trades: int = 0
    number_of_losing_trades: int = 0
    total_profit_loss: float = 0.0
    total_profit: float = 0.0
    total_loss: float = 0.0
    largest_profit: float = 0.0
    largest_loss: float = 0.0
    average_profit_loss: float = 0.0
    average_profit: float = 0.0
    average_loss: float = 0.0
    average_trade_duration: str = ""
    average_winning_trade_duration: str = ""
    average_losing_trade_duration: str = ""
    max_consecutive_winning_trades: int = 0
    max_consecutive_losing_trades: int = 0
    profit_factor: float = 0.0
    profit_to_max_drawdown_ratio: float = 0.0
    profit_loss_standard_deviation: float = 0.0
    profit_loss_downside_deviation: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    total_fees: float = 0.0


class LeanRuntimeStatsResponse(BaseModel):
    """LEAN runtimeStatistics — 5 key fields."""

    equity: float = 0.0
    fees: float = 0.0
    net_profit: float = 0.0
    total_return: float = 0.0
    total_orders: int = 0


class LeanStatisticsResponse(BaseModel):
    """Full LEAN statistics suite."""

    portfolio: LeanPortfolioStatsResponse = Field(default_factory=LeanPortfolioStatsResponse)
    trade: LeanTradeStatsResponse = Field(default_factory=LeanTradeStatsResponse)
    runtime: LeanRuntimeStatsResponse = Field(default_factory=LeanRuntimeStatsResponse)
