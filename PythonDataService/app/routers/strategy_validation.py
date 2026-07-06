from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.routers.engine import _STRATEGY_REGISTRY
from app.schemas.strategy_validation import (
    StrategyValidationCatalog,
    StrategyValidationDetail,
    StrategyValidationFlagRequest,
    StrategyValidationRefreshResult,
)
from app.services.strategy_validation_manifest import (
    DEFAULT_FLAG_EVENTS_PATH,
    DEFAULT_MANIFEST_PATH,
    StrategyRegistrySeed,
    StrategyValidationManifestError,
    StrategyValidationNotFoundError,
    append_strategy_validation_flag_event,
    load_strategy_validation_entries,
    local_strategy_validation_actor,
    reference_code_for_entry,
    refresh_strategy_validation_manifest_evidence,
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


def get_strategy_validation_manifest_path() -> Path:
    return DEFAULT_MANIFEST_PATH


def get_strategy_validation_flag_events_path() -> Path:
    return DEFAULT_FLAG_EVENTS_PATH


def get_strategy_validation_actor() -> str:
    return local_strategy_validation_actor()


@router.get("/strategies", response_model=StrategyValidationCatalog)
async def list_strategy_validations(
    manifest_path: Path = Depends(get_strategy_validation_manifest_path),
    flag_events_path: Path = Depends(get_strategy_validation_flag_events_path),
) -> StrategyValidationCatalog:
    try:
        entries = load_strategy_validation_entries(
            _registry_seeds(),
            manifest_path=manifest_path,
            flag_events_path=flag_events_path,
        )
    except StrategyValidationManifestError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return StrategyValidationCatalog(strategies=entries)


@router.get("/strategies/{strategy_key}", response_model=StrategyValidationDetail)
async def get_strategy_validation(
    strategy_key: str,
    manifest_path: Path = Depends(get_strategy_validation_manifest_path),
    flag_events_path: Path = Depends(get_strategy_validation_flag_events_path),
) -> StrategyValidationDetail:
    try:
        entries = load_strategy_validation_entries(
            _registry_seeds(),
            manifest_path=manifest_path,
            flag_events_path=flag_events_path,
        )
        entry = next((item for item in entries if item.strategy_key == strategy_key), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="Strategy not found")
        return StrategyValidationDetail(
            **entry.model_dump(),
            reference_code=reference_code_for_entry(entry),
        )
    except StrategyValidationManifestError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/strategies/{strategy_key}/refresh", response_model=StrategyValidationRefreshResult)
async def refresh_strategy_validation(
    strategy_key: str,
    manifest_path: Path = Depends(get_strategy_validation_manifest_path),
    flag_events_path: Path = Depends(get_strategy_validation_flag_events_path),
) -> StrategyValidationRefreshResult:
    try:
        return refresh_strategy_validation_manifest_evidence(
            strategy_key,
            _registry_seeds(),
            manifest_path=manifest_path,
            flag_events_path=flag_events_path,
        )
    except StrategyValidationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Strategy not found") from exc
    except StrategyValidationManifestError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/strategies/{strategy_key}/flag", response_model=StrategyValidationDetail)
async def flag_strategy_validation(
    strategy_key: str,
    body: StrategyValidationFlagRequest,
    manifest_path: Path = Depends(get_strategy_validation_manifest_path),
    flag_events_path: Path = Depends(get_strategy_validation_flag_events_path),
    flagged_by: str = Depends(get_strategy_validation_actor),
) -> StrategyValidationDetail:
    try:
        entry = append_strategy_validation_flag_event(
            strategy_key,
            body,
            _registry_seeds(),
            manifest_path=manifest_path,
            flag_events_path=flag_events_path,
            flagged_by=flagged_by,
        )
        return StrategyValidationDetail(
            **entry.model_dump(),
            reference_code=reference_code_for_entry(entry),
        )
    except StrategyValidationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Strategy not found") from exc
    except StrategyValidationManifestError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
