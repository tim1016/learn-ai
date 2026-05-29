"""3-way reconciler coexistence / non-perturbation regression (PRD-B #9).

PRD-B adds two new per-day bundles (``day-N.exec.*``, ``day-N.replay.*``)
and must leave the existing three-way reconciler's ``day-N.*`` bundle
untouched (PRD-B: "Preserve the existing three-way reconcile.py ...
alongside the two new bundles. Three bundles per trading day."). This test
proves: (1) the three bundles' filenames are disjoint (no overwrite), and
(2) the reconciler's output is byte-identical whether or not the harness
bundles share the directory.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from app.engine.live.artifacts import DecisionRow
from app.engine.live.divergence.exec_pipeline import run_layer_a
from app.engine.live.divergence.report_bundler import ReportMetadata, write_report_bundle
from app.engine.live.reconcile import write_day_report
from tests.engine.live.test_reconcile import (
    _make_decisions,
    _make_executions,
    _make_qc,
    _ms,
    _write_run_inputs,
)


def _reconcile_inputs():
    py = _make_decisions(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.0,
                "ema10": 500.0,
                "rsi": 62.0,
                "signal": "ENTER",
                "intended_price": 501.0,
            }
        ]
    )
    qc = _make_qc(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.05,
                "ema10": 500.02,
                "rsi": 62.5,
                "signal": "ENTER",
            }
        ]
    )
    execs = _make_executions(
        [
            {
                "ts_ms": _ms(2026, 5, 4, 14, 45) + 2_000,
                "exec_id": "exec-1",
                "perm_id": 9001,
                "client_order_id": "live-1",
                "account_id": "DU1234",
                "symbol": "SPY",
                "fill_quantity": 200,
                "fill_price": 501.02,
                "fee": 1.0,
            }
        ]
    )
    return py, qc, execs


def _write_reconcile(tmp_path: Path) -> tuple:
    run_dir = tmp_path / "live_runs" / "abcdef"
    qc_dir = tmp_path / "qc" / "2026-05-04"
    docs_dir = tmp_path / "docs"
    py, qc, execs = _reconcile_inputs()
    _write_run_inputs(
        run_dir, qc_dir, decisions=py, executions=execs, qc_indicators=qc, run_ledger={"code_sha": "x"}
    )
    paths = write_day_report(
        run_dir=run_dir,
        qc_dir=qc_dir,
        docs_dir=docs_dir,
        run_label="spy-ema-paper",
        day_n=1,
        day_date=date(2026, 5, 4),
    )
    return run_dir, paths


def test_harness_bundles_coexist_without_perturbing_reconcile(tmp_path) -> None:
    # 1. Write the reconcile bundle, then snapshot its bytes.
    _run_dir, paths = _write_reconcile(tmp_path / "shared")
    snapshot = {
        "parquet": paths.parquet.read_bytes(),
        "json": paths.json.read_bytes(),
        "hashes": paths.hashes.read_bytes(),
        "md": paths.md.read_bytes(),
    }
    reports_dir = paths.parquet.parent  # run_dir/reconcile

    metadata = ReportMetadata(
        run_id="run-1",
        strategy_instance_id="spy-ema:inst-1",
        trading_day=1,
        session_window_ms=(0, 10_000_000_000_000),
        layer="exec",
        tolerances={"slippage_bps": 2.0},
    )
    decisions = pd.DataFrame(
        [
            DecisionRow(
                bar_close_ms=_ms(2026, 5, 4, 14, 45),
                signal="ENTER",
                intended_price=501.0,
                strategy_instance_id="spy-ema:inst-1",
                intended_action="BUY",
            ).as_row()
        ]
    )
    executions = pd.DataFrame(
        [
            {
                "ts_ms": _ms(2026, 5, 4, 14, 45) + 2_000,
                "exec_id": "exec-1",
                "perm_id": 9001,
                "client_order_id": "live-1",
                "account_id": "DU1234",
                "symbol": "SPY",
                "fill_quantity": 200,
                "fill_price": 501.0,
                "fee": 1.0,
                "execution_source": "broker_fill",
                "fill_model": "NEXT_BAR_OPEN",
                "source_bar_close_ms": None,
            }
        ]
    )
    exec_paths = run_layer_a(
        decisions=decisions,
        executions=executions,
        order_links={"live-1": _ms(2026, 5, 4, 14, 45)},
        metadata=metadata,
        reports_dir=reports_dir,
    )
    replay_paths = write_report_bundle(
        [],
        metadata=ReportMetadata(
            run_id="run-1",
            strategy_instance_id="spy-ema:inst-1",
            trading_day=1,
            session_window_ms=(0, 1),
            layer="replay",
            tolerances={},
        ),
        reports_dir=reports_dir,
    )

    # 3. All three bundles present, filenames disjoint.
    reconcile_names = {paths.parquet.name, paths.json.name, paths.hashes.name}
    exec_names = {exec_paths.markdown.name, exec_paths.json.name, exec_paths.parquet.name, exec_paths.hashes.name}
    replay_names = {replay_paths.markdown.name, replay_paths.json.name, replay_paths.parquet.name, replay_paths.hashes.name}
    assert reconcile_names.isdisjoint(exec_names)
    assert reconcile_names.isdisjoint(replay_names)
    assert exec_names.isdisjoint(replay_names)

    # 4. Reconcile's bundle is byte-identical to its pre-harness snapshot —
    #    dropping the harness bundles into the same dir did not perturb it.
    assert paths.parquet.read_bytes() == snapshot["parquet"]
    assert paths.json.read_bytes() == snapshot["json"]
    assert paths.hashes.read_bytes() == snapshot["hashes"]
    assert paths.md.read_bytes() == snapshot["md"]
