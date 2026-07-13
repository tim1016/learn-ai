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

from app.routers.engine import (
    EngineBacktestResponse,
    EngineTradeResponse,
    _save_study_sync,
)


def _response_with_trade(*, quantity: int, pnl_pts: float) -> EngineBacktestResponse:
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
        final_equity=100_000.0 + quantity * pnl_pts - 2,
        net_profit=quantity * pnl_pts - 2,
        total_fees=2.0,
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
    response = _response_with_trade(quantity=140, pnl_pts=1.45)
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
