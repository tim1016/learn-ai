"""ADR 0063 Decision 5's two-legged drain deadline floor.

The effective deadline instant is

::

    max( draining_since_ms + drain_deadline_ms ,
         session_close_ms_utc( first session whose open is at or after
                               draining_since_ms ) )

with ``drain_deadline_ms`` itself floored at 24 hours, and every value
``int64 ms UTC``. The duration leg is deployment-owned; the calendar leg is
the trading calendar's, and the calendar leg is what makes the claim true:
a regular-hours DAY order still open at drain time is force-expired by the
exchange no later than that session's close, so the honest floor is *a
session close the lane can actually reach*. A flat 24 wall-clock hours does
not guarantee one — drain at 15:55 ET and it buys five minutes of the
session it was entered in; drain Wednesday at 15:55 ahead of a Thursday
holiday and the entire remaining 24h is non-trading time. Reasoning a
session boundary out of a wall-clock constant is what
``.claude/rules/temporal-rigor.md`` bans outright, which is why the second
leg derives from the canonical calendar module (``session_windows_ms_utc``)
rather than a duration.

The resolved ``max(...)`` is stored as an absolute instant on the clerk row
at drain time (``clerks.drain_deadline_at_ms``) and never recomputed: both
legs can move after the drain — a deployment may re-tune the duration, a
calendar version bump may change a future session — and §7.3 forbids the
bound moving once a drain is entered.

The canonical calendar is imported *inside* the function, deliberately: the
generic fleet spine stays import-light (no pandas edge at import time —
``tests/broker/fleet/test_import_isolation.py`` fences provider imports, and
pulling the calendar in at module scope would make every coordinator import
pay for a ceremony that runs on the host), and no Alpaca or IBKR module is
ever imported on a fleet path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: The deployment-owned duration's floor: not less than one full day. A
#: deployment may set its duration higher; it may not set it lower, because
#: the deadline is the bound on how long a stalled drain can hold a lane
#: hostage before the operator's separately named exit opens.
DRAIN_DEADLINE_FLOOR_MS = 86_400_000

#: The scan window for the calendar leg. Generous over the canonical
#: calendar's own 14-day forward horizon, padded two days on the front so
#: the ET calendar day of a late-UTC drain instant is always inside the
#: scan regardless of the zone offset.
_CALENDAR_SCAN_PADDING_DAYS = 2
_CALENDAR_SCAN_WINDOW_DAYS = 16


def drain_deadline_at_ms(*, draining_since_ms: int, drain_deadline_ms: int) -> int:
    """Resolve the two-legged deadline floor for one drain, as an absolute instant.

    Raises ``ValueError`` when the duration is below the floor or the
    calendar offers no reachable session in the scan window — a drain that
    cannot name its own bound refuses rather than guessing one.
    """
    if drain_deadline_ms < DRAIN_DEADLINE_FLOOR_MS:
        raise ValueError(
            f"drain_deadline_ms={drain_deadline_ms} is below the "
            f"{DRAIN_DEADLINE_FLOOR_MS} ms floor; a deployment may raise the "
            "duration, never lower it"
        )
    if draining_since_ms < 0:
        raise ValueError("draining_since_ms must be a non-negative int64 ms UTC instant")

    # In-function datetime arithmetic only; the returned value is int64 ms UTC.
    drain_utc_date = datetime.fromtimestamp(draining_since_ms / 1000, tz=UTC).date()
    scan_start = drain_utc_date - timedelta(days=_CALENDAR_SCAN_PADDING_DAYS)
    scan_end = scan_start + timedelta(days=_CALENDAR_SCAN_WINDOW_DAYS)

    from app.lean_sidecar.trading_calendar import session_windows_ms_utc

    windows = session_windows_ms_utc(scan_start, scan_end)
    calendar_leg = next(
        (window.close_ms_utc for window in windows if window.open_ms_utc >= draining_since_ms),
        None,
    )
    if calendar_leg is None:
        raise ValueError(
            "the canonical calendar offers no session whose open is at or after "
            f"draining_since_ms={draining_since_ms} within the "
            f"{_CALENDAR_SCAN_WINDOW_DAYS}-day scan window; a drain cannot name "
            "its deadline bound"
        )
    duration_leg = draining_since_ms + drain_deadline_ms
    return max(duration_leg, calendar_leg)


__all__ = [
    "DRAIN_DEADLINE_FLOOR_MS",
    "drain_deadline_at_ms",
]
