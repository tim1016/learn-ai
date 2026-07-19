"""Tests for the LEAN validating-companion dispatch (app.services.parity_companion)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.routers.engine import EngineBacktestRequest
from app.services.parity_companion import (
    REASON_ADJUSTMENT,
    REASON_NO_TWIN,
    REASON_RESOLUTION,
    REASON_WINDOW,
    companion_ineligibility_reason,
    dispatch_parity_companion,
    mark_parity_failed,
    new_parity_group_id,
)

BACKEND = "http://localhost:5000"


def _request(**overrides) -> EngineBacktestRequest:
    payload = {
        "strategy_name": "spy_ema_crossover",
        "params": {"symbol": "SPY"},
        "from_date": "2026-01-05",
        "to_date": "2026-01-06",
        "resolution": "minute",
        "data_policy": {
            "source": "polygon",
            "symbol": "SPY",
            "adjusted": False,
            "session": "regular",
            "input_bars": {"timespan": "minute", "multiplier": 1},
            "strategy_bars": {"timespan": "minute", "multiplier": 15},
        },
    }
    payload.update(overrides)
    return EngineBacktestRequest.model_validate(payload)


def test_new_parity_group_id_is_run_id_safe():
    group = new_parity_group_id()
    assert group.startswith("pg-")
    assert len(f"companion-{group}") <= 64


@pytest.mark.parametrize(
    ("strategy", "overrides", "expected"),
    [
        ("spy_orb", {}, REASON_NO_TWIN),
        (
            "spy_ema_crossover",
            {
                "data_policy": {
                    "source": "polygon",
                    "symbol": "SPY",
                    "adjusted": True,
                    "session": "regular",
                    "input_bars": {"timespan": "minute", "multiplier": 1},
                    "strategy_bars": {"timespan": "minute", "multiplier": 15},
                }
            },
            REASON_ADJUSTMENT,
        ),
        ("spy_ema_crossover", {"resolution": "daily"}, REASON_RESOLUTION),
        ("spy_ema_crossover", {"from_date": None, "to_date": None, "params": {"symbol": "SPY"}}, REASON_WINDOW),
        ("spy_ema_crossover", {}, None),
    ],
)
def test_companion_ineligibility_reasons(strategy, overrides, expected):
    registration = _STRATEGY_REGISTRY[strategy]
    request = _request(strategy_name=strategy, **overrides)
    reason = companion_ineligibility_reason(registration, request)
    assert reason == expected


@respx.mock
def test_dispatch_eligible_creates_pending_row_and_launches_job():
    created: dict = {}
    launched: dict = {}

    def _capture_verdict(request: httpx.Request) -> httpx.Response:
        created.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 1, "status": "pending"})

    def _capture_job(request: httpx.Request) -> httpx.Response:
        launched.update(json.loads(request.content))
        return httpx.Response(202, json={"id": "job-1"})

    respx.post(f"{BACKEND}/api/parity-verdicts").mock(side_effect=_capture_verdict)
    respx.post(f"{BACKEND}/api/jobs/lean_engine_run").mock(side_effect=_capture_job)

    dispatch_parity_companion(
        registration=_STRATEGY_REGISTRY["spy_ema_crossover"],
        request=_request(),
        parity_group_id="pg-testgroup",
        left_execution_id=42,
    )

    assert created["status"] == "pending"
    assert created["leftExecutionId"] == 42
    body = launched["request"]
    assert body["run_id"] == "companion-pg-testgroup"
    assert body["template"] == "ema_crossover"
    assert body["parity_group_id"] == "pg-testgroup"
    assert body["data_policy"]["adjusted"] is False
    assert body["data_policy"]["strategy_bars"] == {"timespan": "minute", "multiplier": 15}
    # 2026-01-05 is a Monday: window is [Mon 09:30 ET, Wed 09:30 ET).
    assert body["start_ms_utc"] == 1767623400000
    assert body["end_ms_utc"] == 1767796200000


@respx.mock
def test_dispatch_migrated_signal_launches_its_named_lean_template():
    """The canonical signal strategy must not silently dispatch the legacy key."""
    launched: dict = {}

    def _capture_job(request: httpx.Request) -> httpx.Response:
        launched.update(json.loads(request.content))
        return httpx.Response(202, json={"id": "job-signal"})

    respx.post(f"{BACKEND}/api/parity-verdicts").mock(return_value=httpx.Response(200, json={"id": 1}))
    respx.post(f"{BACKEND}/api/jobs/lean_engine_run").mock(side_effect=_capture_job)

    dispatch_parity_companion(
        registration=_STRATEGY_REGISTRY["ema_crossover_signal"],
        request=_request(strategy_name="ema_crossover_signal"),
        parity_group_id="pg-signal-template",
        left_execution_id=43,
    )

    assert launched["request"]["template"] == "ema_crossover_signal"


@respx.mock
def test_dispatch_ineligible_records_unavailable_and_launches_nothing():
    created: dict = {}

    def _capture_verdict(request: httpx.Request) -> httpx.Response:
        created.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 1, "status": "unavailable"})

    respx.post(f"{BACKEND}/api/parity-verdicts").mock(side_effect=_capture_verdict)
    job_route = respx.post(f"{BACKEND}/api/jobs/lean_engine_run").mock(
        return_value=httpx.Response(202, json={})
    )

    dispatch_parity_companion(
        registration=_STRATEGY_REGISTRY["spy_orb"],
        request=_request(strategy_name="spy_orb"),
        parity_group_id="pg-notwin",
        left_execution_id=7,
    )

    assert created["status"] == "unavailable"
    assert json.loads(created["verdictJson"])["reason"] == REASON_NO_TWIN
    assert not job_route.called


@respx.mock
def test_dispatch_marks_run_failed_when_job_submission_rejected():
    marked: dict = {}
    respx.post(f"{BACKEND}/api/parity-verdicts").mock(return_value=httpx.Response(200, json={"id": 1}))
    respx.post(f"{BACKEND}/api/jobs/lean_engine_run").mock(return_value=httpx.Response(503))

    def _capture_mark(request: httpx.Request) -> httpx.Response:
        marked.update(json.loads(request.content))
        return httpx.Response(200, json={"transitioned": True})

    respx.post(f"{BACKEND}/api/parity-verdicts/pg-reject/mark-failed").mock(side_effect=_capture_mark)

    dispatch_parity_companion(
        registration=_STRATEGY_REGISTRY["spy_ema_crossover"],
        request=_request(),
        parity_group_id="pg-reject",
        left_execution_id=42,
    )

    assert marked["status"] == "run_failed"
    assert "503" in marked["detail"]


@respx.mock
def test_mark_parity_failed_swallows_transport_errors():
    respx.post(f"{BACKEND}/api/parity-verdicts/pg-x/mark-failed").mock(
        side_effect=httpx.ConnectError("backend down")
    )

    # Must not raise — parity bookkeeping is best-effort.
    mark_parity_failed("pg-x", status="run_failed", detail="test")
