"""Smoke test: ema_crossover trusted template source is parseable and pinned to spec."""

from __future__ import annotations

import ast

import pytest

from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE


def test_source_is_non_empty_string() -> None:
    assert isinstance(EMA_CROSSOVER_SOURCE, str)
    assert len(EMA_CROSSOVER_SOURCE) > 100


def test_source_parses_as_valid_python() -> None:
    ast.parse(EMA_CROSSOVER_SOURCE)


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
    assert constants["GAP_MIN"] == pytest.approx(0.20)
    assert constants["RSI_LO"] == 50
    assert constants["RSI_HI"] == 70


def test_source_contains_required_handlers() -> None:
    """Verify Initialize, OnConsolidatedBar, OnEndOfAlgorithm exist."""
    tree = ast.parse(EMA_CROSSOVER_SOURCE)
    method_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            method_names.add(node.name)

    assert "Initialize" in method_names
    assert "OnConsolidatedBar" in method_names
    assert "OnEndOfAlgorithm" in method_names


def test_source_consolidates_15_minute_bars() -> None:
    # BAR_MINUTES moved to runtime GetParameter("bar_minutes") in Task 7;
    # the consolidator now uses the local variable.
    assert "TradeBarConsolidator" in EMA_CROSSOVER_SOURCE
    assert "timedelta(minutes=bar_minutes)" in EMA_CROSSOVER_SOURCE


def test_source_uses_wilders_rsi() -> None:
    assert "MovingAverageType.Wilders" in EMA_CROSSOVER_SOURCE


def test_source_liquidates_at_end() -> None:
    assert "OnEndOfAlgorithm" in EMA_CROSSOVER_SOURCE
    assert "Liquidate(self.symbol)" in EMA_CROSSOVER_SOURCE


def test_source_does_not_override_fill_model() -> None:
    """LEAN's default fill model matches Engine Lab's signal_bar_close per Task 1.0 spike — no override needed."""
    assert "SetFillModel" not in EMA_CROSSOVER_SOURCE
    assert "SignalBarCloseFillModel" not in EMA_CROSSOVER_SOURCE
    assert "MarketOnOpenOrder" not in EMA_CROSSOVER_SOURCE


def test_template_reads_new_parameters() -> None:
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE

    src = EMA_CROSSOVER_SOURCE
    assert 'GetParameter("symbol")' in src
    assert 'GetParameter("bar_minutes")' in src
    assert 'GetParameter("session")' in src
    assert 'GetParameter("adjustment")' in src


def test_template_no_longer_sets_wall_clock_warmup() -> None:
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE

    assert "SetWarmUp" not in EMA_CROSSOVER_SOURCE, (
        "Wall-clock warmup must be removed; both engines gate on indicator readiness only"
    )


def test_template_writes_observations_csv_and_state_csv() -> None:
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE

    assert "observations.csv" in EMA_CROSSOVER_SOURCE
    assert "state.csv" in EMA_CROSSOVER_SOURCE


def test_template_observations_csv_header_matches_gate1_spec() -> None:
    """observations.csv must carry full OHLCV per the Gate 1 comparator schema.

    Spec: docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md
    § "Gate 1 — Observations parity": ms_utc, open, high, low, close, volume.
    Must stay in sync with EXPECTED_HEADER in
    app/lean_sidecar/parity_matrix/observations_parity.py.
    """
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE

    assert "ms_utc,open,high,low,close,volume" in EMA_CROSSOVER_SOURCE


def test_template_state_csv_header_matches_spec() -> None:
    """state.csv must have exactly the columns the parity test asserts on."""
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE

    assert "ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal" in EMA_CROSSOVER_SOURCE


def test_template_rejects_non_15_bar_minutes() -> None:
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE

    # Defense-in-depth check at the strategy layer.
    assert "bar_minutes" in EMA_CROSSOVER_SOURCE
    assert "raise ValueError" in EMA_CROSSOVER_SOURCE


def test_template_pins_interactive_brokers_margin_brokerage() -> None:
    """The matrix's LEAN-side oracle locks IBKR margin brokerage so Gate 3
    with assert_fees=True is a meaningful comparison against the engine's
    IbkrEquityCommissionModel. The cell manifest's broker block depends on
    this template being unambiguous about which fee model LEAN ran."""
    assert "SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)" in EMA_CROSSOVER_SOURCE


def test_template_pins_brokerage_before_subscriptions() -> None:
    """LEAN docs: SetBrokerageModel must precede AddEquity so the security's
    fee/fill models are configured by IB at subscribe time."""
    src = EMA_CROSSOVER_SOURCE
    sbm_idx = src.index("SetBrokerageModel(")
    add_equity_idx = src.index("self.AddEquity(")
    assert sbm_idx < add_equity_idx, "SetBrokerageModel must appear before AddEquity"
