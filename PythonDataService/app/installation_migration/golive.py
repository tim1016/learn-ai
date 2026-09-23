"""``migrate-installation go-live``: release every migrated lane at once (#2269).

After ``import`` every clerk volume carries a go-live hold, so no bot can
start on the new host. The order is the safety argument:

1. **Bars on every lane.** Each live lane asks its own IB Gateway for recent
   historical SPY bars (``lane_ibkr_bar_check``) and passes only when at
   least one real bar came back — off-hours too. Any lane that cannot prove
   it refuses the whole go-live: nothing is released.
2. **The operator types that the old machine is off** — only after every
   lane passed, so the confirmation is never asked for, or given, on a host
   that cannot see bars.
3. **Every lane is released** (``lane_go_live_release``). The lane itself
   also refuses a release without the confirmation words or without its own
   fresh passing bar check, so neither half alone releases a lane even for
   a caller that skips this command.

Bots stay stopped: go-live starts nothing, and the operator starts each bot
by hand. Re-running go-live is safe; a released lane releases again as a
recorded no-op.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.installation_migration.errors import MigrationRefused
from app.installation_migration.lanes import GoLiveLanes, Lane
from app.schemas.lane_go_live import OLD_MACHINE_OFF_CONFIRMATION

Emit = Callable[[Mapping[str, object]], None]
#: Shows the operator a prompt and returns the line they typed.
Confirm = Callable[[str], str]

CONFIRMATION_PROMPT = (
    f"Every lane receives IB Gateway bars. Shut the old machine down, then type "
    f'"{OLD_MACHINE_OFF_CONFIRMATION}" to release every lane: '
)


@dataclass(frozen=True, slots=True)
class GoLiveRequest:
    """One go-live invocation's operator inputs."""

    operator: str
    change_ref: str


def run_go_live(
    request: GoLiveRequest, *, lanes: GoLiveLanes, confirm: Confirm, emit: Emit
) -> None:
    """Run go-live; every refusal raises :class:`MigrationRefused`."""
    live = [lane for lane in lanes.list_lanes() if lane.lifecycle_state != "retired"]
    if not live:
        raise MigrationRefused(
            "no_lanes",
            "The coordinator's fleet directory lists no live lane, so there is nothing to "
            "take live. Is the stack up?",
        )
    emit({"step": "lanes", "lanes": [lane.clerk_id for lane in live]})

    _prove_bars_on_every_lane(lanes, live, emit)

    typed = confirm(CONFIRMATION_PROMPT)
    if typed.strip() != OLD_MACHINE_OFF_CONFIRMATION:
        raise MigrationRefused(
            "old_machine_off_not_confirmed",
            f'Go-live needs the exact words "{OLD_MACHINE_OFF_CONFIRMATION}" once the old '
            "machine is shut down. Nothing was released; every lane still holds its bots.",
            details={"lanes": [lane.clerk_id for lane in live]},
        )
    emit({"step": "old-machine-off-confirmed", "operator": request.operator})

    released = _release_every_lane(lanes, live, request, emit)
    emit(
        {
            "step": "complete",
            "released": released,
            "next": "Every lane is live. No bot was started: start each one by hand when "
            "you are ready. Never restart the old machine's copy; going back is a reverse "
            "migration (export here, import there, go-live there).",
        }
    )


def _prove_bars_on_every_lane(lanes: GoLiveLanes, live: list[Lane], emit: Emit) -> None:
    passed: list[str] = []
    for lane in live:
        try:
            check = lanes.ibkr_bar_check(lane)
        except MigrationRefused as exc:
            raise MigrationRefused(
                exc.reason,
                f"{exc.message} Go-live proves bars on every lane before anything is "
                "released: nothing was released, and every lane still holds its bots. Fix "
                "IB Gateway for this lane, then re-run go-live.",
                details={**exc.details, "bars_proven_on": passed},
            ) from exc
        passed.append(lane.clerk_id)
        emit(
            {
                "step": "bars-proven",
                "clerk_id": lane.clerk_id,
                "symbol": check.symbol,
                "bar_count": check.bar_count,
                "last_bar_end_ms": check.last_bar_end_ms,
            }
        )


def _release_every_lane(
    lanes: GoLiveLanes, live: list[Lane], request: GoLiveRequest, emit: Emit
) -> list[str]:
    released: list[str] = []
    for lane in live:
        try:
            receipt = lanes.release_go_live(
                lane, operator=request.operator, change_ref=request.change_ref
            )
        except MigrationRefused as exc:
            pending = [other.clerk_id for other in live if other.clerk_id not in released]
            raise MigrationRefused(
                exc.reason,
                f"{exc.message} Released so far: {', '.join(released) or 'none'}; still "
                f"held: {', '.join(pending)}. Re-run go-live — a released lane releases "
                "again as a no-op.",
                details={**exc.details, "released": released, "still_held": pending},
            ) from exc
        released.append(lane.clerk_id)
        emit(
            {
                "step": "lane-released",
                "clerk_id": lane.clerk_id,
                "receipt_id": receipt.receipt_id,
                "was_held": receipt.was_held,
            }
        )
    return released


__all__ = ["CONFIRMATION_PROMPT", "GoLiveRequest", "run_go_live"]
