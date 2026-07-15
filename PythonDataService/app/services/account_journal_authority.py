"""Durable, account-scoped Clerk-journal authority requalification."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.engine.live.account_artifacts import (
    AccountFreezeEvidence,
    append_account_event,
    read_account_events,
    read_account_freeze,
    write_account_freeze,
)

logger = logging.getLogger(__name__)

JOURNAL_REQUALIFICATION_MIN_DURATION_MS = 15 * 60 * 1_000
JOURNAL_REQUALIFICATION_MIN_OBSERVATIONS = 10
JOURNAL_PARITY_PRE_CUTOVER_INTERVAL_MS = 60 * 1_000
JOURNAL_PARITY_POST_CUTOVER_INTERVAL_MS = 15 * 60 * 1_000
PARITY_EVENT_TYPE = "account_clerk_sidecar_journal_parity"
LEGACY_CUTOVER_EVENT_TYPE = "account_clerk_journal_authority_cutover"
REQUALIFICATION_REQUIRED_EVENT_TYPE = "account_clerk_journal_authority_requalification_required"
REQUALIFIED_EVENT_TYPE = "account_clerk_journal_authority_requalified"
DRIFT_EVENT_TYPE = "account_clerk_journal_authority_drift_detected"
EVENT_STREAM_DOWN_EVENT_TYPE = "account_clerk_event_stream_down"
EVENT_STREAM_RECOVERED_EVENT_TYPE = "account_clerk_event_stream_recovered"


def account_journal_authority_is_active(artifacts_root: Path, account_id: str) -> bool:
    """Return whether this account earned the current qualification contract."""

    return any(
        event.get("event_type") == REQUALIFIED_EVENT_TYPE
        for event in read_account_events(artifacts_root, account_id)
    )


def observe_account_journal_parity(
    artifacts_root: Path,
    account_id: str,
    *,
    journal: dict[str, dict[str, int]],
    legacy: dict[str, dict[str, int]],
    now_ms: int,
) -> None:
    """Append a bounded comparison and advance only this account's authority.

    Durable observation timestamps preserve the one-minute pre-cutover ceiling
    across restarts.  A legacy cutover is invalidated once and cannot regain
    authority until a new, account-local evidence window is complete.
    """

    events = read_account_events(artifacts_root, account_id)
    if _legacy_cutover_requires_requalification(events):
        append_account_event(
            artifacts_root,
            account_id,
            {
                "event_type": REQUALIFICATION_REQUIRED_EVENT_TYPE,
                "ts_ms": now_ms,
                "reason": "LEGACY_THREE_READ_CUTOVER_INVALIDATED",
            },
        )
        return

    active = account_journal_authority_is_active(artifacts_root, account_id)
    latest = _latest_parity_observation(events)
    interval_ms = (
        JOURNAL_PARITY_POST_CUTOVER_INTERVAL_MS
        if active
        else JOURNAL_PARITY_PRE_CUTOVER_INTERVAL_MS
    )
    fingerprint = _parity_fingerprint(journal=journal, legacy=legacy)
    alarm_active = _qualification_alarm_is_active(artifacts_root, account_id, events)
    clean = journal == legacy and not alarm_active
    reason = _parity_reason(journal_matches=journal == legacy, alarm_active=alarm_active)
    # The cadence constrains unchanged background sampling only.  A newly
    # observed mismatch or alarm is an account-safety state transition, not a
    # sample we may defer for up to fifteen minutes after cutover.
    state_changed = (
        latest is None
        or latest.get("fingerprint") != fingerprint
        or latest.get("status") != ("clean" if clean else "drift")
        or latest.get("reason") != reason
    )
    if (
        latest is not None
        and not state_changed
        and now_ms - _event_ts_ms(latest) < interval_ms
    ):
        return

    append_account_event(
        artifacts_root,
        account_id,
        {
            "event_type": PARITY_EVENT_TYPE,
            "ts_ms": now_ms,
            "status": "clean" if clean else "drift",
            "reason": reason,
            "trigger": _parity_trigger(latest, state_changed=state_changed),
            "fingerprint": fingerprint,
            "journal_nonzero": _has_nonzero_exposure(journal),
            "journal": journal,
            "sidecar": legacy,
        },
    )
    if active:
        if not clean:
            _record_post_cutover_drift(
                artifacts_root,
                account_id,
                now_ms=now_ms,
                reason="ACCOUNT_CLERK_JOURNAL_PARITY_DRIFT",
            )
        return
    if clean and _has_requalification_window(read_account_events(artifacts_root, account_id)):
        append_account_event(
            artifacts_root,
            account_id,
            {
                "event_type": REQUALIFIED_EVENT_TYPE,
                "ts_ms": now_ms,
                "reason": "FIFTEEN_MINUTE_ACCOUNT_SCOPED_JOURNAL_PARITY_QUALIFIED",
                "parity_observations": JOURNAL_REQUALIFICATION_MIN_OBSERVATIONS,
            },
        )


def _legacy_cutover_requires_requalification(events: list[dict]) -> bool:
    return (
        any(event.get("event_type") == LEGACY_CUTOVER_EVENT_TYPE for event in events)
        and not any(
            event.get("event_type") in {REQUALIFICATION_REQUIRED_EVENT_TYPE, REQUALIFIED_EVENT_TYPE}
            for event in events
        )
    )


def _latest_parity_observation(events: list[dict]) -> dict | None:
    return next(
        (event for event in reversed(events) if event.get("event_type") == PARITY_EVENT_TYPE),
        None,
    )


def _event_ts_ms(event: dict) -> int:
    value = event.get("ts_ms")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _event_seq(event: dict) -> int:
    value = event.get("seq")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _parity_fingerprint(*, journal: dict[str, dict[str, int]], legacy: dict[str, dict[str, int]]) -> str:
    return json.dumps({"journal": journal, "sidecar": legacy}, sort_keys=True, separators=(",", ":"))


def _parity_reason(*, journal_matches: bool, alarm_active: bool) -> str:
    if alarm_active:
        return "ACCOUNT_CLERK_UNRESOLVED_ALARM"
    return "SIDECAR_JOURNAL_EXPOSURE_MATCH" if journal_matches else "SIDECAR_JOURNAL_EXPOSURE_MISMATCH"


def _parity_trigger(latest: dict | None, *, state_changed: bool) -> str:
    if latest is None:
        return "initial"
    return "state_change" if state_changed else "bounded_background_cadence"


def _has_nonzero_exposure(explained: dict[str, dict[str, int]]) -> bool:
    return any(quantity != 0 for positions in explained.values() for quantity in positions.values())


def _qualification_alarm_is_active(artifacts_root: Path, account_id: str, events: list[dict]) -> bool:
    if read_account_freeze(artifacts_root, account_id) is not None:
        return True
    # A new Clerk must durably report that its callback stream started before
    # parity observations can earn authority after a prior stream death.
    return _latest_event_seq(events, EVENT_STREAM_DOWN_EVENT_TYPE) > _latest_event_seq(
        events,
        EVENT_STREAM_RECOVERED_EVENT_TYPE,
    )


def _latest_event_seq(events: list[dict], event_type: str) -> int:
    return max(
        (_event_seq(event) for event in events if event.get("event_type") == event_type),
        default=0,
    )


def _has_requalification_window(events: list[dict]) -> bool:
    """Enforce duration, observations, nonzero→zero, and alarm-free interval."""

    reset_seq = max(
        (
            _event_seq(event)
            for event in events
            if event.get("event_type")
            in {
                REQUALIFICATION_REQUIRED_EVENT_TYPE,
                REQUALIFIED_EVENT_TYPE,
                "account_freeze_recorded",
                EVENT_STREAM_DOWN_EVENT_TYPE,
            }
        ),
        default=0,
    )
    observations = [
        event
        for event in events
        if event.get("event_type") == PARITY_EVENT_TYPE and _event_seq(event) > reset_seq
    ]
    latest_nonclean = max(
        (index for index, event in enumerate(observations) if event.get("status") != "clean"),
        default=-1,
    )
    clean_observations = observations[latest_nonclean + 1 :]
    if len(clean_observations) < JOURNAL_REQUALIFICATION_MIN_OBSERVATIONS:
        return False
    first_ts = _event_ts_ms(clean_observations[0])
    last_ts = _event_ts_ms(clean_observations[-1])
    if last_ts - first_ts < JOURNAL_REQUALIFICATION_MIN_DURATION_MS:
        return False
    saw_nonzero = False
    for observation in clean_observations:
        if observation.get("journal_nonzero") is True:
            saw_nonzero = True
        elif saw_nonzero:
            return True
    return False


def _record_post_cutover_drift(
    artifacts_root: Path,
    account_id: str,
    *,
    now_ms: int,
    reason: str,
) -> None:
    """Turn post-cutover drift into structured operator-visible evidence."""

    logger.error(
        "account Clerk journal parity drift detected after authority cutover",
        extra={"account_id": account_id, "reason": reason, "observed_at_ms": now_ms},
    )
    append_account_event(
        artifacts_root,
        account_id,
        {
            "event_type": DRIFT_EVENT_TYPE,
            "ts_ms": now_ms,
            "reason": reason,
            "source": "account_journal_authority",
        },
    )
    if read_account_freeze(artifacts_root, account_id) is None:
        write_account_freeze(
            artifacts_root,
            AccountFreezeEvidence(
                account_id=account_id,
                freeze_kind="exposure",
                reason=reason,
                source="account_journal_authority",
                recorded_at_ms=now_ms,
                operator_next_step="RECONCILE_ACCOUNT_JOURNAL_PARITY",
            ),
        )


__all__ = [
    "account_journal_authority_is_active",
    "observe_account_journal_parity",
]
