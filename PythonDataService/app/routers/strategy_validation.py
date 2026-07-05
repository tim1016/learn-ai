from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.routers.engine import _STRATEGY_REGISTRY
from app.schemas.strategy_validation import StrategyValidationCatalog, StrategyValidationDetail
from app.services.strategy_validation_manifest import (
    StrategyRegistrySeed,
    StrategyValidationManifestError,
    load_strategy_validation_entries,
    reference_code_for_entry,
)

router = APIRouter()


def _registry_seeds() -> list[StrategyRegistrySeed]:
    return [
        StrategyRegistrySeed(
            strategy_key=key,
            display_name=registration.display_name,
            description=registration.description,
        )
        for key, registration in sorted(_STRATEGY_REGISTRY.items())
    ]


@router.get("/strategies", response_model=StrategyValidationCatalog)
async def list_strategy_validations() -> StrategyValidationCatalog:
    try:
        entries = load_strategy_validation_entries(_registry_seeds())
    except StrategyValidationManifestError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return StrategyValidationCatalog(strategies=entries)


@router.get("/strategies/{strategy_key}", response_model=StrategyValidationDetail)
async def get_strategy_validation(strategy_key: str) -> StrategyValidationDetail:
    try:
        entries = load_strategy_validation_entries(_registry_seeds())
        entry = next((item for item in entries if item.strategy_key == strategy_key), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="Strategy not found")
        return StrategyValidationDetail(
            **entry.model_dump(),
            reference_code=reference_code_for_entry(entry),
        )
    except StrategyValidationManifestError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
