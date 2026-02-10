"""Pydantic request schemas with validation"""
from pydantic import BaseModel, Field, field_validator
from typing import Optional


class AggregateRequest(BaseModel):
    """Request schema for fetching aggregate bars (OHLCV)"""
    ticker: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    multiplier: int = Field(1, ge=1, description="Timespan multiplier")
    timespan: str = Field("day", description="Timespan: minute, hour, day, week, month")
    from_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    to_date: str = Field(..., description="End date (YYYY-MM-DD)")
    limit: int = Field(50000, ge=1, le=50000, description="Max results")

    @field_validator('timespan')
    @classmethod
    def validate_timespan(cls, v: str) -> str:
        valid = ['minute', 'hour', 'day', 'week', 'month', 'quarter', 'year']
        if v not in valid:
            raise ValueError(f'timespan must be one of {valid}')
        return v


class TradeRequest(BaseModel):
    """Request schema for fetching trade data"""
    ticker: str = Field(..., min_length=1, max_length=20)
    timestamp: Optional[str] = Field(None, description="Timestamp filter (YYYY-MM-DD)")
    limit: int = Field(50000, ge=1, le=50000)


class IndicatorRequest(BaseModel):
    """Request schema for fetching technical indicators"""
    ticker: str = Field(..., min_length=1, max_length=20)
    indicator_type: str = Field(..., description="Indicator: sma, ema, rsi, macd")
    timespan: str = Field("day", description="Timespan")
    window: int = Field(50, ge=1, description="Window period")
    timestamp: Optional[str] = None

    @field_validator('indicator_type')
    @classmethod
    def validate_indicator(cls, v: str) -> str:
        valid = ['sma', 'ema', 'rsi', 'macd']
        if v.lower() not in valid:
            raise ValueError(f'indicator_type must be one of {valid}')
        return v.lower()
