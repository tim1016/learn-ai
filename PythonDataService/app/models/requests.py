"""Pydantic request schemas with validation"""

from typing import Any

from pydantic import BaseModel, Field, field_validator


class AggregateRequest(BaseModel):
    """Request schema for fetching aggregate bars (OHLCV)"""

    ticker: str = Field(..., min_length=1, max_length=50, description="Stock or options ticker symbol")
    multiplier: int = Field(1, ge=1, description="Timespan multiplier")
    timespan: str = Field("day", description="Timespan: minute, hour, day, week, month")
    from_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    to_date: str = Field(..., description="End date (YYYY-MM-DD)")
    limit: int = Field(50000, ge=1, le=50000, description="Max results")
    adjusted: bool = Field(True, description="Adjust for splits/dividends (Polygon default: true)")

    @field_validator("timespan")
    @classmethod
    def validate_timespan(cls, v: str) -> str:
        valid = ["minute", "hour", "day", "week", "month", "quarter", "year"]
        if v not in valid:
            raise ValueError(f"timespan must be one of {valid}")
        return v


class TradeRequest(BaseModel):
    """Request schema for fetching trade data"""

    ticker: str = Field(..., min_length=1, max_length=50)
    timestamp: str | None = Field(None, description="Timestamp filter (YYYY-MM-DD)")
    limit: int = Field(50000, ge=1, le=50000)


class IndicatorRequest(BaseModel):
    """Request schema for fetching technical indicators"""

    ticker: str = Field(..., min_length=1, max_length=20)
    indicator_type: str = Field(..., description="Indicator: sma, ema, rsi, macd")
    timespan: str = Field("day", description="Timespan")
    window: int = Field(50, ge=1, description="Window period")
    timestamp: str | None = None

    @field_validator("indicator_type")
    @classmethod
    def validate_indicator(cls, v: str) -> str:
        valid = ["sma", "ema", "rsi", "macd"]
        if v.lower() not in valid:
            raise ValueError(f"indicator_type must be one of {valid}")
        return v.lower()


class SanitizeRequest(BaseModel):
    """Request schema for the standalone /api/sanitize endpoint"""

    data: list[dict[str, Any]] = Field(..., description="List of market data records to sanitize")
    quantile: float = Field(0.99, ge=0.0, le=1.0, description="Quantile threshold for outlier removal")


class OhlcvBar(BaseModel):
    """Single OHLCV bar for indicator calculation"""

    timestamp: int = Field(..., description="Unix milliseconds")
    open: float
    high: float
    low: float
    close: float
    volume: float


class IndicatorConfig(BaseModel):
    """Configuration for a single indicator"""

    name: str = Field(..., description="Indicator name: sma, ema, rsi, macd, bbands, stoch")
    window: int = Field(14, ge=1, description="Lookback period")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        valid = ["sma", "ema", "rsi", "macd", "bbands", "stoch"]
        if v.lower() not in valid:
            raise ValueError(f"indicator name must be one of {valid}")
        return v.lower()


class OptionsContractsRequest(BaseModel):
    """Request schema for listing options contracts"""

    underlying_ticker: str = Field(..., min_length=1, max_length=20, description="Underlying stock ticker")
    as_of_date: str | None = Field(None, description="As-of date (YYYY-MM-DD)")
    contract_type: str | None = Field(None, description="Filter: call or put")
    strike_price_gte: float | None = Field(None, description="Min strike price")
    strike_price_lte: float | None = Field(None, description="Max strike price")
    expiration_date: str | None = Field(None, description="Exact expiration date (YYYY-MM-DD)")
    expiration_date_gte: str | None = Field(None, description="Min expiration date")
    expiration_date_lte: str | None = Field(None, description="Max expiration date")
    expired: bool | None = Field(None, description="Include expired contracts")
    limit: int = Field(100, ge=1, le=1000, description="Max results")

    @field_validator("contract_type")
    @classmethod
    def validate_contract_type(cls, v: str | None) -> str | None:
        if v is not None and v not in ["call", "put"]:
            raise ValueError('contract_type must be "call" or "put"')
        return v


class OptionsExpirationsRequest(BaseModel):
    """Request schema for listing unique options expiration dates"""

    underlying_ticker: str = Field(..., min_length=1, max_length=20, description="Underlying stock ticker")
    contract_type: str | None = Field(None, description="Filter: call or put")
    expiration_date_gte: str | None = Field(None, description="Min expiration date")
    expiration_date_lte: str | None = Field(None, description="Max expiration date")

    @field_validator("contract_type")
    @classmethod
    def validate_contract_type(cls, v: str | None) -> str | None:
        if v is not None and v not in ["call", "put"]:
            raise ValueError('contract_type must be "call" or "put"')
        return v


class OptionsChainSnapshotRequest(BaseModel):
    """Request schema for fetching options chain snapshot"""

    underlying_ticker: str = Field(..., min_length=1, max_length=20, description="Underlying stock ticker")
    expiration_date: str | None = Field(
        None, description="Filter to this expiration date (YYYY-MM-DD). Defaults to today."
    )


