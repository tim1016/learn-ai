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
  * ``flush()`` materializes the queued rows into a new on-disk parquet
    segment under the stable ``*.parquet`` dataset path.
  * ``close()`` final flush; safe to call multiple times.

The writers publish one atomically-replaced segment per flush so a crash
mid-write cannot corrupt previously durable rows. Pandas/pyarrow reads
``decisions.parquet`` / ``executions.parquet`` / ``trades.parquet`` as
dataset directories, preserving the public artifact paths while avoiding
whole-file in-place rewrites.

Phase C-2a (this PR) ships the writers as a standalone module with
unit tests. Wiring them into ``LiveEngine`` is Phase C-2b — see the
TODO in ``app/engine/live/live_engine.py``.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from app.engine.live.live_state_sidecar import _fsync_parent_dir

if TYPE_CHECKING:
    from app.engine.strategy.spec.schema import StrategySpec

logger = logging.getLogger(__name__)


# ──────────────────────────── Schemas ────────────────────────────────


# Universal decision-row prefix — present for every strategy, in this
# exact order (PRD-A §16.1 Resolution 5). Strategy-specific indicator
# columns are appended after the core by ``resolve_decision_columns``.
CORE_DECISION_COLUMNS = (
    "run_id",
    "strategy_key",
    "strategy_instance_id",
    "bar_close_ms",
    "bar_source",
    "bar_open",
    "bar_high",
    "bar_low",
    "bar_close",
    "bar_volume",
    "signal",
    "intended_action",
    "intended_price",
    "intended_fill_model",
    "decision_latency_ms",
    "mode",
)

# Default strategy-specific columns for the SPY EMA crossover — the only
# live strategy today. Used when a run is driven without a StrategySpec
# (replay / parity tests); the live ``start`` path resolves the columns
# from the spec instead (see ``run.py`` / ``resolve_decision_columns``).
DEFAULT_STRATEGY_DECISION_COLUMNS = ("ema5", "ema10", "rsi")

# The concrete EMA decision schema = core + EMA indicators. Kept as a
# module constant because the reconciler's loader and the schema tests
# reference the EMA shape directly.
DECISION_COLUMNS = CORE_DECISION_COLUMNS + DEFAULT_STRATEGY_DECISION_COLUMNS
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
    # PRD-A §16.1 Resolution 5: discriminate real broker fills from
    # shadow simulated fills, and record the fill model + the source
    # bar a shadow fill was synthesised from (NULL for broker fills).
    "execution_source",
    "fill_model",
    "source_bar_close_ms",
    # VCR-P3-L — broker-reported execution time (int64 ms UTC) for live
    # fills, NULL for shadow / backtest fills. ``ts_ms`` above stays the
    # engine's wall-clock at receipt for backward compatibility; latency
    # analysis and reconciliation joins read ``exec_time_ms`` first and
    # fall back to ``ts_ms`` when NULL (e.g. older parquet files written
    # before this column existed).
    "exec_time_ms",
)
EXECUTION_SOURCE_VALUES = {"broker_fill", "shadow_sim"}
TRADE_COLUMNS = (
    "entry_time_ms",
    "exit_time_ms",
    "entry_price",
    "exit_price",
    "pnl_points",
)
SIGNAL_VALUES = {"ENTER", "EXIT", "HOLD"}
_SEGMENT_PREFIX = "part-"
_SEGMENT_SUFFIX = ".parquet"


class ArtifactSchemaError(ValueError):
    """Raised when a row dict violates the pinned column set or signal vocabulary."""


def resolve_decision_columns(spec: StrategySpec) -> tuple[str, ...]:
    """Resolve a strategy's decisions.parquet schema from its spec.

    The DecisionSchemaResolver of PRD-A §16.1 Resolution 5: the column
    list is ``CORE_DECISION_COLUMNS + [c.name for c in
    spec.decision_columns]``. The spec is authoritative — adding a new
    strategy with different indicators is a spec edit, not an
    ``artifacts.py`` edit.

    Raises ``ArtifactSchemaError`` if a strategy-specific column name
    collides with a reserved core column, or duplicates another
    strategy column (the latter is also caught by the StrategySpec
    validator; re-checked here so the resolver is safe in isolation).
    """
    strat_names = tuple(c.name for c in spec.decision_columns)
    collisions = sorted(set(strat_names) & set(CORE_DECISION_COLUMNS))
    if collisions:
        raise ArtifactSchemaError(
            f"decision_columns collide with reserved core columns: {collisions}"
        )
    if len(strat_names) != len(set(strat_names)):
        dup = sorted({n for n in strat_names if strat_names.count(n) > 1})
        raise ArtifactSchemaError(f"duplicate decision_columns names: {dup}")
    return CORE_DECISION_COLUMNS + strat_names


# ──────────────────────────── Decision rows ──────────────────────────


