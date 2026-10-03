"""POST /api/jobs-internal/recency-chart — the thin job-entry wrapper.

The actual grid execution, statistics, fingerprinting, and persistence are
covered exhaustively at their own seams (tests/research/recency/). This
file only proves the HTTP boundary: request validation and the eager,
pre-dispatch grid-size rejection (D11) — a malformed sweep must never even
reach a worker thread.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.research.recency import service as recency_service
from app.research.recency.models import LaunchView
from app.research.recency.runner import RecencyLaunchConfig, run_recency
from app.routers import jobs as jobs_router
from app.utils.session_anchors import MAX_TIMESTAMP_MS


def test_window_date_resolves_the_et_calendar_date_not_utc() -> None:
    """Window bounds feed EngineBacktestRequest.from_date/to_date, an ET-anchored
    trading date (ADR 0022 (a)) — must not drift a day off UTC.
    """
    # 2026-06-11 02:30 UTC is 2026-06-10 22:30 EDT (UTC-4): the ET calendar
    # date trails the UTC one across this boundary.
    ms = int(datetime(2026, 6, 11, 2, 30, tzinfo=UTC).timestamp() * 1000)
    assert recency_service.window_date(ms) == "2026-06-10"


@pytest.mark.asyncio
async def test_rejects_a_grid_past_the_sanity_ceiling_before_queuing() -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/jobs-internal/recency-chart",
            json={
                "jobId": "job-1",
                "strategies": [
                    {
                        "strategyKey": "ema_crossover_signal",
                        "paramRanges": {
                            "gapBps": {"type": "low_high_step", "low": 0.0, "high": 10_000_000.0, "step": 0.0001}
                        },
                    }
                ],
                "symbols": ["SPY"],
                "windowStartMs": 0,
                "windowEndMs": 1,
            },
        )
    assert response.status_code == 400
    assert "sanity ceiling" in response.json()["detail"]


@pytest.mark.asyncio
async def test_rejects_an_inverted_low_high_range() -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/jobs-internal/recency-chart",
            json={
                "jobId": "job-2",
                "strategies": [
                    {
                        "strategyKey": "ema_crossover_signal",
                        "paramRanges": {"gapBps": {"type": "low_high_step", "low": 5.0, "high": 1.0, "step": 1.0}},
                    }
                ],
                "symbols": ["SPY"],
                "windowStartMs": 0,
                "windowEndMs": 1,
            },
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_rejects_a_repeated_parameter_value_instead_of_scheduling_a_duplicate_cell() -> None:
    """``2, 2`` is a malformed request: two identical cells would run and the second would read as a redelivery."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/jobs-internal/recency-chart",
            json={
                "jobId": "job-dup",
                "strategies": [{"strategyKey": "ema_crossover_signal", "paramRanges": {"gap_bps": {"type": "value_list", "values": [2.0, 2.0]}}}],
                "symbols": ["SPY"],
                "windowStartMs": 0,
                "windowEndMs": 1,
            },
        )
    assert response.status_code == 400
    assert "repeats a value" in response.json()["detail"]


@pytest.mark.asyncio
async def test_rejects_empty_symbols() -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/jobs-internal/recency-chart",
            json={
                "jobId": "job-3",
                "strategies": [
                    {"strategyKey": "ema_crossover_signal", "paramRanges": {"gapBps": {"type": "value_list", "values": [2.0]}}}
                ],
                "symbols": [],
                "windowStartMs": 0,
                "windowEndMs": 1,
            },
        )
    assert response.status_code == 422  # Pydantic min_length violation


class TestValidateBeforeDispatch:
    """A direct or stale client can submit a request that only passes range
    arithmetic (D11) but is otherwise guaranteed to fail every child
    backtest. These must be rejected before a durable launch is created,
    not discovered N-runs-deep into a dispatched grid."""

    async def _post(self, body: dict) -> httpx.Response:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/jobs-internal/recency-chart", json=body)

    @pytest.mark.asyncio
    async def test_rejects_an_unknown_strategy_key(self) -> None:
        response = await self._post(
            {
                "jobId": "job-unknown-strategy",
                "strategies": [
                    {"strategyKey": "not_a_real_strategy", "paramRanges": {"gapBps": {"type": "value_list", "values": [2.0]}}}
                ],
                "symbols": ["SPY"],
                "windowStartMs": 0,
                "windowEndMs": 1,
            }
        )
        assert response.status_code == 400
        assert "unknown strategy_key" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_an_out_of_bounds_parameter_value(self) -> None:
        response = await self._post(
            {
                "jobId": "job-oob-param",
                "strategies": [
                    {
                        "strategyKey": "ema_crossover_signal",
                        "paramRanges": {"gapBps": {"type": "value_list", "values": [150.0]}},  # le=100.0
                    }
                ],
                "symbols": ["SPY"],
                "windowStartMs": 0,
                "windowEndMs": 1,
            }
        )
        assert response.status_code == 400
        assert "parameters invalid" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_an_unsupported_data_policy_label(self) -> None:
        response = await self._post(
            {
                "jobId": "job-bad-policy",
                "strategies": [
                    {"strategyKey": "ema_crossover_signal", "paramRanges": {"gapBps": {"type": "value_list", "values": [2.0]}}}
                ],
                "symbols": ["SPY"],
                "windowStartMs": 0,
                "windowEndMs": 1,
                "dataPolicy": "ibkr-raw-regular-minute",
            }
        )
        assert response.status_code == 400
        assert "data_policy" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_a_window_start_not_before_window_end(self) -> None:
        response = await self._post(
            {
                "jobId": "job-bad-window",
                "strategies": [
                    {"strategyKey": "ema_crossover_signal", "paramRanges": {"gapBps": {"type": "value_list", "values": [2.0]}}}
                ],
                "symbols": ["SPY"],
                "windowStartMs": 1000,
                "windowEndMs": 1000,
            }
        )
        assert response.status_code == 400
        assert "window_start_ms" in response.json()["detail"]


