"""Smoke test: ema_crossover trusted template source is parseable and pinned to spec."""

from __future__ import annotations

import ast

import pytest

from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE
from app.lean_sidecar.trusted_samples.ema_crossover_2_bps import (
    EMA_CROSSOVER_2_BPS_SOURCE,
)


def test_two_bps_template_reads_configurable_gap_and_rsi_gates() -> None:
    ast.parse(EMA_CROSSOVER_2_BPS_SOURCE)
    assert "GAP_BPS_DEFAULT = 2.0" in EMA_CROSSOVER_2_BPS_SOURCE
    assert 'GetParameter("gap_bps")' in EMA_CROSSOVER_2_BPS_SOURCE
    assert 'GetParameter("rsi_min")' in EMA_CROSSOVER_2_BPS_SOURCE
    assert 'GetParameter("rsi_max")' in EMA_CROSSOVER_2_BPS_SOURCE
    assert "gap_bps = 10000.0 * (fast - slow) / slow" in EMA_CROSSOVER_2_BPS_SOURCE
    assert "normalized_gap_ok = gap_bps >= self.gap_bps_min" in EMA_CROSSOVER_2_BPS_SOURCE
    assert "rsi_ok = self.rsi_lo <= rsi <= self.rsi_hi" in EMA_CROSSOVER_2_BPS_SOURCE


def test_class_constants_match_spec() -> None:
    """Pinned to PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json."""
    tree = ast.parse(EMA_CROSSOVER_SOURCE)
    constants: dict[str, int | float] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MyAlgorithm":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Constant)
                ):
                    constants[stmt.targets[0].id] = stmt.value.value

    assert constants["FAST_PERIOD"] == 5
    assert constants["SLOW_PERIOD"] == 10
    assert constants["RSI_PERIOD"] == 14
    # BAR_MINUTES moved to a runtime GetParameter("bar_minutes") in Task 7;
    # EXIT_BARS remains a class constant because its value (5 bars) is
    # strategy logic, not a data-contract parameter.
    assert constants["EXIT_BARS"] == 5
    assert constants["GAP_DEFAULT"] == pytest.approx(0.20)
    assert constants["GAP_BPS_DEFAULT"] == pytest.approx(0.0)
    assert constants["RSI_LO_DEFAULT"] == pytest.approx(50.0)
    assert constants["RSI_HI_DEFAULT"] == pytest.approx(70.0)


def test_source_pins_the_template_algorithm_shape() -> None:
    tree = ast.parse(EMA_CROSSOVER_SOURCE)
    method_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            method_names.add(node.name)

    # Verify Initialize, OnConsolidatedBar, OnEndOfAlgorithm exist.
    assert "Initialize" in method_names, "the template defines the Initialize handler"
    assert "OnConsolidatedBar" in method_names, "the template defines the OnConsolidatedBar handler"
    assert "OnEndOfAlgorithm" in method_names, "the template defines the OnEndOfAlgorithm handler"

    # BAR_MINUTES moved to runtime GetParameter("bar_minutes") in Task 7;
    # the consolidator now uses the local variable.
    assert "TradeBarConsolidator" in EMA_CROSSOVER_SOURCE, "the template consolidates with a TradeBarConsolidator"
    assert "timedelta(minutes=bar_minutes)" in EMA_CROSSOVER_SOURCE, "the consolidator period is bar_minutes"

    assert "MovingAverageType.Wilders" in EMA_CROSSOVER_SOURCE, "the RSI uses Wilder's smoothing"

    assert "OnEndOfAlgorithm" in EMA_CROSSOVER_SOURCE, "the template handles the end of the algorithm"
    assert "Liquidate(self.symbol)" in EMA_CROSSOVER_SOURCE, "the template liquidates at the end"

    # LEAN's default fill model matches Engine Lab's signal_bar_close per Task 1.0 spike — no override needed.
    assert "SetFillModel" not in EMA_CROSSOVER_SOURCE, "the template does not override the fill model"
    assert "SignalBarCloseFillModel" not in EMA_CROSSOVER_SOURCE, "the template does not use SignalBarCloseFillModel"
    assert "MarketOnOpenOrder" not in EMA_CROSSOVER_SOURCE, "the template does not place MarketOnOpenOrder"

    assert "SetWarmUp" not in EMA_CROSSOVER_SOURCE, (
        "Wall-clock warmup must be removed; both engines gate on indicator readiness only"
    )

    # The matrix's LEAN-side oracle locks IBKR margin brokerage so Gate 3
    # with assert_fees=True is a meaningful comparison against the engine's
    # IbkrEquityCommissionModel. The cell manifest's broker block depends on
    # this template being unambiguous about which fee model LEAN ran.
    assert "SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)" in EMA_CROSSOVER_SOURCE, (
        "the template pins the Interactive Brokers margin brokerage"
    )


def test_template_reads_new_parameters() -> None:
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE

    src = EMA_CROSSOVER_SOURCE
    assert 'GetParameter("symbol")' in src
    assert 'GetParameter("bar_minutes")' in src
    assert 'GetParameter("session")' in src
    assert 'GetParameter("adjustment")' in src
    for name in ("gap", "gap_bps", "rsi_min", "rsi_max"):
        assert f'GetParameter("{name}")' in src
    assert "absolute_gap_ok" in src
    assert "normalized_gap_ok" in src
    assert "math.isfinite" in src


def test_signal_template_emits_the_parameterized_base_source() -> None:
    """The named twin's source identity must include its imported implementation."""
    from app.lean_sidecar.trusted_samples.ema_crossover_signal import EMA_CROSSOVER_SIGNAL_SOURCE

    assert EMA_CROSSOVER_SIGNAL_SOURCE is EMA_CROSSOVER_SOURCE
    assert 'GetParameter("gap_bps")' in EMA_CROSSOVER_SIGNAL_SOURCE


def test_template_writes_observations_and_state_csv_with_the_pinned_headers() -> None:
    assert "observations.csv" in EMA_CROSSOVER_SOURCE, "the template writes observations.csv"
    assert "state.csv" in EMA_CROSSOVER_SOURCE, "the template writes state.csv"

    # observations.csv must carry full OHLCV per the Gate 1 comparator schema.
    # Gate 1 comparator columns: ms_utc, open, high, low, close, volume.
    # Must stay in sync with EXPECTED_HEADER in
    # app/lean_sidecar/parity_matrix/observations_parity.py.
    assert "ms_utc,open,high,low,close,volume" in EMA_CROSSOVER_SOURCE, (
        "observations.csv carries the Gate 1 comparator header"
    )

    # state.csv must have exactly the columns the parity test asserts on.
    assert "ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal" in EMA_CROSSOVER_SOURCE, (
        "state.csv carries the columns the parity test asserts on"
    )


def test_template_pins_brokerage_before_subscriptions() -> None:
    """LEAN docs: SetBrokerageModel must precede AddEquity so the security's
    fee/fill models are configured by IB at subscribe time."""
    src = EMA_CROSSOVER_SOURCE
    sbm_idx = src.index("SetBrokerageModel(")
    add_equity_idx = src.index("self.AddEquity(")
    assert sbm_idx < add_equity_idx, "SetBrokerageModel must appear before AddEquity"
