"""Recency Chart grid expansion — lazy cartesian product over symbols x
strategies x parameter combos, with a stable params_hash and a sanity
ceiling that rejects pathological/malformed grids before materialization.
"""

from __future__ import annotations

import inspect

import pytest

from app.research.recency.grid import (
    LowHighStepRange,
    RecencyGridTooLargeError,
    StrategyGridConfig,
    ValueListRange,
    _range_size,
    expand_grid,
    expand_param,
    params_hash,
)


class TestExpandParamValueList:
    def test_returns_values_in_given_order(self) -> None:
        assert expand_param(ValueListRange((0.1, 0.2, 0.3))) == [0.1, 0.2, 0.3]

    def test_single_value_list_is_a_fixed_value(self) -> None:
        assert expand_param(ValueListRange((0.2,))) == [0.2]

    def test_rejects_empty_value_list(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            expand_param(ValueListRange(()))


class TestExpandParamLowHighStep:
    def test_returns_inclusive_ascending_sequence(self) -> None:
        assert expand_param(LowHighStepRange(low=1.0, high=3.0, step=1.0)) == [1.0, 2.0, 3.0]

    def test_high_not_hit_exactly_by_step_is_excluded(self) -> None:
        # 1.0 -> 1.0, 1.5, 2.0 ; 2.5 would exceed high=2.2
        assert expand_param(LowHighStepRange(low=1.0, high=2.2, step=0.5)) == [1.0, 1.5, 2.0]

    def test_rejects_non_positive_step(self) -> None:
        with pytest.raises(ValueError, match="step"):
            expand_param(LowHighStepRange(low=1.0, high=2.0, step=0.0))

    def test_rejects_low_greater_than_high(self) -> None:
        with pytest.raises(ValueError, match="low"):
            expand_param(LowHighStepRange(low=5.0, high=1.0, step=1.0))


class TestRangeSizeMatchesExpandParam:
    """_range_size must count exactly what expand_param will emit — the UI's
    run estimate and the sanity ceiling both trust _range_size, so any
    over/under-count desyncs them from what actually executes."""

    @pytest.mark.parametrize(
        "range_spec",
        [
            LowHighStepRange(low=0.0, high=1.0, step=0.6),
            LowHighStepRange(low=1.0, high=2.2, step=0.5),
            LowHighStepRange(low=1.0, high=3.0, step=1.0),
            LowHighStepRange(low=0.0, high=10.0, step=3.0),
            LowHighStepRange(low=0.1, high=0.3, step=0.1),
            ValueListRange((0.1, 0.2, 0.3)),
        ],
    )
    def test_count_matches_expand_param_length(self, range_spec: LowHighStepRange | ValueListRange) -> None:
        assert _range_size(range_spec) == len(expand_param(range_spec))


class TestParamsHash:
    def test_stable_regardless_of_dict_key_order(self) -> None:
        a = params_hash("ema_crossover_2_bps", {"gap_bps": 2.0, "rsi_min": 50.0})
        b = params_hash("ema_crossover_2_bps", {"rsi_min": 50.0, "gap_bps": 2.0})
        assert a == b

    def test_differs_when_a_value_differs(self) -> None:
        a = params_hash("ema_crossover_2_bps", {"gap_bps": 2.0, "rsi_min": 50.0})
        b = params_hash("ema_crossover_2_bps", {"gap_bps": 3.0, "rsi_min": 50.0})
        assert a != b

    def test_differs_across_strategy_keys_for_identical_params(self) -> None:
        a = params_hash("strategy_a", {"gap": 0.2})
        b = params_hash("strategy_b", {"gap": 0.2})
        assert a != b


class TestExpandGrid:
    def test_cartesian_product_across_symbols_strategies_and_params(self) -> None:
        configs = [
            StrategyGridConfig(
                strategy_key="ema_crossover_2_bps",
                param_ranges={
                    "gap_bps": ValueListRange((1.0, 2.0)),
                    "rsi_min": ValueListRange((50.0,)),
                },
            )
        ]
        runs = list(expand_grid(configs, symbols=["SPY", "AAPL"]))
        assert len(runs) == 4  # 2 symbols x (2 gap values x 1 rsi value)
        assert {r.symbol for r in runs} == {"SPY", "AAPL"}
        assert {r.params["gap_bps"] for r in runs} == {1.0, 2.0}

    def test_each_run_carries_a_stable_params_hash(self) -> None:
        configs = [
            StrategyGridConfig(
                strategy_key="ema_crossover_2_bps",
                param_ranges={"gap_bps": ValueListRange((2.0,))},
            )
        ]
        runs = list(expand_grid(configs, symbols=["SPY"]))
        assert len(runs) == 1
        assert runs[0].params_hash == params_hash("ema_crossover_2_bps", {"gap_bps": 2.0})

    def test_ordering_is_deterministic_across_repeated_calls(self) -> None:
        configs = [
            StrategyGridConfig(
                strategy_key="ema_crossover_2_bps",
                param_ranges={"gap_bps": ValueListRange((1.0, 2.0, 3.0))},
            )
        ]
        first = [(r.symbol, r.strategy_key, r.params_hash) for r in expand_grid(configs, symbols=["SPY", "QQQ"])]
        second = [(r.symbol, r.strategy_key, r.params_hash) for r in expand_grid(configs, symbols=["SPY", "QQQ"])]
        assert first == second

    def test_expansion_is_lazy(self) -> None:
        configs = [
            StrategyGridConfig(
                strategy_key="ema_crossover_2_bps",
                param_ranges={"gap_bps": LowHighStepRange(low=0.0, high=999.0, step=1.0)},
            )
        ]
        result = expand_grid(configs, symbols=["SPY"])
        assert inspect.isgenerator(result) or hasattr(result, "__next__")
        first = next(iter(result))
        assert first.symbol == "SPY"

    def test_rejects_a_grid_past_the_sanity_ceiling_before_iterating(self) -> None:
        configs = [
            StrategyGridConfig(
                strategy_key="ema_crossover_2_bps",
                param_ranges={"gap_bps": LowHighStepRange(low=0.0, high=10_000_000.0, step=0.0001)},
            )
        ]
        with pytest.raises(RecencyGridTooLargeError):
            expand_grid(configs, symbols=["SPY"])

    def test_empty_symbols_produces_no_runs(self) -> None:
        configs = [
            StrategyGridConfig(
                strategy_key="ema_crossover_2_bps",
                param_ranges={"gap_bps": ValueListRange((2.0,))},
            )
        ]
        assert list(expand_grid(configs, symbols=[])) == []

    def test_empty_strategies_produces_no_runs(self) -> None:
        assert list(expand_grid([], symbols=["SPY"])) == []
