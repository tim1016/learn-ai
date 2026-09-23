"""Wire shapes for the lane's installation-migration quiesce reads (#2268)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS


class LaneStopAllBotsRequest(BaseModel):
    """Who is stopping every bot on the lane, and under which change record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operator: str = Field(min_length=1, max_length=128)
    change_ref: str = Field(min_length=1, max_length=512)


class LaneStoppedBotRead(BaseModel):
    """One bot the lane-wide stop ended."""

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    run_id: str


class LaneIntentStoppedBotRead(BaseModel):
    """One bot with no live task whose recorded intent the stop set to STOPPED."""

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    previous_desired_state: str


class LaneStopRefusalRead(BaseModel):
    """One bot whose Stop refused, in the refusal's own words.

    ``run_id`` is null for a bot with no live task whose recorded intent could
    not be read or rewritten.
    """

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    run_id: str | None
    message: str
    detail: str | None


class LaneStopAllBotsReceipt(BaseModel):
    """The durable receipt of one lane-wide stop, as recorded on the lane."""

    model_config = ConfigDict(frozen=True)

    receipt_id: str
    requested_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    completed_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    operator: str
    change_ref: str
    reason: str
    stopped: list[LaneStoppedBotRead]
    intent_stopped: list[LaneIntentStoppedBotRead]
    refused: list[LaneStopRefusalRead]
    still_running: bool
    all_stopped: bool


class LaneAccountQuietRead(BaseModel):
    """This lane's account-quiet answer, read on demand without draining.

    The four conditions are ``confirm_lane_quiet``'s own names; ``outstanding``
    lists the unsatisfied ones in the registry's declared phrasing, so a
    refusal names which condition is open — never an order or a position.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    observed_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    runner_idle: bool
    broker_work_ended: bool
    account_flat: bool
    intents_resolved: bool
    quiet: bool
    outstanding: list[str]


__all__ = [
    "LaneAccountQuietRead",
    "LaneIntentStoppedBotRead",
    "LaneStopAllBotsReceipt",
    "LaneStopAllBotsRequest",
    "LaneStopRefusalRead",
    "LaneStoppedBotRead",
]
