"""The canonical Start window shared by admission and deploy summaries."""

from __future__ import annotations

from app.lean_sidecar.trading_calendar import next_trading_day
from app.schemas.run_admission import StartWindowFact
from app.services.session_authority import scheduled_exchange_phase_at_ms, scheduled_extended_session_bounds
from app.utils.et_words import et_when_words
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


def start_window_next_step(window: StartWindowFact, *, now_ms: int) -> str:
    """The owner's next step for ``window`` as judged at ``now_ms``, which sets the year rule of its words."""
    if window.next_open_ms is None:
        return "Start is allowed in the current session."
    return f"Next Start or Resume window opens {et_when_words(window.next_open_ms, now_ms=now_ms)}."
