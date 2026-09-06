"""Recency Chart grid expansion — lazy cartesian product over symbols x
strategies x parameter combos, with a stable params_hash and a sanity
ceiling that rejects pathological/malformed grids before materialization.
"""

from __future__ import annotations

import inspect

import pytest

from app.research.sweep.grid import (
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

    def test_decimal_grid_has_no_binary_float_artifacts(self) -> None:
        # 0.15 + 3 * 0.15 in raw IEEE-754 float arithmetic is
        # 0.6000000000000001, not the decimal grid the caller specified.
        assert expand_param(LowHighStepRange(low=0.15, high=0.6, step=0.15)) == [0.15, 0.3, 0.45, 0.6]

    def test_decimal_grid_holds_over_a_finer_step(self) -> None:
        assert expand_param(LowHighStepRange(low=0.15, high=0.25, step=0.01)) == [
            0.15,
            0.16,
            0.17,
            0.18,
            0.19,
            0.2,
            0.21,
            0.22,
            0.23,
            0.24,
            0.25,
        ]

    def test_integer_range_stays_whole_floats(self) -> None:
        assert expand_param(LowHighStepRange(low=1.0, high=5.0, step=1.0)) == [1.0, 2.0, 3.0, 4.0, 5.0]


class TestRangeSizeMatchesExpandParam:
    """_range_size must count exactly what expand_param will emit — the UI's
    run estimate and the sanity ceiling both trust _range_size, so any
    over/under-count desyncs them from what actually executes."""

    @pytest.mark.parametrize(
        "range_spec",
        [
            LowHighStepRange(low=0.0, high=1.0, step=0.6),
            LowHighStepRange(low=0.0, high=1.0, step=0.1),
            LowHighStepRange(low=0.0, high=1.0, step=0.3),
            LowHighStepRange(low=1.0, high=2.2, step=0.5),
            LowHighStepRange(low=1.0, high=3.0, step=1.0),
            LowHighStepRange(low=0.0, high=10.0, step=3.0),
            LowHighStepRange(low=0.1, high=0.3, step=0.1),
            ValueListRange((0.1, 0.2, 0.3)),
        ],
    )
    def test_count_matches_expand_param_length(self, range_spec: LowHighStepRange | ValueListRange) -> None:
        assert _range_size(range_spec) == len(expand_param(range_spec))

    @pytest.mark.parametrize(
        ("range_spec", "expected_count"),
        [
            (LowHighStepRange(low=0.0, high=1.0, step=0.6), 2),
            (LowHighStepRange(low=0.0, high=1.0, step=0.1), 11),
            (LowHighStepRange(low=0.0, high=1.0, step=0.3), 4),
        ],
    )
    def test_count_matches_documented_awkward_spans(self, range_spec: LowHighStepRange, expected_count: int) -> None:
        assert _range_size(range_spec) == expected_count == len(expand_param(range_spec))


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

    def test_low_high_step_cell_hashes_identically_to_the_same_value_list_cell(self) -> None:
        """A leader chosen from a low/high/step sweep must be the same cell
        identity as a value-list submission of the same decimal numbers —
        otherwise the two submission styles disagree about which cell
        (search_id, params_hash) a given (strategy, params) pair is."""
        range_values = expand_param(LowHighStepRange(low=0.15, high=0.6, step=0.15))
        assert range_values == [0.15, 0.3, 0.45, 0.6]
        for range_value, value_list_value in zip(range_values, [0.15, 0.3, 0.45, 0.6], strict=True):
            a = params_hash("strategy_a", {"gap": range_value})
            b = params_hash("strategy_a", {"gap": value_list_value})
            assert a == b


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


class TestExpansionNeverPassesHigh:
    """The count is an exact decimal floor, so no emitted value exceeds ``high``
    — even when ``step`` is below the absolute float tolerance the old
    closed form used (which over-counted 0..1e-9 step 1e-10 as 21 values)."""

    def test_step_below_a_float_tolerance_still_stops_at_high(self) -> None:
        spec = LowHighStepRange(low=0.0, high=1e-9, step=1e-10)
        values = expand_param(spec)
        assert len(values) == 11 == _range_size(spec)
        assert values[-1] == 1e-9
        assert all(value <= 1e-9 for value in values)

    @pytest.mark.parametrize(
        ("low", "high", "step"),
        [(0.0, 1.0, 0.6), (0.15, 0.6, 0.15), (0.0, 1.0, 0.3), (-1.0, 1.0, 0.7), (0.5, 0.5, 0.1)],
    )
    def test_last_value_is_within_high(self, low: float, high: float, step: float) -> None:
        values = expand_param(LowHighStepRange(low=low, high=high, step=step))
        assert values[0] == low
        assert values[-1] <= high
        assert len(values) == _range_size(LowHighStepRange(low=low, high=high, step=step))


class TestPathologicalSteps:
    """Fat-fingered steps are refused the documented way, never a context error
    or a grid of duplicate cells (review findings on the Decimal expansion)."""

    def test_an_absurdly_small_step_is_refused_not_a_decimal_context_error(self) -> None:
        # 1 - 1e-28 is 1.0 in float, so the honest refusal is the resolution
        # guard — never decimal.InvalidOperation from a 28-digit quotient.
        spec = LowHighStepRange(low=0.0, high=1.0, step=1e-28)
        with pytest.raises(ValueError, match="collapse to the same float"):
            _range_size(spec)

    def test_a_huge_but_resolvable_count_is_exact(self) -> None:
        assert _range_size(LowHighStepRange(low=0.0, high=1e6, step=1e-9)) == 10**15 + 1

    def test_a_step_below_float_spacing_is_refused_rather_than_collapsing_cells(self) -> None:
        spec = LowHighStepRange(low=0.12345678901234566, high=0.12345678901234568, step=1e-18)
        with pytest.raises(ValueError, match="collapse to the same float"):
            _range_size(spec)
        with pytest.raises(ValueError, match="collapse to the same float"):
            expand_param(spec)

    def test_expanded_values_are_unique(self) -> None:
        values = expand_param(LowHighStepRange(low=0.15, high=0.25, step=0.01))
        assert len(set(values)) == len(values) == 11
