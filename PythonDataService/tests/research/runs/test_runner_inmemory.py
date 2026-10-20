"""In-memory runner tests — A1's reproducibility gate.

Storage and HTTP boundaries are covered by separate suites in A2/A3.
This file proves that ``run_strategy_spec`` produces deterministic
ledger identity and result hashes for the same input contract — the
core invariant Phase A is built on.

The test injects a synthetic data source (``FakeDataReader``) so the
run is hermetic and runs in milliseconds. The shape of the spec
matches the canonical SPY EMA fixture but is constructed inline on a
``TEST`` symbol so the suite doesn't break if the SPY fixture changes
its declared symbol.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.data_lake import run_materialization
from app.data_lake.path_policy import lake_subpath
from app.data_lake.run_materialization import EngineRunMaterialization, LakeMaterializationError
from app.engine.data.availability import MissingSessionsError, check_availability
from app.engine.engine import EquitySnapshot
from app.engine.strategy.spec import StrategySpec
from app.engine.strategy.spec.tests._parity_helpers import (
    FakeDataReader,
    build_minute_bars,
    closes_for_spy_ema,
)
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.runs import RunRequest, run_date_to_ms, run_strategy_spec
from app.research.runs.ledger import RunLedger
from app.research.runs.result import BacktestRunResult
from app.research.runs.runner import _summarize_metrics
from app.services.spec_run_data import materialize_spec_data_source
from app.utils.timestamps import to_ms_utc
from tests._helpers.lean_store import seed_store_day


def _build_test_spec(
    *,
    fast_period: int = 5,
    slow_period: int = 10,
    rsi_period: int = 14,
    rsi_lo: float = 50.0,
    rsi_hi: float = 70.0,
    bars_to_hold: int = 5,
) -> StrategySpec:
    """Construct an EMA-crossover spec on the synthetic ``TEST`` symbol.

    Mirrors the canonical SPY fixture's structure (FreshCross + gap +
    RSI band + N-bar hold) so the runner exercises the same evaluator
    paths as the SPY fixture would, without coupling the test to that
    fixture's declared symbol.
    """
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "TEST EMA crossover",
            "symbols": ["TEST"],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "fast", "kind": "EMA", "period": fast_period, "source": "close"},
                {"id": "slow", "kind": "EMA", "period": slow_period, "source": "close"},
                {
                    "id": "rsi",
                    "kind": "RSI",
                    "period": rsi_period,
                    "source": "close",
                    "ma_type": "wilders",
                },
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {"kind": "FreshCross", "left": "fast", "right": "slow", "direction": "up"},
                    {
                        "kind": "IndicatorComparison",
                        "left": {
                            "kind": "Subtract",
                            "left": {"kind": "IndicatorRef", "indicator": "fast"},
                            "right": {"kind": "IndicatorRef", "indicator": "slow"},
                        },
                        "op": ">=",
                        "right": {"kind": "Const", "value": 0.20},
                    },
                    {
                        "kind": "IndicatorBetween",
                        "indicator": "rsi",
                        "lo": rsi_lo,
                        "hi": rsi_hi,
                        "inclusive": True,
                    },
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [{"kind": "BarsSinceEntry", "op": ">=", "value": bars_to_hold}],
            },
            "diagnostics": {"snapshot_at_entry": ["fast", "slow", "rsi"]},
        }
    )


@pytest.fixture
def fake_data_factory():
    """Build a deterministic synthetic data source on demand.

    The factory is parameterized by symbol/start/end like the real
    LEAN factory in ``app/routers/spec_strategy.py``; the same
    pre-generated bar list is returned every time so two runs see
    identical inputs.
    """
    # ``build_minute_bars`` hard-codes ``symbol="TEST"`` (parity helper
    # constant) which matches the inline spec below. ``TradeBar`` is a
    # frozen dataclass so any rename would have to rebuild the list.
    bars = build_minute_bars(closes_for_spy_ema(2000))

    def factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=bars)

    return factory


def _run(
    spec: StrategySpec,
    factory,
    *,
    start: date = date(2024, 1, 2),
    end: date = date(2024, 12, 31),
    fill_mode: str = "signal_bar_close",
    commission: float = 0.0,
    seed: int = 0,
    data_root_revision: str = "test-revision-1",
    run_id: str | None = None,
) -> tuple[RunLedger, BacktestRunResult]:
    return run_strategy_spec(
        RunRequest(
            spec=spec,
            start_ms=run_date_to_ms(start),
            end_ms=run_date_to_ms(end),
            fill_mode=fill_mode,
            commission_per_order=commission,
            random_seed=seed,
        ),
        data_source_factory=factory,
        data_root_revision=data_root_revision,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Core acceptance gate.
# ---------------------------------------------------------------------------
def test_data_snapshot_id_changes_with_the_data_root_revision_and_not_with_the_fill_mode(fake_data_factory):
    """The data root revision moves the data snapshot id; the fill mode does not.

    Fill mode is part of run identity but not data identity.
    Two runs with the same spec and same data window but different fill
    modes have different ``result_hash`` (different fill timing → different
    trade prices) but identical ``data_snapshot_id`` (same input bars).
    """
    spec = _build_test_spec()
    ledger_a, _ = _run(spec, fake_data_factory, data_root_revision="rev-1")
    ledger_b, _ = _run(spec, fake_data_factory, data_root_revision="rev-2")

    assert ledger_a.strategy_spec_hash == ledger_b.strategy_spec_hash, "data root revision: spec hash is unchanged"
    assert ledger_a.data_snapshot_id != ledger_b.data_snapshot_id, (
        "changing the data root revision changes the data snapshot id"
    )

    ledger_a, _ = _run(spec, fake_data_factory, fill_mode="signal_bar_close")
    ledger_b, _ = _run(spec, fake_data_factory, fill_mode="next_bar_open")

    assert ledger_a.strategy_spec_hash == ledger_b.strategy_spec_hash, "fill mode: spec hash is unchanged"
    assert ledger_a.data_snapshot_id == ledger_b.data_snapshot_id, "fill mode: data snapshot id is stable"
    assert ledger_a.fill_mode != ledger_b.fill_mode, "fill mode: the ledger records the changed fill mode"


@pytest.mark.parametrize(
    ("canonical_mode", "spelling"),
    [
        pytest.param("signal_bar_close", "SIGNAL-BAR-CLOSE", id="signal-bar-close-hyphen-upper"),
        pytest.param("signal_bar_close", "Signal_Bar_Close", id="signal-bar-close-underscore-mixed-case"),
        pytest.param("next_session_open", "NEXT-SESSION-OPEN", id="next-session-open-hyphen-upper"),
    ],
)
def test_fill_mode_spellings_normalize_to_one_ledger_identity(fake_data_factory, canonical_mode: str, spelling: str):
    """The router admits ``"SIGNAL-BAR-CLOSE"``; the runner stores the
    normalized form and treats every variant as the same run identity.
    """
    spec = _build_test_spec()
    canonical, _ = _run(spec, fake_data_factory, fill_mode=canonical_mode)
    variant, _ = _run(spec, fake_data_factory, fill_mode=spelling)

    assert canonical.status == "completed", "the canonical spelling completes"
    # Every spelling must produce the same canonical form so the runs
    # ledger-identify identically (R5 hash-identity invariant).
    assert canonical.fill_mode == canonical_mode, "the canonical spelling is stored as is"
    assert variant.fill_mode == canonical_mode, "the variant normalizes to the canonical fill_mode in the ledger"
    # And produce identical content hashes.
    assert canonical.result_hash == variant.result_hash, "the variant produces the canonical content hash"


def test_slippage_per_share_actually_changes_fills(fake_data_factory):
    """``slippage_per_share`` must propagate into FillModel — recording
    the value in the ledger without applying it would silently invalidate
    any slippage-sensitivity research. Regression test for PR #107.
    """
    spec = _build_test_spec()

    def run_with_slippage(slip: float):
        return run_strategy_spec(
            RunRequest(
                spec=spec,
                start_ms=run_date_to_ms(date(2024, 1, 2)),
                end_ms=run_date_to_ms(date(2024, 12, 31)),
                slippage_per_share=slip,
            ),
            data_source_factory=fake_data_factory,
            data_root_revision="test-revision-1",
        )

    no_slip_ledger, no_slip_result = run_with_slippage(0.0)
    with_slip_ledger, with_slip_result = run_with_slippage(0.05)

    # Both runs must have at least one trade for the assertion to be
    # meaningful — the synthetic series fires the EMA rule a handful of
    # times, but skip if zero (the assertion is then vacuous).
    if not no_slip_result.trades or not with_slip_result.trades:
        pytest.skip("synthetic series produced zero trades")

    # Identity columns differ — the ledger records the slippage.
    assert no_slip_ledger.slippage_per_share == 0.0
    assert with_slip_ledger.slippage_per_share == 0.05
    # And so do the engine outputs — at least one trade price differs.
    no_slip_prices = [(t.entry_price, t.exit_price) for t in no_slip_result.trades]
    with_slip_prices = [(t.entry_price, t.exit_price) for t in with_slip_result.trades]
    assert no_slip_prices != with_slip_prices
    assert no_slip_ledger.result_hash != with_slip_ledger.result_hash


# ---------------------------------------------------------------------------
# Result shape.
# ---------------------------------------------------------------------------
def test_a_normal_run_reports_its_curves_metrics_and_bars_consumed(fake_data_factory):
    spec = _build_test_spec()
    ledger, result = _run(spec, fake_data_factory)

    assert ledger.status == "completed", "equity curve: the run completes"
    assert len(result.equity_curve) > 0, "equity curve is not empty"
    assert len(result.drawdown_curve) == len(result.equity_curve), "drawdown curve is as long as the equity curve"

    # Equity curve timestamps are monotonically increasing int64 ms UTC.
    timestamps = [p.timestamp_ms for p in result.equity_curve]
    assert timestamps == sorted(timestamps), "equity curve timestamps are increasing"
    assert all(isinstance(t, int) for t in timestamps), "equity curve timestamps are ints"

    # Drawdown is in [0, 1].
    for d in result.drawdown_curve:
        assert 0.0 <= d.drawdown_pct <= 1.0, "drawdown is in [0, 1]"

    # Initial equity matches the request.
    assert result.initial_cash == 100_000.0, "initial equity matches the request"

    metrics = result.metrics

    # Counts agree with the trade list.
    assert metrics.total_trades == len(result.trades), "metrics total trades agree with the trade list"
    assert metrics.winning_trades == sum(1 for t in result.trades if t.result == "WIN"), (
        "metrics winning trades agree with the trade list"
    )
    assert metrics.losing_trades == sum(1 for t in result.trades if t.result == "LOSS"), (
        "metrics losing trades agree with the trade list"
    )

    # win_rate is consistent with counts when there are trades.
    if metrics.total_trades > 0:
        expected = metrics.winning_trades / metrics.total_trades
        assert metrics.win_rate is not None, "metrics win_rate is set when there are trades"
        assert abs(metrics.win_rate - expected) < 1e-12, "metrics win_rate is consistent with the counts"

    # The synthetic fixture builds 2,000 bars; the runner filters by the
    # spec's date window but the FakeDataReader yields everything inside
    # [2024-01-02, 2024-12-31] (which covers the whole list).
    assert result.bars_consumed > 0, "the result reports the engine-input bars it consumed"
    assert isinstance(result.bars_consumed, int), "bars_consumed is an int"
    assert ledger.status == "completed", "a run that consumed bars completes"


def test_result_trades_have_bars_held(fake_data_factory):
    """Every trade carries an integer ``bars_held`` for Phase D.

    The synthetic series is tuned to fire the EMA rule a handful of times
    over 2,000 bars, so we assert ``bars_held > 0`` per trade rather than
    a specific count (the count is asserted by the parity tests against
    the hand-coded twin).
    """
    spec = _build_test_spec(bars_to_hold=5)
    _, result = _run(spec, fake_data_factory)

    if not result.trades:
        pytest.skip("synthetic series produced zero trades — assertion vacuously holds")

    for trade in result.trades:
        assert trade.bars_held > 0
        assert isinstance(trade.bars_held, int)


def test_exposure_uses_consolidated_bar_resolution():
    """Exposure converts held 15-minute bars into the minute equity-curve unit.

    Regression: ``bars_held_total`` is counted in consolidated bars,
    while ``total_bars`` is the engine's minute-bar equity curve
    length. Dividing directly understates exposure by ``resolution``.
    """
    ts = datetime(2024, 1, 2, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    equity_curve = [
        EquitySnapshot(
            timestamp_ms=to_ms_utc(ts),
            equity=Decimal("100000"),
            cash=Decimal("100000"),
            holdings_value=Decimal("0"),
        )
    ]

    metrics = _summarize_metrics(
        initial_cash=100_000.0,
        final_equity=100_000.0,
        trades=[],
        equity_curve=equity_curve,
        bars_held_total=5,
        total_bars=100,
        resolution_minutes=15,
    )

    assert metrics.exposure_pct == pytest.approx(0.75, abs=1e-12, rel=0)


# ---------------------------------------------------------------------------
# Failure path.
# ---------------------------------------------------------------------------
def test_a_data_source_that_raises_or_yields_no_bars_fails_the_ledger():
    spec = _build_test_spec()

    # When the data factory raises, the ledger is recorded as failed and
    # result-hash columns are still populated (so storage can be persisted
    # uniformly without a special "no result" branch).
    def broken_factory(symbol, start, end):
        raise RuntimeError("synthetic failure")

    ledger, result = run_strategy_spec(
        RunRequest(
            spec=spec,
            start_ms=run_date_to_ms(date(2024, 1, 2)),
            end_ms=run_date_to_ms(date(2024, 12, 31)),
        ),
        data_source_factory=broken_factory,
        data_root_revision="test",
    )

    assert ledger.status == "failed", "a raising data source: ledger status is failed"
    assert ledger.failure_reason is not None, "a raising data source: failure reason is recorded"
    assert "synthetic failure" in ledger.failure_reason, "a raising data source: reason carries the error"
    assert ledger.result_hash is not None, "a raising data source: result hash is still populated"
    assert ledger.trade_log_hash is not None, "a raising data source: trade log hash is still populated"
    assert ledger.metrics_hash is not None, "a raising data source: metrics hash is still populated"
    assert result.trades == [], "a raising data source: no trades"
    assert result.metrics.total_trades == 0, "a raising data source: zero total trades"
    assert result.warnings == [ledger.failure_reason], "a raising data source: the warning is the failure reason"

    # An empty data reader fails the run instead of completing it with
    # zero trades and the starting cash — a result indistinguishable from a
    # strategy that simply did not fire (#2445). The reason is the one Spec
    # and Strategy Lab give for the same run.
    def empty_factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=[])

    ledger, result = run_strategy_spec(
        RunRequest(
            spec=spec,
            start_ms=run_date_to_ms(date(2024, 1, 2)),
            end_ms=run_date_to_ms(date(2024, 12, 31)),
        ),
        data_source_factory=empty_factory,
        data_root_revision="test-revision-1",
    )

    assert ledger.status == "failed", "#2445 an empty data source: ledger status is failed"
    assert ledger.failure_reason == "missing data: backtest evaluated zero bars for the requested window", (
        "#2445 an empty data source: reason says zero bars were evaluated"
    )
    assert result.bars_consumed == 0, "#2445 an empty data source: no bars consumed"
    assert result.trades == [], "#2445 an empty data source: no trades"


@pytest.mark.usefixtures("seeded_lake_catalog")
def test_a_window_the_lake_does_not_cover_fails_the_ledger_naming_the_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Research runs share the Spec factory, so they refuse the same gap as a failed ledger (#2445).

    The refusal is an expected outcome, not a crash: the ledger carries the
    factory's own message, not a "data source unavailable" wrapper around it.
    """
    window = (date(2024, 11, 25), date(2024, 12, 6))
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(tmp_path))
    root = tmp_path / lake_subpath("polygon_split_adjusted")
    monkeypatch.delenv("LEAN_DATA_ROOT", raising=False)
    monkeypatch.delenv("LEAN_DATA_CACHE", raising=False)
    monkeypatch.setattr(run_materialization, "materialize_engine_run", lambda **kwargs: (
        EngineRunMaterialization("a" * 64, 0, 4, None)
    ))
    for day in expected_sessions(*window):
        if day < date(2024, 12, 2):
            seed_store_day(root, "TEST", day)

    ledger, result = _run(_build_test_spec(), materialize_spec_data_source, start=window[0], end=window[1])

    assert ledger.status == "failed"
    assert ledger.failure_reason == str(MissingSessionsError(check_availability([root], "TEST", *window)))
    assert "missing 2024-12-02..2024-12-06" in ledger.failure_reason
    assert result.trades == []


