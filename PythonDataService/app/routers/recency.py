"""Python-owned numerical endpoints for the Recency Chart."""

from __future__ import annotations

from fastapi import APIRouter

from app.research.recency.stats import HeroCandidate, HeroTrade, select_window_heroes
from app.schemas.recency import RecencyHeroRequest, RecencyHeroResponse, RecencyHeroResponseItem

router = APIRouter()


@router.post("/hero", response_model=RecencyHeroResponse)
async def compute_recency_hero(request: RecencyHeroRequest) -> RecencyHeroResponse:
    """Return Python-authored visible-window winners for each symbol/strategy."""
    candidates = [
        HeroCandidate(
            recency_run_id=candidate.recency_run_id,
            symbol=candidate.symbol,
            strategy_key=candidate.strategy_key,
            params_hash=candidate.params_hash,
            trades=tuple(HeroTrade(entry_ms=trade.entry_ms, pnl=trade.pnl) for trade in candidate.trades),
        )
        for candidate in request.candidates
    ]
    selections = select_window_heroes(candidates, request.from_ms, request.to_ms)
    return RecencyHeroResponse(
        heroes=[RecencyHeroResponseItem.from_engine_result(selection) for selection in selections]
    )
