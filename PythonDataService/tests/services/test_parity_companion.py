"""Tests for the LEAN validating-companion dispatch (app.services.parity_companion)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.routers.engine import EngineBacktestRequest
from app.services import parity_companion
from app.services.parity_companion import (
    REASON_EXECUTION_PROFILE,
    REASON_NO_TWIN,
    REASON_PARAMETERS_UNREPRESENTABLE,
    REASON_WARMUP_UNSUPPORTED,
    REASON_WINDOW,
    companion_ineligibility_reason,
    dispatch_parity_companion,
    mark_parity_failed,
    new_parity_group_id,
)

BACKEND = "http://localhost:5000"
EMA_PARAMETERS = {"gap": 0.20, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}


@pytest.fixture
def verdict_writes(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict]]:
    """Capture the direct verdict writes instead of reaching the database."""
    calls: dict[str, list[dict]] = {"created": [], "failed": []}
    monkeypatch.setattr(parity_companion, "record_parity_disposition_sync", lambda **kwargs: calls["created"].append(kwargs))
    monkeypatch.setattr(
        parity_companion,
        "mark_parity_failed_sync",
        lambda parity_group_id, *, status, detail: calls["failed"].append({"parity_group_id": parity_group_id, "status": status, "detail": detail}),
    )
    return calls


def _request(**overrides) -> EngineBacktestRequest:
    payload = {
        "strategy_name": "ema_crossover_signal",
        "requested_engine": "both",
        "params": {"symbol": "SPY"},
        "from_date": "2026-01-05",
        "to_date": "2026-01-06",
        "resolution": "minute",
        "compatibility_profile": "us-equity-raw-ibkr-v1",
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
        ("sma_crossover", {}, REASON_NO_TWIN),
        (
            "ema_crossover_signal",
            {"compatibility_profile": None},
            REASON_EXECUTION_PROFILE,
        ),
        ("ema_crossover_signal", {"from_date": None, "to_date": None, "params": {"symbol": "SPY"}}, REASON_WINDOW),
        ("ema_crossover_signal", {"warmup_from_date": "2025-12-01"}, REASON_WARMUP_UNSUPPORTED),
        # Strategy Lab posts every schema default on an unedited run, so a
        # parameter sent at its default value must stay eligible -- a presence
        # test here would retire the companion entirely (#1865 review).
        ("ema_crossover_signal", {"params": {"symbol": "SPY", "gap_bps": 0.0}}, None),
        (
            "ema_crossover_signal",
            {"params": {"symbol": "SPY", "gap": 0.20, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}},
            None,
        ),
        # EMA's twin receives every exposed decision gate, so a changed value
        # remains comparable rather than being silently run at a hardcoded
        # default on LEAN.
        (
            "ema_crossover_signal",
            {"params": {"symbol": "SPY", "gap": 0.20, "gap_bps": 0.0, "rsi_min": 55.0, "rsi_max": 70.0}},
            None,
        ),
        ("ema_crossover_signal", {}, None),
        # A changed cadence is representable for rsi_mean_reversion, whose
        # template consolidates at whatever bar_minutes the forwarded data
        # policy carries; the policy carries the executed period, so both
        # engines run the same cadence (#1917 review).
        (
            "rsi_mean_reversion",
            {"params": {"symbol": "SPY", "resolution_minutes": 30}},
            None,
        ),
        # The exception is per template, not global: a changed RSI threshold
        # reaches no twin and is still unrepresentable.
        (
            "rsi_mean_reversion",
            {"params": {"symbol": "SPY", "resolution_minutes": 30, "oversold": 25.0}},
            REASON_PARAMETERS_UNREPRESENTABLE,
        ),
        ("rsi_mean_reversion", {}, None),
    ],
)
def test_companion_ineligibility_reasons(strategy, overrides, expected):
    registration = _STRATEGY_REGISTRY[strategy]
    request = _request(strategy_name=strategy, **overrides)
    reason = companion_ineligibility_reason(registration, request)
    assert reason == expected


@respx.mock
def test_dispatch_eligible_creates_pending_row_and_launches_job(verdict_writes):
    launched: dict = {}

    def _capture_job(request: httpx.Request) -> httpx.Response:
        launched.update(json.loads(request.content))
        return httpx.Response(202, json={"id": "job-1"})

    respx.post(f"{BACKEND}/api/jobs/lean_engine_run").mock(side_effect=_capture_job)

    dispatch_parity_companion(
        registration=_STRATEGY_REGISTRY["ema_crossover_signal"],
        request=_request(),
        parity_group_id="pg-testgroup",
        left_execution_id=42,
        validated_parameters=EMA_PARAMETERS,
    )

    [created] = verdict_writes["created"]
    assert created["status"] == "pending"
    assert created["left_run_id"] == 42
    assert created["parity_group_id"] == "pg-testgroup"
    body = launched["request"]
    assert body["run_id"] == "companion-pg-testgroup"
    assert body["template"] == "ema_crossover_signal"
    assert body["parity_group_id"] == "pg-testgroup"
    assert body["data_policy"]["adjusted"] is False
    assert body["data_policy"]["strategy_bars"] == {"timespan": "minute", "multiplier": 15}
    # 2026-01-05 is a Monday: window is [Mon 09:30 ET, Wed 09:30 ET).
    assert body["start_ms_utc"] == 1767623400000
    assert body["end_ms_utc"] == 1767796200000


@respx.mock
def test_dispatch_migrated_signal_launches_its_named_lean_template(verdict_writes):
    """The canonical signal strategy must not silently dispatch the legacy key."""
    launched: dict = {}

    def _capture_job(request: httpx.Request) -> httpx.Response:
        launched.update(json.loads(request.content))
        return httpx.Response(202, json={"id": "job-signal"})

    respx.post(f"{BACKEND}/api/jobs/lean_engine_run").mock(side_effect=_capture_job)

    dispatch_parity_companion(
        registration=_STRATEGY_REGISTRY["ema_crossover_signal"],
        request=_request(strategy_name="ema_crossover_signal"),
        parity_group_id="pg-signal-template",
        left_execution_id=43,
        validated_parameters=EMA_PARAMETERS,
    )

    assert launched["request"]["template"] == "ema_crossover_signal"


@respx.mock
def test_non_default_ema_gates_reach_the_companion_request(verdict_writes) -> None:
    launched: dict = {}

    def _capture_job(request: httpx.Request) -> httpx.Response:
        launched.update(json.loads(request.content))
        from app.routers.lean_sidecar import TrustedRunRequestModel

        TrustedRunRequestModel.model_validate(launched["request"])
        return httpx.Response(202, json={"id": "job-parameters"})

    respx.post(f"{BACKEND}/api/jobs/lean_engine_run").mock(side_effect=_capture_job)
    parameters = {"gap": 0.35, "gap_bps": 2.5, "rsi_min": 42.0, "rsi_max": 68.0}
    dispatch_parity_companion(
        registration=_STRATEGY_REGISTRY["ema_crossover_signal"],
        request=_request(params={"symbol": "SPY", **parameters}),
        parity_group_id="pg-parameters",
        left_execution_id=44,
        validated_parameters=parameters,
    )

    assert launched["request"]["strategy_parameters"] == parameters
    assert not verdict_writes["failed"]


@respx.mock
def test_dispatch_ineligible_records_unavailable_and_launches_nothing(verdict_writes):
    job_route = respx.post(f"{BACKEND}/api/jobs/lean_engine_run").mock(
        return_value=httpx.Response(202, json={})
    )

    dispatch_parity_companion(
        registration=_STRATEGY_REGISTRY["sma_crossover"],
        request=_request(strategy_name="sma_crossover"),
        parity_group_id="pg-notwin",
        left_execution_id=7,
    )

    [created] = verdict_writes["created"]
    assert created["status"] == "unavailable"
    assert json.loads(created["verdict_json"])["reason"] == REASON_NO_TWIN
    assert not job_route.called


@respx.mock
def test_dispatch_marks_run_failed_when_job_submission_rejected(verdict_writes):
    respx.post(f"{BACKEND}/api/jobs/lean_engine_run").mock(return_value=httpx.Response(503))

    dispatch_parity_companion(
        registration=_STRATEGY_REGISTRY["ema_crossover_signal"],
        request=_request(),
        parity_group_id="pg-reject",
        left_execution_id=42,
        validated_parameters=EMA_PARAMETERS,
    )

    [marked] = verdict_writes["failed"]
    assert marked["parity_group_id"] == "pg-reject"
    assert marked["status"] == "run_failed"
    assert "503" in marked["detail"]


def test_mark_parity_failed_swallows_database_errors(monkeypatch: pytest.MonkeyPatch):
    from app.research.backtest_runs import service as runs_service

    def _boom(coroutine):
        coroutine.close()
        raise RuntimeError("database down")

    monkeypatch.setattr(runs_service, "run_sync", _boom)

    # Must not raise — parity bookkeeping is best-effort.
    mark_parity_failed("pg-x", status="run_failed", detail="test")


def test_only_templates_that_honor_a_cadence_declare_it_policy_backed() -> None:
    """``lean_data_policy_parameter_names`` is a per-template claim.

    ``ema_crossover``'s source rejects any ``bar_minutes`` but 15, so declaring
    ``resolution_minutes`` policy-backed there would swap an honest
    ``unavailable`` verdict for a companion that raises inside LEAN. Only
    ``rsi_mean_reversion``'s template consolidates at the cadence it is handed.
    """
    from app.lean_sidecar.trusted_templates import trusted_template_definition

    for key, registration in _STRATEGY_REGISTRY.items():
        declared = set(registration.lean_data_policy_parameter_names or ())
        if "resolution_minutes" not in declared:
            continue
        assert registration.lean_twin is not None, f"{key} declares policy-backed params without a twin"
        source = trusted_template_definition(registration.lean_twin).source
        assert "bar_minutes != 15" not in source, (
            f"{key}'s twin pins bar_minutes but the registration claims the cadence is policy-backed"
        )


def test_policy_backed_cadence_matches_the_period_the_engine_actually_ran() -> None:
    """The claim rests on the executed consolidator reaching the twin.

    ``_record_actual_strategy_bars`` overwrites ``data_policy.strategy_bars``
    with the strategy's real consolidation period before dispatch, and
    ``lean_sidecar_service`` derives the twin's ``bar_minutes`` from that
    multiplier. If that wiring ever changed, a 30-minute Python run would be
    paired with a 15-minute twin and the manufactured divergence would be
    reported as a finding.
    """
    from decimal import Decimal

    from app.engine.execution.portfolio import Portfolio
    from app.engine.strategy.base import StrategyContext
    from app.routers.engine import _record_actual_strategy_bars

    registration = _STRATEGY_REGISTRY["rsi_mean_reversion"]
    assert "resolution_minutes" in registration.lean_data_policy_parameter_names
    request = _request(strategy_name="rsi_mean_reversion", params={"symbol": "SPY", "resolution_minutes": 30})
    strategy = registration.build(registration.param_schema(symbol="SPY", resolution_minutes=30))
    strategy.ctx = StrategyContext(portfolio=Portfolio(initial_cash=Decimal(100_000)))
    strategy.initialize()

    _record_actual_strategy_bars(request, strategy)

    assert request.data_policy is not None
    assert request.data_policy.strategy_bars.timespan == "minute"
    assert request.data_policy.strategy_bars.multiplier == 30
