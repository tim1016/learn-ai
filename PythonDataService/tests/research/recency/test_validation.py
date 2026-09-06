"""Recency Chart dispatch-time validation — catches a malformed launch
before a durable job record and an entire grid of guaranteed-failing
child backtests are created.
"""

from __future__ import annotations

import pytest

from app.research.recency.validation import RecencyRequestInvalidError, validate_recency_request
from app.research.sweep.grid import LowHighStepRange, StrategyGridConfig, ValueListRange

_DATA_POLICY = "polygon-adjusted-regular-minute"


def _strategy(**param_ranges: object) -> StrategyGridConfig:
    return StrategyGridConfig(strategy_key="ema_crossover_signal", param_ranges=param_ranges)


class TestValidateRecencyRequest:
    def test_accepts_a_well_formed_request(self) -> None:
        validate_recency_request(
            strategies=[_strategy(gap_bps=ValueListRange((2.0,)))],
            symbols=["SPY"],
            window_start_ms=0,
            window_end_ms=1000,
            data_policy=_DATA_POLICY,
        )

    def test_rejects_unknown_strategy_key(self) -> None:
        strategy = StrategyGridConfig(strategy_key="not_a_real_strategy", param_ranges={})
        with pytest.raises(RecencyRequestInvalidError, match="unknown strategy_key"):
            validate_recency_request(
                strategies=[strategy], symbols=["SPY"], window_start_ms=0, window_end_ms=1000, data_policy=_DATA_POLICY
            )

    def test_rejects_a_strategy_that_is_not_recency_supported(self) -> None:
        strategy = StrategyGridConfig(strategy_key="deployment_validation", param_ranges={})
        with pytest.raises(RecencyRequestInvalidError, match="not recency-supported"):
            validate_recency_request(
                strategies=[strategy], symbols=["SPY"], window_start_ms=0, window_end_ms=1000, data_policy=_DATA_POLICY
            )

    def test_rejects_an_unknown_parameter_name(self) -> None:
        strategy = _strategy(not_a_real_param=ValueListRange((1.0,)))
        with pytest.raises(RecencyRequestInvalidError, match="parameters invalid"):
            validate_recency_request(
                strategies=[strategy], symbols=["SPY"], window_start_ms=0, window_end_ms=1000, data_policy=_DATA_POLICY
            )

    def test_rejects_an_out_of_bounds_parameter_value(self) -> None:
        # gap_bps is constrained to [0, 100]; 150 violates le=100.
        strategy = _strategy(gap_bps=LowHighStepRange(low=2.0, high=150.0, step=10.0))
        with pytest.raises(RecencyRequestInvalidError, match="parameters invalid"):
            validate_recency_request(
                strategies=[strategy], symbols=["SPY"], window_start_ms=0, window_end_ms=1000, data_policy=_DATA_POLICY
            )

    def test_rejects_empty_symbols(self) -> None:
        with pytest.raises(RecencyRequestInvalidError, match="symbols"):
            validate_recency_request(
                strategies=[_strategy(gap_bps=ValueListRange((2.0,)))],
                symbols=[],
                window_start_ms=0,
                window_end_ms=1000,
                data_policy=_DATA_POLICY,
            )

    def test_rejects_a_blank_symbol(self) -> None:
        with pytest.raises(RecencyRequestInvalidError, match="symbols"):
            validate_recency_request(
                strategies=[_strategy(gap_bps=ValueListRange((2.0,)))],
                symbols=["SPY", "  "],
                window_start_ms=0,
                window_end_ms=1000,
                data_policy=_DATA_POLICY,
            )

    def test_rejects_a_window_start_not_before_window_end(self) -> None:
        with pytest.raises(RecencyRequestInvalidError, match="window_start_ms"):
            validate_recency_request(
                strategies=[_strategy(gap_bps=ValueListRange((2.0,)))],
                symbols=["SPY"],
                window_start_ms=1000,
                window_end_ms=1000,
                data_policy=_DATA_POLICY,
            )

    def test_rejects_an_unsupported_data_policy_label(self) -> None:
        with pytest.raises(RecencyRequestInvalidError, match="data_policy"):
            validate_recency_request(
                strategies=[_strategy(gap_bps=ValueListRange((2.0,)))],
                symbols=["SPY"],
                window_start_ms=0,
                window_end_ms=1000,
                data_policy="ibkr-raw-regular-minute",
            )


def test_a_range_the_floats_cannot_resolve_is_rejected_at_preflight_not_in_the_worker() -> None:
    from app.research.recency.service import RecencyLaunchRejected, validate_launch
    from app.research.sweep.grid import LowHighStepRange, StrategyGridConfig

    # Endpoint neighbours resolve but an interior pair collapses to one float;
    # only materialising the grid detects it, so preflight must materialise.
    with pytest.raises(RecencyLaunchRejected, match="invalid parameter range"):
        validate_launch(
            launch_id="preflight-collapse",
            strategies=[
                StrategyGridConfig(
                    strategy_key="sma_crossover",
                    param_ranges={"short_window": LowHighStepRange(0.9999999999999944, 0.9999999999999949, 1.0880185641326534e-16)},
                )
            ],
            symbols=["SPY"],
            window_start_ms=1_700_000_000_000,
            window_end_ms=1_700_086_400_000,
            data_policy="polygon-adjusted-regular-minute",
            fill_mode="signal_bar_close",
            commission_per_order=1.0,
        )
