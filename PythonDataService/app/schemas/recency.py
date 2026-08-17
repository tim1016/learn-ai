"""HTTP contracts for Python-owned Recency Chart computations."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.recency.stats import HeroSelection


class RecencyHeroTradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_ms: int = Field(ge=0)
    pnl: float


class RecencyHeroCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recency_run_id: int = Field(gt=0)
    symbol: str = Field(min_length=1, max_length=20)
    strategy_key: str = Field(min_length=1)
    params_hash: str = Field(min_length=1)
    trades: list[RecencyHeroTradeRequest] = Field(min_length=1)


class RecencyHeroRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_ms: int = Field(ge=0)
    to_ms: int = Field(ge=0)
    candidates: list[RecencyHeroCandidateRequest]

    @model_validator(mode="after")
    def validate_window(self) -> RecencyHeroRequest:
        if self.from_ms > self.to_ms:
            raise ValueError("from_ms must be less than or equal to to_ms")
        return self


class RecencyHeroResponseItem(BaseModel):
    recency_run_id: int
    symbol: str
    strategy_key: str
    params_hash: str
    total_pnl: float

    @classmethod
    def from_engine_result(cls, result: HeroSelection) -> RecencyHeroResponseItem:
        return cls(
            recency_run_id=result.recency_run_id,
            symbol=result.symbol,
            strategy_key=result.strategy_key,
            params_hash=result.params_hash,
            total_pnl=result.total_pnl,
        )


class RecencyHeroResponse(BaseModel):
    heroes: list[RecencyHeroResponseItem]
