"""Tests for app.engine.live.reconcile.

Synthetic three-way fixtures triggering each divergence class per spec
``docs/superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md``
sections 6.1–6.5. The schemas tested here are the contract that
``run.py`` (Phase C) must produce when it wires the live runtime's
artifact writers — this module stands alone with no IBKR or live
dependency.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.engine.live.reconcile import (
    CrossEngineClass,
    CrossEngineTolerances,
    DaySummary,
    FillClass,
    FillTolerances,
    ReconcileSchemaError,
    build_hash_manifest,
    build_reconciliation_table,
    classify_cross_engine,
    classify_fill,
    file_sha256,
    load_python_decisions,
    load_python_executions,
    load_qc_indicators,
    render_day_md,
    summarize_day,
    write_day_report,
    write_week_rollup,
)

# ──────────────────────────── Helpers ────────────────────────────────


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    """Build an int64 ms UTC timestamp for the bar close."""
    return int(pd.Timestamp(year, month, day, hour, minute, tz="UTC").value // 1_000_000)


def _make_decisions(rows: list[dict]) -> pd.DataFrame:
    cols = ["bar_close_ms", "ema5", "ema10", "rsi", "signal", "intended_price"]
    return pd.DataFrame(rows, columns=cols)


def _make_qc(rows: list[dict]) -> pd.DataFrame:
    cols = ["bar_close_ms", "ema5", "ema10", "rsi", "signal"]
    return pd.DataFrame(rows, columns=cols)


def _make_executions(rows: list[dict]) -> pd.DataFrame:
    cols = [
        "ts_ms",
        "exec_id",
        "perm_id",
        "client_order_id",
        "account_id",
        "symbol",
        "fill_quantity",
        "fill_price",
        "fee",
    ]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols)


def _write_run_inputs(
    run_dir: Path,
    qc_dir: Path,
    *,
    decisions: pd.DataFrame,
    executions: pd.DataFrame,
    qc_indicators: pd.DataFrame,
    run_ledger: dict | None = None,
    qc_trades: pd.DataFrame | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    qc_dir.mkdir(parents=True, exist_ok=True)
    decisions.to_parquet(run_dir / "decisions.parquet", index=False)
    executions.to_parquet(run_dir / "executions.parquet", index=False)
    qc_indicators.to_csv(qc_dir / "indicators.csv", index=False)
    if run_ledger is not None:
        (run_dir / "run_ledger.json").write_text(json.dumps(run_ledger), encoding="utf-8")
    if qc_trades is not None:
        qc_trades.to_csv(qc_dir / "trades.csv", index=False)


# ──────────────────────────── classify_cross_engine ──────────────────


def test_classify_cross_engine_none_when_indicators_and_signals_agree() -> None:
    cls = classify_cross_engine(
        py_ema5=500.0,
        py_ema10=500.0,
        py_rsi=60.0,
        py_signal="HOLD",
        qc_ema5=500.05,
        qc_ema10=500.02,
        qc_rsi=60.5,
        qc_signal="HOLD",
        tols=CrossEngineTolerances(),
    )
    assert cls == CrossEngineClass.NONE


def test_classify_cross_engine_data_when_ema_outside_tolerance() -> None:
    cls = classify_cross_engine(
        py_ema5=500.0,
        py_ema10=500.0,
        py_rsi=60.0,
        py_signal="HOLD",
        qc_ema5=500.5,
        qc_ema10=500.0,
        qc_rsi=60.0,
        qc_signal="HOLD",  # ema5 delta = 0.5 > 0.10
        tols=CrossEngineTolerances(),
    )
    assert cls == CrossEngineClass.DATA


def test_classify_cross_engine_data_when_rsi_outside_tolerance() -> None:
    cls = classify_cross_engine(
        py_ema5=500.0,
        py_ema10=500.0,
        py_rsi=60.0,
        py_signal="ENTER",
        qc_ema5=500.0,
        qc_ema10=500.0,
        qc_rsi=63.0,
        qc_signal="ENTER",  # rsi delta = 3 > 2
        tols=CrossEngineTolerances(),
    )
    # Data class wins even though signals match — § 6.2: indicators outside tol → data
    assert cls == CrossEngineClass.DATA


def test_classify_cross_engine_engine_when_indicators_agree_but_signals_differ() -> None:
    cls = classify_cross_engine(
        py_ema5=500.0,
        py_ema10=500.0,
        py_rsi=60.0,
        py_signal="ENTER",
        qc_ema5=500.05,
        qc_ema10=500.02,
        qc_rsi=60.5,
        qc_signal="HOLD",
        tols=CrossEngineTolerances(),
    )
    assert cls == CrossEngineClass.ENGINE


def test_classify_cross_engine_data_class_takes_precedence_over_signal_mismatch() -> None:
    """Indicators outside tolerance → data, regardless of whether signals also differ.

    The whole point of the taxonomy is that ENGINE only flags when we
    can prove the engines saw the same inputs. If indicators disagree
    we cannot make that claim.
    """
    cls = classify_cross_engine(
        py_ema5=500.0,
        py_ema10=500.0,
        py_rsi=60.0,
        py_signal="ENTER",
        qc_ema5=502.0,
        qc_ema10=502.0,
        qc_rsi=70.0,
        qc_signal="HOLD",
        tols=CrossEngineTolerances(),
    )
    assert cls == CrossEngineClass.DATA


# ──────────────────────────── classify_fill ──────────────────────────


def test_classify_fill_none_when_both_sides_missing() -> None:
    assert classify_fill(None, None, None, None, None, None, FillTolerances()) == FillClass.NONE


def test_classify_fill_breach_when_only_one_side_has_fill() -> None:
    assert classify_fill(500.0, None, _ms(2026, 5, 4, 14, 45), None, 200, None, FillTolerances()) == FillClass.BREACH
    assert classify_fill(None, 500.0, None, _ms(2026, 5, 4, 14, 45), None, 200, FillTolerances()) == FillClass.BREACH


def test_classify_fill_within_tolerance_for_small_price_delta() -> None:
    intended_ms = _ms(2026, 5, 4, 14, 45)
    assert (
        classify_fill(
            intended_price=500.00,
            fill_price=500.04,
            intended_time_ms=intended_ms,
            fill_time_ms=intended_ms + 2_000,
            intended_quantity=200,
            fill_quantity=200,
            tols=FillTolerances(),
        )
        == FillClass.WITHIN_TOL
    )


def test_classify_fill_breach_when_price_exceeds_tolerance() -> None:
    intended_ms = _ms(2026, 5, 4, 14, 45)
    assert (
        classify_fill(
            intended_price=500.00,
            fill_price=500.10,
            intended_time_ms=intended_ms,
            fill_time_ms=intended_ms + 1_000,
            intended_quantity=200,
            fill_quantity=200,
            tols=FillTolerances(),
        )
        == FillClass.BREACH
    )


def test_classify_fill_breach_when_time_delta_exceeds_seconds_tolerance() -> None:
    intended_ms = _ms(2026, 5, 4, 14, 45)
    assert (
        classify_fill(
            intended_price=500.00,
            fill_price=500.00,
            intended_time_ms=intended_ms,
            fill_time_ms=intended_ms + 6_000,  # 6 s > 5 s
            intended_quantity=200,
            fill_quantity=200,
            tols=FillTolerances(),
        )
        == FillClass.BREACH
    )


# ──────────────────────────── build_reconciliation_table ─────────────


def test_build_reconciliation_table_inner_joins_on_bar_close_ms() -> None:
    py = _make_decisions(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 30),
                "ema5": 500.0,
                "ema10": 499.5,
                "rsi": 60.0,
                "signal": "HOLD",
                "intended_price": 500.0,
            },
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.0,
                "ema10": 500.0,
                "rsi": 62.0,
                "signal": "ENTER",
                "intended_price": 501.0,
            },
            {
                "bar_close_ms": _ms(2026, 5, 4, 15, 0),
                "ema5": 502.0,
                "ema10": 501.0,
                "rsi": 63.0,
                "signal": "HOLD",
                "intended_price": 502.0,
            },
        ]
    )
    qc = _make_qc(
        [
            # 14:30 missing on QC side → excluded from join
            {"bar_close_ms": _ms(2026, 5, 4, 14, 45), "ema5": 501.05, "ema10": 500.02, "rsi": 62.5, "signal": "ENTER"},
            {"bar_close_ms": _ms(2026, 5, 4, 15, 0), "ema5": 502.05, "ema10": 501.02, "rsi": 63.5, "signal": "HOLD"},
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
            },
        ]
    )

    table = build_reconciliation_table(py, qc, execs, CrossEngineTolerances(), FillTolerances())

    assert len(table) == 2
    assert list(table["bar_close_ms"]) == [
        _ms(2026, 5, 4, 14, 45),
        _ms(2026, 5, 4, 15, 0),
    ]
    enter_row = table.iloc[0]
    assert enter_row["python_signal"] == "ENTER"
    assert enter_row["qc_signal"] == "ENTER"
    assert enter_row["cross_engine_class"] == CrossEngineClass.NONE.value
    assert enter_row["fill_class"] == FillClass.WITHIN_TOL.value
    assert enter_row["python_fill_price"] == pytest.approx(501.02)
    assert enter_row["python_intended_price"] == pytest.approx(501.0)

    hold_row = table.iloc[1]
    assert hold_row["python_signal"] == "HOLD"
    assert hold_row["fill_class"] == FillClass.NONE.value


def test_build_reconciliation_table_engine_class_with_clean_indicators() -> None:
    py = _make_decisions(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.0,
                "ema10": 500.0,
                "rsi": 62.0,
                "signal": "ENTER",
                "intended_price": 501.0,
            },
        ]
    )
    qc = _make_qc(
        [
            {"bar_close_ms": _ms(2026, 5, 4, 14, 45), "ema5": 501.0, "ema10": 500.0, "rsi": 62.0, "signal": "HOLD"},
        ]
    )
    table = build_reconciliation_table(py, qc, _make_executions([]), CrossEngineTolerances(), FillTolerances())
    assert table.iloc[0]["cross_engine_class"] == CrossEngineClass.ENGINE.value


# ──────────────────────────── summarize_day + halt ───────────────────


def test_summarize_day_triggers_halt_on_engine_class() -> None:
    py = _make_decisions(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.0,
                "ema10": 500.0,
                "rsi": 62.0,
                "signal": "ENTER",
                "intended_price": 501.0,
            },
        ]
    )
    qc = _make_qc(
        [
            {"bar_close_ms": _ms(2026, 5, 4, 14, 45), "ema5": 501.0, "ema10": 500.0, "rsi": 62.0, "signal": "HOLD"},
        ]
    )
    table = build_reconciliation_table(py, qc, _make_executions([]), CrossEngineTolerances(), FillTolerances())
    summary = summarize_day(table, py, qc, day_n=1, day_date=date(2026, 5, 4))

    assert summary.halt_triggered is True
    assert any("engine-class" in r for r in summary.halt_reasons)
    assert summary.cross_engine == 1


def test_summarize_day_triggers_halt_on_fill_breach() -> None:
    py = _make_decisions(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.0,
                "ema10": 500.0,
                "rsi": 62.0,
                "signal": "ENTER",
                "intended_price": 501.0,
            },
        ]
    )
    qc = _make_qc(
        [
            {"bar_close_ms": _ms(2026, 5, 4, 14, 45), "ema5": 501.05, "ema10": 500.02, "rsi": 62.5, "signal": "ENTER"},
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
                "fill_price": 501.20,  # 0.20 > 0.05 atol
                "fee": 1.0,
            },
        ]
    )
    table = build_reconciliation_table(py, qc, execs, CrossEngineTolerances(), FillTolerances())
    summary = summarize_day(table, py, qc, day_n=1, day_date=date(2026, 5, 4))

    assert summary.halt_triggered is True
    assert any("fill-class" in r for r in summary.halt_reasons)


def test_summarize_day_no_halt_when_only_data_class_present() -> None:
    py = _make_decisions(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.0,
                "ema10": 500.0,
                "rsi": 62.0,
                "signal": "HOLD",
                "intended_price": 501.0,
            },
        ]
    )
    qc = _make_qc(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 502.0,
                "ema10": 501.0,
                "rsi": 65.0,
                "signal": "HOLD",
            },  # outside tol
        ]
    )
    table = build_reconciliation_table(py, qc, _make_executions([]), CrossEngineTolerances(), FillTolerances())
    summary = summarize_day(table, py, qc, day_n=1, day_date=date(2026, 5, 4))

    assert summary.halt_triggered is False
    assert summary.cross_data == 1


# ──────────────────────────── SHA-256 manifest ───────────────────────


def test_file_sha256_is_deterministic_for_same_bytes(tmp_path: Path) -> None:
    p1 = tmp_path / "a.bin"
    p2 = tmp_path / "b.bin"
    p1.write_bytes(b"hello world")
    p2.write_bytes(b"hello world")
    assert file_sha256(p1) == file_sha256(p2)
    assert file_sha256(p1) == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_build_hash_manifest_returns_none_for_missing_files(tmp_path: Path) -> None:
    json_path = tmp_path / "x.json"
    json_path.write_text("{}")
    manifest = build_hash_manifest(
        json_path=json_path,
        parquet_path=tmp_path / "missing.parquet",
        py_executions_path=tmp_path / "missing_exec.parquet",
        py_trades_path=tmp_path / "missing_trades.parquet",
        qc_trades_path=tmp_path / "missing_qc_trades.csv",
        qc_indicators_path=tmp_path / "missing_qc_ind.csv",
        run_ledger_path=tmp_path / "missing_ledger.json",
    )
    assert manifest["reconcile_json"] is not None
    assert manifest["reconcile_parquet"] is None
    assert manifest["python_executions_parquet"] is None


def test_day_hashes_manifest_includes_hydration_receipt_if_present(tmp_path: Path) -> None:
    """When <run_dir>/indicator_state_hydration.json exists its SHA-256 appears in day-N.hashes.json."""
    import hashlib

    run_dir = tmp_path / "live_runs" / "abcdef"
    qc_dir = tmp_path / "qc" / "2026-05-04"
    docs_dir = tmp_path / "docs" / "spy-ema-crossover-paper-2026-05"

    py = _make_decisions(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.0,
                "ema10": 500.0,
                "rsi": 62.0,
                "signal": "HOLD",
                "intended_price": 501.0,
            },
        ]
    )
    qc = _make_qc(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.05,
                "ema10": 500.02,
                "rsi": 62.5,
                "signal": "HOLD",
            },
        ]
    )
    _write_run_inputs(run_dir, qc_dir, decisions=py, executions=_make_executions([]), qc_indicators=qc)

    # Write the hydration receipt that Task 7 produces.
    hydration_content = json.dumps({"hydrated_from": "previous_state", "bars_loaded": 20})
    hydration_path = run_dir / "indicator_state_hydration.json"
    hydration_path.write_text(hydration_content, encoding="utf-8")

    expected_sha = hashlib.sha256(hydration_content.encode()).hexdigest()

    paths = write_day_report(
        run_dir=run_dir,
        qc_dir=qc_dir,
        docs_dir=docs_dir,
        run_label="spy-ema-crossover-paper-2026-05",
        day_n=1,
        day_date=date(2026, 5, 4),
    )

    # 1. hashes.json must contain the key and the correct SHA-256.
    hashes = json.loads(paths.hashes.read_text(encoding="utf-8"))
    assert "indicator_state_hydration.json" in hashes, (
        "day-N.hashes.json must include 'indicator_state_hydration.json' when the file exists"
    )
    assert hashes["indicator_state_hydration.json"] == expected_sha

    # 2. The committed Markdown receipt must also mention the SHA.
    md_text = paths.md.read_text(encoding="utf-8")
    assert expected_sha in md_text, "day-N.md artifact_hashes block must embed the hydration receipt SHA-256"


def test_day_hashes_manifest_omits_hydration_receipt_when_absent(tmp_path: Path) -> None:
    """When indicator_state_hydration.json does not exist the key is absent from day-N.hashes.json."""
    run_dir = tmp_path / "live_runs" / "nohyd"
    qc_dir = tmp_path / "qc" / "2026-05-04"
    docs_dir = tmp_path / "docs" / "spy-ema-crossover-paper-2026-05"

    py = _make_decisions(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.0,
                "ema10": 500.0,
                "rsi": 62.0,
                "signal": "HOLD",
                "intended_price": 501.0,
            },
        ]
    )
    qc = _make_qc(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.05,
                "ema10": 500.02,
                "rsi": 62.5,
                "signal": "HOLD",
            },
        ]
    )
    _write_run_inputs(run_dir, qc_dir, decisions=py, executions=_make_executions([]), qc_indicators=qc)

    # Deliberately do NOT write indicator_state_hydration.json.

    paths = write_day_report(
        run_dir=run_dir,
        qc_dir=qc_dir,
        docs_dir=docs_dir,
        run_label="spy-ema-crossover-paper-2026-05",
        day_n=1,
        day_date=date(2026, 5, 4),
    )

    hashes = json.loads(paths.hashes.read_text(encoding="utf-8"))
    assert "indicator_state_hydration.json" not in hashes


# ──────────────────────────── Markdown rendering ─────────────────────


def test_render_day_md_includes_required_sections(tmp_path: Path) -> None:
    py = _make_decisions(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.0,
                "ema10": 500.0,
                "rsi": 62.0,
                "signal": "ENTER",
                "intended_price": 501.0,
            },
        ]
    )
    qc = _make_qc(
        [
            {"bar_close_ms": _ms(2026, 5, 4, 14, 45), "ema5": 501.05, "ema10": 500.02, "rsi": 62.5, "signal": "ENTER"},
        ]
    )
    # Provide an in-tolerance fill so the day classifies clean (focus
    # of this test is Markdown structure, not halt logic).
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
            },
        ]
    )
    table = build_reconciliation_table(py, qc, execs, CrossEngineTolerances(), FillTolerances())
    summary = summarize_day(table, py, qc, day_n=1, day_date=date(2026, 5, 4))
    manifest = {"reconcile_json": "abc123", "reconcile_parquet": None}

    md = render_day_md(
        summary=summary,
        table=table,
        hash_manifest=manifest,
        run_label="spy-ema-crossover-paper-2026-05",
        cross_tols=CrossEngineTolerances(),
        fill_tols=FillTolerances(),
    )

    assert "# Day 1 reconciliation — 2026-05-04" in md
    assert "spy-ema-crossover-paper-2026-05" in md
    assert "artifact_hashes:" in md
    assert "reconcile_json: abc123" in md
    assert "reconcile_parquet: ~" in md
    assert "## Tolerances applied" in md
    assert "## Counts" in md
    assert "## Notable rows" in md
    assert "Halt triggered for next session:** no" in md


def test_render_day_md_marks_halt_when_engine_class_present() -> None:
    py = _make_decisions(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.0,
                "ema10": 500.0,
                "rsi": 62.0,
                "signal": "ENTER",
                "intended_price": 501.0,
            },
        ]
    )
    qc = _make_qc(
        [
            {"bar_close_ms": _ms(2026, 5, 4, 14, 45), "ema5": 501.0, "ema10": 500.0, "rsi": 62.0, "signal": "HOLD"},
        ]
    )
    table = build_reconciliation_table(py, qc, _make_executions([]), CrossEngineTolerances(), FillTolerances())
    summary = summarize_day(table, py, qc, day_n=1, day_date=date(2026, 5, 4))
    md = render_day_md(
        summary=summary,
        table=table,
        hash_manifest={},
        run_label="x",
        cross_tols=CrossEngineTolerances(),
        fill_tols=FillTolerances(),
    )
    assert "Halt triggered for next session:** engine-class divergence" in md


# ──────────────────────────── End-to-end day write ───────────────────


def test_write_day_report_writes_all_four_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "live_runs" / "abcdef"
    qc_dir = tmp_path / "qc" / "2026-05-04"
    docs_dir = tmp_path / "docs" / "spy-ema-crossover-paper-2026-05"

    py = _make_decisions(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.0,
                "ema10": 500.0,
                "rsi": 62.0,
                "signal": "ENTER",
                "intended_price": 501.0,
            },
            {
                "bar_close_ms": _ms(2026, 5, 4, 15, 0),
                "ema5": 502.0,
                "ema10": 501.0,
                "rsi": 63.0,
                "signal": "HOLD",
                "intended_price": 502.0,
            },
        ]
    )
    qc = _make_qc(
        [
            {"bar_close_ms": _ms(2026, 5, 4, 14, 45), "ema5": 501.05, "ema10": 500.02, "rsi": 62.5, "signal": "ENTER"},
            {"bar_close_ms": _ms(2026, 5, 4, 15, 0), "ema5": 502.05, "ema10": 501.02, "rsi": 63.5, "signal": "HOLD"},
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
            },
        ]
    )
    _write_run_inputs(
        run_dir,
        qc_dir,
        decisions=py,
        executions=execs,
        qc_indicators=qc,
        run_ledger={"code_sha": "abc123"},
    )

    paths = write_day_report(
        run_dir=run_dir,
        qc_dir=qc_dir,
        docs_dir=docs_dir,
        run_label="spy-ema-crossover-paper-2026-05",
        day_n=1,
        day_date=date(2026, 5, 4),
    )

    assert paths.parquet.exists()
    assert paths.json.exists()
    assert paths.hashes.exists()
    assert paths.md.exists()

    # Hash sidecar matches the manifest embedded in md
    hashes = json.loads(paths.hashes.read_text())
    assert len(hashes["reconcile_parquet"]) == 64
    assert len(hashes["reconcile_json"]) == 64
    md_text = paths.md.read_text(encoding="utf-8")
    assert hashes["reconcile_parquet"] in md_text
    assert hashes["reconcile_json"] in md_text

    # Reconcile parquet matches what we'd build directly
    written_table = pd.read_parquet(paths.parquet)
    assert list(written_table.columns) == [
        "bar_close_ms",
        "python_signal",
        "python_ema5",
        "python_ema10",
        "python_rsi",
        "qc_signal",
        "qc_ema5",
        "qc_ema10",
        "qc_rsi",
        "cross_engine_class",
        "python_fill_price",
        "python_intended_price",
        "fill_class",
    ]

    # Halt flag NOT written for a clean day
    assert not (run_dir / "halt.flag").exists()


def test_write_day_report_writes_halt_flag_on_engine_divergence(tmp_path: Path) -> None:
    run_dir = tmp_path / "live_runs" / "abcdef"
    qc_dir = tmp_path / "qc" / "2026-05-04"
    docs_dir = tmp_path / "docs"

    py = _make_decisions(
        [
            {
                "bar_close_ms": _ms(2026, 5, 4, 14, 45),
                "ema5": 501.0,
                "ema10": 500.0,
                "rsi": 62.0,
                "signal": "ENTER",
                "intended_price": 501.0,
            },
        ]
    )
    qc = _make_qc(
        [
            # indicators agree, signals don't → engine class
            {"bar_close_ms": _ms(2026, 5, 4, 14, 45), "ema5": 501.0, "ema10": 500.0, "rsi": 62.0, "signal": "HOLD"},
        ]
    )
    _write_run_inputs(run_dir, qc_dir, decisions=py, executions=_make_executions([]), qc_indicators=qc)

    write_day_report(
        run_dir=run_dir,
        qc_dir=qc_dir,
        docs_dir=docs_dir,
        run_label="x",
        day_n=2,
        day_date=date(2026, 5, 4),
    )

    halt_flag = run_dir / "halt.flag"
    assert halt_flag.exists()
    payload = json.loads(halt_flag.read_text())
    assert payload["day_n"] == 2
    assert any("engine-class" in r for r in payload["reasons"])


# ──────────────────────────── Loaders / schema ───────────────────────


def test_load_python_decisions_rejects_missing_columns(tmp_path: Path) -> None:
    bad = pd.DataFrame({"bar_close_ms": [1], "ema5": [1.0]})
    path = tmp_path / "decisions.parquet"
    bad.to_parquet(path, index=False)
    with pytest.raises(ReconcileSchemaError) as exc:
        load_python_decisions(path)
    assert "missing required columns" in str(exc.value)


def test_load_qc_indicators_rejects_unknown_signal_value(tmp_path: Path) -> None:
    bad = _make_qc(
        [
            {"bar_close_ms": _ms(2026, 5, 4, 14, 45), "ema5": 501.0, "ema10": 500.0, "rsi": 62.0, "signal": "MAYBE"},
        ]
    )
    path = tmp_path / "indicators.csv"
    bad.to_csv(path, index=False)
    with pytest.raises(ReconcileSchemaError) as exc:
        load_qc_indicators(path)
    assert "unrecognized signal values" in str(exc.value)


def test_load_python_executions_passes_for_well_formed_input(tmp_path: Path) -> None:
    df = _make_executions(
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
            },
        ]
    )
    path = tmp_path / "executions.parquet"
    df.to_parquet(path, index=False)
    out = load_python_executions(path)
    assert len(out) == 1
    assert out.iloc[0]["fill_quantity"] == 200


# ──────────────────────────── Week rollup ────────────────────────────


def test_write_week_rollup_aggregates_days(tmp_path: Path) -> None:
    run_dir = tmp_path / "live_runs" / "abcdef"
    docs_dir = tmp_path / "docs" / "x"
    (run_dir / "reconcile").mkdir(parents=True)
    docs_dir.mkdir(parents=True)
    # Fake a couple of daily artifacts so the hash manifest can hash them.
    (docs_dir / "day-1.md").write_text("# day 1")
    (docs_dir / "day-2.md").write_text("# day 2")
    (run_dir / "reconcile" / "day-1.json").write_text("{}")
    (run_dir / "reconcile" / "day-2.json").write_text("{}")

    days = [
        DaySummary(
            day_n=1,
            day_date="2026-05-04",
            bars_total=26,
            bars_python_only=0,
            bars_qc_only=0,
            cross_none=26,
            cross_data=0,
            cross_engine=0,
            fill_none=24,
            fill_within_tol=2,
            fill_breach=0,
            halt_triggered=False,
            halt_reasons=(),
        ),
        DaySummary(
            day_n=2,
            day_date="2026-05-05",
            bars_total=26,
            bars_python_only=0,
            bars_qc_only=0,
            cross_none=25,
            cross_data=1,
            cross_engine=0,
            fill_none=26,
            fill_within_tol=0,
            fill_breach=0,
            halt_triggered=False,
            halt_reasons=(),
        ),
    ]
    week_path = write_week_rollup(run_dir=run_dir, docs_dir=docs_dir, run_label="x", days=days)

    md = week_path.read_text(encoding="utf-8")
    assert "# Week rollup — x" in md
    assert "day_n: 1" in md
    assert "day_n: 2" in md
    assert "**Days:** 2" in md
