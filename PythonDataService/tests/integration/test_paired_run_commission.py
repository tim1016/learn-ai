"""A paired run's commission is the pinned IBKR fee model, never a flat number (#2465).

The compatibility profile pins execution to the IBKR equity tier on both
engines, so a flat ``commission_per_order`` the request carries is a value
the run silently ignores. The request boundary refuses it, and the
persisted paired run records its commission as not applicable instead of
a number it never applied. Python-only runs keep the editable flat
commission exactly as before.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.research.backtest_runs.engine_payload import build_engine_run_payload
from app.research.backtest_runs.records import record_from_payload
from app.schemas.engine_backtest import EngineBacktestResponse, EngineTradeResponse


def _paired_request_kwargs() -> dict[str, object]:
    return {
        "strategy_name": "ema_crossover_signal",
        "requested_engine": "both",
        "compatibility_profile": "us-equity-raw-ibkr-v1",
        "data_policy": {
            "source": "polygon",
            "symbol": "SPY",
            "adjusted": False,
            "session": "regular",
            "input_bars": {"timespan": "minute", "multiplier": 1},
            "strategy_bars": {"timespan": "minute", "multiplier": 15},
            "timestamp_policy": "bar_close_ms_utc",
            "timezone": "America/New_York",
            "provider_kind": "live",
            "fixture_id": None,
            "fixture_sha256": None,
        },
    }


def test_a_paired_request_with_an_explicit_flat_commission_is_refused_naming_the_pinned_model() -> None:
    from app.schemas.engine_backtest import EngineBacktestRequest

    with pytest.raises(ValidationError) as excinfo:
        EngineBacktestRequest(**_paired_request_kwargs(), commission_per_order=99)

    message = str(excinfo.value)
    assert "IBKR" in message
    assert "commission_per_order" in message


def test_a_paired_request_without_a_commission_is_accepted() -> None:
    from app.schemas.engine_backtest import EngineBacktestRequest

    request = EngineBacktestRequest(**_paired_request_kwargs())

    assert request.compatibility_profile == "us-equity-raw-ibkr-v1"


def test_a_python_only_request_keeps_its_flat_commission() -> None:
    from app.schemas.engine_backtest import EngineBacktestRequest

    request = EngineBacktestRequest(
        strategy_name="ema_crossover_signal",
        requested_engine="python",
        commission_per_order=2.5,
    )

    assert request.commission_per_order == 2.5


def _response_with_trade(*, fees: float) -> EngineBacktestResponse:
    """A one-trade response whose equity reconciles under a fee policy charging ``fees``.

    10 shares in and out at 710/712: gross 20. The strict run report
    requires final equity to equal initial cash plus fee-adjusted P&L.
    """
    trade = EngineTradeResponse(
        trade_number=1,
        entry_time=1_736_173_800_000,
        entry_price=710.0,
        exit_time=1_736_179_200_000,
        exit_price=712.0,
        quantity=10,
        indicators={},
        pnl_pts=2.0,
        pnl_pct=2.0 / 710.0,
        result="WIN",
        signal_reason="test",
    )
    return EngineBacktestResponse(
        success=True,
        strategy_name="ema_crossover_signal",
        fill_mode="signal_bar_close",
        initial_cash=100_000.0,
        final_equity=100_000.0 + 20.0 - fees,
        net_profit=20.0 - fees,
        total_fees=fees,
        total_trades=1,
        winning_trades=1,
        losing_trades=0,
        win_rate=1.0,
        trades=[trade],
    )


def test_a_persisted_paired_run_records_commission_as_not_applicable() -> None:
    """The row never stores a flat commission the paired run did not apply."""
    payload = build_engine_run_payload(
        response=_response_with_trade(fees=2.0),
        symbol="SPY",
        start_date="2025-01-13",
        end_date="2025-01-17",
        resolution="minute",
        parameters={"symbol": "SPY"},
        duration_ms=1_000,
        commission_per_order=None,
        compatibility_profile="us-equity-raw-ibkr-v1",
        requested_engine="both",
    )

    assert payload["commission_per_order"] is None
    record = record_from_payload(payload)
    assert record.commission_per_order is None


def test_a_persisted_python_run_keeps_its_flat_commission() -> None:
    payload = build_engine_run_payload(
        response=_response_with_trade(fees=5.0),
        symbol="SPY",
        start_date="2025-01-13",
        end_date="2025-01-17",
        resolution="minute",
        parameters={"symbol": "SPY"},
        duration_ms=1_000,
        commission_per_order=2.5,
        compatibility_profile=None,
        requested_engine="python",
    )

    record = record_from_payload(payload)
    assert record.commission_per_order == 2.5
