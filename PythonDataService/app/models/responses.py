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


class CalculateIndicatorsResponse(BaseModel):
    """Response from indicator calculation"""
    success: bool
    ticker: str
    indicators: List[IndicatorResult] = []
    error: Optional[str] = None
