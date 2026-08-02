"""Broker-parameterized bot runner routes (transport only).

``/api/brokers/{broker}/bots/...`` — Alpaca Bot Control v2, S2 (#1260).
The router resolves the ``{broker}`` segment via the broker registry
(unknown broker → typed 404), calls the in-container
:class:`BotTaskRegistry`, and translates typed runner errors to HTTP.
No business logic lives here (router-freeze discipline).
"""

from __future__ import annotations

import logging
from typing import NoReturn

from fastapi import APIRouter, HTTPException

from app.broker.contract.errors import BrokerError
from app.broker.contract.registry import get_broker_registry
from app.routers.brokers import _raise_http
from app.schemas.broker_bots import BotStatusView, DeployBotRequest, StopBotRequest
from app.services.bot_runner import (
    BotRunnerError,
    BotTaskRegistry,
    get_bot_task_registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brokers", tags=["broker-bots"])


def _resolve_broker(broker: str) -> str:
    """Resolve the path segment against the broker registry (404 when unknown)."""
    try:
        get_broker_registry().resolve(broker)
    except BrokerError as error:
        _raise_http(error)
    return broker


def _require_registry() -> BotTaskRegistry:
    registry = get_bot_task_registry()
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail="Bot runner is not available (service still starting or shut down).",
        )
    return registry


def _raise_runner_error(error: BotRunnerError) -> NoReturn:
    raise HTTPException(
        status_code=error.http_status,
        detail={
            "message": str(error),
            "why": error.detail,
            "admission": (
                error.admission_decision.model_dump(mode="json")
                if error.admission_decision is not None
                else None
            ),
        },
    )


@router.post(
    "/{broker}/bots",
    response_model=BotStatusView,
    status_code=201,
    summary="Deploy and start a log-only bot bound to this broker",
)
async def deploy_bot(broker: str, request: DeployBotRequest) -> BotStatusView:
    _resolve_broker(broker)
    registry = _require_registry()
    try:
        return await registry.deploy(
            broker=broker,
            strategy_instance_id=request.strategy_instance_id,
            symbol=request.symbol,
            use_rth=request.use_rth,
            mode=request.mode,
            quantity=request.quantity,
        )
    except BotRunnerError as error:
        _raise_runner_error(error)


@router.get(
    "/{broker}/bots",
    response_model=list[BotStatusView],
    summary="List bots whose durable binding carries this broker tag",
)
async def list_bots(broker: str) -> list[BotStatusView]:
    _resolve_broker(broker)
    registry = _require_registry()
    try:
        return registry.list_bots(broker)
    except BotRunnerError as error:
        _raise_runner_error(error)


@router.get(
    "/{broker}/bots/{strategy_instance_id}",
    response_model=BotStatusView,
    summary="One bot's roster row (artifact-derived state + registry liveness)",
)
async def get_bot_status(broker: str, strategy_instance_id: str) -> BotStatusView:
    _resolve_broker(broker)
    registry = _require_registry()
    try:
        return registry.status(broker, strategy_instance_id)
    except BotRunnerError as error:
        _raise_runner_error(error)


@router.post(
    "/{broker}/bots/{strategy_instance_id}/stop",
    response_model=BotStatusView,
    summary="Stop a running bot (durable STOPPED intent first, then reap)",
)
async def stop_bot(
    broker: str, strategy_instance_id: str, request: StopBotRequest | None = None
) -> BotStatusView:
    _resolve_broker(broker)
    registry = _require_registry()
    try:
        return await registry.stop(
            broker,
            strategy_instance_id,
            reason=request.reason if request is not None else None,
        )
    except BotRunnerError as error:
        _raise_runner_error(error)
