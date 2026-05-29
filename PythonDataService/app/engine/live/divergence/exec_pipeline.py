"""Layer A pipeline orchestrator (PRD-B #9).

Reads a day's decisions + executions artifacts, builds typed rows, matches
them (``ExecutionMatcher``), classifies divergences
(``ExecutionDivergenceClassifier``), and writes the ``day-N.exec`` bundle
(``ReportBundler``). A thin orchestrator over already-tested pure functions
— no new divergence logic here. The matcher/classifier stay pure (no
Parquet reads inside them); this pipeline is the layer that reads.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pandas as pd

from app.engine.live.artifacts import DecisionRow, ExecutionRow
from app.engine.live.divergence.execution_divergence import (
    ExecutionTolerances,
    classify_execution_divergences,
)
from app.engine.live.divergence.execution_matcher import match_executions
from app.engine.live.divergence.report_bundler import (
    BundlePaths,
    ReportMetadata,
    write_report_bundle,
)


def _decision_rows(decisions: pd.DataFrame) -> list[DecisionRow]:
    from app.engine.live.artifacts import CORE_DECISION_COLUMNS

    indicator_cols = [c for c in decisions.columns if c not in CORE_DECISION_COLUMNS]
    rows: list[DecisionRow] = []
    for rec in decisions.to_dict("records"):
        rows.append(
            DecisionRow(
                bar_close_ms=int(rec["bar_close_ms"]),
                signal=str(rec["signal"]),
                intended_price=float(rec["intended_price"]),
                strategy_instance_id=str(rec.get("strategy_instance_id", "")),
                bar_source=str(rec.get("bar_source", "")),
                intended_action=str(rec.get("intended_action", "")),
                decision_latency_ms=_opt(rec.get("decision_latency_ms")),
                indicator_values={c: rec[c] for c in indicator_cols},
            )
        )
    return rows


def _execution_rows(executions: pd.DataFrame) -> list[ExecutionRow]:
    rows: list[ExecutionRow] = []
    for rec in executions.to_dict("records"):
        src_bar = rec.get("source_bar_close_ms")
        rows.append(
            ExecutionRow(
                ts_ms=int(rec["ts_ms"]),
                exec_id=str(rec["exec_id"]),
                perm_id=int(rec["perm_id"]),
                client_order_id=str(rec["client_order_id"]),
                account_id=str(rec["account_id"]),
                symbol=str(rec["symbol"]),
                fill_quantity=int(rec["fill_quantity"]),
                fill_price=float(rec["fill_price"]),
                fee=float(rec["fee"]),
                execution_source=str(rec.get("execution_source", "broker_fill")),
                fill_model=str(rec.get("fill_model", "NEXT_BAR_OPEN")),
                source_bar_close_ms=None if src_bar is None or pd.isna(src_bar) else int(src_bar),
            )
        )
    return rows


def _opt(value: object) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def run_layer_a(
    *,
    decisions: pd.DataFrame,
    executions: pd.DataFrame,
    order_links: Mapping[str, int],
    metadata: ReportMetadata,
    reports_dir: Path,
    tolerances: ExecutionTolerances | None = None,
) -> BundlePaths:
    """Run the Layer A ``ExecutionDivergence`` pipeline for one trading day."""
    tolerances = tolerances or ExecutionTolerances()

    ledger = match_executions(
        _decision_rows(decisions),
        _execution_rows(executions),
        session_window=metadata.session_window_ms,
        order_links=order_links,
    )

    divergences = []
    for row in ledger:
        divergences.extend(classify_execution_divergences(row, tolerances))

    return write_report_bundle(divergences, metadata=metadata, reports_dir=reports_dir)


__all__ = ["run_layer_a"]