class StockSnapshotRequest(BaseModel):
    """Request schema for single stock ticker snapshot"""

    ticker: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")


class StockSnapshotsRequest(BaseModel):
    """Request schema for multiple stock ticker snapshots"""

    tickers: list[str] | None = Field(None, description="List of tickers. If omitted, returns all.")


class MarketMoversRequest(BaseModel):
    """Request schema for top market movers"""

    direction: str = Field(..., description="'gainers' or 'losers'")

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v: str) -> str:
        if v not in ("gainers", "losers"):
            raise ValueError("direction must be 'gainers' or 'losers'")
        return v


class UnifiedSnapshotRequest(BaseModel):
    """Request schema for unified v3 snapshots"""

    tickers: list[str] | None = Field(None, description="Optional list of tickers to filter")
    limit: int = Field(10, ge=1, le=250, description="Max results (default 10, max 250)")


class CalculateIndicatorsRequest(BaseModel):
    """Request to calculate technical indicators from OHLCV data"""

    ticker: str = Field(..., min_length=1, max_length=20)
    bars: list[OhlcvBar] = Field(..., min_length=1)
    indicators: list[IndicatorConfig] = Field(..., min_length=1)


class TickerListRequest(BaseModel):
    """Request schema for fetching basic info for a list of tickers"""

    tickers: list[str] = Field(..., min_length=1, description="List of ticker symbols")


class TickerDetailRequest(BaseModel):
    """Request schema for fetching detailed overview of a single ticker"""

    ticker: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")


class RelatedTickersRequest(BaseModel):
    """Request schema for fetching related companies for a ticker"""

    ticker: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")


class IndicatorTableRequest(BaseModel):
    """Request to generate a full TradingView-style indicator table from Polygon minute data"""

    ticker: str = Field(..., min_length=1, max_length=20, description="Ticker symbol")
    from_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    to_date: str = Field(..., description="End date (YYYY-MM-DD)")
    multiplier: int = Field(1, ge=1, description="Timespan multiplier for aggregates")
    timespan: str = Field("minute", description="Timespan: minute, hour, day")
    ema_periods: list[int] = Field(
        default=[5, 10, 20, 30, 40, 50, 100, 200],
        description="EMA periods to calculate",
    )
    bb_length: int = Field(20, ge=1, description="Bollinger Bands length")
    bb_std: float = Field(2.0, gt=0, description="Bollinger Bands standard deviation")
    supertrend_length: int = Field(10, ge=1, description="Supertrend ATR length")
    supertrend_multiplier: float = Field(3.0, gt=0, description="Supertrend multiplier")
    rsi_length: int = Field(14, ge=1, description="RSI period")
    rsi_ma_length: int = Field(14, ge=1, description="RSI moving average period")
    macd_fast: int = Field(12, ge=1, description="MACD fast period")
    macd_slow: int = Field(26, ge=1, description="MACD slow period")
    macd_signal: int = Field(9, ge=1, description="MACD signal period")
    adx_length: int = Field(14, ge=1, description="ADX period")
    session: str = Field(
        "extended",
        description="'rth' for regular trading hours (09:30-16:00 ET), 'extended' for all hours",
    )
    forward_fill: bool = Field(
        False,
        description="Fill missing minute bars with previous close (volume=0)",
    )
    adjusted: bool = Field(True, description="Adjust for splits/dividends (Polygon default: true)")

    @field_validator("timespan")
    @classmethod
    def validate_timespan(cls, v: str) -> str:
        valid = ["minute", "hour", "day"]
        if v not in valid:
            raise ValueError(f"timespan must be one of {valid}")
        return v


class OptionsCompanionConfig(BaseModel):
    """Optional companion-file config emitted alongside the underlying dataset CSV.

    When enabled, the ZIP response includes separate ``options_calls.csv`` and
    ``options_puts.csv`` files (subject to ``include_calls`` / ``include_puts``)
    containing 1-minute option aggregates for contracts picked around the
    underlying's prior-day close on each expiry date. Greeks and IV are
    computed locally via QuantLib (see ``options_companion_service``).
    """

    enabled: bool = Field(False, description="Emit options companion CSV files in the ZIP")
    strikes_each_side: int = Field(
        5, ge=1, le=25, description="Strikes above AND below ATM per type (default 5 + 1 ATM = 11 per type)"
    )
    include_calls: bool = Field(True, description="Emit options_calls.csv")
    include_puts: bool = Field(True, description="Emit options_puts.csv")
    expiry_mode: str = Field(
        "same_day",
        description="'same_day' = 0DTE only; 'nearest_within_days' = nearest expiry within max_dte days",
    )
    max_dte: int = Field(
        7,
        ge=0,
        le=60,
        description="When expiry_mode='nearest_within_days', the max days-to-expiry to look ahead",
    )
    # Per-field toggles (each omits its column(s) when false)
    include_ohlcv: bool = Field(True, description="Include option OHLCV columns")
    include_vwap: bool = Field(True, description="Include option VWAP column")
    include_transactions: bool = Field(True, description="Include option transactions count column")
    include_open_interest: bool = Field(False, description="Include OI column (live contracts only)")
    include_iv: bool = Field(True, description="Include implied volatility column (solved per bar)")
    include_delta: bool = Field(True, description="Include delta Greek")
    include_gamma: bool = Field(True, description="Include gamma Greek")
    include_theta: bool = Field(True, description="Include theta Greek")
    include_vega: bool = Field(True, description="Include vega Greek")
    include_rho: bool = Field(False, description="Include rho Greek")
    risk_free_rate: float = Field(
        0.05, ge=0, le=0.25, description="Flat annualized risk-free rate used in IV/Greeks solves"
    )
    dividend_yield: float = Field(0.0, ge=0, le=0.25, description="Flat continuous dividend yield for Greeks")

    @field_validator("expiry_mode")
    @classmethod
    def validate_expiry_mode(cls, v: str) -> str:
        valid = ["same_day", "nearest_within_days"]
        if v not in valid:
            raise ValueError(f"expiry_mode must be one of {valid}")
        return v


