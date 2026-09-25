"""The statistics basis a run's headline numbers and verdict grade (#2447).

Owner decision #2424: the headline Sharpe, Sortino, max drawdown and the run
verdict come from the marked equity curve in every mode, compatibility pairs
included. Before #2447 the compatibility profile substituted the closed-trade
ledger for the curve, and a closed-trade drawdown can never exceed the marked
one for an all-in strategy -- the error always flattered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.lean_sidecar.config import COMPATIBILITY_PROFILE_US_EQUITY_RAW_IBKR_V1
from app.schemas.engine_backtest import EngineBacktestRequest
from app.services.engine_backtest_service import _aggregate_backtest_response

_NOW_MS = 1_736_173_800_000  # 2025-01-06 14:30 UTC, inside a regular session


def _equity_snapshot(ms: int, equity: float) -> SimpleNamespace:
    return SimpleNamespace(
        timestamp_ms=ms,
        equity=Decimal(str(equity)),
        cash=Decimal(str(equity)),
        holdings_value=Decimal(0),
    )


def _marked_curve() -> list[SimpleNamespace]:
    """10k start, an all-in position that halves mid-trade, a near-flat exit.

    The closed-trade ledger sees only -1% (entry 100, exit 99); the marked
    curve saw -50% while the position was open.
    """
    minute = 60_000
    return [
        _equity_snapshot(_NOW_MS, 10_000.0),
        _equity_snapshot(_NOW_MS + 15 * minute, 5_000.0),
        _equity_snapshot(_NOW_MS + 30 * minute, 5_500.0),
        _equity_snapshot(_NOW_MS + 45 * minute, 9_900.0),
    ]


def _result() -> SimpleNamespace:
    """The parts of ``BacktestResult`` the aggregation stage reads."""
    return SimpleNamespace(
        initial_cash=Decimal(10_000),
        final_equity=Decimal(9_900),
        net_profit=Decimal(-100),
        total_fees=Decimal(0),
        order_events=[],
        log_lines=[],
        bars=[object()],
        equity_curve=_marked_curve(),
        insights=[],
        insight_summary={},
    )


def _strategy() -> SimpleNamespace:
    """The parts of ``Strategy`` the aggregation stage reads."""
    from app.engine.strategy.base import LoggedTrade

    trade = LoggedTrade(
        entry_time_ms=_NOW_MS,
        entry_price=Decimal(100),
        exit_time_ms=_NOW_MS + 45 * 60_000,
        exit_price=Decimal(99),
        quantity=100,
        pnl_pts=Decimal(-1),
        pnl_pct=Decimal("-0.01"),
        result="LOSS",
        indicators={},
        signal_reason="test",
    )
    return SimpleNamespace(
        start_date=datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
        end_date=datetime(2025, 1, 6, 21, 0, tzinfo=UTC),
        trade_log=[trade],
        ctx=None,
    )


_RAW_DATA_POLICY: dict[str, Any] = {
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
}


def _request(**overrides: Any) -> EngineBacktestRequest:
    values: dict[str, Any] = {
        "strategy_name": "ema_crossover_signal",
        "from_date": "2025-01-06",
        "to_date": "2025-01-06",
        "save_study": False,
        "summary_only": True,
    }
    values.update(overrides)
    return EngineBacktestRequest.model_validate(values)


def _aggregate(request: EngineBacktestRequest) -> Any:
    return _aggregate_backtest_response(
        result=_result(),
        request=request,
        strategy=_strategy(),
        lake_manifest=None,
        on_phase=lambda _phase: None,
        on_log=lambda _line: None,
    )


def test_a_compatibility_pair_grades_the_marked_equity_curve() -> None:
    """#2447: an all-in trade with a 50% mid-trade drawdown used to report ~1%
    max drawdown in paired mode (the closed-trade ledger's own -1%); the
    headline number now grades the marked curve exactly as a normal run."""
    response = _aggregate(
        _request(
            compatibility_profile=COMPATIBILITY_PROFILE_US_EQUITY_RAW_IBKR_V1,
            data_policy=dict(_RAW_DATA_POLICY),
        )
    )

    # (10_000 - 5_000) / 10_000: the marked trough, not the ledger's -1%.
    assert response.statistics["max_drawdown_pct"] == 0.5


def test_verdict_inputs_are_identical_in_paired_and_normal_mode() -> None:
    """#2447: for the same run, the statistics feeding ``compute_run_verdict``
    are identical in compatibility-paired and normal mode."""
    paired = _aggregate(
        _request(
            compatibility_profile=COMPATIBILITY_PROFILE_US_EQUITY_RAW_IBKR_V1,
            data_policy=dict(_RAW_DATA_POLICY),
        )
    )
    normal = _aggregate(_request())

    assert paired.statistics == normal.statistics
    assert paired.run_verdict is not None and normal.run_verdict is not None
    # The verdict stamps its own wall-clock generation time; every graded
    # input and every grade must be identical between the two modes.
    assert paired.run_verdict.model_dump(exclude={"generated_at_ms"}) == (
        normal.run_verdict.model_dump(exclude={"generated_at_ms"})
    )
