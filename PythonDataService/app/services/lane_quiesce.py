"""The lane half of installation migration's quiesce step (#2268).

``migrate-installation export`` moves the whole installation only when every
account is flat, and it gets there through two lane-local acts, each reached
through the coordinator's catalog-routed operations:

- **stop every bot on the lane, and record that it did.** The operator's
  Stop, per bot, so each bot's durable desired state reads ``STOPPED`` and it
  stays stopped wherever the lane next boots. The record is one receipt per
  call under the lane's own artifact root — on the lane's volume, so it
  travels in the migration bundle with the evidence it describes. Nothing
  here drains the lane or touches its assignment.
- **answer the account-quiet read without draining.** The answer is the one
  #2154 composed for the drain ceremony (``fleet_boot.lane_quiet_probe`` over
  ``observe_account_quiet``); this module only reads it on demand, so the
  lane's registry state is untouched and its assignment stays effective.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from app.broker.alpaca.clerk.fleet_boot import (
    LANE_QUIET_OBSERVATION_TIMEOUT_S,
    LaneQuietAnswer,
    LaneQuietProbe,
)
from app.services.bot_runner import LaneStopOutcome, LaneStoppedBot, LaneStopRefusal
from app.utils.atomic_file import atomic_write_bytes
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: The durable reason every bot stopped by a lane-wide stop records.
LANE_STOP_ALL_REASON = "lane_stop_all"

#: Receipts live under the lane's artifact root, one file per call.
STOP_ALL_RECEIPTS_DIRECTORY = "lane_stop_all_receipts"

#: How long an on-demand account-quiet read waits: the drain beat's own bound
#: for the same two broker reads. A read that overruns is no answer.
ACCOUNT_QUIET_READ_TIMEOUT_S = LANE_QUIET_OBSERVATION_TIMEOUT_S


class LaneBotStopper(Protocol):
    """The bot runner surface a lane-wide stop needs."""

    @property
    def artifacts_root(self) -> Path: ...

    async def stop_every_running_bot(
        self, *, updated_by: str, reason: str
    ) -> LaneStopOutcome: ...


@dataclass(frozen=True, slots=True)
class LaneStopAllReceipt:
    """The durable record of one lane-wide stop."""

    receipt_id: str
    requested_at_ms: int
    completed_at_ms: int
    operator: str
    change_ref: str
    stopped: tuple[LaneStoppedBot, ...]
    refused: tuple[LaneStopRefusal, ...]
    still_running: bool

    @property
    def all_stopped(self) -> bool:
        """Every Stop succeeded and no task survived them."""
        return not self.refused and not self.still_running

    def to_json(self) -> dict[str, Any]:
        """The receipt's durable and wire shape."""
        return {
            "receipt_id": self.receipt_id,
            "requested_at_ms": self.requested_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "operator": self.operator,
            "change_ref": self.change_ref,
            "reason": LANE_STOP_ALL_REASON,
            "stopped": [
                {"strategy_instance_id": bot.strategy_instance_id, "run_id": bot.run_id}
                for bot in self.stopped
            ],
            "refused": [
                {
                    "strategy_instance_id": bot.strategy_instance_id,
                    "run_id": bot.run_id,
                    "message": bot.message,
                    "detail": bot.detail,
                }
                for bot in self.refused
            ],
            "still_running": self.still_running,
            "all_stopped": self.all_stopped,
        }


async def stop_all_bots_on_lane(
    registry: LaneBotStopper,
    *,
    operator: str,
    change_ref: str,
    clock: Callable[[], int] = now_ms_utc,
) -> LaneStopAllReceipt:
    """Stop every running bot on this lane and durably record what happened.

    The receipt is written whether or not every Stop succeeded: an incomplete
    stop is exactly the evidence an operator needs, and its ``all_stopped``
    says so rather than the absence of a file.
    """
    requested_at_ms = clock()
    outcome = await registry.stop_every_running_bot(
        updated_by=operator, reason=LANE_STOP_ALL_REASON
    )
    receipt = LaneStopAllReceipt(
        receipt_id=uuid4().hex,
        requested_at_ms=requested_at_ms,
        completed_at_ms=clock(),
        operator=operator,
        change_ref=change_ref,
        stopped=outcome.stopped,
        refused=outcome.refused,
        still_running=outcome.still_running,
    )
    path = (
        registry.artifacts_root
        / STOP_ALL_RECEIPTS_DIRECTORY
        / f"{receipt.requested_at_ms}-{receipt.receipt_id}.json"
    )
    atomic_write_bytes(
        path, json.dumps(receipt.to_json(), sort_keys=True, indent=2).encode("utf-8")
    )
    logger.warning(
        "Lane-wide bot stop recorded",
        extra={
            "action": "lane_stop_all_recorded",
            "receipt_id": receipt.receipt_id,
            "operator": operator,
            "change_ref": change_ref,
            "stopped_count": len(receipt.stopped),
            "refused_count": len(receipt.refused),
            "still_running": receipt.still_running,
        },
    )
    return receipt


@dataclass(frozen=True, slots=True)
class LaneAccountQuietSource:
    """The canonical lane-quiet probe plus the account it answers for."""

    account_id: str
    probe: LaneQuietProbe


_ACCOUNT_QUIET_SOURCE: LaneAccountQuietSource | None = None


def set_lane_account_quiet_source(source: LaneAccountQuietSource | None) -> None:
    """Install (or clear) this process's account-quiet source."""
    global _ACCOUNT_QUIET_SOURCE
    _ACCOUNT_QUIET_SOURCE = source


def get_lane_account_quiet_source() -> LaneAccountQuietSource | None:
    """This process's account-quiet source, or ``None`` on a lane with no clerk."""
    return _ACCOUNT_QUIET_SOURCE


async def read_lane_account_quiet(source: LaneAccountQuietSource) -> LaneQuietAnswer | None:
    """One fresh account-quiet answer, or ``None`` when the lane cannot observe.

    ``None`` is no answer — an unreadable broker or an overrun read — never a
    "not quiet" one, exactly as on the drain beat.
    """
    try:
        return await asyncio.wait_for(source.probe(), timeout=ACCOUNT_QUIET_READ_TIMEOUT_S)
    except TimeoutError:
        logger.warning(
            "Account-quiet read timed out",
            extra={
                "action": "lane_account_quiet_read_timed_out",
                "account_id": source.account_id,
                "timeout_s": ACCOUNT_QUIET_READ_TIMEOUT_S,
            },
        )
        return None


__all__ = [
    "ACCOUNT_QUIET_READ_TIMEOUT_S",
    "LANE_STOP_ALL_REASON",
    "STOP_ALL_RECEIPTS_DIRECTORY",
    "LaneAccountQuietSource",
    "LaneStopAllReceipt",
    "get_lane_account_quiet_source",
    "read_lane_account_quiet",
    "set_lane_account_quiet_source",
    "stop_all_bots_on_lane",
]