class DatasetGenerationRequest(BaseModel):
    """Request to generate a full indicator dataset with chunked OHLCV fetching"""

    ticker: str = Field(..., min_length=1, max_length=20, description="Ticker symbol")
    from_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    to_date: str = Field(..., description="End date (YYYY-MM-DD)")
    indicator_entries: list[dict[str, Any]] = Field(
        default=[],
        description="List of indicator entries, each with 'name' and optional 'params' dict. "
        "e.g. [{'name': 'ema', 'params': {'length': 20}}, {'name': 'rsi', 'params': {'length': 14}}]",
    )
    session: str = Field(
        "extended",
        description="'rth' for regular trading hours only (09:30-16:00 ET), 'extended' for all hours",
    )
    forward_fill: bool = Field(
        True,
        description="Fill missing minute bars with previous close (volume=0) for continuous indicator calculation",
    )
    warmup: bool = Field(
        True,
        description="Fetch extra bars before from_date to warm up indicator calculations",
    )
    timespan: str = Field(
        "minute",
        description="Bar timespan: 'minute', 'hour', or 'day'",
    )
    multiplier: int = Field(
        1,
        ge=1,
        le=60,
        description="Bar multiplier (e.g., 5 with timespan='minute' gives 5-min bars)",
    )
    adjusted: bool = Field(True, description="Adjust for splits/dividends (Polygon default: true)")
    sort: str = Field(
        "asc",
        description="Polygon aggregate sort order: 'asc' (oldest first) or 'desc' (newest first). "
        "Applies to the upstream Polygon request; the downstream merged result stays ascending.",
    )
    limit: int = Field(
        50000,
        ge=1,
        le=50000,
        description="Polygon aggregate per-request limit (1–50000). Higher values mean fewer chunks.",
    )
    options_companion: OptionsCompanionConfig | None = Field(
        None,
        description="Optional options companion file config. When set with enabled=True, the ZIP gains "
        "options_calls.csv and/or options_puts.csv with aggregates + Greeks per contract.",
    )
    include_quality_report: bool = Field(
        False,
        description="When true, run the data-quality pipeline on the fetched bars and bundle "
        "quality_report.md into the ZIP alongside the dataset.",
    )

    # ── Polygon reference-endpoint companion toggles ──────────
    # Each toggle adds one file to the ZIP sourced from the named Polygon
    # endpoint. Default off so the base ZIP stays minimal; the UI surfaces
    # explicit warnings for the tick-level options (trades/quotes).
    include_splits: bool = Field(False, description="Bundle splits.csv (Polygon /stocks/v1/splits)")
    include_dividends: bool = Field(False, description="Bundle dividends.csv (Polygon /stocks/v1/dividends)")
    include_ticker_overview: bool = Field(
        False, description="Bundle ticker_overview.json (Polygon /v3/reference/tickers/{ticker})"
    )
    include_news: bool = Field(False, description="Bundle news.csv (Polygon /v2/reference/news)")
    include_financials: bool = Field(
        False,
        description="Bundle financials.csv (Polygon /vX/reference/financials, quarterly filings)",
    )
    include_trades: bool = Field(
        False,
        description="Bundle trades.csv (Polygon /v3/trades). TICK-LEVEL — millions of rows; "
        "use a short date window. Capped server-side at 500k rows.",
    )
    include_quotes: bool = Field(
        False,
        description="Bundle quotes.csv (Polygon /v3/quotes, NBBO). TICK-LEVEL — millions of rows; "
        "use a short date window. Capped server-side at 500k rows.",
    )

    @field_validator("timespan")
    @classmethod
    def validate_dataset_timespan(cls, v: str) -> str:
        valid = ["second", "minute", "hour", "day", "week", "month", "quarter", "year"]
        if v not in valid:
            raise ValueError(f"timespan must be one of {valid}")
        return v

    @field_validator("sort")
    @classmethod
    def validate_dataset_sort(cls, v: str) -> str:
        if v not in ("asc", "desc"):
            raise ValueError("sort must be 'asc' or 'desc'")
        return v
