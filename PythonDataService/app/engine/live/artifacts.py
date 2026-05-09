"""Per-run live-runtime artifact writers.

Pinned to the schemas the reconciler reads in
``app.engine.live.reconcile`` (decisions.parquet, executions.parquet,
trades.parquet — see that module's docstring for the column contract).

Three writer classes:
  * ``DecisionWriter``   — one row per consolidated 15-min bar
  * ``ExecutionWriter``  — one row per broker-reported fill event
  * ``TradeWriter``      — one row per closed trade (entry + exit pair)

Each writer is a thin file-backed buffer:
  * ``append(...)`` validates the row dict against the pinned column
    set and queues it in memory.
  * ``flush()`` materializes the queued rows into the on-disk parquet,
    appending to any existing rows (cumulative across the run).
  * ``close()`` final flush; safe to call multiple times.

The writers are intentionally simple: per-bar append cost is negligible
(microseconds), so we don't worry about chunked writes inside the
day. The end-of-session ``close()`` is the only path that touches the
file system in the steady state.

Phase C-2a (this PR) ships the writers as a standalone module with
unit tests. Wiring them into ``LiveEngine`` is Phase C-2b — see the
TODO in ``app/engine/live/live_engine.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


# ──────────────────────────── Schemas ────────────────────────────────


DECISION_COLUMNS = ("bar_close_ms", "ema5", "ema10", "rsi", "signal", "intended_price")
EXECUTION_COLUMNS = (
    "ts_ms",
    "exec_id",
    "perm_id",
    "client_order_id",
    "account_id",
    "symbol",
    "fill_quantity",
    "fill_price",
    "fee",
)
TRADE_COLUMNS = (
    "entry_time_ms",
    "exit_time_ms",
    "entry_price",
    "exit_price",
    "pnl_points",
)
SIGNAL_VALUES = {"ENTER", "EXIT", "HOLD"}


class ArtifactSchemaError(ValueError):
    """Raised when a row dict violates the pinned column set or signal vocabulary."""


# ──────────────────────────── Decision rows ──────────────────────────


@dataclass(frozen=True)
class DecisionRow:
    """One per consolidated 15-min bar.

    ``intended_price`` is the bar close used for share-count math at
    signal time; it carries a value on every row (HOLD bars too) so
    the reconciler can distinguish "no signal" from "no fill" without
    a NaN-or-not check on the price column. The reconciler suppresses
    fill-comparison on HOLD rows internally — see
    ``reconcile.build_reconciliation_table``.
    """

    bar_close_ms: int
    ema5: float
    ema10: float
    rsi: float
    signal: str
    intended_price: float

    def __post_init__(self) -> None:
        if self.signal not in SIGNAL_VALUES:
            raise ArtifactSchemaError(
                f"DecisionRow.signal={self.signal!r} not in {sorted(SIGNAL_VALUES)}"
            )


# ──────────────────────────── Execution rows ─────────────────────────


@dataclass(frozen=True)
class ExecutionRow:
    """One per broker-reported fill event.

    Indexed by the broker primary keys (``exec_id``, ``perm_id``,
    ``account_id``) per § 7 — that's how the intra-day fatal halt
    detects outside-mutation. ``client_order_id`` joins back to the
    Python-owned-orders table for ownership filtering.

    ``fill_quantity`` is signed: positive = buy, negative = sell.
    """

    ts_ms: int
    exec_id: str
    perm_id: int
    client_order_id: str
    account_id: str
    symbol: str
    fill_quantity: int
    fill_price: float
    fee: float


# ──────────────────────────── Trade rows ─────────────────────────────


@dataclass(frozen=True)
class TradeRow:
    """One per closed trade (entry/exit pair). PnL is in price points."""

    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    exit_price: float
    pnl_points: float


# ──────────────────────────── Writer base ────────────────────────────


class _ParquetAppendWriter:
    """File-backed buffered writer.

    Rows are appended to an in-memory list; ``flush()`` materializes
    them to disk. If the parquet already exists the writer reads the
    existing rows, concatenates, and rewrites — pyarrow's parquet
    format does not support O(1) row append, so this is the simplest
    correct implementation. For the live runtime's append cadence
    (≤ 26 decision rows + a handful of executions per RTH day),
    rewriting per flush is cheap.

    Subclasses provide the pinned column set; the writer enforces
    column completeness on ``append`` and column ordering on flush.
    """

    columns: tuple[str, ...] = ()

    def __init__(self, path: Path) -> None:
        self._path = path
        self._buffer: list[dict] = []
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def append(self, row: dict) -> None:
        if self._closed:
            raise RuntimeError(f"{type(self).__name__}: append after close")
        missing = set(self.columns) - row.keys()
        if missing:
            raise ArtifactSchemaError(
                f"{type(self).__name__}: row missing required columns {sorted(missing)}"
            )
        extra = set(row.keys()) - set(self.columns)
        if extra:
            raise ArtifactSchemaError(
                f"{type(self).__name__}: row has extra columns {sorted(extra)} "
                f"(pinned set is {self.columns})"
            )
        self._buffer.append({col: row[col] for col in self.columns})

    def flush(self) -> None:
        if not self._buffer:
            return
        new_df = pd.DataFrame(self._buffer, columns=list(self.columns))
        if self._path.exists():
            existing = pd.read_parquet(self._path)
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            combined = new_df
        combined.to_parquet(self._path, index=False)
        self._buffer.clear()

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True


# ──────────────────────────── Concrete writers ───────────────────────


class DecisionWriter(_ParquetAppendWriter):
    """Writes decisions.parquet — one row per consolidated 15-min bar."""

    columns = DECISION_COLUMNS

    def append_row(self, row: DecisionRow) -> None:
        self.append(
            {
                "bar_close_ms": int(row.bar_close_ms),
                "ema5": float(row.ema5),
                "ema10": float(row.ema10),
                "rsi": float(row.rsi),
                "signal": str(row.signal),
                "intended_price": float(row.intended_price),
            }
        )


class ExecutionWriter(_ParquetAppendWriter):
    """Writes executions.parquet — one row per broker fill event."""

    columns = EXECUTION_COLUMNS

    def append_row(self, row: ExecutionRow) -> None:
        self.append(
            {
                "ts_ms": int(row.ts_ms),
                "exec_id": str(row.exec_id),
                "perm_id": int(row.perm_id),
                "client_order_id": str(row.client_order_id),
                "account_id": str(row.account_id),
                "symbol": str(row.symbol),
                "fill_quantity": int(row.fill_quantity),
                "fill_price": float(row.fill_price),
                "fee": float(row.fee),
            }
        )


class TradeWriter(_ParquetAppendWriter):
    """Writes trades.parquet — one row per closed trade (entry/exit pair)."""

    columns = TRADE_COLUMNS

    def append_row(self, row: TradeRow) -> None:
        self.append(
            {
                "entry_time_ms": int(row.entry_time_ms),
                "exit_time_ms": int(row.exit_time_ms),
                "entry_price": float(row.entry_price),
                "exit_price": float(row.exit_price),
                "pnl_points": float(row.pnl_points),
            }
        )


# ──────────────────────────── Bundle ─────────────────────────────────


@dataclass
class LiveArtifactWriters:
    """Convenience bundle — open all three writers under one ``run_dir``.

    Wire-in pattern (Phase C-2b):
        writers = LiveArtifactWriters.open(run_dir)
        try:
            ...drive the live engine, calling
            writers.decisions.append_row(...) per bar,
            writers.executions.append_row(...) per fill,
            writers.trades.append_row(...) per closed trade...
        finally:
            writers.close_all()
    """

    decisions: DecisionWriter
    executions: ExecutionWriter
    trades: TradeWriter

    @classmethod
    def open(cls, run_dir: Path) -> LiveArtifactWriters:
        return cls(
            decisions=DecisionWriter(run_dir / "decisions.parquet"),
            executions=ExecutionWriter(run_dir / "executions.parquet"),
            trades=TradeWriter(run_dir / "trades.parquet"),
        )

    def flush_all(self) -> None:
        self.decisions.flush()
        self.executions.flush()
        self.trades.flush()

    def close_all(self) -> None:
        self.decisions.close()
        self.executions.close()
        self.trades.close()
