"""Unit tests for the broker-activity health state machine (PR 5).

Every transition of ``compose_broker_activity_health`` is covered.  Tests
are grouped by state so the intent of each transition is clear.
"""

from __future__ import annotations

from app.operator.notices.broker_activity_health import compose_broker_activity_health
from app.schemas.live_runs import BrokerActivityHealth

# ── helpers ──────────────────────────────────────────────────────────────────

_NOW_MS = 1_700_000_000_000


def _registered_at(offset_ms: int = 0) -> int:
    """Return a registration timestamp ``offset_ms`` before ``_NOW_MS``."""
    return _NOW_MS - offset_ms


class _FakePublisher:
    """Minimal publisher double for the state machine tests."""

    def __init__(
        self,
        *,
        is_running: bool = True,
        latest_row_ms: int | None = None,
        last_seq: int = 0,
    ) -> None:
        self._is_running = is_running
        self._latest_row_ms = latest_row_ms
        self._last_seq = last_seq

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def latest_row_ms(self) -> int | None:
        return self._latest_row_ms

    def last_persisted_seq(self) -> int:
        return self._last_seq


def _compose(
    *,
    publisher: object = None,
    registered_at_ms: int | None = None,
    last_row_ms: int | None = None,
    now_ms: int = _NOW_MS,
    starting_timeout_ms: int = 30_000,
    degraded_after_idle_ms: int = 60_000,
) -> BrokerActivityHealth:
    return compose_broker_activity_health(
        publisher=publisher,  # type: ignore[arg-type]
        registered_at_ms=registered_at_ms,
        last_row_ms=last_row_ms,
        now_ms=now_ms,
        starting_timeout_ms=starting_timeout_ms,
        degraded_after_idle_ms=degraded_after_idle_ms,
    )


# ── unavailable (publisher is None) ──────────────────────────────────────────

class TestUnavailableWhenNotRegistered:
    def test_state_is_unavailable(self) -> None:
        result = _compose(publisher=None)
        assert result.state == "unavailable"

    def test_headline_is_not_running_code(self) -> None:
        result = _compose(publisher=None)
        assert result.headline is not None
        assert result.headline.code == "activity.publisher_not_running"

    def test_headline_tier_is_critical(self) -> None:
        result = _compose(publisher=None)
        assert result.headline is not None
        assert result.headline.tier == "critical"

    def test_notices_contains_headline(self) -> None:
        result = _compose(publisher=None)
        assert len(result.notices) == 1
        assert result.notices[0].code == "activity.publisher_not_running"

    def test_copy_does_not_claim_the_bot_process_is_stopped(self) -> None:
        result = _compose(publisher=None)
        assert result.headline is not None
        assert "Start the bot process" not in result.headline.message
        assert "capture" in result.headline.message

    def test_facts_publisher_registered_false(self) -> None:
        result = _compose(publisher=None)
        assert result.facts.publisher_registered is False
        assert result.facts.publisher_running is False


# ── starting (registered but not running, within starting-timeout) ────────────

class TestStartingState:
    def test_state_is_starting_when_age_below_timeout(self) -> None:
        pub = _FakePublisher(is_running=False)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(10_000),  # 10 s ago
            starting_timeout_ms=30_000,
        )
        assert result.state == "starting"

    def test_headline_is_publisher_starting_code(self) -> None:
        pub = _FakePublisher(is_running=False)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(5_000),
            starting_timeout_ms=30_000,
        )
        assert result.headline is not None
        assert result.headline.code == "activity.publisher_starting"

    def test_headline_tier_is_info(self) -> None:
        pub = _FakePublisher(is_running=False)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(1_000),
            starting_timeout_ms=30_000,
        )
        assert result.headline is not None
        assert result.headline.tier == "info"

    def test_boundary_just_before_timeout_is_starting(self) -> None:
        """Age of exactly (starting_timeout_ms - 1) is still 'starting'."""
        pub = _FakePublisher(is_running=False)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(29_999),
            starting_timeout_ms=30_000,
        )
        assert result.state == "starting"


# ── unavailable (registered but not running, past starting-timeout) ───────────

