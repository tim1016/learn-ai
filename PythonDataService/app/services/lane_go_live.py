"""The lane half of installation migration's go-live (#2269).

``migrate-installation go-live`` releases every migrated lane's hold only
after two facts, each reached through the coordinator's catalog-routed
operations:

- **IB Gateway delivers bars to this lane.** ``lane_ibkr_bar_check`` asks the
  lane's own IB Gateway connection for recent *historical* SPY minute bars,
  so it answers off-hours too, and passes only when at least one real bar
  came back. ``FeedHealth`` cannot prove this: it counts an idle but
  connected feed as healthy. The read-only IBKR feed supplies live bars and
  Alpaca handles accounts and orders (ADR 0062's provider decision); this
  check reads, it never subscribes or trades.
- **The operator says the old machine is off.** ``lane_go_live_release``
  refuses unless its request carries the exact confirmation words **and**
  this lane passed a bar check within :data:`BAR_CHECK_FRESHNESS_MS` — so
  neither fact alone releases a lane, even for a caller that skips the CLI.

The last passing check lives in this process only: a lane that restarted
since its check must pass again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.broker.ibkr.bars import IBKRBarStreamError, fetch_historical_minute_bars
from app.broker.ibkr.client import BrokerError, get_client
from app.broker_configuration.runtime import resolve_clerk_dir
from app.services.go_live_hold import (
    GoLiveHoldState,
    GoLiveReleaseReceipt,
    read_go_live_hold,
    release_go_live_hold,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: The instrument whose bars prove the feed: the owner's reference symbol.
BAR_CHECK_SYMBOL = "SPY"
#: Trailing calendar days asked for. Wide enough that a long weekend (a
#: Friday holiday, then Saturday and Sunday) still ends inside the window.
BAR_CHECK_LOOKBACK_DAYS = 5
#: The lane's own bound on the whole check (qualify + one historical request).
IBKR_BAR_CHECK_TIMEOUT_S = 30.0
#: How long a passing check counts toward a release.
BAR_CHECK_FRESHNESS_MS = 15 * 60_000


class LaneBarCheckFailed(Exception):
    """The lane could not prove IB Gateway delivers bars, and says why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class GoLiveBarCheckRequired(Exception):
    """A release was asked for without a fresh passing bar check on this lane."""


@dataclass(frozen=True, slots=True)
class LaneBarCheck:
    """One passing bar check: how many bars came back, and when they span."""

    symbol: str
    bar_count: int
    first_bar_start_ms: int
    last_bar_end_ms: int
    checked_at_ms: int

    def to_json(self) -> dict[str, Any]:
        """The check's durable and wire shape."""
        return {
            "symbol": self.symbol,
            "bar_count": self.bar_count,
            "first_bar_start_ms": self.first_bar_start_ms,
            "last_bar_end_ms": self.last_bar_end_ms,
            "checked_at_ms": self.checked_at_ms,
        }


class GoLiveHoldRootUnresolvedError(Exception):
    """This process cannot name the clerk volume root its go-live hold lives at."""


def lane_go_live_hold_root() -> Path:
    """Where this process's go-live hold lives: its clerk volume root.

    Import lays the marker at the root of each clerk volume, and every role
    that hosts a bot runner mounts that volume at the clerk directory
    (``ALPACA_CLERK_DIR``, else its default) — the same root
    ``open_fleet_lane`` proves a fleet lane's identity against. Not the bot
    runner's artifacts root: the combined role keeps that on the shared
    ``/app/artifacts`` bind, outside the volume, where no marker ever lands.

    A relative clerk directory names no volume; it raises rather than
    resolving against whatever the working directory happens to be.
    """
    root = resolve_clerk_dir()
    if not root.is_absolute():
        raise GoLiveHoldRootUnresolvedError(
            f"The clerk directory {root} is not an absolute path, so this process cannot "
            "tell which clerk volume carries its go-live hold. Set ALPACA_CLERK_DIR to the "
            "clerk volume's mount point."
        )
    return root


def lane_go_live_hold() -> GoLiveHoldState:
    """This lane's go-live hold, read at its clerk volume root; fails closed.

    A root that cannot be resolved holds, exactly as an unreadable one does
    (:func:`read_go_live_hold`); only a readable root without a marker — a
    machine never migrated, or one already released — does not.
    """
    try:
        root = lane_go_live_hold_root()
    except GoLiveHoldRootUnresolvedError as exc:
        return GoLiveHoldState(held=True, problem=str(exc), problem_kind="root_unreadable")
    return read_go_live_hold(root)


_LAST_PASSING: LaneBarCheck | None = None


