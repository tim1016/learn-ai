"""Pydantic response schemas"""
from pydantic import BaseModel
from typing import List, Dict, Any, Optional


class SanitizedDataResponse(BaseModel):
    """Standard response schema for sanitized data"""
    success: bool
    data: List[Dict[str, Any]]
    summary: Dict[str, Any]
    ticker: str
    data_type: str
    error: Optional[str] = None


class SanitizeResponse(BaseModel):
    """Response schema for the standalone /api/sanitize endpoint"""
    success: bool
    data: List[Dict[str, Any]]
    summary: Dict[str, Any]
    error: Optional[str] = None


class IndicatorDataPoint(BaseModel):
    """A single indicator value at a timestamp"""
    timestamp: int
    value: Optional[float] = None
    signal: Optional[float] = None
    histogram: Optional[float] = None
    upper: Optional[float] = None
    lower: Optional[float] = None


class IndicatorResult(BaseModel):
    """Result for a single indicator calculation"""
    name: str
    window: int
    data: List[IndicatorDataPoint]


class OptionsContractItem(BaseModel):
    """A single options contract"""
    ticker: str
    underlying_ticker: Optional[str] = None
    contract_type: Optional[str] = None
    strike_price: Optional[float] = None
    expiration_date: Optional[str] = None
    exercise_style: Optional[str] = None
    shares_per_contract: Optional[float] = None
    primary_exchange: Optional[str] = None


class OptionsContractsResponse(BaseModel):
    """Response schema for options contracts listing"""
    success: bool
    contracts: List[OptionsContractItem] = []
    count: int = 0
    error: Optional[str] = None


class GreeksSnapshot(BaseModel):
    """Greeks for an options contract snapshot"""
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None


class DaySnapshot(BaseModel):
    """Day OHLCV for an options contract snapshot"""
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    vwap: Optional[float] = None


class OptionsContractSnapshotItem(BaseModel):
    """A single options contract snapshot with greeks and day data"""
    ticker: Optional[str] = None
    contract_type: Optional[str] = None
    strike_price: Optional[float] = None
    expiration_date: Optional[str] = None
    break_even_price: Optional[float] = None
    implied_volatility: Optional[float] = None
    open_interest: Optional[float] = None
    greeks: Optional[GreeksSnapshot] = None
    day: Optional[DaySnapshot] = None


class UnderlyingSnapshot(BaseModel):
    """Underlying asset info from snapshot"""
    ticker: str
    price: Optional[float] = 0
    change: Optional[float] = 0
    change_percent: Optional[float] = 0


class OptionsChainSnapshotResponse(BaseModel):
    """Response schema for options chain snapshot"""
    success: bool
    underlying: Optional[UnderlyingSnapshot] = None
    contracts: List[OptionsContractSnapshotItem] = []
    count: int = 0
    error: Optional[str] = None


class CalculateIndicatorsResponse(BaseModel):
    """Response from indicator calculation"""
    success: bool
    ticker: str
    indicators: List[IndicatorResult] = []
    error: Optional[str] = None


# ------------------------------------------------------------------
# Stock Snapshot responses (v2 & v3)
# ------------------------------------------------------------------

class SnapshotBar(BaseModel):
    """OHLCV bar from a snapshot (day or prev_day)"""
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    vwap: Optional[float] = None


class MinuteBar(SnapshotBar):
    """Most recent minute bar with accumulated volume and timestamp"""
    accumulated_volume: Optional[float] = None
    timestamp: Optional[int] = None


class StockTickerSnapshot(BaseModel):
    """Snapshot data for a single stock ticker (v2 API)"""
    ticker: Optional[str] = None
    day: Optional[SnapshotBar] = None
    prev_day: Optional[SnapshotBar] = None
    min: Optional[MinuteBar] = None
    todays_change: Optional[float] = None
    todays_change_percent: Optional[float] = None
    updated: Optional[int] = None


class StockSnapshotResponse(BaseModel):
    """Response for a single stock ticker snapshot"""
    success: bool
    snapshot: Optional[StockTickerSnapshot] = None
    error: Optional[str] = None


class StockSnapshotsResponse(BaseModel):
    """Response for multiple stock ticker snapshots"""
    success: bool
    snapshots: List[StockTickerSnapshot] = []
    count: int = 0
    error: Optional[str] = None


class MarketMoversResponse(BaseModel):
    """Response for top market movers (gainers/losers)"""
    success: bool
    tickers: List[StockTickerSnapshot] = []
    count: int = 0
    error: Optional[str] = None


class UnifiedSnapshotSession(BaseModel):
    """Session data from unified v3 snapshot"""
    price: Optional[float] = None
    change: Optional[float] = None
    change_percent: Optional[float] = None
    open: Optional[float] = None
    close: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    previous_close: Optional[float] = None
    volume: Optional[float] = None


class UnifiedSnapshotItem(BaseModel):
    """A single item from the unified v3 snapshot"""
    ticker: Optional[str] = None
    type: Optional[str] = None
    market_status: Optional[str] = None
    name: Optional[str] = None
    session: Optional[UnifiedSnapshotSession] = None


class UnifiedSnapshotResponse(BaseModel):
    """Response for unified v3 snapshots"""
    success: bool
    results: List[UnifiedSnapshotItem] = []
    count: int = 0
    error: Optional[str] = None


# ------------------------------------------------------------------
# Market Monitor responses
# ------------------------------------------------------------------

class ExchangeStatus(BaseModel):
    """Status of individual exchanges"""
    nyse: Optional[str] = None
    nasdaq: Optional[str] = None
    otc: Optional[str] = None


class MarketStatusResponse(BaseModel):
    """Current market status response"""
    success: bool
    market: str = "unknown"
    exchanges: ExchangeStatus = ExchangeStatus()
    early_hours: bool = False
    after_hours: bool = False
    server_time: str = ""
    server_time_readable: str = "N/A"
    error: Optional[str] = None


class MarketHolidayEvent(BaseModel):
    """A single upcoming market holiday event"""
    date: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    open: Optional[str] = None
    close: Optional[str] = None
    exchanges: List[str] = []


class MarketHolidaysResponse(BaseModel):
    """Upcoming market holidays response"""
    success: bool
    events: List[MarketHolidayEvent] = []
    count: int = 0
    error: Optional[str] = None


class MarketDashboardResponse(BaseModel):
    """Combined market status + holidays for the dashboard"""
    success: bool
    status: Optional[MarketStatusResponse] = None
    holidays: Optional[MarketHolidaysResponse] = None
    error: Optional[str] = None
