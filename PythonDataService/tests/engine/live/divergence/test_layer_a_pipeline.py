"""Layer A end-to-end pipeline (PRD-B #9).

``run_layer_a`` reads a day's decisions + executions, matches them,
classifies divergences, and writes the ``day-N.exec`` bundle. Tested on a
synthetic day from the artifact DataFrames through to the gate value and
category counts.
"""

from __future__ import annotations

import json

import pandas as pd

from app.engine.live.artifacts import DecisionRow, ExecutionRow
from app.engine.live.divergence.exec_pipeline import run_layer_a
from app.engine.live.divergence.report_bundler import ReportMetadata


def _decisions(*rows: DecisionRow) -> pd.DataFrame:
    return pd.DataFrame([r.as_row() for r in rows])


def _executions(*rows: ExecutionRow) -> pd.DataFrame:
    records = [
        {
            "ts_ms": r.ts_ms,
            "exec_id": r.exec_id,
            "perm_id": r.perm_id,
            "client_order_id": r.client_order_id,
            "account_id": r.account_id,
            "symbol": r.symbol,
            "fill_quantity": r.fill_quantity,
            "fill_price": r.fill_price,
            "fee": r.fee,
            "execution_source": r.execution_source,
            "fill_model": r.fill_model,
            "source_bar_close_ms": r.source_bar_close_ms,
        }
        for r in rows
    ]
    return pd.DataFrame(records)


def _metadata() -> ReportMetadata:
    return ReportMetadata(
        run_id="run-1",
        strategy_instance_id="spy-ema:inst-1",
        trading_day=1,
        session_window_ms=(0, 100_000),
        layer="exec",
        tolerances={"slippage_bps": 2.0},
    )


def test_run_layer_a_clean_day_passes(tmp_path) -> None:
    decisions = _decisions(
        DecisionRow(
            bar_close_ms=1000,
            signal="ENTER",
            intended_price=100.0,
            strategy_instance_id="spy-ema:inst-1",
            intended_action="BUY",
            decision_latency_ms=10.0,
        )
    )
    executions = _executions(
        ExecutionRow(
            ts_ms=1005,
            exec_id="ex-1",
            perm_id=1,
            client_order_id="co-1",
            account_id="DU1",
            symbol="SPY",
            fill_quantity=10,
            fill_price=100.0,  # no slippage
            fee=1.00,  # matches IBKR min-fee prediction
        )
    )

    paths = run_layer_a(
        decisions=decisions,
        executions=executions,
        order_links={"co-1": 1000},
        metadata=_metadata(),
        reports_dir=tmp_path,
    )

    assert paths.json.name == "day-1.exec.json"
    summary = json.loads(paths.json.read_text())
    assert summary["passed"] is True
    assert summary["gating_breach_count"] == 0


def test_run_layer_a_categorically_complete_day_fails_gate(tmp_path) -> None:
    decisions = _decisions(
        DecisionRow(
            bar_close_ms=1000,
            signal="ENTER",
            intended_price=100.0,
            strategy_instance_id="spy-ema:inst-1",
            intended_action="BUY",
            decision_latency_ms=10.0,
        ),
        DecisionRow(
            bar_close_ms=2000,
            signal="ENTER",
            intended_price=100.0,
            strategy_instance_id="spy-ema:inst-1",
            intended_action="BUY",  # no fill → MISSED
            decision_latency_ms=10.0,
        ),
    )
    executions = _executions(
        # Matched to decision@1000: 10 bps slippage (gating) + fee drift (non-gating).
        ExecutionRow(
            ts_ms=1005,
            exec_id="ex-1",
            perm_id=1,
            client_order_id="co-1",
            account_id="DU1",
            symbol="SPY",
            fill_quantity=10,
            fill_price=100.10,
            fee=5.00,  # predicted ~1.00 → COMMISSION_DRIFT
        ),
        # A sell fill linked to no decision → EXTRA (direction blocks fallback).
        ExecutionRow(
            ts_ms=5005,
            exec_id="ex-orphan",
            perm_id=2,
            client_order_id="orphan",
            account_id="DU1",
            symbol="SPY",
            fill_quantity=-10,
            fill_price=100.0,
            fee=1.00,
        ),
    )

    paths = run_layer_a(
        decisions=decisions,
        executions=executions,
        order_links={"co-1": 1000},
        metadata=_metadata(),
        reports_dir=tmp_path,
    )

    summary = json.loads(paths.json.read_text())
    assert summary["passed"] is False
    counts = summary["counts_by_category"]
    assert counts["slippage"] == 1
    assert counts["missed"] == 1
    assert counts["extra"] == 1
    assert counts["commission_drift"] == 1
    # Slippage/missed/extra gate; commission_drift is non-gating.
    assert set(summary["gating_categories"]) == {"slippage", "missed", "extra"}