def _fail(reason: str, message: str) -> LaneBarCheckFailed:
    global _LAST_PASSING
    _LAST_PASSING = None
    logger.warning(
        "Go-live bar check failed",
        extra={"action": "lane_ibkr_bar_check_failed", "reason": reason, "error": message},
    )
    return LaneBarCheckFailed(reason, message)


async def check_ibkr_historical_bars(
    *, clock: Callable[[], int] = now_ms_utc
) -> LaneBarCheck:
    """Ask this lane's IB Gateway for recent historical bars; pass only on a real bar.

    Any failure — no client, a disconnected or soft-lost socket, a timeout, a
    refused request, or zero bars — raises :class:`LaneBarCheckFailed` naming
    it, and forgets any earlier pass.
    """
    global _LAST_PASSING
    try:
        client = get_client()
    except BrokerError as exc:
        raise _fail("ibkr_gateway_unreachable", f"This lane has no IB Gateway client: {exc}") from exc
    if not client.is_connected() or client.connection_lost:
        raise _fail(
            "ibkr_gateway_unreachable",
            "This lane's IB Gateway connection is down"
            + (" (connectivity lost, code 1100)." if client.is_connected() else "."),
        )
    try:
        bars = await asyncio.wait_for(
            fetch_historical_minute_bars(
                client,
                BAR_CHECK_SYMBOL,
                duration=f"{BAR_CHECK_LOOKBACK_DAYS} D",
                use_rth=True,
            ),
            timeout=IBKR_BAR_CHECK_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise _fail(
            "ibkr_bar_check_timed_out",
            f"IB Gateway did not answer a historical {BAR_CHECK_SYMBOL} bar request within "
            f"{IBKR_BAR_CHECK_TIMEOUT_S:g} s.",
        ) from exc
    except (IBKRBarStreamError, BrokerError, ValueError) as exc:
        raise _fail(
            "ibkr_bar_check_failed",
            f"IB Gateway refused the historical {BAR_CHECK_SYMBOL} bar request: {exc}",
        ) from exc
    if not bars:
        raise _fail(
            "ibkr_no_bars",
            f"IB Gateway answered the historical {BAR_CHECK_SYMBOL} bar request with no bars "
            f"for the last {BAR_CHECK_LOOKBACK_DAYS} days; a connection that returns no bar "
            "proves nothing.",
        )
    check = LaneBarCheck(
        symbol=BAR_CHECK_SYMBOL,
        bar_count=len(bars),
        first_bar_start_ms=min(bar.start_ms for bar in bars),
        last_bar_end_ms=max(bar.end_ms for bar in bars),
        checked_at_ms=clock(),
    )
    _LAST_PASSING = check
    logger.info(
        "Go-live bar check passed",
        extra={"action": "lane_ibkr_bar_check_passed", **check.to_json()},
    )
    return check


def forget_bar_checks() -> None:
    """Drop this process's last passing check (lifespan teardown and tests)."""
    global _LAST_PASSING
    _LAST_PASSING = None


def release_lane_go_live(
    lane_root: Path,
    *,
    operator: str,
    change_ref: str,
    clock: Callable[[], int] = now_ms_utc,
) -> GoLiveReleaseReceipt:
    """Release this lane's go-live hold, only behind a fresh passing bar check.

    The caller has already required the operator's confirmation words (the
    request model refuses without them); this adds the bar check, so the
    lane itself enforces both halves.
    """
    check = _LAST_PASSING
    now_ms = clock()
    if check is None or now_ms - check.checked_at_ms > BAR_CHECK_FRESHNESS_MS:
        logger.warning(
            "Go-live release refused: no fresh bar check",
            extra={
                "action": "lane_go_live_release_refused",
                "operator": operator,
                "change_ref": change_ref,
                "last_check_at_ms": None if check is None else check.checked_at_ms,
            },
        )
        raise GoLiveBarCheckRequired(
            "This lane has not passed an IB Gateway bar check in the last "
            f"{BAR_CHECK_FRESHNESS_MS // 60_000} minutes (or restarted since); run the "
            "bar check, then release."
        )
    return release_go_live_hold(
        lane_root,
        operator=operator,
        change_ref=change_ref,
        bar_check=check.to_json(),
        clock=lambda: now_ms,
    )


__all__ = [
    "BAR_CHECK_FRESHNESS_MS",
    "BAR_CHECK_LOOKBACK_DAYS",
    "BAR_CHECK_SYMBOL",
    "IBKR_BAR_CHECK_TIMEOUT_S",
    "GoLiveBarCheckRequired",
    "GoLiveHoldRootUnresolvedError",
    "LaneBarCheck",
    "LaneBarCheckFailed",
    "check_ibkr_historical_bars",
    "forget_bar_checks",
    "lane_go_live_hold",
    "lane_go_live_hold_root",
    "release_lane_go_live",
]