@dataclass(frozen=True)
class DecisionRow:
    """One per consolidated 15-min bar.

    Carries the universal ``CORE_DECISION_COLUMNS`` plus a generic
    ``indicator_values`` dict for the strategy-specific columns the
    spec declares (PRD-A §16.1 Resolution 5). The core context fields
    (``run_id`` / ``strategy_*`` / ``mode`` / ``bar_source``) default to
    empty so replay and unit tests can build a row from the minimal
    decision data; the live runtime populates them from the run ledger.

    ``intended_price`` carries a value on every row (HOLD bars too) so
    the reconciler can distinguish "no signal" from "no fill" without a
    NaN-or-not check on the price column. The reconciler suppresses
    fill-comparison on HOLD rows internally — see
    ``reconcile.build_reconciliation_table``.

    The bar OHLCV / latency fields are nullable (``None`` ⇒ NaN in the
    parquet) — present in the schema for the Layer B divergence
    baseline, populated when the engine has the consolidated bar.
    """

    bar_close_ms: int
    signal: str
    intended_price: float
    # Core context — defaulted so a row is constructible from minimal data.
    run_id: str = ""
    strategy_key: str = ""
    strategy_instance_id: str = ""
    bar_source: str = ""
    bar_open: float | None = None
    bar_high: float | None = None
    bar_low: float | None = None
    bar_close: float | None = None
    bar_volume: float | None = None
    intended_action: str = ""
    intended_fill_model: str = ""
    decision_latency_ms: float | None = None
    mode: str = ""
    # Strategy-specific columns, keyed by the names spec.decision_columns
    # declares (e.g. {"ema5": .., "ema10": .., "rsi": ..} for SPY EMA).
    # Values pass through to the parquet with their own type so a spec's
    # declared dtype (float64 / int64 / string / bool) is preserved.
    indicator_values: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.signal not in SIGNAL_VALUES:
            raise ArtifactSchemaError(
                f"DecisionRow.signal={self.signal!r} not in {sorted(SIGNAL_VALUES)}"
            )
        collisions = sorted(set(self.indicator_values) & set(CORE_DECISION_COLUMNS))
        if collisions:
            raise ArtifactSchemaError(
                f"DecisionRow.indicator_values collide with core columns: {collisions}"
            )

    def as_row(self) -> dict:
        """Flatten to the parquet row dict: core columns + indicator values.

        The writer validates this against its resolved column set, so a
        strategy that emits the wrong indicator keys fails fast (extra
        or missing columns) rather than writing a silently-wrong schema.
        """
        row: dict = {
            "run_id": str(self.run_id),
            "strategy_key": str(self.strategy_key),
            "strategy_instance_id": str(self.strategy_instance_id),
            "bar_close_ms": int(self.bar_close_ms),
            "bar_source": str(self.bar_source),
            "bar_open": _opt_float(self.bar_open),
            "bar_high": _opt_float(self.bar_high),
            "bar_low": _opt_float(self.bar_low),
            "bar_close": _opt_float(self.bar_close),
            "bar_volume": _opt_float(self.bar_volume),
            "signal": str(self.signal),
            "intended_action": str(self.intended_action),
            "intended_price": float(self.intended_price),
            "intended_fill_model": str(self.intended_fill_model),
            "decision_latency_ms": _opt_float(self.decision_latency_ms),
            "mode": str(self.mode),
        }
        # Strategy-specific values pass through with their own type so a
        # spec declaring dtype "string" / "bool" / "int64" isn't coerced
        # to float (or crashed by float("...")); the strategy is
        # responsible for emitting values matching its declared dtypes.
        row.update(dict(self.indicator_values))
        return row


def _opt_float(value: float | None) -> float | None:
    return None if value is None else float(value)


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
    # PRD-A §16.1 Resolution 5. Defaults describe a real SPY EMA broker
    # fill; the shadow adapter (PRD-C) sets execution_source="shadow_sim"
    # with the synthesised fill model + the source bar it filled from.
    execution_source: str = "broker_fill"
    fill_model: str = "NEXT_BAR_OPEN"
    source_bar_close_ms: int | None = None
    # VCR-P3-L — broker-reported execution time. ``ts_ms`` records the
    # engine's wall-clock at fill receipt; ``exec_time_ms`` records what
    # IBKR's ``Execution.time`` reported. They differ by network +
    # event-loop latency; for live latency analysis and post-restart
    # reconciliation joins the broker time is authoritative. ``None`` on
    # backtest / shadow fills (no broker).
    exec_time_ms: int | None = None

    def __post_init__(self) -> None:
        if self.execution_source not in EXECUTION_SOURCE_VALUES:
            raise ArtifactSchemaError(
                f"ExecutionRow.execution_source={self.execution_source!r} "
                f"not in {sorted(EXECUTION_SOURCE_VALUES)}"
            )


