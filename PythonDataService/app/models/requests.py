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

    @field_validator("timespan")
    @classmethod
    def validate_dataset_timespan(cls, v: str) -> str:
        valid = ["minute", "hour", "day"]
        if v not in valid:
            raise ValueError(f"timespan must be one of {valid}")
        return v
