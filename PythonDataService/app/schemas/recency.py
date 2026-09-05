"""HTTP contracts for the Python-owned Recency Chart reads and mutations (PRD #1927).

The shapes are the ones the retired GraphQL ``recencyTrades`` / ``recencyHero``
queries served, in snake_case; every temporal value is ``int64 ms UTC`` and
every monetary value is a JSON number (storage precision is ``numeric(18,8)``,
the browser DTO is a float — review F14).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.research.recency.stats import HeroSelection


class RecencyTradeMembershipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    recency_run_id: int
    study_id: int | None
    created_at_ms: int


class RecencyTradeResponse(BaseModel):
    """Validated straight from the repository's ``TradeView`` (``from_attributes``)."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    strategy_key: str
    params_hash: str
    params_json: str
    fingerprint: str
    entry_ms: int
    exit_ms: int
    pnl_pts: float
    pnl_pct: float
    quantity: float
    pnl: float
    holding_sessions: int
    sharpe: float | None
    study_id: int | None
    recency_run_id: int
    is_synthetic_exit: bool
    signal_reason: str
    memberships: list[RecencyTradeMembershipResponse]


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


class RecencyRunMutationResponse(BaseModel):
    recency_run_id: int


class RecencyLaunchMutationResponse(BaseModel):
    launch_id: str
