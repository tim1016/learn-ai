"""The go-live hold a migrated lane carries until go-live releases it (#2269).

``migrate-installation import`` restores every lane volume on the new host
and writes one marker file at each clerk volume's root. While that file
exists the lane's bot-start seam refuses (``BotTaskRegistry``, beside the
drained-lane gate): the new host has not yet proven that IB Gateway delivers
bars there, and the operator has not yet said the old machine is off. Only
bot starts are held — the owner rejected a guard on every order; manual
trading stays off (its flag unset), every account moved flat, and no bot
auto-resumes after a restart.

``migrate-installation go-live`` removes the marker through the lane's own
``lane_go_live_release`` operation (:func:`release_go_live_hold`), which
writes a receipt on the same volume first — no lane is released without one.

The hold fails closed: a marker that exists but cannot be parsed still
holds, and a lane that cannot tell whether its marker exists — the stat
fails, or the volume root is not a readable directory — holds too.

Deliberately light (pydantic and the file helpers only): the host-side
import tool builds the marker with the same model the lane reads.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.utils.atomic_file import atomic_write_bytes, fsync_parent_dir
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: The marker's name at the root of a clerk volume (the lane's artifact root).
GO_LIVE_HOLD_MARKER = "go-live-pending.json"

#: Release receipts live beside the marker, one file per release.
GO_LIVE_RECEIPTS_DIRECTORY = "go_live_receipts"


class GoLiveHoldMarker(BaseModel):
    """What import records in each marker: which bundle put the lane on hold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["learn-ai-go-live-hold"]
    schema_version: Literal[1]
    written_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    volume: str
    source_commit: str
    registry_id: str


def go_live_marker_bytes(marker: GoLiveHoldMarker) -> bytes:
    """The marker's on-disk bytes."""
    return marker.model_dump_json(indent=2).encode("utf-8")


#: Why a lane holds without a marker it could read.
#:
#: - ``root_unreadable``: the lane's artifact root is not a readable directory,
#:   or could not be resolved at all.
#: - ``marker_unreachable``: the marker could not be read, and nothing at its
#:   path is a regular file this lane can see — whether a marker exists
#:   cannot be established.
#: - ``marker_unreadable``: a marker file is present but its bytes could not
#:   be read.
#: - ``marker_unparseable``: a marker file was read but is not a go-live hold
#:   marker.
GoLiveHoldProblemKind = Literal[
    "root_unreadable", "marker_unreachable", "marker_unreadable", "marker_unparseable"
]

#: The problems under which a marker file is known to be present, so release
#: may remove it; under every other problem the lane cannot tell and refuses.
_MARKER_PRESENT_PROBLEMS: frozenset[GoLiveHoldProblemKind] = frozenset(
    {"marker_unreadable", "marker_unparseable"}
)


@dataclass(frozen=True, slots=True)
class GoLiveHoldState:
    """Whether this lane is held, and what it could read about the hold.

    Exactly one of three shapes: not held (nothing else set); held by a
    marker it read (``marker``); or held because something could not be read
    (``problem_kind`` and its ``problem`` text, no ``marker``).
    """

    held: bool
    marker: GoLiveHoldMarker | None = None
    problem: str | None = None
    problem_kind: GoLiveHoldProblemKind | None = None

    def __post_init__(self) -> None:
        if (self.problem is None) != (self.problem_kind is None):
            raise ValueError("a go-live hold problem needs both its kind and its text")
        if self.problem_kind is not None:
            if not self.held or self.marker is not None:
                raise ValueError("a go-live hold problem holds the lane without a marker")
        elif self.held != (self.marker is not None):
            raise ValueError("a go-live hold without a problem is held exactly when it has a marker")


def _marker_path(lane_root: Path) -> Path:
    return lane_root / GO_LIVE_HOLD_MARKER


def _is_visible_file(path: Path) -> bool:
    """Whether a regular file is visibly at ``path``; ``False`` when unsure."""
    try:
        return path.is_file()
    except OSError:
        return False


def read_go_live_hold(lane_root: Path) -> GoLiveHoldState:
    """This lane's go-live hold, failing closed on anything unreadable."""
    try:
        if not lane_root.is_dir():
            return GoLiveHoldState(
                held=True,
                problem=f"the lane's artifact root {lane_root} is not a readable directory",
                problem_kind="root_unreadable",
            )
        raw = _marker_path(lane_root).read_bytes()
    except FileNotFoundError:
        return GoLiveHoldState(held=False)
    except OSError as exc:
        return GoLiveHoldState(
            held=True,
            problem=f"{_marker_path(lane_root)} could not be read: {exc}",
            problem_kind=(
                "marker_unreadable"
                if _is_visible_file(_marker_path(lane_root))
                else "marker_unreachable"
            ),
        )
    try:
        return GoLiveHoldState(held=True, marker=GoLiveHoldMarker.model_validate_json(raw))
    except ValidationError as exc:
        return GoLiveHoldState(
            held=True,
            problem=f"{_marker_path(lane_root)} is not a go-live hold marker: {exc}",
            problem_kind="marker_unparseable",
        )


