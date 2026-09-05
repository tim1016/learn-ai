"""The body FastAPI's ``HTTPException`` sends, so a route can declare its error responses in the contract."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorDetailResponse(BaseModel):
    detail: str = Field(description="Why the request was refused, in the words the route chose.")