class TestUnavailableWhenTimedOut:
    def test_state_is_unavailable_after_timeout(self) -> None:
        pub = _FakePublisher(is_running=False)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(30_000),  # exactly at timeout
            starting_timeout_ms=30_000,
        )
        assert result.state == "unavailable"

    def test_headline_is_not_running_code(self) -> None:
        pub = _FakePublisher(is_running=False)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(60_000),
            starting_timeout_ms=30_000,
        )
        assert result.headline is not None
        assert result.headline.code == "activity.publisher_not_running"

    def test_headline_tier_is_critical(self) -> None:
        pub = _FakePublisher(is_running=False)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            starting_timeout_ms=30_000,
        )
        assert result.headline is not None
        assert result.headline.tier == "critical"

    def test_copy_distinguishes_detached_capture_from_host_process_state(self) -> None:
        pub = _FakePublisher(is_running=False)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            starting_timeout_ms=30_000,
        )
        assert result.headline is not None
        assert "host process state is separate" in result.headline.message


# ── ready (running, no rows yet, within silent-boot window) ───────────────────

class TestReadySilentBootWindow:
    def test_state_is_ready_within_silent_boot_window(self) -> None:
        pub = _FakePublisher(is_running=True, latest_row_ms=None)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(10_000),  # 10 s ago
            last_row_ms=None,
            degraded_after_idle_ms=60_000,
        )
        assert result.state == "ready"

    def test_no_headline_when_ready(self) -> None:
        pub = _FakePublisher(is_running=True, latest_row_ms=None)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(10_000),
            last_row_ms=None,
            degraded_after_idle_ms=60_000,
        )
        assert result.headline is None
        assert result.notices == []

    def test_boundary_just_before_degraded_is_ready(self) -> None:
        """Age of exactly (degraded_after_idle_ms - 1) with no rows is still 'ready'."""
        pub = _FakePublisher(is_running=True, latest_row_ms=None)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(59_999),
            last_row_ms=None,
            degraded_after_idle_ms=60_000,
        )
        assert result.state == "ready"


# ── degraded (running, no rows yet, past silent-boot window) ──────────────────

class TestDegradedNoRowsYet:
    def test_state_is_degraded_past_silent_boot_window(self) -> None:
        pub = _FakePublisher(is_running=True, latest_row_ms=None)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(60_000),  # exactly at threshold
            last_row_ms=None,
            degraded_after_idle_ms=60_000,
        )
        assert result.state == "degraded"

    def test_headline_is_degraded_code(self) -> None:
        pub = _FakePublisher(is_running=True, latest_row_ms=None)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            last_row_ms=None,
            degraded_after_idle_ms=60_000,
        )
        assert result.headline is not None
        assert result.headline.code == "activity.publisher_degraded"

    def test_headline_tier_is_warning(self) -> None:
        pub = _FakePublisher(is_running=True, latest_row_ms=None)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            last_row_ms=None,
            degraded_after_idle_ms=60_000,
        )
        assert result.headline is not None
        assert result.headline.tier == "warning"


# ── degraded (running, last row is stale) ────────────────────────────────────

class TestDegradedStaleRows:
    def test_state_is_degraded_when_last_row_is_stale(self) -> None:
        pub = _FakePublisher(is_running=True)
        stale_row_ms = _NOW_MS - 60_000  # exactly at the threshold
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            last_row_ms=stale_row_ms,
            degraded_after_idle_ms=60_000,
        )
        assert result.state == "degraded"

    def test_state_is_ready_when_last_row_is_fresh(self) -> None:
        pub = _FakePublisher(is_running=True)
        fresh_row_ms = _NOW_MS - 30_000  # 30 s ago, below threshold
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            last_row_ms=fresh_row_ms,
            degraded_after_idle_ms=60_000,
        )
        assert result.state == "ready"

    def test_just_before_stale_threshold_is_ready(self) -> None:
        pub = _FakePublisher(is_running=True)
        row_ms = _NOW_MS - 59_999
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            last_row_ms=row_ms,
            degraded_after_idle_ms=60_000,
        )
        assert result.state == "ready"


# ── ready (running, fresh rows) ───────────────────────────────────────────────

