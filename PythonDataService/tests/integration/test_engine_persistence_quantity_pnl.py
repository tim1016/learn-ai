"""The engine run's persist payload — per-trade dollar P&L, fees, and the strict run report.

Regression roots: Bug A (QUANTITY_MISMATCH + PNL_DRIFT) in the engine
auto-save. Before the fix, the persisted trade carried ``pnl_pts`` (per-share
points) and no quantity, so a 140-share position with a $1.45/share move was
recorded off by a factor of ~140 with no error path. See
``.claude/rules/numerical-rigor.md`` → ``QUANTITY_MISMATCH`` / ``PNL_DRIFT``.

The payload is built by the pure ``build_engine_run_payload`` (PRD #1929),
so these rules are asserted on the payload itself, not on an HTTP body.
"""

from __future__ import annotations

import json

import pytest

from app.models.responses import (
    LeanPortfolioStatsResponse,
    LeanStatisticsResponse,
    LeanTradeStatsResponse,
)
from app.research.backtest_runs.engine_payload import build_engine_run_payload
from app.research.backtest_runs.records import RunPayloadError, record_from_payload
from app.routers.engine import EngineBacktestResponse, EngineTradeResponse


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


def _payload(response: EngineBacktestResponse, **overrides):
    kwargs = dict(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        parameters={"symbol": "SPY"},
        duration_ms=1234,
    )
    kwargs.update(overrides)
    return build_engine_run_payload(**kwargs)


def test_engine_run_payload_includes_quantity_and_dollar_pnl() -> None:
    """The persisted trade must carry the resolved fill quantity and PnL in
    dollars net of the round-trip commission (entry fee + exit fee).
    """
    payload = _payload(_response_with_trade(quantity=140, pnl_pts=1.45, commission_per_order=1.0), commission_per_order=1.0)

    assert payload["trades"], "no trades in the payload"
    trade = payload["trades"][0]
    assert trade["quantity"] == 140
    # 140 × 1.45 − 2 × 1.0 = 201.00 (net of round-trip commission).
    assert trade["pnl"] == pytest.approx(201.0, abs=1e-9)
    equity = json.loads(payload["equity_curve_json"])
    assert equity["schema_version"] == 2
    assert equity["realized"]["cadence"] == "trade_exit"
    assert equity["realized"]["points"][-1]["e"] == pytest.approx(100_201.0, abs=1e-6)
    # The payload is the canonical persist shape: the record builder accepts it as is.
    record = record_from_payload(payload)
    assert record.source == "engine" and record.trades[0].pnl == pytest.approx(201.0, abs=1e-9)


def test_engine_run_payload_with_zero_commission() -> None:
    """A zero commission produces a clean gross-PnL row — useful for synthetic
    tests where commissions would muddy the equality check.
    """
    payload = _payload(_response_with_trade(quantity=10, pnl_pts=2.0), commission_per_order=0.0)

    trade = payload["trades"][0]
    assert trade["quantity"] == 10
    assert trade["pnl"] == pytest.approx(20.0, abs=1e-9)


def test_engine_run_payload_uses_executed_ibkr_fees_for_compatibility_runs() -> None:
    """Compatibility persistence must mirror the fee model the engine ran.

    A zero legacy flat-fee input does not disable the pinned IBKR fee model.
    Quantities above 200 shares make its per-share tier exceed the $1 floor,
    which catches any persistence code that incorrectly reuses the UI input.
    """
    response = _response_with_trade(quantity=250, pnl_pts=1.45)
    response.total_fees = 2.50
    response.final_equity = 100_000.0 + 250 * 1.45 - 2.50
    response.net_profit = 250 * 1.45 - 2.50

    payload = _payload(response, commission_per_order=0.0, compatibility_profile="us-equity-raw-ibkr-v1")

    assert payload["trades"][0]["pnl"] == pytest.approx(360.0, abs=1e-9)
    equity = json.loads(payload["equity_curve_json"])
    assert equity["realized"]["points"][-1]["e"] == pytest.approx(100_360.0, abs=1e-6)


def test_engine_run_payload_preserves_a_synthetic_terminal_exit_receipt() -> None:
    """The end-of-algorithm close remains visibly identified in history."""
    response = _response_with_trade(quantity=10, pnl_pts=2.0)
    response.trades[0].is_synthetic_exit = True
    response.trades[0].signal_reason = "EndOfAlgorithm (synthetic exit)"

    persisted = _payload(response)["trades"][0]

    assert persisted["is_synthetic_exit"] is True
    assert persisted["signal_reason"] == "EndOfAlgorithm (synthetic exit)"


