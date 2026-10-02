"""Custom Dark Bright Gates: saved per strategy, judged by the data plane (#2639 D8–D10).

Transport only: parsing, resolving and judging live in
``app.services.strategy_gates``; storage in ``app.services.strategy_gate_store``.
"""

from __future__ import annotations

import asyncio
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.schemas.strategy_gates import (
    CustomGate,
    CustomGateInput,
    GateCatalogue,
    GateEvaluationRequest,
    GateEvaluationResponse,
    GateRefusalBody,
    GateRequestInvalidBody,
    StrategyGateList,
)
from app.services.strategy_gate_store import GateNotFoundError, GateStoreError, StrategyGateStore
from app.services.strategy_gates import GateExpressionError, compile_gate, evaluate_gates, gate_catalogue
from app.services.strategy_view import ResolvedStrategyView, StrategyViewUnavailableError
from app.utils.timestamps import now_ms_utc

router = APIRouter()

_REFUSALS = {
    status.HTTP_404_NOT_FOUND: {"model": GateRefusalBody},
    # A refused expression, or a body that failed validation.
    status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": GateRefusalBody | GateRequestInvalidBody},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": GateRefusalBody},
}


def get_gate_store() -> StrategyGateStore:
    """The data plane's gate store; tests override this dependency."""
    return StrategyGateStore()


def _refuse(http_status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=http_status, detail={"code": code, "message": message})


def _strategy_view(
    strategy_key: str, settings: dict | None = None, *, symbol: str | None = None
) -> ResolvedStrategyView:
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    try:
        if registration is None:
            raise StrategyViewUnavailableError(f"Strategy '{strategy_key}' is not registered in this build.")
        return ResolvedStrategyView.for_settings(
            strategy_key, settings, symbol=symbol or registration.param_schema().symbol
        )
    except StrategyViewUnavailableError as exc:
        _refuse(status.HTTP_404_NOT_FOUND, "STRATEGY_VIEW_UNAVAILABLE", str(exc))


def _compiled(strategy_key: str, draft: CustomGateInput) -> tuple[list, float]:
    try:
        return compile_gate(_strategy_view(strategy_key), draft)
    except GateExpressionError as exc:
        _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "GATE_EXPRESSION_REFUSED", str(exc))


@router.get("/catalogue", response_model=GateCatalogue)
async def catalogue() -> GateCatalogue:
    """The catalogue indicators a gate can read, so the editor offers no name the data plane would refuse."""
    return GateCatalogue(indicators=list(await asyncio.to_thread(gate_catalogue)))


@router.get("/{strategy_key}", response_model=StrategyGateList, responses=_REFUSALS)
async def list_gates(strategy_key: str, store: StrategyGateStore = Depends(get_gate_store)) -> StrategyGateList:
    _strategy_view(strategy_key)
    try:
        return StrategyGateList(strategy_key=strategy_key, gates=store.for_strategy(strategy_key))
    except GateStoreError as exc:
        _refuse(status.HTTP_503_SERVICE_UNAVAILABLE, "GATE_STORE_UNAVAILABLE", str(exc))


@router.post("/{strategy_key}", response_model=CustomGate, status_code=status.HTTP_201_CREATED, responses=_REFUSALS)
async def create_gate(
    strategy_key: str, draft: CustomGateInput, store: StrategyGateStore = Depends(get_gate_store)
) -> CustomGate:
    # Resolving a catalogue name may compute the catalogue probe once; keep it off the event loop.
    terms, constant = await asyncio.to_thread(_compiled, strategy_key, draft)
    now_ms = now_ms_utc()
    try:
        return store.create(
            lambda gate_id: CustomGate(
                gate_id=gate_id,
                strategy_key=strategy_key,
                label=draft.label,
                expression=draft.expression,
                sign=draft.sign,
                terms=terms,
                constant=constant,
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
        )
    except GateStoreError as exc:
        _refuse(status.HTTP_503_SERVICE_UNAVAILABLE, "GATE_STORE_UNAVAILABLE", str(exc))


@router.put("/{strategy_key}/{gate_id}", response_model=CustomGate, responses=_REFUSALS)
async def replace_gate(
    strategy_key: str, gate_id: str, draft: CustomGateInput, store: StrategyGateStore = Depends(get_gate_store)
) -> CustomGate:
    # Resolving a catalogue name may compute the catalogue probe once; keep it off the event loop.
    terms, constant = await asyncio.to_thread(_compiled, strategy_key, draft)
    try:
        existing = store.get(strategy_key, gate_id)
        return store.replace(
            existing.model_copy(
                update={
                    "label": draft.label,
                    "expression": draft.expression,
                    "sign": draft.sign,
                    "terms": terms,
                    "constant": constant,
                    "updated_at_ms": now_ms_utc(),
                }
            )
        )
    except GateNotFoundError as exc:
        _refuse(status.HTTP_404_NOT_FOUND, "GATE_NOT_FOUND", str(exc))
    except GateStoreError as exc:
        _refuse(status.HTTP_503_SERVICE_UNAVAILABLE, "GATE_STORE_UNAVAILABLE", str(exc))


@router.delete("/{strategy_key}/{gate_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_REFUSALS)
async def delete_gate(strategy_key: str, gate_id: str, store: StrategyGateStore = Depends(get_gate_store)) -> Response:
    try:
        store.delete(strategy_key, gate_id)
    except GateNotFoundError as exc:
        _refuse(status.HTTP_404_NOT_FOUND, "GATE_NOT_FOUND", str(exc))
    except GateStoreError as exc:
        _refuse(status.HTTP_503_SERVICE_UNAVAILABLE, "GATE_STORE_UNAVAILABLE", str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{strategy_key}/evaluate", response_model=GateEvaluationResponse, responses=_REFUSALS)
async def evaluate(
    strategy_key: str, request: GateEvaluationRequest, store: StrategyGateStore = Depends(get_gate_store)
) -> GateEvaluationResponse:
    """Judge the strategy's saved gates, and an unsaved draft, on the caller's decision candles."""
    view = _strategy_view(strategy_key, dict(request.settings), symbol=request.symbol)
    try:
        gates = store.for_strategy(strategy_key)
        # Catalogue columns are computed here; keep that off the data plane's event loop.
        results, chart_computed, notices = await asyncio.to_thread(
            evaluate_gates, view, gates, request.candles, symbol=request.symbol, draft=request.draft
        )
    except GateExpressionError as exc:
        _refuse(status.HTTP_422_UNPROCESSABLE_ENTITY, "GATE_EXPRESSION_REFUSED", str(exc))
    except GateStoreError as exc:
        _refuse(status.HTTP_503_SERVICE_UNAVAILABLE, "GATE_STORE_UNAVAILABLE", str(exc))
    return GateEvaluationResponse(results=results, chart_computed=chart_computed, notices=notices)