@pytest.mark.usefixtures("seeded_lake_catalog")
def test_lake_backed_research_run_records_admitted_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = (date(2024, 11, 25), date(2024, 12, 6))
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(tmp_path))
    root = tmp_path / lake_subpath("polygon_split_adjusted")
    for day in expected_sessions(*window):
        seed_store_day(root, "TEST", day)
    monkeypatch.setattr(run_materialization, "materialize_engine_run", lambda **kwargs: (
        EngineRunMaterialization("b" * 64, 0, 9, None)
    ))

    ledger, result = _run(_build_test_spec(), materialize_spec_data_source, start=window[0], end=window[1])

    assert ledger.status == "completed", ledger.failure_reason
    assert result.bars_consumed > 0
    assert ledger.data_snapshot_id.endswith("|lake:" + "b" * 64)


def test_lake_refusal_is_a_research_failure_with_the_coverage_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(**kwargs: object) -> EngineRunMaterialization:
        raise LakeMaterializationError("incomplete minute coverage for TEST: provider_no_data")

    monkeypatch.setattr(run_materialization, "materialize_engine_run", refuse)
    ledger, result = _run(_build_test_spec(), materialize_spec_data_source)

    assert ledger.status == "failed"
    assert ledger.failure_reason == "Lake refused this run: incomplete minute coverage for TEST: provider_no_data"
    assert result.trades == []


