"""Regression test for Bug A (QUANTITY_MISMATCH + PNL_DRIFT) in the engine auto-save.

Before this fix, ``_save_study_sync`` posted ``pnL: t.pnl_pts`` (per-share
points) and never supplied ``quantity``. The .NET ``BacktestTrade.Quantity``
column then defaulted to ``1`` and ``BacktestTrade.PnL`` recorded the
per-share gain instead of the dollar P&L of the actual fill. For a 140-share
position with a $1.45/share move, the row was off by a factor of ~140 with
no error path.

See ``.claude/rules/numerical-rigor.md`` → ``QUANTITY_MISMATCH`` /
``PNL_DRIFT`` and the divergence trace at ``StrategyExecutions`` rows 41/42
(run on 2026-05-21).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from app.models.responses import (
    LeanPortfolioStatsResponse,
    LeanStatisticsResponse,
    LeanTradeStatsResponse,
)
from app.routers.engine import (
    EngineBacktestResponse,
    EngineTradeResponse,
    _save_study_sync,
)


def _response_with_trade(
    *,
    quantity: int,
    pnl_pts: float,
    commission_per_order: float = 0.0,
) -> EngineBacktestResponse:
    trade = EngineTradeResponse(
        trade_number=1,
        entry_time=1_736_173_800_000,
        entry_price=710.0,
        exit_time=1_736_179_200_000,
        exit_price=710.0 + pnl_pts,
        quantity=quantity,
        indicators={},
        pnl_pts=pnl_pts,
        pnl_pct=pnl_pts / 710.0,
        result="WIN" if pnl_pts >= 0 else "LOSS",
        signal_reason="test",
    )
    return EngineBacktestResponse(
        success=True,
        strategy_name="ema_crossover",
        fill_mode="signal_bar_close",
        initial_cash=100_000.0,
        final_equity=100_000.0 + quantity * pnl_pts - 2 * commission_per_order,
        net_profit=quantity * pnl_pts - 2 * commission_per_order,
        total_fees=2 * commission_per_order,
        total_trades=1,
        winning_trades=1 if pnl_pts >= 0 else 0,
        losing_trades=0 if pnl_pts >= 0 else 1,
        win_rate=1.0 if pnl_pts >= 0 else 0.0,
        trades=[trade],
    )


@respx.mock
def test_save_study_payload_includes_quantity_and_dollar_pnl() -> None:
    """The persisted trade must carry the resolved fill quantity and PnL in
    dollars net of the round-trip commission (entry fee + exit fee).
    """
    response = _response_with_trade(
        quantity=140,
        pnl_pts=1.45,
        commission_per_order=1.0,
    )
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 42})

    respx.post("http://localhost:5000/api/studies").mock(side_effect=_capture)

    study_id = _save_study_sync(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        params_json="{}",
        duration_ms=1234,
        commission_per_order=1.0,
    )

    assert study_id == 42
    assert captured["trades"], "no trades posted"
    trade = captured["trades"][0]
    assert trade["quantity"] == 140
    # 140 × 1.45 − 2 × 1.0 = 201.00 (net of round-trip commission).
    assert trade["pnL"] == pytest.approx(201.0, abs=1e-9)
    equity = json.loads(captured["equityCurveJson"])
    assert equity["schema_version"] == 2
    assert equity["realized"]["cadence"] == "trade_exit"
    assert equity["realized"]["points"][-1]["e"] == pytest.approx(100_201.0, abs=1e-6)


@respx.mock
def test_save_study_payload_with_zero_commission() -> None:
    """A zero commission produces a clean gross-PnL row — useful for synthetic
    tests where commissions would muddy the equality check.
    """
    response = _response_with_trade(quantity=10, pnl_pts=2.0)
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 99})

    respx.post("http://localhost:5000/api/studies").mock(side_effect=_capture)

    _save_study_sync(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        params_json="{}",
        duration_ms=1,
        commission_per_order=0.0,
    )

    trade = captured["trades"][0]
    assert trade["quantity"] == 10
    assert trade["pnL"] == pytest.approx(20.0, abs=1e-9)


@respx.mock
def test_save_study_payload_uses_executed_ibkr_fees_for_compatibility_runs() -> None:
    """Compatibility persistence must mirror the fee model the engine ran.

    A zero legacy flat-fee input does not disable the pinned IBKR fee model.
    Quantities above 200 shares make its per-share tier exceed the $1 floor,
    which catches any persistence code that incorrectly reuses the UI input.
    """
    response = _response_with_trade(quantity=250, pnl_pts=1.45)
    response.total_fees = 2.50
    response.final_equity = 100_000.0 + 250 * 1.45 - 2.50
    response.net_profit = 250 * 1.45 - 2.50
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"id": 102})

    respx.post("http://localhost:5000/api/studies").mock(side_effect=_capture)

    study_id = _save_study_sync(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        params_json="{}",
        duration_ms=1,
        commission_per_order=0.0,
        compatibility_profile="us-equity-raw-ibkr-v1",
    )

    assert study_id == 102
    assert captured["trades"][0]["pnL"] == pytest.approx(360.0, abs=1e-9)
    equity = json.loads(captured["equityCurveJson"])
    assert equity["realized"]["points"][-1]["e"] == pytest.approx(100_360.0, abs=1e-6)


@respx.mock
def test_save_study_payload_preserves_a_synthetic_terminal_exit_receipt() -> None:
    """The end-of-algorithm close remains visibly identified in history."""
    response = _response_with_trade(quantity=10, pnl_pts=2.0)
    response.trades[0].is_synthetic_exit = True
    response.trades[0].signal_reason = "EndOfAlgorithm (synthetic exit)"
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 97})

    respx.post("http://localhost:5000/api/studies").mock(side_effect=_capture)

    _save_study_sync(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        params_json="{}",
        duration_ms=1,
    )

    persisted = captured["trades"][0]
    assert persisted["isSyntheticExit"] is True
    assert persisted["signalReason"] == "EndOfAlgorithm (synthetic exit)"


@respx.mock
def test_save_study_payload_keeps_a_zero_trade_run_flat_until_its_last_chart_bar() -> None:
    """A data-backed zero-trade run persists a valid flat staircase.

    Chart-bar timestamps are producer-authored session evidence. This guards
    against manufacturing UTC-midnight timestamps merely to draw a flat line.
    """
    response = EngineBacktestResponse(
        success=True,
        strategy_name="ema_crossover",
        fill_mode="signal_bar_close",
        initial_cash=100_000.0,
        final_equity=100_000.0,
        net_profit=0.0,
        total_fees=0.0,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        win_rate=0.0,
        chart_bars=[
            {"t": 1_736_173_800_000},
            {"t": 1_736_179_200_000},
        ],
    )
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 98})

    respx.post("http://localhost:5000/api/studies").mock(side_effect=_capture)

    study_id = _save_study_sync(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        params_json="{}",
        duration_ms=1,
    )

    assert study_id == 98
    equity = json.loads(captured["equityCurveJson"])
    assert equity["realized"]["points"] == [
        {"t": 1_736_173_800_000, "e": 100_000.0},
        {"t": 1_736_179_200_000, "e": 100_000.0},
    ]


@respx.mock
def test_save_study_preparation_failure_does_not_abort_a_completed_run() -> None:
    """Absent producer timestamps leave persistence best-effort, never fatal."""
    response = EngineBacktestResponse(
        success=True,
        strategy_name="ema_crossover",
        fill_mode="signal_bar_close",
        initial_cash=100_000.0,
        final_equity=100_000.0,
        net_profit=0.0,
        total_fees=0.0,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        win_rate=0.0,
    )
    route = respx.post("http://localhost:5000/api/studies").mock(
        return_value=httpx.Response(200, json={"id": 96})
    )

    study_id = _save_study_sync(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        params_json="{}",
        duration_ms=1,
    )

    assert study_id is None
    assert route.called is False


@respx.mock
def test_save_study_rejects_a_non_reconciling_realized_equity_ledger() -> None:
    """The persisted staircase must agree with headline final equity to 1e-6."""
    response = _response_with_trade(quantity=10, pnl_pts=2.0)
    response.final_equity += 0.01
    route = respx.post("http://localhost:5000/api/studies").mock(
        return_value=httpx.Response(200, json={"id": 95})
    )

    study_id = _save_study_sync(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        params_json="{}",
        duration_ms=1,
        commission_per_order=0.0,
    )

    assert study_id is None
    assert route.called is False


@respx.mock
def test_save_study_payload_preserves_unavailable_risk_metrics_as_null() -> None:
    """Undefined ratios are persisted as null rather than a fabricated zero."""
    response = _response_with_trade(quantity=10, pnl_pts=2.0)
    response.statistics = {
        "max_drawdown_pct": 0.0,
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "profit_factor": None,
    }
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"id": 100})

    respx.post("http://localhost:5000/api/studies").mock(side_effect=_capture)

    study_id = _save_study_sync(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        params_json="{}",
        duration_ms=1,
        commission_per_order=0.0,
    )

    assert study_id == 100
    assert captured["sharpeRatio"] is None
    assert captured["sortinoRatio"] is None
    assert captured["profitFactor"] is None


@respx.mock
def test_save_study_payload_uses_canonical_engine_statistics_for_headlines() -> None:
    """Run 77 regression: persisted headlines and readiness share metric identities."""
    response = _response_with_trade(quantity=10, pnl_pts=2.0)
    response.statistics = {
        "max_drawdown_pct": 0.0257,
        "sharpe_ratio": 1.43,
        "sortino_ratio": 2.59,
        "profit_factor": 2.00,
        "cagr": 0.0398,
    }
    response.lean_statistics = LeanStatisticsResponse(
        portfolio=LeanPortfolioStatsResponse(
            drawdown=0.0191,
            sharpe_ratio=1.54,
            sortino_ratio=1.00,
            compounding_annual_return=0.0412,
        ),
        trade=LeanTradeStatsResponse(profit_factor=1.86),
    )
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"id": 101})

    respx.post("http://localhost:5000/api/studies").mock(side_effect=_capture)

    _save_study_sync(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        params_json="{}",
        duration_ms=1,
        commission_per_order=0.0,
    )

    assert captured["maxDrawdown"] == pytest.approx(0.0257, abs=1e-12)
    assert captured["sharpeRatio"] == pytest.approx(1.43, abs=1e-12)
    assert captured["sortinoRatio"] == pytest.approx(2.59, abs=1e-12)
    assert captured["profitFactor"] == pytest.approx(2.00, abs=1e-12)
    assert captured["compoundingAnnualReturn"] == pytest.approx(0.0398, abs=1e-12)


@respx.mock
def test_save_study_payload_includes_validation_analytics_envelope() -> None:
    """The frozen analytics envelope must survive persistence — the run
    report renders it from the row, never from the transient response."""
    from app.schemas.engine_validation import EngineValidationAnalyticsResponse

    response = _response_with_trade(quantity=10, pnl_pts=2.0)
    response.validation_analytics = EngineValidationAnalyticsResponse()
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 7})

    respx.post("http://localhost:5000/api/studies").mock(side_effect=_capture)

    _save_study_sync(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        params_json="{}",
        duration_ms=1,
        commission_per_order=0.0,
    )

    envelope = json.loads(captured["validationAnalyticsJson"])
    assert envelope["schema_version"] == 1
    assert envelope["engine"] == "python"
    assert envelope["computed_at_ms"] > 0
    assert set(envelope["analytics"].keys()) == {
        "horizons",
        "timing_cells",
        "seasonality",
        "rolling_trade_stability",
    }


@respx.mock
def test_save_study_payload_analytics_null_when_absent() -> None:
    """No analytics on the response → honest null column, not a crash."""
    response = _response_with_trade(quantity=10, pnl_pts=2.0)
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 8})

    respx.post("http://localhost:5000/api/studies").mock(side_effect=_capture)

    _save_study_sync(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        params_json="{}",
        duration_ms=1,
        commission_per_order=0.0,
    )

    assert captured["validationAnalyticsJson"] is None
