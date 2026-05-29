"""Tests for app.engine.live.artifacts.

Schemas pinned here MUST match the loader column-set assertions in
``app.engine.live.reconcile`` — these tests round-trip a writer to its
matching reconcile loader to lock the contract.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.engine.live.artifacts import (
    CORE_DECISION_COLUMNS,
    DECISION_COLUMNS,
    EXECUTION_COLUMNS,
    TRADE_COLUMNS,
    ArtifactSchemaError,
    DecisionRow,
    DecisionWriter,
    ExecutionRow,
    ExecutionWriter,
    LiveArtifactWriters,
    TradeRow,
    TradeWriter,
    resolve_decision_columns,
)
from app.engine.live.reconcile import (
    load_python_decisions,
    load_python_executions,
)
from app.engine.strategy.spec.schema import DecisionColumnSpec


def _ema_decision(
    *,
    bar_close_ms: int,
    signal: str,
    intended_price: float,
    ema5: float = 0.0,
    ema10: float = 0.0,
    rsi: float = 50.0,
) -> DecisionRow:
    """Build a SPY-EMA DecisionRow from the legacy (ema5/ema10/rsi) shape.

    The indicator columns are now generic (``indicator_values`` keyed by
    the spec's decision_columns); this helper keeps the EMA-flavoured
    tests readable.
    """
    return DecisionRow(
        bar_close_ms=bar_close_ms,
        signal=signal,
        intended_price=intended_price,
        indicator_values={"ema5": ema5, "ema10": ema10, "rsi": rsi},
    )


# ──────────────────────────── resolve_decision_columns ───────────────


def test_resolve_decision_columns_composes_core_plus_strategy() -> None:
    cols = resolve_decision_columns(
        _spec_with_decision_columns(
            [DecisionColumnSpec(name="ema5"), DecisionColumnSpec(name="rsi")]
        )
    )
    assert cols == (*CORE_DECISION_COLUMNS, "ema5", "rsi")


def test_resolve_decision_columns_rejects_core_collision() -> None:
    with pytest.raises(ArtifactSchemaError, match="reserved core columns"):
        resolve_decision_columns(
            _spec_with_decision_columns([DecisionColumnSpec(name="bar_close_ms")])
        )


def test_resolve_decision_columns_two_strategies_differ() -> None:
    """A second strategy with different indicators resolves to a
    different parquet matrix — no bespoke artifacts.py edit."""
    a = resolve_decision_columns(
        _spec_with_decision_columns([DecisionColumnSpec(name="ema5")])
    )
    b = resolve_decision_columns(
        _spec_with_decision_columns(
            [DecisionColumnSpec(name="vwap"), DecisionColumnSpec(name="band_upper")]
        )
    )
    assert a[-1:] == ("ema5",)
    assert b[-2:] == ("vwap", "band_upper")
    assert a[: len(CORE_DECISION_COLUMNS)] == b[: len(CORE_DECISION_COLUMNS)]


def _spec_with_decision_columns(decision_columns: list[DecisionColumnSpec]):
    from app.engine.strategy.spec.schema import StrategySpec

    return StrategySpec(
        schema_version="1.0",
        name="synthetic",
        symbols=["SPY"],
        resolution={"period_minutes": 15},
        indicators=[],
        entry={"logic": "AND", "conditions": [], "size": {"kind": "SetHoldings", "fraction": 1.0}},
        exit={"logic": "OR", "conditions": []},
        decision_columns=decision_columns,
    )


# ──────────────────────────── DecisionWriter ─────────────────────────


def test_decision_writer_appends_and_flushes(tmp_path: Path) -> None:
    writer = DecisionWriter(tmp_path / "decisions.parquet")
    writer.append_row(
        _ema_decision(
            bar_close_ms=1_700_000_000_000,
            ema5=501.0,
            ema10=500.0,
            rsi=62.0,
            signal="ENTER",
            intended_price=501.0,
        )
    )
    assert writer.buffered == 1
    writer.flush()
    assert writer.buffered == 0

    df = pd.read_parquet(tmp_path / "decisions.parquet")
    assert list(df.columns) == list(DECISION_COLUMNS)
    assert len(df) == 1
    assert df.iloc[0]["signal"] == "ENTER"


def test_decision_writer_appends_across_two_flushes(tmp_path: Path) -> None:
    """Second flush should append to the file, not overwrite it."""
    writer = DecisionWriter(tmp_path / "decisions.parquet")
    for i in range(3):
        writer.append_row(
            _ema_decision(
                bar_close_ms=1_700_000_000_000 + i * 900_000,
                ema5=501.0 + i,
                ema10=500.0 + i,
                rsi=62.0,
                signal="HOLD",
                intended_price=501.0 + i,
            )
        )
    writer.flush()

    writer.append_row(
        _ema_decision(
            bar_close_ms=1_700_000_000_000 + 5 * 900_000,
            ema5=510.0,
            ema10=505.0,
            rsi=68.0,
            signal="ENTER",
            intended_price=510.0,
        )
    )
    writer.flush()

    df = pd.read_parquet(tmp_path / "decisions.parquet")
    assert len(df) == 4
    assert list(df["signal"]) == ["HOLD", "HOLD", "HOLD", "ENTER"]


def test_decision_writer_round_trips_through_reconcile_loader(tmp_path: Path) -> None:
    """The reconcile loader must accept what the writer produces — schema lock."""
    writer = DecisionWriter(tmp_path / "decisions.parquet")
    writer.append_row(
        _ema_decision(
            bar_close_ms=1_700_000_000_000,
            ema5=501.0,
            ema10=500.0,
            rsi=62.0,
            signal="ENTER",
            intended_price=501.0,
        )
    )
    writer.close()

    loaded = load_python_decisions(tmp_path / "decisions.parquet")
    assert len(loaded) == 1
    assert loaded.iloc[0]["signal"] == "ENTER"


def test_decision_writer_rejects_unknown_signal() -> None:
    with pytest.raises(ArtifactSchemaError):
        _ema_decision(bar_close_ms=0, signal="MAYBE", intended_price=0.0)


def test_decision_row_rejects_indicator_core_collision() -> None:
    with pytest.raises(ArtifactSchemaError, match="collide with core"):
        DecisionRow(
            bar_close_ms=0,
            signal="HOLD",
            intended_price=0.0,
            indicator_values={"signal": 1.0},
        )


def test_decision_writer_rejects_missing_indicator_column(tmp_path: Path) -> None:
    """A strategy that omits a declared indicator column fails fast."""
    writer = DecisionWriter(tmp_path / "decisions.parquet")  # default EMA schema
    with pytest.raises(ArtifactSchemaError, match="missing required columns"):
        writer.append_row(
            DecisionRow(
                bar_close_ms=0,
                signal="HOLD",
                intended_price=0.0,
                indicator_values={"ema5": 1.0, "ema10": 1.0},  # missing rsi
            )
        )


def test_decision_writer_rejects_extra_columns(tmp_path: Path) -> None:
    writer = DecisionWriter(tmp_path / "decisions.parquet")
    with pytest.raises(ArtifactSchemaError, match="extra columns"):
        writer.append({**{c: 0 for c in DECISION_COLUMNS}, "rogue": 1})


def test_decision_writer_rejects_missing_columns(tmp_path: Path) -> None:
    writer = DecisionWriter(tmp_path / "decisions.parquet")
    incomplete = {c: 0 for c in DECISION_COLUMNS if c != "rsi"}
    with pytest.raises(ArtifactSchemaError, match="missing required columns"):
        writer.append(incomplete)


def test_decision_writer_close_is_idempotent(tmp_path: Path) -> None:
    writer = DecisionWriter(tmp_path / "decisions.parquet")
    writer.append_row(_ema_decision(bar_close_ms=0, signal="HOLD", intended_price=1.0, ema5=1.0, ema10=1.0))
    writer.close()
    writer.close()
    df = pd.read_parquet(tmp_path / "decisions.parquet")
    assert len(df) == 1


def test_decision_writer_append_after_close_raises(tmp_path: Path) -> None:
    writer = DecisionWriter(tmp_path / "decisions.parquet")
    writer.close()
    with pytest.raises(RuntimeError, match="append after close"):
        writer.append_row(_ema_decision(bar_close_ms=0, signal="HOLD", intended_price=0.0))


def test_decision_writer_uses_resolved_schema(tmp_path: Path) -> None:
    """A non-EMA resolved schema writes its own indicator columns."""
    cols = resolve_decision_columns(
        _spec_with_decision_columns([DecisionColumnSpec(name="vwap")])
    )
    writer = DecisionWriter(tmp_path / "decisions.parquet", cols)
    writer.append_row(
        DecisionRow(
            bar_close_ms=0,
            signal="HOLD",
            intended_price=1.0,
            indicator_values={"vwap": 12.34},
        )
    )
    writer.close()
    df = pd.read_parquet(tmp_path / "decisions.parquet")
    assert list(df.columns) == list(cols)
    assert df.iloc[0]["vwap"] == pytest.approx(12.34)


# ──────────────────────────── ExecutionWriter ────────────────────────


def test_execution_writer_round_trips_through_reconcile_loader(tmp_path: Path) -> None:
    writer = ExecutionWriter(tmp_path / "executions.parquet")
    writer.append_row(
        ExecutionRow(
            ts_ms=1_700_000_000_000,
            exec_id="exec-abc",
            perm_id=9001,
            client_order_id="live-1",
            account_id="DU1234",
            symbol="SPY",
            fill_quantity=200,
            fill_price=501.02,
            fee=1.0,
        )
    )
    writer.close()

    loaded = load_python_executions(tmp_path / "executions.parquet")
    assert len(loaded) == 1
    assert int(loaded.iloc[0]["fill_quantity"]) == 200
    assert loaded.iloc[0]["client_order_id"] == "live-1"
    # New PRD-A columns: a real broker fill is tagged broker_fill / NEXT_BAR_OPEN.
    assert loaded.iloc[0]["execution_source"] == "broker_fill"
    assert loaded.iloc[0]["fill_model"] == "NEXT_BAR_OPEN"


def test_execution_writer_columns_match_pinned_set(tmp_path: Path) -> None:
    writer = ExecutionWriter(tmp_path / "executions.parquet")
    writer.append_row(
        ExecutionRow(
            ts_ms=0, exec_id="x", perm_id=1, client_order_id="x",
            account_id="DU", symbol="SPY", fill_quantity=1, fill_price=1.0, fee=0.0,
        )
    )
    writer.flush()
    df = pd.read_parquet(tmp_path / "executions.parquet")
    assert list(df.columns) == list(EXECUTION_COLUMNS)


def test_execution_row_rejects_unknown_source() -> None:
    with pytest.raises(ArtifactSchemaError, match="execution_source"):
        ExecutionRow(
            ts_ms=0, exec_id="x", perm_id=1, client_order_id="x",
            account_id="DU", symbol="SPY", fill_quantity=1, fill_price=1.0, fee=0.0,
            execution_source="bogus",
        )


def test_execution_writer_records_shadow_sim(tmp_path: Path) -> None:
    writer = ExecutionWriter(tmp_path / "executions.parquet")
    writer.append_row(
        ExecutionRow(
            ts_ms=0, exec_id="s", perm_id=0, client_order_id="shadow-1",
            account_id="", symbol="SPY", fill_quantity=1, fill_price=1.0, fee=0.0,
            execution_source="shadow_sim", fill_model="BAR_CLOSE", source_bar_close_ms=123,
        )
    )
    writer.flush()
    df = pd.read_parquet(tmp_path / "executions.parquet")
    assert df.iloc[0]["execution_source"] == "shadow_sim"
    assert int(df.iloc[0]["source_bar_close_ms"]) == 123


# ──────────────────────────── TradeWriter ────────────────────────────


def test_trade_writer_columns_match_pinned_set(tmp_path: Path) -> None:
    writer = TradeWriter(tmp_path / "trades.parquet")
    writer.append_row(
        TradeRow(
            entry_time_ms=1_700_000_000_000,
            exit_time_ms=1_700_000_004_500,
            entry_price=500.0,
            exit_price=502.5,
            pnl_points=2.5,
        )
    )
    writer.close()
    df = pd.read_parquet(tmp_path / "trades.parquet")
    assert list(df.columns) == list(TRADE_COLUMNS)
    assert df.iloc[0]["pnl_points"] == pytest.approx(2.5)


# ──────────────────────────── LiveArtifactWriters bundle ─────────────


def test_live_artifact_writers_bundle_opens_three_writers_under_run_dir(tmp_path: Path) -> None:
    bundle = LiveArtifactWriters.open(tmp_path)
    bundle.decisions.append_row(_ema_decision(bar_close_ms=0, signal="HOLD", intended_price=1.0, ema5=1.0, ema10=1.0))
    bundle.executions.append_row(
        ExecutionRow(
            ts_ms=0, exec_id="x", perm_id=1, client_order_id="x",
            account_id="DU", symbol="SPY", fill_quantity=1, fill_price=1.0, fee=0.0,
        )
    )
    bundle.trades.append_row(
        TradeRow(
            entry_time_ms=0, exit_time_ms=1, entry_price=1.0, exit_price=2.0, pnl_points=1.0,
        )
    )
    bundle.close_all()

    assert (tmp_path / "decisions.parquet").exists()
    assert (tmp_path / "executions.parquet").exists()
    assert (tmp_path / "trades.parquet").exists()


def test_live_artifact_writers_flush_then_close_is_safe(tmp_path: Path) -> None:
    bundle = LiveArtifactWriters.open(tmp_path)
    bundle.decisions.append_row(_ema_decision(bar_close_ms=0, signal="HOLD", intended_price=1.0, ema5=1.0, ema10=1.0))
    bundle.flush_all()
    bundle.close_all()
    bundle.close_all()  # idempotent

    df = pd.read_parquet(tmp_path / "decisions.parquet")
    assert len(df) == 1


def test_empty_writer_close_does_not_create_file(tmp_path: Path) -> None:
    """No rows ⇒ no on-disk parquet. Avoids spurious empty files in the run dir."""
    writer = DecisionWriter(tmp_path / "decisions.parquet")
    writer.close()
    assert not (tmp_path / "decisions.parquet").exists()