# ---------------------------------------------------------------------------
# Lineage fields (forward-compat for Phases C/D/E).
# ---------------------------------------------------------------------------
def test_parent_run_id_round_trips(fake_data_factory):
    spec = _build_test_spec()
    ledger, _ = run_strategy_spec(
        RunRequest(
            spec=spec,
            start_ms=run_date_to_ms(date(2024, 1, 2)),
            end_ms=run_date_to_ms(date(2024, 12, 31)),
            parent_run_id="parent-abc",
            parent_spec_hash="spec-hash-xyz",
        ),
        data_source_factory=fake_data_factory,
        data_root_revision="test-revision-1",
    )
    assert ledger.parent_run_id == "parent-abc"
    assert ledger.parent_spec_hash == "spec-hash-xyz"


# ---------------------------------------------------------------------------
# The fill modes beyond the two the router first offered.
# ---------------------------------------------------------------------------
def test_decision_minute_open_fills_at_the_open_of_the_minute_the_bucket_is_emitted_on(fake_data_factory) -> None:
    """The runner runs the mode and records it (#2599)."""
    spec = _build_test_spec()
    at_close, close_result = _run(spec, fake_data_factory, fill_mode="signal_bar_close")
    _, next_bar_result = _run(spec, fake_data_factory, fill_mode="next_bar_open")
    ledger, result = _run(spec, fake_data_factory, fill_mode="Decision-Minute-Open")

    assert ledger.status == "completed"
    assert ledger.fill_mode == "decision_minute_open"
    assert result.trades
    opens = {bar.start_ms: float(bar.open) for bar in build_minute_bars(closes_for_spy_ema(2000))}
    assert [trade.entry_price for trade in result.trades] == [opens[trade.entry_time_ms] for trade in result.trades]
    # ``next_bar_open`` also fills at a minute's open: the minute after this one.
    minute = {start_ms: index for index, start_ms in enumerate(sorted(opens))}
    assert [minute[trade.entry_time_ms] + 1 for trade in result.trades] == [
        minute[trade.entry_time_ms] for trade in next_bar_result.trades
    ]
    assert ledger.result_hash != at_close.result_hash
    assert [trade.entry_price for trade in result.trades] != [trade.entry_price for trade in close_result.trades]


