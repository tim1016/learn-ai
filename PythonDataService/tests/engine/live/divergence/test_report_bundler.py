"""Tests for the shared ``ReportBundler`` (Layer A + Layer B).

Asserts on the produced bundle — the four files, the pass/fail gate, the
per-category counts, the recorded tolerances, and the SHA-256 manifest —
for synthetic divergence-row lists. The bundler is layer-agnostic: the
category set and tolerance set are inputs, never hard-coded.
"""

from __future__ import annotations

import json

import pandas as pd

from app.engine.live.divergence.common import Severity
from app.engine.live.divergence.execution_divergence import (
    ExecutionDivergence,
    ExecutionDivergenceCategory,
)
from app.engine.live.divergence.report_bundler import (
    ReportMetadata,
    write_report_bundle,
)


def _metadata(layer: str = "exec") -> ReportMetadata:
    return ReportMetadata(
        run_id="run-1",
        strategy_instance_id="spy-ema:inst-1",
        trading_day=1,
        session_window_ms=(0, 10_000),
        layer=layer,
        tolerances={"slippage_bps": 2.0},
    )


def test_clean_report_writes_four_files_and_passes(tmp_path) -> None:
    paths = write_report_bundle([], metadata=_metadata(), reports_dir=tmp_path)

    assert paths.markdown.name == "day-1.exec.md"
    assert paths.json.name == "day-1.exec.json"
    assert paths.parquet.name == "day-1.exec.parquet"
    assert paths.hashes.name == "day-1.exec.hashes.json"
    for p in (paths.markdown, paths.json, paths.parquet, paths.hashes):
        assert p.exists()

    summary = json.loads(paths.json.read_text())
    assert summary["passed"] is True
    assert summary["gating_breach_count"] == 0
    assert summary["gating_categories"] == []


def test_per_category_counts_and_tolerances_recorded(tmp_path) -> None:
    divergences = [
        _slippage(Severity.GATING),
        _slippage(Severity.GATING),
        ExecutionDivergence(
            category=ExecutionDivergenceCategory.MISSED,
            severity=Severity.GATING,
            magnitude=1.0,
            applied_tolerance=0.0,
            bar_close_ms=2000,
        ),
    ]

    paths = write_report_bundle(divergences, metadata=_metadata(), reports_dir=tmp_path)
    summary = json.loads(paths.json.read_text())

    assert summary["counts_by_category"] == {"slippage": 2, "missed": 1}
    # Tolerances recorded verbatim so day-over-day comparisons are anchored.
    assert summary["tolerances"] == {"slippage_bps": 2.0}


def test_parquet_carries_raw_divergence_rows(tmp_path) -> None:
    paths = write_report_bundle(
        [_slippage(Severity.GATING)], metadata=_metadata(), reports_dir=tmp_path
    )

    df = pd.read_parquet(paths.parquet)
    assert len(df) == 1
    assert df.iloc[0]["category"] == "slippage"
    assert df.iloc[0]["exec_id"] == "exec-1"


def test_replay_layer_uses_the_same_code_path(tmp_path) -> None:
    # Layer B feeds the same bundler with layer="replay"; only the file stem
    # and the input rows differ.
    paths = write_report_bundle([], metadata=_metadata(layer="replay"), reports_dir=tmp_path)

    assert paths.markdown.name == "day-1.replay.md"
    assert paths.json.name == "day-1.replay.json"
    assert all(p.exists() for p in (paths.markdown, paths.json, paths.parquet, paths.hashes))


def test_manifest_hashes_cover_sibling_files(tmp_path) -> None:
    paths = write_report_bundle(
        [_slippage(Severity.GATING)], metadata=_metadata(), reports_dir=tmp_path
    )

    manifest = json.loads(paths.hashes.read_text())
    assert manifest["markdown_sha256"]
    assert manifest["json_sha256"]
    assert manifest["parquet_sha256"]
    # A 64-char hex SHA-256 digest.
    assert len(manifest["parquet_sha256"]) == 64


def _slippage(severity: Severity) -> ExecutionDivergence:
    return ExecutionDivergence(
        category=ExecutionDivergenceCategory.SLIPPAGE,
        severity=severity,
        magnitude=5.0,
        applied_tolerance=2.0,
        exec_id="exec-1",
        bar_close_ms=1000,
    )


def test_gating_divergence_fails_the_gate(tmp_path) -> None:
    paths = write_report_bundle(
        [_slippage(Severity.GATING)], metadata=_metadata(), reports_dir=tmp_path
    )

    summary = json.loads(paths.json.read_text())
    assert summary["passed"] is False
    assert summary["gating_breach_count"] == 1
    assert summary["gating_categories"] == ["slippage"]


def test_non_gating_divergence_does_not_fail_the_gate(tmp_path) -> None:
    paths = write_report_bundle(
        [_slippage(Severity.NON_GATING)], metadata=_metadata(), reports_dir=tmp_path
    )

    summary = json.loads(paths.json.read_text())
    assert summary["passed"] is True
    assert summary["gating_breach_count"] == 0
