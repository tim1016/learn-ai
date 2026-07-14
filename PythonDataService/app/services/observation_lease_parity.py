"""Replay durable Account Observation Lease shadow comparisons for cutover.

The lease is allowed to be stricter than the legacy Account Truth submit gate,
but must never be weaker.  This module reads the paired outcomes recorded at
real submit boundaries and makes that promotion invariant explicit.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.engine.live.account_artifacts import read_account_events
from app.engine.live.account_observation_lease import (
    ACCOUNT_OBSERVATION_LEASE_GATE_ID,
    ACCOUNT_OBSERVATION_LEASE_GATE_SOURCE,
)
from app.lean_sidecar.trading_calendar import is_trading_day
from app.services.account_truth_snapshot import ACCOUNT_TRUTH_GATE_ID, ACCOUNT_TRUTH_GATE_SOURCE

ACCOUNT_OBSERVATION_LEASE_SHADOW_COMPARISON_EVENT = (
    "account_observation_lease_shadow_comparison"
)
_NY_TZ = ZoneInfo("America/New_York")
_GATE_STATUSES = frozenset({"pass", "block"})


@dataclass(frozen=True)
class AccountObservationLeaseShadowComparison:
    """One paired raw-truth and durable-lease outcome from a submit boundary."""

    event_index: int
    recorded_at_ms: int
    session_date: str
    truth_status: str
    lease_status: str

    @property
    def lease_is_weaker(self) -> bool:
        return self.truth_status == "block" and self.lease_status == "pass"

    @property
    def lease_is_stricter(self) -> bool:
        return self.truth_status == "pass" and self.lease_status == "block"


@dataclass(frozen=True)
class InvalidAccountObservationLeaseShadowComparison:
    """A comparison row that cannot be relied on for promotion evidence."""

    event_index: int
    reason: str


@dataclass(frozen=True)
class AccountObservationLeaseParityReport:
    """Replay result and the exact evidence needed to authorize cutover."""

    comparisons: tuple[AccountObservationLeaseShadowComparison, ...]
    invalid_comparisons: tuple[InvalidAccountObservationLeaseShadowComparison, ...]
    observed_session_dates: tuple[str, ...]
    minimum_sessions: int

    @property
    def comparison_count(self) -> int:
        return len(self.comparisons)

    @property
    def invalid_comparison_count(self) -> int:
        return len(self.invalid_comparisons)

    @property
    def lease_weaker_comparisons(self) -> tuple[AccountObservationLeaseShadowComparison, ...]:
        return tuple(comparison for comparison in self.comparisons if comparison.lease_is_weaker)

    @property
    def lease_stricter_comparisons(self) -> tuple[AccountObservationLeaseShadowComparison, ...]:
        return tuple(comparison for comparison in self.comparisons if comparison.lease_is_stricter)

    @property
    def cutover_ready(self) -> bool:
        """Only valid, multi-session evidence without weaker lease outcomes passes."""

        return (
            len(self.observed_session_dates) >= self.minimum_sessions
            and not self.invalid_comparisons
            and not self.lease_weaker_comparisons
        )


def assess_observation_lease_shadow_parity(
    account_events: Iterable[Mapping[str, object]],
    *,
    minimum_sessions: int = 3,
) -> AccountObservationLeaseParityReport:
    """Replay paired outcomes and report whether they meet the promotion gate.

    A comparison contributes a session only when its America/New_York date is
    an NYSE trading day according to the canonical calendar. This is durable
    backend evidence, never a frontend-derived day count. A weaker lease
    outcome blocks cutover even if it occurred outside a trading session.
    """

    if minimum_sessions < 1:
        raise ValueError("minimum_sessions must be positive")

    comparisons: list[AccountObservationLeaseShadowComparison] = []
    invalid: list[InvalidAccountObservationLeaseShadowComparison] = []
    session_dates: set[str] = set()
    for event_index, event in enumerate(account_events):
        if event.get("event_type") != ACCOUNT_OBSERVATION_LEASE_SHADOW_COMPARISON_EVENT:
            continue
        comparison, reason = _parse_shadow_comparison(event, event_index=event_index)
        if comparison is None:
            invalid.append(
                InvalidAccountObservationLeaseShadowComparison(
                    event_index=event_index,
                    reason=reason,
                )
            )
            continue
        comparisons.append(comparison)
        if is_trading_day(date.fromisoformat(comparison.session_date)):
            session_dates.add(comparison.session_date)

    return AccountObservationLeaseParityReport(
        comparisons=tuple(comparisons),
        invalid_comparisons=tuple(invalid),
        observed_session_dates=tuple(sorted(session_dates)),
        minimum_sessions=minimum_sessions,
    )


def assess_observation_lease_shadow_parity_from_artifacts(
    artifacts_root: Path,
    account_id: str,
    *,
    minimum_sessions: int = 3,
) -> AccountObservationLeaseParityReport:
    """Replay the canonical account journal for one account's promotion gate."""

    return assess_observation_lease_shadow_parity(
        read_account_events(artifacts_root, account_id),
        minimum_sessions=minimum_sessions,
    )


def _parse_shadow_comparison(
    event: Mapping[str, object],
    *,
    event_index: int,
) -> tuple[AccountObservationLeaseShadowComparison | None, str]:
    recorded_at_ms = event.get("recorded_at_ms")
    if not isinstance(recorded_at_ms, int) or isinstance(recorded_at_ms, bool) or recorded_at_ms < 0:
        return None, "recorded_at_ms must be a non-negative int64 ms UTC value"
    truth_status = event.get("truth_status")
    if event.get("truth_gate_id") != ACCOUNT_TRUTH_GATE_ID or event.get(
        "truth_source"
    ) != ACCOUNT_TRUTH_GATE_SOURCE:
        return None, "truth gate identity is not account.account_truth"
    if not isinstance(truth_status, str) or truth_status not in _GATE_STATUSES:
        return None, "truth_status must be pass or block"
    lease_status = event.get("lease_status")
    if event.get("lease_gate_id") != ACCOUNT_OBSERVATION_LEASE_GATE_ID or event.get(
        "lease_source"
    ) != ACCOUNT_OBSERVATION_LEASE_GATE_SOURCE:
        return None, "lease gate identity is not account.observation_lease"
    if not isinstance(lease_status, str) or lease_status not in _GATE_STATUSES:
        return None, "lease_status must be pass or block"
    try:
        session_date = datetime.fromtimestamp(recorded_at_ms / 1_000, tz=UTC).astimezone(
            _NY_TZ
        ).date()
    except (OSError, OverflowError, ValueError):
        return None, "recorded_at_ms cannot be converted to an America/New_York session date"
    return (
        AccountObservationLeaseShadowComparison(
            event_index=event_index,
            recorded_at_ms=recorded_at_ms,
            session_date=session_date.isoformat(),
            truth_status=truth_status,
            lease_status=lease_status,
        ),
        "",
    )


__all__ = [
    "ACCOUNT_OBSERVATION_LEASE_SHADOW_COMPARISON_EVENT",
    "AccountObservationLeaseParityReport",
    "AccountObservationLeaseShadowComparison",
    "InvalidAccountObservationLeaseShadowComparison",
    "assess_observation_lease_shadow_parity",
    "assess_observation_lease_shadow_parity_from_artifacts",
]
