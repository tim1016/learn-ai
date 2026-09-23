"""Wire shapes for installation migration's go-live lane operations (#2269)."""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from app.services.go_live_hold import GoLiveHoldMarker
from app.utils.session_anchors import MAX_TIMESTAMP_MS

#: The exact words the operator types, and the release request carries, to
#: say the old machine is off. The lane refuses a release without them, so a
#: passing bar check alone never releases a lane.
OldMachineOffConfirmation = Literal["the old machine is off"]
OLD_MACHINE_OFF_CONFIRMATION: str = get_args(OldMachineOffConfirmation)[0]


class LaneIbkrBarCheckRead(BaseModel):
    """Proof that IB Gateway returned real historical bars to this lane.

    Only a check that returned at least one bar answers 200; the instants are
    the first bar's start and the last bar's end (``int64 ms UTC``).
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    bar_count: int = Field(ge=1)
    first_bar_start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    last_bar_end_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    checked_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)


class LaneGoLiveReleaseRequest(BaseModel):
    """Who releases the lane, under which change record, and their confirmation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operator: str = Field(min_length=1, max_length=128)
    change_ref: str = Field(min_length=1, max_length=512)
    old_machine_off_confirmation: OldMachineOffConfirmation


class LaneGoLiveReleaseReceipt(BaseModel):
    """The durable receipt of one go-live release, as recorded on the lane."""

    model_config = ConfigDict(frozen=True)

    receipt_id: str
    released_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    operator: str
    change_ref: str
    was_held: bool
    marker: GoLiveHoldMarker | None
    marker_problem: str | None
    bar_check: LaneIbkrBarCheckRead


__all__ = [
    "OLD_MACHINE_OFF_CONFIRMATION",
    "LaneGoLiveReleaseReceipt",
    "LaneGoLiveReleaseRequest",
    "LaneIbkrBarCheckRead",
    "OldMachineOffConfirmation",
]
