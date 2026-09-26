"""The shared calendar gate for regular-session Start, Resume and Deploy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.broker.alpaca.clerk.program_leg import LegRefusal
from app.lean_sidecar.trading_calendar import next_trading_day
from app.services.session_authority import scheduled_extended_session_bounds


@dataclass(frozen=True)
class DeployWindow:
    ready: bool
    premarket: bool
    next_open_ms: int | None
    refusal: LegRefusal | None


def deploy_window(now_ms: int) -> DeployWindow:
    day = datetime.fromtimestamp(now_ms / 1000, UTC).astimezone(ZoneInfo("America/New_York")).date()
    bounds = scheduled_extended_session_bounds(day)
    if bounds is not None and bounds.open_ms <= now_ms < bounds.rth_close_ms:
        return DeployWindow(True, now_ms < bounds.rth_open_ms, None, None)
    if bounds is None or now_ms >= bounds.rth_close_ms:
        bounds = scheduled_extended_session_bounds(next_trading_day(day))
    if bounds is None:
        raise RuntimeError("The canonical calendar returned no next trading session")
    label = (
        datetime.fromtimestamp(bounds.open_ms / 1000, UTC)
        .astimezone(ZoneInfo("America/New_York"))
        .strftime("%a %b %d at %H:%M ET")
    )
    return DeployWindow(
        False,
        False,
        bounds.open_ms,
        LegRefusal(
            reason_code="DEPLOY_WINDOW_CLOSED",
            available_at_ms=bounds.open_ms,
            explanation="Regular-session bots may start from pre-market open through the regular close.",
            next_step=f"Next Start or Resume window opens {label}.",
        ),
    )
