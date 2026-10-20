"""Recency Chart grid expansion — lazy cartesian product over symbols x
strategies x parameter combos, with a stable params_hash and a sanity
ceiling that rejects pathological/malformed grids before materialization.
"""

from __future__ import annotations

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
    @pytest.mark.parametrize(
        ("range_spec", "expected"),
        [
            pytest.param(ValueListRange((0.1, 0.2, 0.3)), (0.1, 0.2, 0.3), id="returns_values_in_given_order"),
            pytest.param(ValueListRange((0.2,)), (0.2,), id="single_value_list_is_a_fixed_value"),
        ],
    )
    def test_expands_to_its_values(self, range_spec: ValueListRange, expected: tuple[float, ...]) -> None:
        assert expand_param(range_spec) == list(expected)

    def test_rejects_empty_value_list(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            expand_param(ValueListRange(()))


class TestExpandParamLowHighStep:
    @pytest.mark.parametrize(
        ("range_spec", "expected"),
        [
            pytest.param(LowHighStepRange(low=1.0, high=3.0, step=1.0), (1.0, 2.0, 3.0), id="returns_inclusive_ascending_sequence"),
            # 1.0 -> 1.0, 1.5, 2.0 ; 2.5 would exceed high=2.2
            pytest.param(LowHighStepRange(low=1.0, high=2.2, step=0.5), (1.0, 1.5, 2.0), id="high_not_hit_exactly_by_step_is_excluded"),
            # 0.15 + 3 * 0.15 in raw IEEE-754 float arithmetic is
            # 0.6000000000000001, not the decimal grid the caller specified.
            pytest.param(
                LowHighStepRange(low=0.15, high=0.6, step=0.15),
                (0.15, 0.3, 0.45, 0.6),
                id="decimal_grid_has_no_binary_float_artifacts",
            ),
            pytest.param(
                LowHighStepRange(low=0.15, high=0.25, step=0.01),
                (0.15, 0.16, 0.17, 0.18, 0.19, 0.2, 0.21, 0.22, 0.23, 0.24, 0.25),
                id="decimal_grid_holds_over_a_finer_step",
            ),
        ],
    )
    def test_expands_to_the_expected_sequence(self, range_spec: LowHighStepRange, expected: tuple[float, ...]) -> None:
        assert expand_param(range_spec) == list(expected)

    @pytest.mark.parametrize(
        ("range_spec", "match"),
        [
            pytest.param(LowHighStepRange(low=1.0, high=2.0, step=0.0), "step", id="rejects_non_positive_step"),
            pytest.param(LowHighStepRange(low=5.0, high=1.0, step=1.0), "low", id="rejects_low_greater_than_high"),
        ],
    )
    def test_rejects_an_invalid_range(self, range_spec: LowHighStepRange, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            expand_param(range_spec)


class TestRangeSizeMatchesExpandParam:
    """_range_size must count exactly what expand_param will emit — the UI's
    run estimate and the sanity ceiling both trust _range_size, so any
    over/under-count desyncs them from what actually executes."""

    @pytest.mark.parametrize(
        ("range_spec", "expected_count"),
        [
            pytest.param(LowHighStepRange(low=0.0, high=1.0, step=0.6), 2, id="documented_0_to_1_step_0_6"),
            pytest.param(LowHighStepRange(low=0.0, high=1.0, step=0.1), 11, id="documented_0_to_1_step_0_1"),
            pytest.param(LowHighStepRange(low=0.0, high=1.0, step=0.3), 4, id="documented_0_to_1_step_0_3"),
            pytest.param(LowHighStepRange(low=1.0, high=2.2, step=0.5), 3, id="1_to_2_2_step_0_5"),
            pytest.param(LowHighStepRange(low=1.0, high=3.0, step=1.0), 3, id="1_to_3_step_1"),
            pytest.param(LowHighStepRange(low=0.0, high=10.0, step=3.0), 4, id="0_to_10_step_3"),
            pytest.param(LowHighStepRange(low=0.1, high=0.3, step=0.1), 3, id="0_1_to_0_3_step_0_1"),
            pytest.param(ValueListRange((0.1, 0.2, 0.3)), 3, id="value_list"),
        ],
    )
    def test_count_matches_expand_param_length(self, range_spec: LowHighStepRange | ValueListRange, expected_count: int) -> None:
        assert _range_size(range_spec) == len(expand_param(range_spec)), "the count equals the length expand_param emits"
        assert _range_size(range_spec) == expected_count, "the count is the documented count for the span"


class TestParamsHash:
    def test_identity_follows_the_strategy_key_and_values_not_dict_order(self) -> None:
        a = params_hash("ema_crossover_2_bps", {"gap_bps": 2.0, "rsi_min": 50.0})

        assert params_hash("ema_crossover_2_bps", {"rsi_min": 50.0, "gap_bps": 2.0}) == a, "stable regardless of dict key order"
        assert params_hash("ema_crossover_2_bps", {"gap_bps": 3.0, "rsi_min": 50.0}) != a, "differs when a value differs"
        assert params_hash("strategy_a", {"gap": 0.2}) != params_hash("strategy_b", {"gap": 0.2}), "differs across strategy keys for identical params"

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

    def test_rejects_a_grid_past_the_sanity_ceiling_before_iterating(self) -> None:
        configs = [
            StrategyGridConfig(
                strategy_key="ema_crossover_2_bps",
                param_ranges={"gap_bps": LowHighStepRange(low=0.0, high=10_000_000.0, step=0.0001)},
            )
        ]
        with pytest.raises(RecencyGridTooLargeError):
            expand_grid(configs, symbols=["SPY"])

    def test_an_empty_symbol_or_strategy_list_produces_no_runs(self) -> None:
        configs = [
            StrategyGridConfig(
                strategy_key="ema_crossover_2_bps",
                param_ranges={"gap_bps": ValueListRange((2.0,))},
            )
        ]
        assert list(expand_grid(configs, symbols=[])) == [], "empty symbols produces no runs"
        assert list(expand_grid([], symbols=["SPY"])) == [], "empty strategies produces no runs"


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

    @pytest.mark.parametrize(
        "spec",
        [
            # 1 - 1e-28 is 1.0 in float, so the honest refusal is the resolution
            # guard — never decimal.InvalidOperation from a 28-digit quotient.
            pytest.param(
                LowHighStepRange(low=0.0, high=1.0, step=1e-28),
                id="an_absurdly_small_step_is_refused_not_a_decimal_context_error",
            ),
            pytest.param(
                LowHighStepRange(low=0.12345678901234566, high=0.12345678901234568, step=1e-18),
                id="a_step_below_float_spacing_is_refused_rather_than_collapsing_cells",
            ),
        ],
    )
    def test_an_unresolvable_step_is_refused(self, spec: LowHighStepRange) -> None:
        with pytest.raises(ValueError, match="collapse to the same float"):
            _range_size(spec)
        with pytest.raises(ValueError, match="collapse to the same float"):
            expand_param(spec)

    def test_a_huge_but_resolvable_count_is_exact(self) -> None:
        assert _range_size(LowHighStepRange(low=0.0, high=1e6, step=1e-9)) == 10**15 + 1

    def test_a_single_cell_range_is_not_a_resolution_problem(self) -> None:
        assert expand_param(LowHighStepRange(low=1.0, high=1.0, step=1e-20)) == [1.0]

    def test_a_nan_step_is_refused_as_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            _range_size(LowHighStepRange(low=0.0, high=1.0, step=float("nan")))


    def test_a_long_span_is_not_rounded_up_to_an_extra_cell(self) -> None:
        # 1e-30 + 9 * 0.1 exceeds 0.9 exactly; a 28-digit working precision
        # would round the span to 0.9 and admit a tenth cell (review finding).
        spec = LowHighStepRange(low=1e-30, high=0.9, step=0.1)
        assert _range_size(spec) == 9
        values = expand_param(spec)
        assert len(values) == 9
        assert values[-1] == 0.8
