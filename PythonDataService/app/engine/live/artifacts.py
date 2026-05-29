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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

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

    def __post_init__(self) -> None:
        if self.execution_source not in EXECUTION_SOURCE_VALUES:
            raise ArtifactSchemaError(
                f"ExecutionRow.execution_source={self.execution_source!r} "
                f"not in {sorted(EXECUTION_SOURCE_VALUES)}"
            )


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
