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

    @pytest.mark.parametrize(
        ("strategy_key", "param_ranges", "match"),
        [
            ("not_a_real_strategy", (), "unknown strategy_key"),
            ("deployment_validation", (), "not recency-supported"),
            ("ema_crossover_signal", (("not_a_real_param", ValueListRange((1.0,))),), "parameters invalid"),
            # gap_bps is constrained to [0, 100]; 150 violates le=100.
            ("ema_crossover_signal", (("gap_bps", LowHighStepRange(low=2.0, high=150.0, step=10.0)),), "parameters invalid"),
        ],
        ids=[
            "rejects_unknown_strategy_key",
            "rejects_a_strategy_that_is_not_recency_supported",
            "rejects_an_unknown_parameter_name",
            "rejects_an_out_of_bounds_parameter_value",
        ],
    )
    def test_rejects_an_invalid_strategy(
        self, strategy_key: str, param_ranges: tuple[tuple[str, object], ...], match: str
    ) -> None:
        strategy = StrategyGridConfig(strategy_key=strategy_key, param_ranges=dict(param_ranges))
        with pytest.raises(RecencyRequestInvalidError, match=match):
            validate_recency_request(
                strategies=[strategy], symbols=["SPY"], window_start_ms=0, window_end_ms=1000, data_policy=_DATA_POLICY
            )

    @pytest.mark.parametrize(
        ("symbols", "window_start_ms", "window_end_ms", "data_policy", "match"),
        [
            ((), 0, 1000, _DATA_POLICY, "symbols"),
            (("SPY", "  "), 0, 1000, _DATA_POLICY, "symbols"),
            (("SPY",), 1000, 1000, _DATA_POLICY, "window_start_ms"),
            (("SPY",), 0, 1000, "ibkr-raw-regular-minute", "data_policy"),
        ],
        ids=[
            "rejects_empty_symbols",
            "rejects_a_blank_symbol",
            "rejects_a_window_start_not_before_window_end",
            "rejects_an_unsupported_data_policy_label",
        ],
    )
    def test_rejects_an_invalid_request_envelope(
        self, symbols: tuple[str, ...], window_start_ms: int, window_end_ms: int, data_policy: str, match: str
    ) -> None:
        with pytest.raises(RecencyRequestInvalidError, match=match):
            validate_recency_request(
                strategies=[_strategy(gap_bps=ValueListRange((2.0,)))],
                symbols=list(symbols),
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                data_policy=data_policy,
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


def test_duplicate_strategy_keys_are_refused() -> None:
    """Two entries for one strategy expand onto overlapping cell identities a dispatched launch cannot tell apart (#3051)."""
    import pytest as _pytest

    from app.research.recency.service import RecencyLaunchRejected, validate_launch
    from app.research.sweep.grid import StrategyGridConfig, ValueListRange

    with _pytest.raises(RecencyLaunchRejected, match="duplicate entries: sma_crossover"):
        validate_launch(
            launch_id="launch-dup",
            strategies=[
                StrategyGridConfig(strategy_key="sma_crossover", param_ranges={"x": ValueListRange((1.0,))}),
                StrategyGridConfig(strategy_key="sma_crossover", param_ranges={"y": ValueListRange((2.0,))}),
            ],
            symbols=["SPY"],
            window_start_ms=0,
            window_end_ms=1,
            data_policy="polygon-adjusted-regular-minute",
            fill_mode="signal_bar_close",
            commission_per_order=0.0,
        )