class TestWindowCeiling:
    """The Recency window is an instant pair, so it carries the ADR 0022 (g)
    ceiling — at the request boundary and when a stored launch is resumed."""

    _STRATEGIES = [{"strategyKey": "ema_crossover_signal", "paramRanges": {"gapBps": {"type": "value_list", "values": [2.0]}}}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["windowStartMs", "windowEndMs"])
    async def test_a_window_bound_past_the_ceiling_is_a_422(self, field: str) -> None:
        body = {"jobId": "job-past-ceiling", "strategies": self._STRATEGIES, "symbols": ["SPY"], "windowStartMs": 0, "windowEndMs": 1}
        body[field] = MAX_TIMESTAMP_MS + 1
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/jobs-internal/recency-chart", json=body)
        assert response.status_code == 422, response.text

    @pytest.mark.asyncio
    async def test_resuming_a_stored_launch_past_the_ceiling_is_not_resumable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A launch stored before the ceiling existed: re-validating its row on
        # resume must refuse it as NOT_RESUMABLE, not fail with a 500.
        stored = {
            "strategies": [{"strategy_key": "ema_crossover_signal", "param_ranges": {"gapBps": {"type": "value_list", "values": [2.0]}}}],
            "symbols": ["SPY"],
            "window_start_ms": 0,
            "window_end_ms": MAX_TIMESTAMP_MS + 1,
        }
        launch = LaunchView(
            launch_id="launch-past-ceiling",
            status="FAILED",
            job_id="job-original",
            attempt=1,
            expected_runs=1,
            succeeded_runs=0,
            failed_runs=1,
            created_at_ms=0,
            completed_at_ms=1,
            deleted_at_ms=None,
            config_json=json.dumps(stored),
        )

        async def load_launch(launch_id: str) -> LaunchView | None:
            return launch if launch_id == launch.launch_id else None

        monkeypatch.setattr(recency_service, "load_launch", load_launch)
        body = {
            "jobId": "job-resume",
            "resumeLaunchId": launch.launch_id,
            "strategies": self._STRATEGIES,
            "symbols": ["SPY"],
            "windowStartMs": 0,
            "windowEndMs": 1,
        }
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/jobs-internal/recency-chart", json=body)

        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "NOT_RESUMABLE"
        assert "window_end_ms" in detail["message"]


def _trade_fingerprint(config: RecencyLaunchConfig) -> str:
    """The identity the runner gives the one trade of a launch's one cell."""
    trade = SimpleNamespace(entry_time=100, exit_time=200, pnl_pts=2.0, pnl_pct=0.02, quantity=10, is_synthetic_exit=False, signal_reason="")
    result = SimpleNamespace(success=True, error=None, trades=[trade], study_id=None, data_policy=SimpleNamespace(model_dump_json=lambda: "{}"))
    persisted: list[Any] = []
    run_recency(
        config,
        execute_backtest_fn=lambda run_spec, config: result,
        persist_fn=persisted.append,
        strategy_code_version_fn=lambda strategy_key: "v1",
    )
    return persisted[0].trades[0].fingerprint


