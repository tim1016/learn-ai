"""Unit tests for app.data_lake.backfill.run_backfill.

These tests never touch Postgres or Polygon: run_backfill's ensure_fn is
injectable, so the per-day orchestration (session iteration, progress
callbacks, typed-failure propagation, lease-timeout retry) is exercised
against a small in-memory fake that mimics ensure_data's per-day contract.
Real-catalog claim/lease behavior stays covered by
tests/unit/data_lake/test_catalog_write_ops.py and
tests/unit/data_lake/test_ensure_data.py (Postgres-gated).

Issue: #1836.
"""

from __future__ import annotations

import asyncio
from datetime import date
from uuid import UUID

import pytest

from app.data_lake.backfill import BackfillDayProgress, run_backfill
from app.data_lake.types import ArtifactFailure, ArtifactRecord, DataAvailabilityResult, DataRunSpec

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


class TestLeaseTimeoutRetry:
    async def test_retries_a_lease_timeout_and_succeeds_once_the_other_worker_completes(self) -> None:
        attempts: list[int] = []

        async def fake_ensure(day_spec: DataRunSpec) -> DataAvailabilityResult:
            attempts.append(1)
            if len(attempts) < 3:
                return _failure_result(day_spec, reason="lease_timeout", detail="another worker has the lease")
            return _ok_result(day_spec)

        spec = _spec(start=date(2024, 5, 20), end=date(2024, 5, 20))
        result = await run_backfill(spec, ensure_fn=fake_ensure)

        assert len(attempts) == 3
        assert result.overall_status == "complete"
        assert result.failures == []

    async def test_gives_up_after_max_retries_and_surfaces_lease_timeout(self) -> None:
        async def fake_ensure(day_spec: DataRunSpec) -> DataAvailabilityResult:
            return _failure_result(day_spec, reason="lease_timeout", detail="stuck")

        spec = _spec(start=date(2024, 5, 20), end=date(2024, 5, 20))
        result = await run_backfill(spec, ensure_fn=fake_ensure)

        assert result.overall_status == "failed"
        assert result.failures[0].reason == "lease_timeout"

    async def test_non_lease_failure_is_not_retried(self) -> None:
        attempts: list[int] = []

        async def fake_ensure(day_spec: DataRunSpec) -> DataAvailabilityResult:
            attempts.append(1)
            return _failure_result(day_spec, reason="provider_rate_limited")

        spec = _spec(start=date(2024, 5, 20), end=date(2024, 5, 20))
        result = await run_backfill(spec, ensure_fn=fake_ensure)

        assert len(attempts) == 1
        assert result.failures[0].reason == "provider_rate_limited"


class TestConcurrentOverlappingBackfills:
    """Two run_backfill() calls over overlapping ranges must coalesce
    through the same claim/lease contract ensure_data uses: exactly one
    fetch per (symbol, day), and both jobs report the day as complete.

    This fake models the catalog's claim semantics directly (first caller
    to claim an identity wins; concurrent callers see a transient
    lease_timeout until the winner completes) so the coalescing behavior of
    run_backfill's retry loop is exercised without a live Postgres — the
    catalog's own claim atomicity is covered separately by
    tests/unit/data_lake/test_catalog_write_ops.py.
    """

    async def test_two_overlapping_jobs_share_one_fetch_per_day_and_both_complete(self) -> None:
        fetch_calls: dict[date, int] = {}
        claimed: dict[date, str] = {}  # day -> "fetching" | "complete"
        lock = asyncio.Lock()

        async def fake_ensure(day_spec: DataRunSpec) -> DataAvailabilityResult:
            day = day_spec.start_trading_date
            async with lock:
                state = claimed.get(day)
                if state == "complete":
                    return _ok_result(day_spec)
                if state == "fetching":
                    return _failure_result(day_spec, reason="lease_timeout", detail="in-flight elsewhere")
                claimed[day] = "fetching"

            # Simulate the winner's fetch — outside the lock, like a real
            # Polygon call would be.
            fetch_calls[day] = fetch_calls.get(day, 0) + 1
            await asyncio.sleep(0.01)

            async with lock:
                claimed[day] = "complete"
            return _ok_result(day_spec)

        spec_a = _spec(start=date(2024, 5, 20), end=date(2024, 5, 22))
        spec_b = _spec(start=date(2024, 5, 21), end=date(2024, 5, 23))

        result_a, result_b = await asyncio.gather(
            run_backfill(spec_a, ensure_fn=fake_ensure),
            run_backfill(spec_b, ensure_fn=fake_ensure),
        )

        assert result_a.overall_status == "complete"
        assert result_b.overall_status == "complete"
        assert result_a.failures == []
        assert result_b.failures == []
        # The overlapping days (05-21, 05-22) were fetched exactly once
        # across both jobs — no duplicate fetch.
        for day, count in fetch_calls.items():
            assert count == 1, f"{day} was fetched {count} times, expected exactly 1"
        assert set(fetch_calls) == {date(2024, 5, 20), date(2024, 5, 21), date(2024, 5, 22), date(2024, 5, 23)}
