"""Unit tests for app.data_lake.backfill.run_backfill.

These tests never touch Postgres or Polygon: run_backfill's ensure_fn and
status_fn are both injectable, so the per-day orchestration (session
iteration, progress callbacks, typed-failure propagation, lease-wait
polling) is exercised against small in-memory fakes that mimic
ensure_data's and the catalog's per-day contract. Real-catalog claim/lease
behavior stays covered by tests/unit/data_lake/test_catalog_write_ops.py
and tests/unit/data_lake/test_ensure_data.py (Postgres-gated).

Issue: #1836.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from uuid import UUID

import pytest

from app.data_lake.backfill import BackfillDayProgress, BackfillWaitProgress, run_backfill
from app.data_lake.catalog_client import MinuteBarLeaseStatus
from app.data_lake.types import ArtifactFailure, ArtifactIdentity, ArtifactRecord, DataAvailabilityResult, DataRunSpec

pytestmark = pytest.mark.asyncio


def _spec(
    start: date = date(2024, 5, 20),
    end: date = date(2024, 5, 24),
    symbols: list[str] | None = None,
) -> DataRunSpec:
    return DataRunSpec(
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=symbols or ["SPY"],
        start_trading_date=start,
        end_trading_date=end,
        lean_image_digest="sha256:test",
    )


def _artifact(trading_date: date, symbol: str = "SPY", artifact_id: int = 1) -> ArtifactRecord:
    return ArtifactRecord(
        id=artifact_id,
        artifact_kind="time_series_bars",
        market="usa",
        symbol=symbol,
        trading_date=trading_date,
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
        data_contract_hash="a" * 64,
        file_path=f"equity/usa/minute/{symbol.lower()}/{trading_date.strftime('%Y%m%d')}_trade.zip",
        file_sha256="b" * 64,
        row_count=390,
        first_bar_start_ms=0,
        last_bar_start_ms=0,
    )


def _ok_result(spec: DataRunSpec, artifact_id: int = 1) -> DataAvailabilityResult:
    trading_date = spec.start_trading_date
    return DataAvailabilityResult(
        request_id=spec.request_id,
        overall_status="complete",
        lean_data_root_path="/tmp/lake",
        data_availability_hash="hash",
        artifacts=[_artifact(trading_date, spec.symbols[0], artifact_id)],
        failures=[],
        skipped_non_sessions=[],
        fetched_artifact_count=1,
        reused_artifact_count=0,
        completed_at_ms=1,
        duration_ms=1,
    )


def _failure_result(spec: DataRunSpec, reason: str, detail: str = "boom") -> DataAvailabilityResult:
    trading_date = spec.start_trading_date
    return DataAvailabilityResult(
        request_id=spec.request_id,
        overall_status="failed",
        lean_data_root_path="/tmp/lake",
        data_availability_hash="hash",
        artifacts=[],
        failures=[
            ArtifactFailure(
                artifact_kind="time_series_bars",
                symbol=spec.symbols[0],
                trading_date=trading_date,
                data_type="trade",
                reason=reason,
                detail=detail,
                attempt_count=1,
            )
        ],
        skipped_non_sessions=[],
        fetched_artifact_count=0,
        reused_artifact_count=0,
        completed_at_ms=1,
        duration_ms=1,
    )


class TestSessionIteration:
    async def test_calls_ensure_once_per_canonical_session_and_narrows_the_range(self) -> None:
        """2024-05-20..24 is a normal trading week (Mon-Fri): 5 NYSE sessions.
        Each sub-call must be scoped to a single day and opt out of the
        symbol/range-scoped artifacts a per-day call cannot correctly build."""
        seen_specs: list[DataRunSpec] = []

        async def fake_ensure(day_spec: DataRunSpec) -> DataAvailabilityResult:
            seen_specs.append(day_spec)
            return _ok_result(day_spec)

        result = await run_backfill(_spec(), ensure_fn=fake_ensure)

        assert result.total_sessions == 5
        assert [s.start_trading_date for s in seen_specs] == [
            date(2024, 5, 20),
            date(2024, 5, 21),
            date(2024, 5, 22),
            date(2024, 5, 23),
            date(2024, 5, 24),
        ]
        for s in seen_specs:
            assert s.start_trading_date == s.end_trading_date
            assert s.include_factor_files is False
            assert s.include_map_files is False
            assert s.include_daily_trade is False
        assert result.days_completed == 5
        assert result.days_with_failures == 0
        assert result.fetched_artifact_count == 5
        assert result.overall_status == "complete"

    async def test_skips_weekend_and_holiday_days(self) -> None:
        """2024-05-25/26 is a weekend and 2024-05-27 is Memorial Day
        (NYSE holiday) — the canonical calendar must exclude all three,
        leaving only 2024-05-24 and 2024-05-28."""
        calls: list[date] = []

        async def fake_ensure(day_spec: DataRunSpec) -> DataAvailabilityResult:
            calls.append(day_spec.start_trading_date)
            return _ok_result(day_spec)

        spec = _spec(start=date(2024, 5, 24), end=date(2024, 5, 28))
        result = await run_backfill(spec, ensure_fn=fake_ensure)

        assert calls == [date(2024, 5, 24), date(2024, 5, 28)]
        assert result.total_sessions == 2

    async def test_empty_range_produces_zero_sessions_and_complete_status(self) -> None:
        async def fake_ensure(day_spec: DataRunSpec) -> DataAvailabilityResult:  # pragma: no cover
            raise AssertionError("must not be called when the range has no sessions")

        spec = _spec(start=date(2024, 5, 25), end=date(2024, 5, 26))  # a weekend
        result = await run_backfill(spec, ensure_fn=fake_ensure)

        assert result.total_sessions == 0
        assert result.overall_status == "complete"


class TestProgressCallback:
    async def test_emits_one_progress_callback_per_day_in_order(self) -> None:
        progress_events: list[BackfillDayProgress] = []

        async def fake_ensure(day_spec: DataRunSpec) -> DataAvailabilityResult:
            return _ok_result(day_spec)

        spec = _spec(start=date(2024, 5, 20), end=date(2024, 5, 22))
        await run_backfill(spec, ensure_fn=fake_ensure, on_day_progress=progress_events.append)

        assert [p.day_index for p in progress_events] == [1, 2, 3]
        assert all(p.total_days == 3 for p in progress_events)
        assert [p.trading_date for p in progress_events] == [
            date(2024, 5, 20),
            date(2024, 5, 21),
            date(2024, 5, 22),
        ]
        assert all(p.fetched_count == 1 and p.failures == () for p in progress_events)

    async def test_cancel_check_raised_on_second_day_stops_the_loop(self) -> None:
        calls: list[date] = []

        class _Cancelled(Exception):
            pass

        def cancel_check() -> None:
            if len(calls) >= 1:
                raise _Cancelled("stop")

        async def fake_ensure(day_spec: DataRunSpec) -> DataAvailabilityResult:
            calls.append(day_spec.start_trading_date)
            return _ok_result(day_spec)

        spec = _spec(start=date(2024, 5, 20), end=date(2024, 5, 24))
        with pytest.raises(_Cancelled):
            await run_backfill(spec, ensure_fn=fake_ensure, cancel_check=cancel_check)

        # Cancelled before the second day's ensure_fn call.
        assert calls == [date(2024, 5, 20)]


class TestTypedFailurePropagation:
    async def test_typed_failure_reason_survives_into_progress_and_final_result(self) -> None:
        """Failures carry the lake's typed reason codes end to end — never
        collapsed into prose."""
        progress_events: list[BackfillDayProgress] = []

        async def fake_ensure(day_spec: DataRunSpec) -> DataAvailabilityResult:
            if day_spec.start_trading_date == date(2024, 5, 21):
                return _failure_result(day_spec, reason="provider_auth_error", detail="401 from Polygon")
            return _ok_result(day_spec)

        spec = _spec(start=date(2024, 5, 20), end=date(2024, 5, 22))
        result = await run_backfill(spec, ensure_fn=fake_ensure, on_day_progress=progress_events.append)

        # Final result.
        assert result.days_with_failures == 1
        assert result.days_completed == 2
        assert result.overall_status == "partial"
        assert len(result.failures) == 1
        assert result.failures[0].reason == "provider_auth_error"
        assert result.failures[0].detail == "401 from Polygon"

        # Progress events — the failing day's callback carries the same
        # typed reason, not a stringified summary.
        failing_day = next(p for p in progress_events if p.trading_date == date(2024, 5, 21))
        assert len(failing_day.failures) == 1
        assert failing_day.failures[0].reason == "provider_auth_error"

    async def test_all_days_failing_reports_overall_status_failed(self) -> None:
        async def fake_ensure(day_spec: DataRunSpec) -> DataAvailabilityResult:
            return _failure_result(day_spec, reason="unknown_symbol")

        spec = _spec(start=date(2024, 5, 20), end=date(2024, 5, 20))
        result = await run_backfill(spec, ensure_fn=fake_ensure)

        assert result.overall_status == "failed"
        assert result.days_with_failures == 1
        assert result.days_completed == 0


class _FakeLeaseCatalog:
    """In-process model of the real catalog's per-identity claim state
    machine ('fetching' -> 'complete' | 'failed'), keyed exactly like the
    real DataLakeArtifacts unique index: (symbol, trading_date, data_type).

    ensure_fn mirrors ensure_data's own claim contract: the first caller
    to see no row wins the claim and does the (simulated) fetch; every
    other caller sees the existing row, and — critically, matching the
    real bug this fake exists to reproduce — reports reason="lease_timeout"
    whether that row is still 'fetching' *or* already permanently
    'failed' (ensure_data's own claim fallback, select_coverage_minute_bars,
    only ever checks for 'complete'; it cannot see 'failed'). Only
    status_fn can see the true state — which is exactly the gap
    run_backfill's lease-wait loop closes.
    """

    def __init__(self, *, fetch_seconds: float = 0.0, lease_ttl_ms: int = 300_000) -> None:
        self._rows: dict[tuple, dict] = {}
        self._lock = asyncio.Lock()
        self.fetch_seconds = fetch_seconds
        self.lease_ttl_ms = lease_ttl_ms
        self.fetch_calls: dict[tuple, int] = {}

    @staticmethod
    def _key(symbol: str, trading_date: date, data_type: str) -> tuple:
        return (symbol, trading_date, data_type)

    def seed_in_flight(self, symbol: str, trading_date: date, data_type: str = "trade") -> None:
        """Pre-seed a row as already claimed (status='fetching') by some
        other, external winner — for tests that drive the winner's
        completion/failure directly rather than through this fake's own
        ensure_fn claim race."""
        key = self._key(symbol, trading_date, data_type)
        self._rows[key] = {
            "status": "fetching",
            "lease_expires_at_ms": int(time.time() * 1000) + self.lease_ttl_ms,
            "last_error": None,
            "error_message": None,
        }

    def complete(self, symbol: str, trading_date: date, data_type: str = "trade") -> None:
        self._rows[self._key(symbol, trading_date, data_type)]["status"] = "complete"

    def fail(self, symbol: str, trading_date: date, reason: str, detail: str, data_type: str = "trade") -> None:
        row = self._rows[self._key(symbol, trading_date, data_type)]
        row["status"] = "failed"
        row["last_error"] = reason
        row["error_message"] = detail

    async def ensure_fn(self, day_spec: DataRunSpec) -> DataAvailabilityResult:
        symbol = day_spec.symbols[0]
        trading_date = day_spec.start_trading_date
        data_type = "trade"
        key = self._key(symbol, trading_date, data_type)

        async with self._lock:
            row = self._rows.get(key)
            won = row is None
            if won:
                row = {
                    "status": "fetching",
                    "lease_expires_at_ms": int(time.time() * 1000) + self.lease_ttl_ms,
                    "last_error": None,
                    "error_message": None,
                }
                self._rows[key] = row

        if not won:
            if row["status"] == "complete":
                return _ok_result(day_spec)
            return _failure_result(day_spec, reason="lease_timeout", detail="in-flight elsewhere")

        self.fetch_calls[key] = self.fetch_calls.get(key, 0) + 1
        if self.fetch_seconds:
            await asyncio.sleep(self.fetch_seconds)
        async with self._lock:
            row["status"] = "complete"
        return _ok_result(day_spec)

    async def status_fn(self, identity: ArtifactIdentity) -> MinuteBarLeaseStatus | None:
        row = self._rows.get(self._key(identity.symbol or "", identity.trading_date, identity.data_type or "trade"))
        if row is None:
            return None
        return MinuteBarLeaseStatus(
            status=row["status"],
            lease_expires_at_ms=row["lease_expires_at_ms"],
            last_error=row["last_error"],
            error_message=row["error_message"],
        )


class TestLeaseWait:
    """Important 1 (review round 1): the wait must be bounded by the
    winner's own lease TTL, not a fixed sub-second retry budget — a real
    fetch can legitimately run for tens of seconds. These tests use
    latencies well past the old ~0.75s budget to pin that."""

    async def test_loser_waits_out_a_slow_winner_and_coalesces(self) -> None:
        """Important 2 (review round 1): the winner's fetch (~2s) is a wide
        margin past the old retry budget's floor — the loser must still
        coalesce to 'complete', not report a spurious lease_timeout."""
        catalog = _FakeLeaseCatalog(fetch_seconds=2.0)
        trading_date = date(2024, 5, 20)
        wait_progress: list[BackfillWaitProgress] = []

        winner_spec = _spec(start=trading_date, end=trading_date)
        loser_spec = _spec(start=trading_date, end=trading_date)

        winner_result, loser_result = await asyncio.gather(
            run_backfill(winner_spec, ensure_fn=catalog.ensure_fn, status_fn=catalog.status_fn),
            run_backfill(
                loser_spec,
                ensure_fn=catalog.ensure_fn,
                status_fn=catalog.status_fn,
                on_wait=wait_progress.append,
            ),
        )

        assert winner_result.overall_status == "complete"
        assert loser_result.overall_status == "complete"
        assert loser_result.failures == []
        # Exactly one real fetch happened — the loser coalesced onto it.
        assert catalog.fetch_calls[("SPY", trading_date, "trade")] == 1
        # The loser actually waited (not an instant no-op) — on_wait fired.
        assert len(wait_progress) >= 1
        assert all(p.trading_date == trading_date and p.symbol == "SPY" for p in wait_progress)

    async def test_loser_surfaces_the_winners_real_permanent_failure_reason(self) -> None:
        """Important 1's second requirement: when the winner permanently
        fails the row, the loser must report that real reason, not a
        misleading lease_timeout."""
        catalog = _FakeLeaseCatalog()
        trading_date = date(2024, 5, 20)
        catalog.seed_in_flight("SPY", trading_date)

        async def winner_fails_shortly() -> None:
            await asyncio.sleep(0.6)
            catalog.fail("SPY", trading_date, reason="provider_auth_error", detail="401 from Polygon")

        winner_task = asyncio.create_task(winner_fails_shortly())
        try:
            spec = _spec(start=trading_date, end=trading_date)
            result = await run_backfill(spec, ensure_fn=catalog.ensure_fn, status_fn=catalog.status_fn)
        finally:
            await winner_task

        assert result.overall_status == "failed"
        assert len(result.failures) == 1
        assert result.failures[0].reason == "provider_auth_error"
        assert result.failures[0].detail == "401 from Polygon"

    async def test_gives_up_once_the_winners_own_lease_has_expired(self) -> None:
        """A winner that never completes or fails (e.g. it crashed) must
        not be waited on forever — once its own lease TTL has elapsed,
        the loser gives up and reports lease_timeout."""
        catalog = _FakeLeaseCatalog(lease_ttl_ms=-1)  # already expired the instant it's claimed
        trading_date = date(2024, 5, 20)
        catalog.seed_in_flight("SPY", trading_date)

        spec = _spec(start=trading_date, end=trading_date)
        result = await run_backfill(spec, ensure_fn=catalog.ensure_fn, status_fn=catalog.status_fn)

        assert result.overall_status == "failed"
        assert result.failures[0].reason == "lease_timeout"

    async def test_non_lease_failure_never_touches_status_fn(self) -> None:
        attempts: list[int] = []

        async def fake_ensure(day_spec: DataRunSpec) -> DataAvailabilityResult:
            attempts.append(1)
            return _failure_result(day_spec, reason="provider_rate_limited")

        async def unexpected_status_fn(identity: ArtifactIdentity) -> MinuteBarLeaseStatus | None:
            raise AssertionError("status_fn must not be called for a non-lease failure")

        spec = _spec(start=date(2024, 5, 20), end=date(2024, 5, 20))
        result = await run_backfill(spec, ensure_fn=fake_ensure, status_fn=unexpected_status_fn)

        assert len(attempts) == 1
        assert result.failures[0].reason == "provider_rate_limited"


class TestConcurrentOverlappingBackfills:
    """Two run_backfill() calls over overlapping ranges must coalesce
    through the same claim/lease contract ensure_data uses: exactly one
    fetch per (symbol, day), and both jobs report every day as complete.

    _FakeLeaseCatalog models the catalog's claim semantics directly (first
    caller to claim an identity wins; concurrent callers see a transient
    lease_timeout, resolved by run_backfill's lease-wait loop polling
    status_fn) so this coalescing behavior is exercised without a live
    Postgres — the catalog's own claim atomicity is covered separately by
    tests/unit/data_lake/test_catalog_write_ops.py.
    """

    async def test_two_overlapping_jobs_share_one_fetch_per_day_and_both_complete(self) -> None:
        catalog = _FakeLeaseCatalog(fetch_seconds=0.05)

        spec_a = _spec(start=date(2024, 5, 20), end=date(2024, 5, 22))
        spec_b = _spec(start=date(2024, 5, 21), end=date(2024, 5, 23))

        result_a, result_b = await asyncio.gather(
            run_backfill(spec_a, ensure_fn=catalog.ensure_fn, status_fn=catalog.status_fn),
            run_backfill(spec_b, ensure_fn=catalog.ensure_fn, status_fn=catalog.status_fn),
        )

        assert result_a.overall_status == "complete"
        assert result_b.overall_status == "complete"
        assert result_a.failures == []
        assert result_b.failures == []
        # The overlapping days (05-21, 05-22) were fetched exactly once
        # across both jobs — no duplicate fetch.
        for key, count in catalog.fetch_calls.items():
            assert count == 1, f"{key} was fetched {count} times, expected exactly 1"
        fetched_days = {key[1] for key in catalog.fetch_calls}
        assert fetched_days == {date(2024, 5, 20), date(2024, 5, 21), date(2024, 5, 22), date(2024, 5, 23)}