class TestFillModeIdentity:
    """A new launch runs, stores and fingerprints the canonical fill-mode name;
    a stored launch keeps the spelling its cells were fingerprinted under (#2599)."""

    _STORED = {
        "strategies": [{"strategy_key": "ema_crossover_signal", "param_ranges": {"gap_bps": {"type": "value_list", "values": [2.0]}}}],
        "symbols": ["SPY"],
        "window_start_ms": 0,
        "window_end_ms": 1,
    }
    _BODY = {
        "strategies": [{"strategyKey": "ema_crossover_signal", "paramRanges": {"gap_bps": {"type": "value_list", "values": [2.0]}}}],
        "symbols": ["SPY"],
        "windowStartMs": 0,
        "windowEndMs": 1,
    }

    @pytest.fixture
    def seen(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """What a launch stores and what its worker is handed, with no database and no thread."""
        seen: dict[str, Any] = {}

        async def create_launch(launch: recency_service.ValidatedLaunch, *, request: dict[str, Any]) -> bool:
            seen["stored"] = request
            return True

        def run_launch(config: RecencyLaunchConfig, **_: Any) -> dict[str, Any]:
            seen["config"] = config
            return {}

        monkeypatch.setattr(recency_service, "create_launch", create_launch)
        monkeypatch.setattr(recency_service, "run_launch", run_launch)
        monkeypatch.setattr(jobs_router, "run_in_thread", lambda job_id, work, **_: work(None, None))
        return seen

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/jobs-internal/recency-chart", json=body)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spelling", ["Signal-Bar-Close", " close "])
    async def test_a_new_launch_stores_and_fingerprints_the_canonical_name(self, seen: dict[str, Any], spelling: str) -> None:
        canonical = await self._post({**self._BODY, "jobId": "job-canonical", "fillMode": "signal_bar_close"})
        assert canonical.status_code == 202, canonical.text
        canonical_fingerprint = _trade_fingerprint(seen["config"])

        response = await self._post({**self._BODY, "jobId": "job-spelled", "fillMode": spelling})

        assert response.status_code == 202, response.text
        assert seen["stored"]["fill_mode"] == "signal_bar_close"
        assert _trade_fingerprint(seen["config"]) == canonical_fingerprint

    @pytest.mark.asyncio
    async def test_an_unknown_fill_mode_is_refused_before_anything_is_stored(self, seen: dict[str, Any]) -> None:
        response = await self._post({**self._BODY, "jobId": "job-magic", "fillMode": "magic"})

        assert response.status_code == 400, response.text
        assert "signal_bar_close, next_bar_open or decision_minute_open" in response.json()["detail"]
        assert seen == {}

    @pytest.mark.asyncio
    async def test_a_resumed_launch_keeps_the_identity_its_stored_spelling_gave_it(
        self, seen: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stored before launches named the mode canonically: its recorded cells
        # were fingerprinted under "close", so the rest of the grid must be too.
        launch = LaunchView(
            launch_id="launch-short-name",
            status="FAILED",
            job_id="job-original",
            attempt=1,
            expected_runs=1,
            succeeded_runs=0,
            failed_runs=1,
            created_at_ms=0,
            completed_at_ms=1,
            deleted_at_ms=None,
            config_json=json.dumps({**self._STORED, "fill_mode": "close"}),
        )

        async def load_launch(launch_id: str) -> LaunchView | None:
            return launch if launch_id == launch.launch_id else None

        monkeypatch.setattr(recency_service, "load_launch", load_launch)
        canonical = await self._post({**self._BODY, "jobId": "job-canonical", "fillMode": "signal_bar_close"})
        assert canonical.status_code == 202, canonical.text
        canonical_fingerprint = _trade_fingerprint(seen["config"])

        response = await self._post({**self._BODY, "jobId": "job-resume", "resumeLaunchId": launch.launch_id})

        assert response.status_code == 202, response.text
        assert seen["config"].launch_id == launch.launch_id
        assert seen["config"].fill_mode == "close"
        assert _trade_fingerprint(seen["config"]) != canonical_fingerprint


class TestRecordRecencyAbortState:
    """The abort path must move a launch off RUNNING without hiding the abort.

    A launch that dies mid-flight has no summary, so the terminal-state write
    carries no run counts. If that write fails — the backend is down, or it
    rejects the body — the exception that actually ended the launch is what
    the operator needs to see, so the failure is logged, not raised. The write
    itself goes straight to the Python-owned table (PRD #1927).
    """

    def test_returns_normally_so_the_original_exception_survives(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise ConnectionError("database unreachable")

        monkeypatch.setattr(recency_service, "record_terminal_status", boom)

        with caplog.at_level(logging.ERROR, logger="app.research.recency.service"):
            recency_service.record_abort_state("launch-1", "FAILED", attempt=3)

        assert "failed to record recency launch terminal state" in caplog.text

    def test_forwards_the_terminal_status_when_the_write_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def capture(launch_id: str, status: str, **kwargs: object) -> None:
            seen.update({"launch_id": launch_id, "status": status, **kwargs})

        monkeypatch.setattr(recency_service, "record_terminal_status", capture)

        recency_service.record_abort_state("launch-2", "CANCELLED", attempt=1)

        assert seen == {"launch_id": "launch-2", "status": "CANCELLED", "attempt": 1}
