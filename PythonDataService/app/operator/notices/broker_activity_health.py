"""Broker-activity publisher health state machine (PR 5 / ADR 0014 §4).

Backend-only composition function that derives a typed health verdict from
raw publisher facts.  The frontend renders the ``state`` token verbatim and
shows the server-authored ``headline`` notice; it never re-derives state.

State machine (in priority order):
1. publisher is None (not registered) → ``unavailable``
2. registered but ``is_running == False`` and age < ``starting_timeout_ms`` → ``starting``
3. registered but ``is_running == False`` and age >= ``starting_timeout_ms`` → ``unavailable``
4. registered + ``is_running`` → ``ready``

Broker-activity rows are event-driven fills/cancels/pending-intent evidence,
not a heartbeat.  A quiet account can go many minutes without a new row while
capture is healthy, so row recency is exposed as facts but no longer demotes
the publisher's health by itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.operator.notices.schema import (
    OperatorNotice,
    OperatorNoticeAction,
)
from app.schemas.live_runs import (
    BrokerActivityHealth,
    BrokerActivityHealthFacts,
)

if TYPE_CHECKING:
    from app.services.broker_activity_publisher import BrokerActivityPublisher


_RUNBOOK = "broker-activity-health"


def _notice(
    *,
    code: str,
    tier: str,
    title: str,
    message: str,
) -> OperatorNotice:
    return OperatorNotice(
        code=code,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        title=title,
        message=message,
        action=OperatorNoticeAction(kind="wait", label=None, target=None),
        runbook_slug=_RUNBOOK,
    )


def compose_broker_activity_health(
    *,
    publisher: BrokerActivityPublisher | None,
    registered_at_ms: int | None,
    last_row_ms: int | None,
    now_ms: int,
    starting_timeout_ms: int = 30_000,
    degraded_after_idle_ms: int = 60_000,
) -> BrokerActivityHealth:
    """Derive the typed broker-activity health verdict from raw publisher facts.

    Parameters
    ----------
    publisher:
        The registered publisher for this instance, or ``None`` when none
        has been registered.
    registered_at_ms:
        Wall-clock ms when the publisher was first registered for this
        instance.  ``None`` when ``publisher`` is ``None``.
    last_row_ms:
        Wall-clock ms of the most recent row authored by the publisher, or
        ``None`` when no rows have been authored yet.
    now_ms:
        Current wall-clock time (``int64 ms UTC``).
    starting_timeout_ms:
        How long we wait for a registered publisher to start running before
        declaring it ``unavailable``.  Default: 30 s.
    degraded_after_idle_ms:
        Deprecated compatibility knob. Broker-activity rows are not a
        heartbeat, so row idleness is no longer used to demote health.
    """
    publisher_registered = publisher is not None
    publisher_running = publisher is not None and publisher.is_running

    seconds_since_registered: int | None = None
    if registered_at_ms is not None:
        seconds_since_registered = max(0, (now_ms - registered_at_ms) // 1000)

    seconds_since_last_row: int | None = None
    if last_row_ms is not None:
        seconds_since_last_row = max(0, (now_ms - last_row_ms) // 1000)

    latest_row_seq: int | None = None
    if publisher is not None:
        latest_row_seq = publisher.last_persisted_seq() or None

    facts = BrokerActivityHealthFacts(
        publisher_registered=publisher_registered,
        publisher_running=publisher_running,
        latest_row_seq=latest_row_seq,
        seconds_since_registered=seconds_since_registered,
        seconds_since_last_row=seconds_since_last_row,
    )

    # ── state machine ──────────────────────────────────────────────────────

    # 1. Not registered at all.
    if publisher is None or not publisher_registered:
        notice = _notice(
            code="activity.publisher_not_running",
            tier="critical",
            title="Activity capture is unavailable",
            message=(
                "No broker-activity publisher is registered for this instance. "
                "The bot process may still be running, but the cockpit cannot "
                "confirm durable activity capture from the data plane."
            ),
        )
        return BrokerActivityHealth(
            state="unavailable",
            headline=notice,
            notices=[notice],
            facts=facts,
        )

    age_ms = (now_ms - registered_at_ms) if registered_at_ms is not None else 0

    # 2 & 3. Registered but not yet running.
    if not publisher_running:
        if age_ms < starting_timeout_ms:
            notice = _notice(
                code="activity.publisher_starting",
                tier="info",
                title="Activity feed is starting",
                message=(
                    "The broker-activity publisher has been registered and is starting up. "
                    "Activity events will appear shortly."
                ),
            )
            return BrokerActivityHealth(
                state="starting",
                headline=notice,
                notices=[notice],
                facts=facts,
            )
        else:
            notice = _notice(
                code="activity.publisher_not_running",
                tier="critical",
                title="Activity capture is detached",
                message=(
                    "The broker-activity publisher was registered but failed to start "
                    "within the expected window. The host process state is separate; "
                    "check the data-plane publisher before trusting an empty feed."
                ),
            )
            return BrokerActivityHealth(
                state="unavailable",
                headline=notice,
                notices=[notice],
                facts=facts,
            )

    # Publisher is registered + running from here. Broker activity is
    # event-driven, so no recent row is not evidence of a stalled feed.
    return BrokerActivityHealth(
        state="ready",
        headline=None,
        notices=[],
        facts=facts,
    )
