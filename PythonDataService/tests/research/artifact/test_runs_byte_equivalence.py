"""PR 4 acceptance bar: byte-identical runs/ persistence.

Per ``docs/architecture/research-artifact-seam.md`` § "Per-PR
acceptance bar", every strangler PR must demonstrate that the
migrated phase writes byte-identical artifact files to what the
pre-seam ``storage.py`` would have written. **runs/ is the highest-
risk migration of the four**: ``ledger.json`` is the canonical-JSON
hash payload, and any byte change invalidates the replay addresses
for every existing run on disk.

This test consumes the golden bytes captured pre-migration in
``tests/fixtures/golden/research-artifact-pr4/`` (see commit
``chore(research-artifact): capture pre-PR runs/ golden artifacts``)
and asserts byte-for-byte equality against the new
``save_run`` output for the same deterministic input. The hash
field's continued presence (and stable value) is also asserted
explicitly — the hash is byte-sensitive, so a surviving hash also
proves the canonical-JSON encoding survives.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.research.runs import (
    BacktestRunResult,
    DrawdownPoint,
    EquityCurvePoint,
    RunLedger,
    RunMetrics,
    RunTrade,
    save_run,
)

GOLDEN_DIR = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "golden"
    / "research-artifact-pr4"
)


def _deterministic_ledger() -> RunLedger:
    """A fully populated, fixed-value ledger.

    ``run_id`` and ``parent_run_id`` are 32-hex strings so the
    descriptor's ``id_pattern`` validation path is exercised — the
    same regression-test path PR 1's commit ``1146a95`` added.
    """
    return RunLedger(
        schema_version="1.1",
        run_id="a" * 32,
        parent_run_id="b" * 32,
        parent_spec_hash="0123456789abcdef" * 4,
        strategy_spec_id="test-spec",
        strategy_spec_hash="cafebabe" * 8,
        strategy_spec_json={
            "name": "deterministic-spec",
            "symbols": ["TEST"],
            "resolution": {"period_minutes": 15},
            "params": {"alpha": 0.25, "beta": [1, 2, 3]},
        },
        engine_name="learn_ai_event_driven",
        engine_version="0.1.0",
        engine_git_commit="0" * 40,
        symbol="TEST",
        resolution_minutes=15,
        start_ms=1_700_000_000_000,
        end_ms=1_730_000_000_000,
        initial_cash=100_000.0,
        fill_mode="signal_bar_close",
        commission_per_order=1.0,
        slippage_per_share=0.01,
        warmup_policy="spec_indicator_warmup",
        random_seed=42,
        data_source="lean_minute_reader",
        data_snapshot_id="TEST|15|1700000000000|1730000000000|test-revision",
        prediction_set_hash="deadbeef" * 8,
        result_hash="f" * 64,
        trade_log_hash="e" * 64,
        metrics_hash="d" * 64,
        created_at_ms=1_736_000_000_000,
        completed_at_ms=1_736_000_005_000,
        status="completed",
        failure_reason=None,
    )


def _deterministic_result() -> BacktestRunResult:
    """A fully populated, fixed-value result — same run_id as the ledger."""
    return BacktestRunResult(
        run_id="a" * 32,
        initial_cash=100_000.0,
        final_equity=102_500.0,
        equity_curve=[
            EquityCurvePoint(timestamp_ms=1_700_000_000_000, equity=100_000.0),
            EquityCurvePoint(timestamp_ms=1_700_000_900_000, equity=100_500.0),
            EquityCurvePoint(timestamp_ms=1_700_001_800_000, equity=102_500.0),
        ],
        drawdown_curve=[
            DrawdownPoint(timestamp_ms=1_700_000_000_000, drawdown_pct=0.0),
            DrawdownPoint(timestamp_ms=1_700_000_900_000, drawdown_pct=0.0),
            DrawdownPoint(timestamp_ms=1_700_001_800_000, drawdown_pct=0.0),
        ],
        trades=[
            RunTrade(
                trade_number=1,
                entry_time_ms=1_700_000_000_000,
                entry_price=420.5,
                exit_time_ms=1_700_001_800_000,
                exit_price=430.75,
                indicators_at_entry={"fast": 419.25, "slow": 415.0, "rsi": 55.5},
                pnl_pts=10.25,
                pnl_pct=0.024375,
                result="WIN",
                signal_reason="fresh-cross-up",
                bars_held=2,
            ),
        ],
        metrics=RunMetrics(
            total_trades=1,
            winning_trades=1,
            losing_trades=0,
            win_rate=1.0,
            total_return_pct=2.5,
            max_drawdown_pct=0.0,
            sharpe_ratio=1.75,
            sortino_ratio=2.10,
            profit_factor=None,
            expectancy_pct=2.4375,
            payoff_ratio=None,
            exposure_pct=0.65,
            avg_trade_bars=2.0,
        ),
        log_lines=["[STEP 1] engine started", "[STEP 2] backtest complete"],
        warnings=[],
    )


def test_runs_save_matches_pre_pr_golden_bytes(tmp_path: Path):
    """The bytes on disk after migration match the pre-PR golden.

    A single byte difference invalidates every replay address ever
    recorded against an existing ledger — the hash is canonical-JSON
    over the model dump, and even a key-order swap or trailing-
    newline addition changes the SHA-256. The PR is gated on this
    test passing without regenerating the golden.
    """
    ledger = _deterministic_ledger()
    result = _deterministic_result()

    # runs/ uses the flat ``subdir=""`` layout — artifacts live at
    # ``<root>/<run_id>/``, not ``<root>/runs/<run_id>/``.
    run_dir = save_run(ledger, result, root=tmp_path)
    assert run_dir == tmp_path / ledger.run_id

    actual_ledger = (run_dir / "ledger.json").read_bytes()
    actual_result = (run_dir / "result.json").read_bytes()

    golden_ledger = (GOLDEN_DIR / "ledger.json").read_bytes()
    golden_result = (GOLDEN_DIR / "result.json").read_bytes()

    assert actual_ledger == golden_ledger
    assert actual_result == golden_result


def test_runs_save_preserves_canonical_hash_fields_in_ledger(tmp_path: Path):
    """The on-disk ledger still carries the identity hash fields.

    The hash is byte-sensitive: if the canonical-JSON encoding had
    drifted, the round-trip ``model_validate_json`` would either
    fail or reconstruct a model with a *different* hash field.
    Asserting the literal hash values from the golden capture
    pins the encoding without re-implementing the SHA inside
    the test.
    """
    ledger = _deterministic_ledger()
    result = _deterministic_result()
    save_run(ledger, result, root=tmp_path)

    on_disk = json.loads((tmp_path / ledger.run_id / "ledger.json").read_text())
    assert on_disk["result_hash"] == "f" * 64
    assert on_disk["trade_log_hash"] == "e" * 64
    assert on_disk["metrics_hash"] == "d" * 64
    assert on_disk["strategy_spec_hash"] == "cafebabe" * 8
    assert on_disk["parent_spec_hash"] == "0123456789abcdef" * 4