def commission_observed_count(fees: Iterable[float | None]) -> int:
    """COMMISSION_OBSERVED metric (PRD-B story 2): count of fills whose
    commission was successfully captured — fee present and not NaN.

    Derived from the execution artifact's ``fee`` column so the count can
    never drift from what was actually recorded. A NaN fee is a commission
    the broker has not yet reported (the commissionReport had not arrived
    when the fill was written); it does not count as observed.
    """
    return sum(1 for f in fees if f is not None and not math.isnan(float(f)))


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
    them to disk as a new parquet dataset segment. The stable public
    path remains ``*.parquet`` because pyarrow can read a directory of
    parquet parts at that path. Legacy single-file parquet paths are
    still appendable through an atomic read-concat-replace fallback.

    Subclasses provide the pinned column set; the writer enforces
    column completeness on ``append`` and column ordering on flush.
    """

    columns: tuple[str, ...] = ()

    def __init__(self, path: Path) -> None:
        self._path = path
        self._buffer: list[dict] = []
        self._closed = False
        self._next_segment_index: int | None = None

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
        if self._path.is_file():
            existing = pd.read_parquet(self._path)
            combined = pd.concat([existing, new_df], ignore_index=True)
            _write_parquet_file_atomic(self._path, combined)
        else:
            segment_name = self._next_segment_name()
            _write_parquet_segment_atomic(self._path, segment_name, new_df)
        self._buffer.clear()

    def _next_segment_name(self) -> str:
        if self._next_segment_index is None:
            self._next_segment_index = _max_segment_index(self._path) + 1
        while True:
            segment_name = f"{_SEGMENT_PREFIX}{self._next_segment_index:06d}{_SEGMENT_SUFFIX}"
            self._next_segment_index += 1
            if not (self._path / segment_name).exists():
                return segment_name

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True


# ──────────────────────────── Concrete writers ───────────────────────


class DecisionWriter(_ParquetAppendWriter):
    """Writes decisions.parquet — one row per consolidated 15-min bar.

    The column set is resolved from the strategy spec at run init
    (``resolve_decision_columns``) and passed in here; it defaults to
    the SPY EMA schema (``DECISION_COLUMNS``) for the no-spec replay /
    parity path. ``append_row`` flattens the DecisionRow and the base
    writer enforces the row matches this exact column set — so a
    strategy emitting the wrong indicator keys fails fast.
    """

    def __init__(self, path: Path, columns: tuple[str, ...] = DECISION_COLUMNS) -> None:
        super().__init__(path)
        self.columns = columns

    def append_row(self, row: DecisionRow) -> None:
        self.append(row.as_row())


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
                "execution_source": str(row.execution_source),
                "fill_model": str(row.fill_model),
                "source_bar_close_ms": (
                    None if row.source_bar_close_ms is None else int(row.source_bar_close_ms)
                ),
                "exec_time_ms": (
                    None if row.exec_time_ms is None else int(row.exec_time_ms)
                ),
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


# ──────────────────────────── Atomic parquet publish ─────────────────


def _max_segment_index(dataset_dir: Path) -> int:
    if not dataset_dir.is_dir():
        return 0
    max_index = 0
    for child in dataset_dir.iterdir():
        name = child.name
        if not (
            name.startswith(_SEGMENT_PREFIX)
            and name.endswith(_SEGMENT_SUFFIX)
            and child.is_file()
        ):
            continue
        index_text = name[len(_SEGMENT_PREFIX) : -len(_SEGMENT_SUFFIX)]
        if index_text.isdecimal():
            max_index = max(max_index, int(index_text))
    return max_index


def _write_parquet_file_atomic(path: Path, frame: pd.DataFrame) -> None:
    """Crash-safe fallback for appending to a legacy single-file parquet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        frame.to_parquet(tmp_path, index=False)
        _fsync_file(tmp_path)
        os.replace(tmp_path, path)
        _fsync_parent_dir(path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _write_parquet_segment_atomic(
    dataset_dir: Path,
    segment_name: str,
    frame: pd.DataFrame,
) -> None:
    dataset_dir.parent.mkdir(parents=True, exist_ok=True)
    if dataset_dir.exists():
        _write_segment_into_existing_dataset(dataset_dir, segment_name, frame)
        return

    tmp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{dataset_dir.name}.",
            suffix=".tmp",
            dir=str(dataset_dir.parent),
        )
    )
    try:
        segment_path = tmp_dir / segment_name
        frame.to_parquet(segment_path, index=False)
        _fsync_file(segment_path)
        _fsync_parent_dir(segment_path)
        os.replace(tmp_dir, dataset_dir)
        _fsync_parent_dir(dataset_dir)
    except Exception:
        with contextlib.suppress(OSError):
            shutil.rmtree(tmp_dir)
        raise


def _write_segment_into_existing_dataset(
    dataset_dir: Path,
    segment_name: str,
    frame: pd.DataFrame,
) -> None:
    tmp_path = dataset_dir.parent / f".{dataset_dir.name}.{segment_name}.tmp"
    final_path = dataset_dir / segment_name
    try:
        frame.to_parquet(tmp_path, index=False)
        _fsync_file(tmp_path)
        os.replace(tmp_path, final_path)
        _fsync_parent_dir(final_path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _fsync_file(path: Path) -> None:
    with path.open("rb") as fh:
        os.fsync(fh.fileno())


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
    def open(
        cls,
        run_dir: Path,
        decision_columns: tuple[str, ...] = DECISION_COLUMNS,
    ) -> LiveArtifactWriters:
        return cls(
            decisions=DecisionWriter(run_dir / "decisions.parquet", decision_columns),
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
