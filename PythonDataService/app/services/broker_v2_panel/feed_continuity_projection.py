"""IBKR source-feed continuity projection for the bot control panel (spec §7).

Split out of ``panel_projection_service`` because it is the only part of that
module that reasons about the IBKR source stream (``app.services.source_bar_ledger``)
rather than the Clerk/action panel state the rest of the module projects.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.marketdata.feed import (
    RESUME_HOLE_AFTER_HOURS,
    RESUME_HOLE_UNFILLED,
    WARMUP_HISTORY_UNAVAILABLE,
)
from app.schemas.broker_v2_panel import (
    FeedContinuityEventView,
    FeedContinuityView,
    StartupJoinView,
    WarmupJoinView,
)
from app.services.source_bar_ledger import (
    RetainedContinuityEvent,
    RetainedStartupJoin,
    RetainedWarmupJoin,
)

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


WARMUP_REFUSAL_COPY: dict[str, tuple[str, str]] = {
    RESUME_HOLE_AFTER_HOURS: (
        "Refused: after-hours hole",
        "This bot decides on extended-hours minutes, and some passed while it was stopped. "
        "IBKR history does not reproduce extended-hours minutes exactly, so the gap cannot "
        "be filled and the run was refused rather than warmed across it. Deploy a new bot "
        "instead of resuming this one.",
    ),
    RESUME_HOLE_UNFILLED: (
        "Refused: gap could not be filled",
        "IBKR history did not return every regular-hours minute that passed while the bot "
        "was stopped, so the run was refused rather than warmed across the gap. Check that "
        "IB Gateway's historical-data farm is connected, then resume again.",
    ),
    WARMUP_HISTORY_UNAVAILABLE: (
        "Refused: warmup history unavailable",
        "IB Gateway did not return the warmup history the run needs -- its sealed lookback, or "
        "the minute its live stream joined partway through -- before the startup deadline, so "
        "it was refused rather than started cold or across a gap. Before starting again, check "
        "that the Gateway is logged in, its historical-data farm is connected (not paced or "
        "down), and the symbol qualifies as a contract.",
    ),
}
"""Operator copy for each warmup refusal. The duty-outcome card reads the same map."""


_STARTUP_JOIN_COPY: dict[str, tuple[str, str]] = {
    "waiting_for_stream": (
        "Preparing: waiting for the live stream to join",
        "The bot has subscribed to IBKR and is waiting for its first print. Warmup starts once "
        "the minute the stream joins has closed; nothing is decided before then, and no "
        "deadline runs yet.",
    ),
    "filling": (
        "Preparing: filling the minutes before the live stream from IBKR history",
        "The live stream has joined. Warmup history up to the minute it takes over is being "
        "fetched; the run is refused if that is not done before the deadline.",
    ),
    "history_joined": (
        "Preparing: history joined, rebuilding state",
        "Warmup history now reaches the live stream. The bot is replaying it and will take "
        "live bars next.",
    ),
    "refused": (
        "Refused while preparing",
        "The run never traded. Why it was refused, and what that left at the broker, is under "
        "its duty outcome.",
    ),
    "ready": (
        "Ready: warmup met the live stream",
        "Warmup and live bars are contiguous. The bot trades the next on-time decision; any "
        "decision that fell due while it prepared was refused as late, never caught up.",
    ),
}
"""Operator copy for each startup-join state. A refusal's reason is stated once, on
the duty outcome (``WARMUP_REFUSAL_COPY``); this view adds only what it could not fill."""


def build_startup_join(
    join: RetainedStartupJoin | None, *, running: bool
) -> StartupJoinView | None:
    """Project where the current run is in joining warmup to its stream, or ``None``.

    A run that is no longer running and never refused shows nothing: its
    preparation is history, and the duty outcome says how it ended.
    """
    if join is None:
        return None
    if join.refused_at_ms is not None:
        state = "refused"
        label, explanation = _STARTUP_JOIN_COPY[state]
    elif not running:
        return None
    else:
        if join.ready_at_ms is not None:
            state = "ready"
        elif join.history_joined_at_ms is not None:
            state = "history_joined"
        elif join.live_from_ms is not None:
            state = "filling"
        else:
            state = "waiting_for_stream"
        label, explanation = _STARTUP_JOIN_COPY[state]
    return StartupJoinView(
        run_id=join.run_id,
        state=state,
        label=label,
        explanation=explanation,
        opened_at_ms=join.opened_at_ms,
        live_from_ms=join.live_from_ms,
        joined_minute_start_ms=join.joined_minute_start_ms,
        deadline_ms=join.deadline_ms,
        missing_start_ms=join.missing_start_ms,
        missing_end_ms=join.missing_end_ms,
        reason_code=join.reason_code,
    )


def build_warmup_join(join: RetainedWarmupJoin | None) -> WarmupJoinView | None:
    """Project one run's recorded warmup join, or ``None`` when it recorded none."""
    if join is None:
        return None
    history_only = join.warm_from_ms is not None
    if join.outcome == "refused":
        assert join.reason_code is not None  # the store's CHECK pairs them
        label, explanation = WARMUP_REFUSAL_COPY.get(
            join.reason_code,
            ("Refused during warmup", "The run was refused before it decided anything."),
        )
    elif join.outcome == "contiguous":
        label, explanation = (
            "Warmed on its retained bars",
            "The bars kept from earlier runs already reached this resume; nothing was missing.",
        )
    elif history_only:
        label, explanation = (
            "Warmed from IBKR history",
            f"The bot was stopped for longer than its warmup lookback, so it warmed on that "
            f"lookback's IBKR 1-minute history ({join.filled_count} bars), as a fresh deploy does.",
        )
    else:
        label, explanation = (
            f"Filled {join.filled_count} missing bars from IBKR history",
            "The minutes that passed while the bot was stopped were fetched from IBKR 1-minute "
            "history, which matches the live-built regular-hours minutes exactly, and replayed "
            "before the bot decided anything.",
        )
    return WarmupJoinView(
        run_id=join.run_id,
        state=join.outcome,
        label=label,
        explanation=explanation,
        retained_end_ms=join.retained_end_ms,
        joined_at_ms=join.joined_at_ms,
        filled_count=join.filled_count,
        filled_start_ms=join.filled_start_ms,
        filled_end_ms=join.filled_end_ms,
        warmed_from_history_only=history_only,
        reason_code=join.reason_code,
    )