def test_an_unknown_fill_mode_is_refused_naming_every_mode_the_runner_runs(fake_data_factory) -> None:
    with pytest.raises(ValueError, match="signal_bar_close, next_bar_open, next_session_open or decision_minute_open"):
        _run(_build_test_spec(), fake_data_factory, fill_mode="magic")


# ---------------------------------------------------------------------------
# bars_consumed surfacing (silent-failure signal).
# ---------------------------------------------------------------------------
def test_warmup_prerolls_fresh_cross_without_permitting_pre_window_entries():
    """Fold warmup must continue indicator/cross state into TEST.

    A monotonic rise creates one artificial up-cross when the EMA state is
    cold-started at the report boundary. Pre-rolling from the prior day means
    the cross already happened in TRAIN, so it must not be rediscovered in
    TEST; entries are disabled while that state is being primed.
    """
    spec = StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "TEST warmup cross",
            "symbols": ["TEST"],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "fast", "kind": "EMA", "period": 2, "source": "close"},
                {"id": "slow", "kind": "EMA", "period": 3, "source": "close"},
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "kind": "FreshCross",
                        "left": "fast",
                        "right": "slow",
                        "direction": "up",
                    }
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [{"kind": "BarsSinceEntry", "op": ">=", "value": 1}],
            },
            "diagnostics": {"snapshot_at_entry": ["fast", "slow"]},
        }
    )
    training_closes = [100.0 + index * 0.1 for index in range(56)]
    report_closes = [105.5] * 20 + [105.5 + (index + 1) * 0.1 for index in range(224)]
    bars = build_minute_bars(training_closes + report_closes)

    def factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=bars)

    cold_ledger, cold_result = run_strategy_spec(
        RunRequest(
            spec=spec,
            start_ms=run_date_to_ms(date(2024, 1, 3)),
            end_ms=run_date_to_ms(date(2024, 1, 4)),
        ),
        data_source_factory=factory,
        data_root_revision="test-revision-1",
    )
    warm_ledger, warm_result = run_strategy_spec(
        RunRequest(
            spec=spec,
            start_ms=run_date_to_ms(date(2024, 1, 3)),
            end_ms=run_date_to_ms(date(2024, 1, 4)),
            warmup_start_ms=run_date_to_ms(date(2024, 1, 2)),
        ),
        data_source_factory=factory,
        data_root_revision="test-revision-1",
    )

    assert cold_ledger.status == warm_ledger.status == "completed"
    assert len(cold_result.trades) == 1
    assert warm_result.trades == []
    assert warm_ledger.warmup_start_ms is not None
    assert warm_ledger.warmup_start_ms < warm_ledger.start_ms
    assert all(point.timestamp_ms >= warm_ledger.start_ms for point in warm_result.equity_curve)
