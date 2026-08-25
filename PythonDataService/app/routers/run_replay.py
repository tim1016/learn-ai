"""Per-run replay-parity receipts (transport only).

``/api/brokers/{broker}/bots/{sid}/runs/{run_id}/replay-receipt`` — Direction 2
(docs/audits/strategy-execution-research-directions-2026-08-24.md). GET reads
the durable receipt; POST regenerates it for a completed run. All business
logic lives in ``app.services.run_replay_proof`` (router-freeze discipline).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.routers.broker_bots import _raise_runner_error, _require_registry, _resolve_broker
from app.schemas.run_replay import RunReplayReceipt
from app.services.bot_runner import BotRunnerError
from app.services.run_replay_proof import RunReplayUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brokers", tags=["run-replay"])


@router.get(
    "/{broker}/bots/{strategy_instance_id}/runs/{run_id}/replay-receipt",
    response_model=RunReplayReceipt,
    summary="Read one run's durable replay-parity receipt",
)
async def read_run_replay_receipt(
    broker: str, strategy_instance_id: str, run_id: str
) -> RunReplayReceipt:
    _resolve_broker(broker)
    registry = _require_registry()
    receipt = registry.run_replay_receipt(broker, strategy_instance_id, run_id)
    if receipt is None:
        raise HTTPException(
            status_code=404,
            detail=f"Run '{run_id}' has no replay receipt; POST this path to generate one.",
        )
    return receipt


@router.post(
    "/{broker}/bots/{strategy_instance_id}/runs/{run_id}/replay-receipt",
    response_model=RunReplayReceipt,
    summary="Recompute one completed run's replay-parity receipt from its retained bars",
)
async def generate_run_replay_receipt(
    broker: str, strategy_instance_id: str, run_id: str
) -> RunReplayReceipt:
    _resolve_broker(broker)
    registry = _require_registry()
    try:
        return await registry.generate_run_replay_receipt(broker, strategy_instance_id, run_id)
    except RunReplayUnavailableError as error:
        raise HTTPException(
            status_code=error.http_status,
            detail={"message": str(error), "why": error.detail},
        ) from error
    except BotRunnerError as error:
        _raise_runner_error(error)
