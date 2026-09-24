"""IBKR source-feed continuity projection for the bot control panel (spec §7).

Split out of ``panel_projection_service`` because it is the only part of that
module that reasons about the IBKR source stream (``app.services.source_bar_ledger``)
rather than the Clerk/action panel state the rest of the module projects.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.schemas.broker_v2_panel import FeedContinuityEventView, FeedContinuityView
from app.services.source_bar_ledger import RetainedContinuityEvent

_CONTINUITY_EVENT_COPY: dict[str, tuple[str, str]] = {
    "interruption": (
        "Feed interrupted",
        "IBKR delivery stopped and same-run recovery began.",
    ),
    "recovered": (
        "Feed recovered",
        "IBKR delivery resumed under the run's continuity rules.",
    ),
    "gap": (
        "Non-decision gap recorded",
        "An unprovable data window outside the strategy's decision session was omitted.",
    ),
    "substituted": (
        "Gap substituted",
        "Authorized historical evidence replaced a missing live-data window.",
    ),
    "refused": (
        "Continuity refused",
        "A strategy decision window could not be proven, so further decisions were refused.",
    ),
}

_GAP_CAUSE_COPY: dict[str, tuple[str, str]] = {
    # The join minute can fall inside the decision session, so the generic
    # "outside the strategy's decision session" gap copy would be false (#2364).
    "stream_joined": (
        "Partial first minute omitted",
        "The stream joined partway through this minute; it was omitted, not decided on.",
    ),
}

_CONTINUITY_CAUSE_COPY: dict[str, str] = {
    "socket_down": "The IBKR socket disconnected.",
    "soft_loss_1100": "IBKR reported connectivity loss while the socket remained open.",
    "data_lost_1101": "IBKR restored connectivity but dropped the market-data subscription, so it was requested again.",
    "stall": "The IBKR real-time bar subscription stopped advancing.",
    "generation_changed": "The IBKR connection was replaced while this stream was active.",
}


def _event_copy(event: RetainedContinuityEvent) -> tuple[str, str]:
    """The backend-authored label and explanation for one continuity fact."""
    if event.kind == "gap" and event.cause in _GAP_CAUSE_COPY:
        return _GAP_CAUSE_COPY[event.cause]
    label, explanation = _CONTINUITY_EVENT_COPY[event.kind]
    cause_copy = _CONTINUITY_CAUSE_COPY.get(event.cause or "")
    if event.kind == "interruption" and cause_copy is not None:
        return label, f"{cause_copy} Same-run recovery began."
    return label, explanation


def _duration_label(duration_ms: int) -> str:
    seconds = max(0, duration_ms) // 1_000
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return (
            f"{minutes} minute{'s' if minutes != 1 else ''}"
            if remaining_seconds == 0
            else f"{minutes}m {remaining_seconds}s"
        )
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m" if remaining_minutes else f"{hours}h"


def build_feed_continuity(
    events: Sequence[RetainedContinuityEvent] | None,
    *,
    run_id: str | None,
    latest_bar_at_ms: int | None,
    now_ms: int,
) -> FeedContinuityView:
    """Project durable source-stream facts into one current-run operator view."""
    if events is None:
        return FeedContinuityView(
            provider_label="IBKR market data",
            run_id=run_id,
            state="not_recorded",
            state_label="Continuity not recorded",
            explanation="Run-scoped IBKR continuity evidence is not available for this bot.",
            interruption_count=0,
            recovery_count=0,
            unresolved_count=0,
            decision_impact_count=0,
            last_interruption_at_ms=None,
            last_recovery_at_ms=None,
            latest_bar_at_ms=latest_bar_at_ms,
            events=[],
        )

    interruption_count = sum(event.kind == "interruption" for event in events)
    recovery_count = sum(event.kind == "recovered" for event in events)
    decision_impact_count = sum(event.kind == "refused" for event in events)
    unresolved_count = max(0, interruption_count - recovery_count)
    last_interruption_at_ms = next(
        (event.observed_at_ms for event in reversed(events) if event.kind == "interruption"),
        None,
    )
    last_recovery_at_ms = next(
        (event.observed_at_ms for event in reversed(events) if event.kind == "recovered"),
        None,
    )

    if decision_impact_count:
        # A refusal is terminal for its decision window even when the
        # interruption that caused it was never paired with a `recovered`
        # event (the deadline-miss path in ibkr_continuity.py raises without
        # recording one) — check this before `unresolved_count` so that case
        # surfaces as "compromised", not as a merely still-recovering one.
        state = "compromised"
        state_label = "Continuity refused"
        explanation = "One or more strategy decision windows could not be proven from live data."
    elif unresolved_count:
        state = "interrupted"
        state_label = "Interrupted"
        explanation = "IBKR delivery is interrupted and same-run recovery is in progress."
    elif interruption_count:
        state = "recovered"
        state_label = "Recovered"
        explanation = "Every recorded IBKR interruption recovered within this run's continuity rules."
    else:
        state = "continuous"
        state_label = "Continuous"
        explanation = "No IBKR delivery interruptions have been recorded in this run."

    pending_interruptions: list[int] = []
    event_views: list[FeedContinuityEventView] = []
    for event in events:
        duration_ms: int | None = None
        if event.kind == "interruption":
            pending_interruptions.append(event.observed_at_ms)
        elif event.kind == "recovered" and pending_interruptions:
            duration_ms = max(0, event.observed_at_ms - pending_interruptions.pop())
        event_label, event_explanation = _event_copy(event)
        event_views.append(
            FeedContinuityEventView(
                evidence_seq=event.evidence_seq,
                kind=event.kind,
                occurred_at_ms=event.observed_at_ms,
                label=event_label,
                explanation=event_explanation,
                cause=event.cause,
                duration_ms=duration_ms,
                duration_label=(_duration_label(duration_ms) if duration_ms is not None else None),
                window_start_ms=event.window_start_ms,
                window_end_ms=event.window_end_ms,
            )
        )

    if unresolved_count and pending_interruptions:
        newest_opened_at_ms = pending_interruptions[-1]
        for index in range(len(event_views) - 1, -1, -1):
            view = event_views[index]
            if view.kind != "interruption" or view.occurred_at_ms != newest_opened_at_ms:
                continue
            active_duration_ms = max(0, now_ms - newest_opened_at_ms)
            event_views[index] = view.model_copy(
                update={
                    "duration_ms": active_duration_ms,
                    "duration_label": _duration_label(active_duration_ms),
                }
            )
            break

    return FeedContinuityView(
        provider_label="IBKR market data",
        run_id=run_id,
        state=state,
        state_label=state_label,
        explanation=explanation,
        interruption_count=interruption_count,
        recovery_count=recovery_count,
        unresolved_count=unresolved_count,
        decision_impact_count=decision_impact_count,
        last_interruption_at_ms=last_interruption_at_ms,
        last_recovery_at_ms=last_recovery_at_ms,
        latest_bar_at_ms=latest_bar_at_ms,
        events=event_views[-12:],
    )
