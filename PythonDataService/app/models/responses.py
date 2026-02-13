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