class GoLiveHoldUnreadableError(Exception):
    """The lane cannot establish whether it is held, so it cannot release."""


class GoLiveReleaseFailedError(Exception):
    """The release's receipt could not be written, or its marker not removed."""


@dataclass(frozen=True, slots=True)
class GoLiveReleaseReceipt:
    """The durable record of one go-live release on this lane."""

    receipt_id: str
    released_at_ms: int
    operator: str
    change_ref: str
    was_held: bool
    marker: dict[str, Any] | None
    marker_problem: str | None
    bar_check: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        """The receipt's durable and wire shape."""
        return {
            "receipt_id": self.receipt_id,
            "released_at_ms": self.released_at_ms,
            "operator": self.operator,
            "change_ref": self.change_ref,
            "was_held": self.was_held,
            "marker": self.marker,
            "marker_problem": self.marker_problem,
            "bar_check": self.bar_check,
        }


def release_go_live_hold(
    lane_root: Path,
    *,
    operator: str,
    change_ref: str,
    bar_check: dict[str, Any],
    clock: Callable[[], int] = now_ms_utc,
) -> GoLiveReleaseReceipt:
    """Record the release durably, then remove this lane's marker (if any).

    Receipt first: a lane is never released without its durable record, so a
    receipt that cannot be written raises and leaves the marker — the lane
    stays held. A receipt whose marker then could not be removed is a record
    of an attempt, and the lane still holds; re-running go-live finishes it.

    Idempotent: a lane with no marker writes a receipt saying so. A marker
    that exists but does not parse is still removed — go-live is exactly the
    ceremony that ends it — and the receipt names the problem. A lane that
    cannot tell whether a marker exists raises, and stays held.
    """
    state = read_go_live_hold(lane_root)
    if state.problem is not None and state.problem_kind not in _MARKER_PRESENT_PROBLEMS:
        raise GoLiveHoldUnreadableError(state.problem)
    receipt = GoLiveReleaseReceipt(
        receipt_id=uuid4().hex,
        released_at_ms=clock(),
        operator=operator,
        change_ref=change_ref,
        was_held=state.held,
        marker=None if state.marker is None else state.marker.model_dump(),
        marker_problem=state.problem,
        bar_check=bar_check,
    )
    receipt_path = (
        lane_root / GO_LIVE_RECEIPTS_DIRECTORY / f"{receipt.released_at_ms}-{receipt.receipt_id}.json"
    )
    try:
        atomic_write_bytes(
            receipt_path,
            json.dumps(receipt.to_json(), sort_keys=True, indent=2).encode("utf-8"),
        )
    except OSError as exc:
        raise GoLiveReleaseFailedError(
            f"The release receipt {receipt_path} could not be written ({exc}); the "
            "go-live hold was left in place, so this lane still starts no bots."
        ) from exc
    if state.held:
        try:
            _marker_path(lane_root).unlink()
        except OSError as exc:
            raise GoLiveReleaseFailedError(
                f"The go-live hold {_marker_path(lane_root)} could not be removed ({exc}); "
                f"receipt {receipt_path} records the attempt, and this lane still starts "
                "no bots. Re-run go-live."
            ) from exc
        try:
            fsync_parent_dir(_marker_path(lane_root))
        except OSError as exc:
            raise GoLiveReleaseFailedError(
                f"The go-live hold {_marker_path(lane_root)} was removed, but the removal "
                f"could not be made durable ({exc}); receipt {receipt_path} records it. "
                "Re-run go-live to confirm the release."
            ) from exc
    logger.warning(
        "Go-live hold released",
        extra={
            "action": "lane_go_live_released",
            "receipt_id": receipt.receipt_id,
            "operator": operator,
            "change_ref": change_ref,
            "was_held": state.held,
            "marker_problem": state.problem,
        },
    )
    return receipt


__all__ = [
    "GO_LIVE_HOLD_MARKER",
    "GO_LIVE_RECEIPTS_DIRECTORY",
    "GoLiveHoldMarker",
    "GoLiveHoldProblemKind",
    "GoLiveHoldState",
    "GoLiveHoldUnreadableError",
    "GoLiveReleaseFailedError",
    "GoLiveReleaseReceipt",
    "go_live_marker_bytes",
    "read_go_live_hold",
    "release_go_live_hold",
]