class TestReadyWithFreshRows:
    def test_state_is_ready_with_recent_row(self) -> None:
        pub = _FakePublisher(is_running=True)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            last_row_ms=_NOW_MS - 5_000,  # 5 s ago
        )
        assert result.state == "ready"

    def test_no_headline_when_ready(self) -> None:
        pub = _FakePublisher(is_running=True)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            last_row_ms=_NOW_MS - 5_000,
        )
        assert result.headline is None
        assert result.notices == []


# ── facts ─────────────────────────────────────────────────────────────────────

class TestFacts:
    def test_seconds_since_registered_computed(self) -> None:
        pub = _FakePublisher(is_running=True)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(30_000),  # 30 s ago
            last_row_ms=_NOW_MS - 1_000,
        )
        assert result.facts.seconds_since_registered == 30

    def test_seconds_since_last_row_computed(self) -> None:
        pub = _FakePublisher(is_running=True)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            last_row_ms=_NOW_MS - 10_000,  # 10 s ago
        )
        assert result.facts.seconds_since_last_row == 10

    def test_seconds_since_last_row_none_when_no_rows(self) -> None:
        pub = _FakePublisher(is_running=True, latest_row_ms=None)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            last_row_ms=None,
        )
        assert result.facts.seconds_since_last_row is None

    def test_facts_publisher_running_true_when_running(self) -> None:
        pub = _FakePublisher(is_running=True)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            last_row_ms=_NOW_MS - 1_000,
        )
        assert result.facts.publisher_registered is True
        assert result.facts.publisher_running is True

    def test_latest_row_seq_from_publisher(self) -> None:
        pub = _FakePublisher(is_running=True, last_seq=42)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            last_row_ms=_NOW_MS - 1_000,
        )
        assert result.facts.latest_row_seq == 42

    def test_latest_row_seq_none_when_empty_wal(self) -> None:
        pub = _FakePublisher(is_running=True, last_seq=0)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),
            last_row_ms=None,
        )
        # last_persisted_seq() returns 0 (empty WAL) → coerced to None
        assert result.facts.latest_row_seq is None


# ── Finding 1: cold-start with no in-process rows (PR reviewer P2) ───────────


class TestColdStartNoInProcessRows:
    """latest_row_ms=None because the publisher has not yet authored a row
    in this process (regardless of what the WAL contains).  The health
    composer must use the registered_at grace path, not a stale WAL ts."""

    def test_ready_during_grace_window(self) -> None:
        """latest_row_ms=None + recently registered → ready (silent boot)."""
        pub = _FakePublisher(is_running=True, latest_row_ms=None)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(5_000),  # 5 s ago
            last_row_ms=None,
            starting_timeout_ms=30_000,
            degraded_after_idle_ms=60_000,
        )
        assert result.state == "ready"
        assert result.headline is None

    def test_degraded_after_grace_expires(self) -> None:
        """latest_row_ms=None + registered > grace ago → degraded."""
        pub = _FakePublisher(is_running=True, latest_row_ms=None)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(120_000),  # 2 minutes ago
            last_row_ms=None,
            starting_timeout_ms=30_000,
            degraded_after_idle_ms=60_000,
        )
        assert result.state == "degraded"
        assert result.headline is not None
        assert result.headline.code == "activity.publisher_degraded"

    def test_boundary_exactly_at_grace_is_degraded(self) -> None:
        """Age == degraded_after_idle_ms with no rows → degraded (closed bound)."""
        pub = _FakePublisher(is_running=True, latest_row_ms=None)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(60_000),  # exactly at threshold
            last_row_ms=None,
            degraded_after_idle_ms=60_000,
        )
        assert result.state == "degraded"

    def test_boundary_one_ms_before_grace_is_ready(self) -> None:
        """Age == degraded_after_idle_ms - 1 ms → still ready."""
        pub = _FakePublisher(is_running=True, latest_row_ms=None)
        result = _compose(
            publisher=pub,
            registered_at_ms=_registered_at(59_999),
            last_row_ms=None,
            degraded_after_idle_ms=60_000,
        )
        assert result.state == "ready"
