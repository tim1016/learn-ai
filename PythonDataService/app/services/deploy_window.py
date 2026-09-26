"""The canonical Start window shared by admission and deploy summaries."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.lean_sidecar.trading_calendar import next_trading_day
from app.schemas.run_admission import StartWindowFact
from app.services.session_authority import scheduled_exchange_phase_at_ms, scheduled_extended_session_bounds
from app.utils.session_anchors import et_date_at_ms


def deploy_window(now_ms: int) -> StartWindowFact:
    phase = scheduled_exchange_phase_at_ms(now_ms)
    if phase in {"PRE", "RTH"}:
        return StartWindowFact(state="PREMARKET" if phase == "PRE" else "REGULAR")
    day = et_date_at_ms(now_ms)
    bounds = scheduled_extended_session_bounds(day)
    if bounds is None or now_ms >= bounds.rth_close_ms:
        bounds = scheduled_extended_session_bounds(next_trading_day(day))
    if bounds is None:
        raise RuntimeError("The canonical calendar returned no next trading session")
    return StartWindowFact(state="CLOSED", next_open_ms=bounds.open_ms)


def session_label(instant: int) -> str:
    return datetime.fromtimestamp(instant / 1000, UTC).astimezone(ZoneInfo("America/New_York")).strftime("%a %b %d %H:%M ET")


def start_window_next_step(window: StartWindowFact) -> str:
    if window.next_open_ms is None:
        return "Start is allowed in the current session."
    return f"Next Start or Resume window opens {session_label(window.next_open_ms)}."
