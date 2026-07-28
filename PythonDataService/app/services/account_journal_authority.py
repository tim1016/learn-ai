"""Durable, account-scoped Clerk-journal authority requalification."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.account_artifacts import (
    AccountArtifactError,
    AccountFreezeEvidence,
    account_artifact_file_path,
    append_account_event,
    read_account_freeze,
    read_legacy_account_events,
    write_account_freeze,
)
from app.engine.live.live_state_sidecar import _file_lock
from app.schemas.artifact_io import atomic_write_pydantic_artifact

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
ACCOUNT_JOURNAL_AUTHORITY_FILENAME = "account_journal_authority.json"


class _JournalParityObservation(BaseModel):
    """One typed comparison used solely by the authority requalification gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at_ms: int = Field(ge=0)
    status: str = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=128)
    fingerprint: str = Field(min_length=1, max_length=4096)
    journal_nonzero: bool


class _AccountJournalAuthorityState(BaseModel):
    """Dedicated Clerk-qualification state, not an operator-history fold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    qualified_at_ms: int | None = Field(default=None, ge=0)
    requalification_required_at_ms: int | None = Field(default=None, ge=0)
    event_stream_state: str = Field(default="unknown", pattern="^(unknown|down|recovered)$")
    event_stream_changed_at_ms: int | None = Field(default=None, ge=0)
    observations: tuple[_JournalParityObservation, ...] = ()


def account_journal_authority_is_active(artifacts_root: Path, account_id: str) -> bool:
    """Return whether the typed Clerk-qualification artifact is active."""

    return _read_or_migrate_authority_state(artifacts_root, account_id).qualified_at_ms is not None


def record_account_journal_stream_state(
    artifacts_root: Path,
    account_id: str,
    *,
    state: str,
    changed_at_ms: int,
) -> None:
    """Record a Clerk stream transition in the authority artifact.

    A callback stream death immediately revokes previously earned journal
    qualification; it must not wait for an operator-history projection or a
    later parity sample to notice it.
    """

    if state not in {"down", "recovered"}:
        raise ValueError("account journal stream state must be down or recovered")
    path = _authority_path(artifacts_root, account_id)
    with _file_lock(path):
        current = _read_or_migrate_authority_state_locked(artifacts_root, account_id, path)
        _write_authority_state_locked(
            path,
            current.model_copy(
                update={
                    "qualified_at_ms": None if state == "down" else current.qualified_at_ms,
                    "requalification_required_at_ms": (
                        changed_at_ms if state == "down" else current.requalification_required_at_ms
                    ),
                    "event_stream_state": state,
                    "event_stream_changed_at_ms": changed_at_ms,
                }
            ),
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

    path = _authority_path(artifacts_root, account_id)
    with _file_lock(path):
        _observe_account_journal_parity_locked(
            artifacts_root,
            account_id,
            journal=journal,
            legacy=legacy,
            now_ms=now_ms,
            authority=_read_or_migrate_authority_state_locked(artifacts_root, account_id, path),
            path=path,
        )


def _observe_account_journal_parity_locked(
    artifacts_root: Path,
    account_id: str,
    *,
    journal: dict[str, dict[str, int]],
    legacy: dict[str, dict[str, int]],
    now_ms: int,
    authority: _AccountJournalAuthorityState,
    path: Path,
) -> None:
    """Advance a parity state while its account-local authority lock is held."""

    if authority.requalification_required_at_ms == 0 and not authority.observations:
        _write_authority_state_locked(
            path,
            authority.model_copy(update={"requalification_required_at_ms": now_ms}),
        )
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

    active = authority.qualified_at_ms is not None
    latest = authority.observations[-1] if authority.observations else None
    interval_ms = (
        JOURNAL_PARITY_POST_CUTOVER_INTERVAL_MS
        if active
        else JOURNAL_PARITY_PRE_CUTOVER_INTERVAL_MS
    )
    fingerprint = _parity_fingerprint(journal=journal, legacy=legacy)
    alarm_active = _qualification_alarm_is_active(artifacts_root, account_id, authority)
    clean = journal == legacy and not alarm_active
    reason = _parity_reason(journal_matches=journal == legacy, alarm_active=alarm_active)
    # The cadence constrains unchanged background sampling only.  A newly
    # observed mismatch or alarm is an account-safety state transition, not a
    # sample we may defer for up to fifteen minutes after cutover.
    state_changed = (
        latest is None
        or latest.fingerprint != fingerprint
        or latest.status != ("clean" if clean else "drift")
        or latest.reason != reason
    )
    if (
        latest is not None
        and not state_changed
        and now_ms - latest.observed_at_ms < interval_ms
    ):
        return

    observation = _JournalParityObservation(
        observed_at_ms=now_ms,
        status="clean" if clean else "drift",
        reason=reason,
        fingerprint=fingerprint,
        journal_nonzero=_has_nonzero_exposure(journal),
    )
    next_authority = authority.model_copy(
        update={
            "observations": (*authority.observations[-127:], observation),
            "qualified_at_ms": authority.qualified_at_ms if clean else None,
            "requalification_required_at_ms": (
                authority.requalification_required_at_ms if clean else now_ms
            ),
        }
    )
    if active:
        if not clean:
            _write_authority_state_locked(path, next_authority)
            _append_parity_event(
                artifacts_root,
                account_id,
                now_ms=now_ms,
                latest=latest,
                state_changed=state_changed,
                observation=observation,
                journal=journal,
                legacy=legacy,
            )
            _record_post_cutover_drift(
                artifacts_root,
                account_id,
                now_ms=now_ms,
                reason="ACCOUNT_CLERK_JOURNAL_PARITY_DRIFT",
            )
        else:
            _write_authority_state_locked(path, next_authority)
            _append_parity_event(
                artifacts_root,
                account_id,
                now_ms=now_ms,
                latest=latest,
                state_changed=state_changed,
                observation=observation,
                journal=journal,
                legacy=legacy,
            )
        return
    if clean and _has_requalification_window(next_authority):
        next_authority = next_authority.model_copy(
            update={"qualified_at_ms": now_ms, "requalification_required_at_ms": None}
        )
        _write_authority_state_locked(path, next_authority)
        _append_parity_event(
            artifacts_root,
            account_id,
            now_ms=now_ms,
            latest=latest,
            state_changed=state_changed,
            observation=observation,
            journal=journal,
            legacy=legacy,
        )
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
        return
    _write_authority_state_locked(path, next_authority)
    _append_parity_event(
        artifacts_root,
        account_id,
        now_ms=now_ms,
        latest=latest,
        state_changed=state_changed,
        observation=observation,
        journal=journal,
        legacy=legacy,
    )


def _append_parity_event(
    artifacts_root: Path,
    account_id: str,
    *,
    now_ms: int,
    latest: _JournalParityObservation | None,
    state_changed: bool,
    observation: _JournalParityObservation,
    journal: dict[str, dict[str, int]],
    legacy: dict[str, dict[str, int]],
) -> None:
    """Mirror an already-persisted parity state for the operator timeline."""

    append_account_event(
        artifacts_root,
        account_id,
        {
            "event_type": PARITY_EVENT_TYPE,
            "ts_ms": now_ms,
            "status": observation.status,
            "reason": observation.reason,
            "trigger": _parity_trigger(latest, state_changed=state_changed),
            "fingerprint": observation.fingerprint,
            "journal_nonzero": observation.journal_nonzero,
            "journal": journal,
            "sidecar": legacy,
        },
    )


def _authority_path(artifacts_root: Path, account_id: str) -> Path:
    return account_artifact_file_path(
        artifacts_root,
        account_id,
        ACCOUNT_JOURNAL_AUTHORITY_FILENAME,
    )


def _read_authority_state(artifacts_root: Path, account_id: str) -> _AccountJournalAuthorityState:
    path = _authority_path(artifacts_root, account_id)
    try:
        state = _AccountJournalAuthorityState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AccountArtifactError("account journal authority artifact is unreadable") from exc
    if state.account_id != account_id:
        raise AccountArtifactError("account journal authority artifact belongs to another account")
    return state


def _seed_authority_state_from_legacy(
    artifacts_root: Path,
    account_id: str,
) -> _AccountJournalAuthorityState:
    """Build the one-time compatibility snapshot before a typed read exists."""

    # The old shared journal is immutable historical evidence only.  It can
    # seed a one-time compatibility state but is never consulted again for a
    # forward authority decision.
    legacy = read_legacy_account_events(artifacts_root, account_id)
    qualified_at_ms = max(
        (
            _legacy_event_ts_ms(event)
            for event in legacy
            if event.get("event_type") == REQUALIFIED_EVENT_TYPE
        ),
        default=None,
    )
    has_cutover = any(event.get("event_type") == LEGACY_CUTOVER_EVENT_TYPE for event in legacy)
    if has_cutover and qualified_at_ms is None:
        return _AccountJournalAuthorityState(
            account_id=account_id,
            requalification_required_at_ms=0,
        )
    return _AccountJournalAuthorityState(account_id=account_id, qualified_at_ms=qualified_at_ms)


def _read_or_migrate_authority_state(
    artifacts_root: Path,
    account_id: str,
) -> _AccountJournalAuthorityState:
    """Snapshot legacy qualification once before it becomes a safety input."""

    path = _authority_path(artifacts_root, account_id)
    with _file_lock(path):
        return _read_or_migrate_authority_state_locked(artifacts_root, account_id, path)


def _read_or_migrate_authority_state_locked(
    artifacts_root: Path,
    account_id: str,
    path: Path,
) -> _AccountJournalAuthorityState:
    if path.exists():
        return _read_authority_state(artifacts_root, account_id)
    state = _seed_authority_state_from_legacy(artifacts_root, account_id)
    _write_authority_state_locked(path, state)
    return state


def _write_authority_state(artifacts_root: Path, state: _AccountJournalAuthorityState) -> None:
    path = _authority_path(artifacts_root, state.account_id)
    with _file_lock(path):
        _write_authority_state_locked(path, state)


def _write_authority_state_locked(path: Path, state: _AccountJournalAuthorityState) -> None:
    """Persist state while the matching artifact lock is already held."""

    atomic_write_pydantic_artifact(path, state)


def _legacy_event_ts_ms(event: dict) -> int:
    value = event.get("ts_ms")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _parity_fingerprint(*, journal: dict[str, dict[str, int]], legacy: dict[str, dict[str, int]]) -> str:
    return json.dumps({"journal": journal, "sidecar": legacy}, sort_keys=True, separators=(",", ":"))


def _parity_reason(*, journal_matches: bool, alarm_active: bool) -> str:
    if alarm_active:
        return "ACCOUNT_CLERK_UNRESOLVED_ALARM"
    return "SIDECAR_JOURNAL_EXPOSURE_MATCH" if journal_matches else "SIDECAR_JOURNAL_EXPOSURE_MISMATCH"


def _parity_trigger(latest: _JournalParityObservation | None, *, state_changed: bool) -> str:
    if latest is None:
        return "initial"
    return "state_change" if state_changed else "bounded_background_cadence"


def _has_nonzero_exposure(explained: dict[str, dict[str, int]]) -> bool:
    return any(quantity != 0 for positions in explained.values() for quantity in positions.values())


def _qualification_alarm_is_active(
    artifacts_root: Path,
    account_id: str,
    authority: _AccountJournalAuthorityState,
) -> bool:
    if read_account_freeze(artifacts_root, account_id) is not None:
        return True
    # A new Clerk must durably report that its callback stream started before
    # parity observations can earn authority after a prior stream death.
    return authority.event_stream_state == "down"


def _has_requalification_window(authority: _AccountJournalAuthorityState) -> bool:
    """Enforce duration, observations, nonzero→zero, and alarm-free interval."""

    observations = [
        event
        for event in authority.observations
        if authority.requalification_required_at_ms is None
        or event.observed_at_ms > authority.requalification_required_at_ms
    ]
    latest_nonclean = max(
        (index for index, event in enumerate(observations) if event.status != "clean"),
        default=-1,
    )
    clean_observations = observations[latest_nonclean + 1 :]
    if len(clean_observations) < JOURNAL_REQUALIFICATION_MIN_OBSERVATIONS:
        return False
    first_ts = clean_observations[0].observed_at_ms
    last_ts = clean_observations[-1].observed_at_ms
    if last_ts - first_ts < JOURNAL_REQUALIFICATION_MIN_DURATION_MS:
        return False
    saw_nonzero = False
    for observation in clean_observations:
        if observation.journal_nonzero:
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