def test_engine_run_payload_keeps_a_zero_trade_run_flat_until_its_last_chart_bar() -> None:
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

    equity = json.loads(_payload(response)["equity_curve_json"])

    assert equity["realized"]["points"] == [
        {"t": 1_736_173_800_000, "e": 100_000.0},
        {"t": 1_736_179_200_000, "e": 100_000.0},
    ]


def test_engine_run_payload_refuses_a_run_without_producer_timestamps() -> None:
    """Absent producer timestamps cannot back a strict run report."""
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

    with pytest.raises(RunPayloadError, match="no timestamps"):
        _payload(response)


def test_engine_run_payload_rejects_a_non_reconciling_realized_equity_ledger() -> None:
    """The persisted staircase must agree with headline final equity to 1e-6."""
    response = _response_with_trade(quantity=10, pnl_pts=2.0)
    response.final_equity += 0.01

    with pytest.raises(RunPayloadError, match="does not reconcile"):
        _payload(response, commission_per_order=0.0)


def test_preparation_failure_leaves_the_completed_run_unsaved_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A report the builder refuses yields a null run id and never reaches the writer."""
    from app.research.backtest_runs import service

    response = _response_with_trade(quantity=10, pnl_pts=2.0)
    response.final_equity += 0.01
    writes: list = []

    def _record(coroutine):
        writes.append(coroutine)
        coroutine.close()
        return None

    monkeypatch.setattr(service, "run_sync", _record)

    run_id = service.persist_engine_response_sync(
        response=response,
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        resolution="minute",
        parameters={"symbol": "SPY"},
        duration_ms=1,
    )

    assert run_id is None
    assert writes == []


def test_engine_run_payload_preserves_unavailable_risk_metrics_as_null() -> None:
    """Undefined ratios are persisted as null rather than a fabricated zero."""
    response = _response_with_trade(quantity=10, pnl_pts=2.0)
    response.statistics = {
        "max_drawdown_pct": 0.0,
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "profit_factor": None,
    }

    payload = _payload(response, commission_per_order=0.0)

    assert payload["sharpe_ratio"] is None
    assert payload["sortino_ratio"] is None
    assert payload["profit_factor"] is None
    record = record_from_payload(payload)
    assert record.sharpe_ratio is None and record.sortino_ratio is None and record.profit_factor is None


def test_engine_run_payload_uses_canonical_engine_statistics_for_headlines() -> None:
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

    payload = _payload(response, commission_per_order=0.0)

    assert payload["max_drawdown"] == pytest.approx(0.0257, abs=1e-12)
    assert payload["sharpe_ratio"] == pytest.approx(1.43, abs=1e-12)
    assert payload["sortino_ratio"] == pytest.approx(2.59, abs=1e-12)
    assert payload["profit_factor"] == pytest.approx(2.00, abs=1e-12)
    # The engine's headline wins even though the LEAN projection carries different numbers.
    record = record_from_payload(payload)
    assert record.sharpe_ratio == pytest.approx(1.43, abs=1e-12)


def test_engine_run_payload_includes_validation_analytics_envelope() -> None:
    """The frozen analytics envelope must survive persistence — the run
    report renders it from the row, never from the transient response."""
    from app.schemas.engine_validation import EngineValidationAnalyticsResponse

    response = _response_with_trade(quantity=10, pnl_pts=2.0)
    response.validation_analytics = EngineValidationAnalyticsResponse()

    envelope = json.loads(_payload(response, commission_per_order=0.0)["validation_analytics_json"])

    assert envelope["schema_version"] == 2
    assert envelope["engine"] == "python"
    assert envelope["computed_at_ms"] > 0
    assert set(envelope["analytics"].keys()) == {
        "horizons",
        "timing_cells",
        "seasonality",
        "rolling_trade_stability",
        "sharpe_pnl_divergence",
    }


def test_engine_run_payload_analytics_null_when_absent() -> None:
    """No analytics on the response → honest null column, not a crash."""
    payload = _payload(_response_with_trade(quantity=10, pnl_pts=2.0), commission_per_order=0.0)

    assert payload["validation_analytics_json"] is None
    assert record_from_payload(payload).validation_analytics_json is None
