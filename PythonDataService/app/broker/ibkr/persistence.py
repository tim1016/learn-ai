"""Tick-stream persistence — gated behind ``IBKR_PERSIST_TICKS``.

Phase 1 status: **stub**. Persisting every option-chain tick is in
scope per the user's "yes" decision, but the schema decision (CSV vs
Parquet, partition layout, retention policy, replay tooling) deserves a
separate, explicit follow-up commit. The goal here is to put the seam
in place — a ``TickWriter`` ABC with a ``ParquetTickWriter``
implementation that buffers and flushes per-day Parquet files — without
silently committing schema choices that are hard to reverse.

What works in Phase 1:
* The writer is wired through ``IbkrSettings.persist_ticks``.
* The ``NoopTickWriter`` is the default; flipping the flag swaps in the
  Parquet writer.
* The Parquet writer's flush logic is intentionally minimal: append-on-
  flush, one file per (date, symbol). No compaction, no upserts.

Phase 1.5 follow-ups, captured here so we don't lose them:
* Decide whether ticks live alongside vol-surface fixtures
  (``tests/fixtures/...``) or under a runtime ``/data/...`` mount.
* Replay tool: ``app/broker/ibkr/replay.py`` to feed persisted ticks
  back through the same chain-stream contract for offline UI work.
* Postgres landing zone if forensic SQL queries become necessary.

Per ``rules/numerical-rigor.md`` timestamp policy: all stored timestamps
are ``int64`` ms UTC. No ISO strings, no naive datetimes.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrChainSnapshot,
    IbkrPnLTick,
)

logger = logging.getLogger(__name__)


class TickWriter(ABC):
    """Append-only sink for chain snapshots."""

    @abstractmethod
    async def write(self, snapshot: IbkrChainSnapshot) -> None: ...

    @abstractmethod
    async def flush(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class NoopTickWriter(TickWriter):
    """Default writer — discards everything. Used when the flag is off."""

    async def write(self, snapshot: IbkrChainSnapshot) -> None:
        return

    async def flush(self) -> None:
        return

    async def close(self) -> None:
        return


class ParquetTickWriter(TickWriter):
    """In-memory buffer, flushed to ``{persist_dir}/{date}/{symbol}.parquet``.

    Phase 1 minimal viable shape:
    * Buffer rows in a list-of-dicts.
    * Flush on ``flush()`` or every ``flush_every_n`` writes.
    * One file per (UTC date, symbol). New writes append to the file
      via ``pyarrow.parquet.write_to_dataset`` style — Phase 1.5 will
      replace this with explicit row-group append once the schema
      stabilises.

    Deliberately not implemented yet: compression choice, partition
    layout, schema versioning, replay surface. See module docstring.
    """

    def __init__(self, persist_dir: str, flush_every_n: int = 200) -> None:
        self._dir = Path(persist_dir)
        self._buffer: list[dict] = []
        self._flush_every = max(1, flush_every_n)

    async def write(self, snapshot: IbkrChainSnapshot) -> None:
        for q in snapshot.quotes:
            self._buffer.append(
                {
                    "as_of_ms": snapshot.as_of_ms,
                    "symbol": snapshot.symbol,
                    "expiry_ms": snapshot.expiry_ms,
                    "underlying_price": snapshot.underlying_price,
                    "strike": q.strike,
                    "right": q.right,
                    "bid": q.bid,
                    "ask": q.ask,
                    "last": q.last,
                    "bid_size": q.bid_size,
                    "ask_size": q.ask_size,
                    "iv": q.iv,
                    "delta": q.delta,
                    "gamma": q.gamma,
                    "theta": q.theta,
                    "vega": q.vega,
                    "greeks_source": q.greeks_source,
                    "ts_ms": q.ts_ms,
                }
            )
        if len(self._buffer) >= self._flush_every:
            await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            return
        # Lazy imports — pyarrow / pandas are heavy; only pay the cost
        # when persistence is actually enabled.
        import pandas as pd

        df = pd.DataFrame(self._buffer)
        if df.empty:
            self._buffer.clear()
            return

        # Partition by UTC date of as_of_ms for a clean per-day file.
        df["_date"] = pd.to_datetime(df["as_of_ms"], unit="ms", utc=True).dt.strftime(
            "%Y-%m-%d"
        )
        for (date_str, symbol), part in df.groupby(["_date", "symbol"]):
            out_dir = self._dir / date_str
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{symbol}.parquet"
            part = part.drop(columns=["_date"])
            try:
                if out_path.exists():
                    existing = pd.read_parquet(out_path)
                    combined = pd.concat([existing, part], ignore_index=True)
                else:
                    combined = part
                combined.to_parquet(out_path, index=False)
            except Exception as exc:
                logger.error(
                    "ParquetTickWriter flush failed for %s: %s",
                    out_path,
                    exc,
                )
        self._buffer.clear()
        logger.debug("Flushed tick buffer to %s", self._dir)

    async def close(self) -> None:
        await self.flush()


def make_writer(persist: bool, persist_dir: str) -> TickWriter:
    """Factory honoured by the router and tests."""
    if not persist:
        return NoopTickWriter()
    Path(persist_dir).mkdir(parents=True, exist_ok=True)
    logger.info(
        "ParquetTickWriter active. Tick archive: %s (started at %s)",
        persist_dir,
        datetime.now(tz=UTC).isoformat(),
    )
    return ParquetTickWriter(persist_dir)


# ── Phase 2c: account + P&L writers ────────────────────────────────────


class AccountSnapshotWriter(ABC):
    """Append-only sink for ``IbkrAccountSummary`` snapshots."""

    @abstractmethod
    async def write(self, snapshot: IbkrAccountSummary) -> None: ...

    @abstractmethod
    async def flush(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class NoopAccountWriter(AccountSnapshotWriter):
    """Default account writer — discards everything."""

    async def write(self, snapshot: IbkrAccountSummary) -> None:
        return

    async def flush(self) -> None:
        return

    async def close(self) -> None:
        return


class ParquetAccountWriter(AccountSnapshotWriter):
    """``{persist_dir}/{date}/account.parquet`` — one row per snapshot.

    Same minimal-shape principle as ``ParquetTickWriter`` (Phase 1):
    flush on demand, append-merge on existing file, no compaction.
    """

    def __init__(self, persist_dir: str, flush_every_n: int = 50) -> None:
        self._dir = Path(persist_dir)
        self._buffer: list[dict] = []
        self._flush_every = max(1, flush_every_n)

    async def write(self, snapshot: IbkrAccountSummary) -> None:
        self._buffer.append(snapshot.model_dump())
        if len(self._buffer) >= self._flush_every:
            await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            return
        import pandas as pd

        df = pd.DataFrame(self._buffer)
        df["_date"] = pd.to_datetime(df["fetched_at_ms"], unit="ms", utc=True).dt.strftime(
            "%Y-%m-%d"
        )
        for date_str, part in df.groupby("_date"):
            out_dir = self._dir / date_str
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "account.parquet"
            part = part.drop(columns=["_date"])
            try:
                if out_path.exists():
                    existing = pd.read_parquet(out_path)
                    combined = pd.concat([existing, part], ignore_index=True)
                else:
                    combined = part
                combined.to_parquet(out_path, index=False)
            except Exception as exc:
                logger.error("ParquetAccountWriter flush failed for %s: %s", out_path, exc)
        self._buffer.clear()
        logger.debug("Flushed account buffer to %s", self._dir)

    async def close(self) -> None:
        await self.flush()


def make_account_writer(persist: bool, persist_dir: str) -> AccountSnapshotWriter:
    """Phase 2c factory — flag-gated, default OFF."""
    if not persist:
        return NoopAccountWriter()
    Path(persist_dir).mkdir(parents=True, exist_ok=True)
    logger.info(
        "ParquetAccountWriter active. Archive: %s (started at %s)",
        persist_dir,
        datetime.now(tz=UTC).isoformat(),
    )
    return ParquetAccountWriter(persist_dir)


class PnLTickWriter(ABC):
    """Append-only sink for ``IbkrPnLTick`` rows.

    Same row schema for account-level (``con_id`` NULL) and per-position;
    consumers split on ``con_id`` IS NULL when querying the parquet.
    """

    @abstractmethod
    async def write(self, tick: IbkrPnLTick) -> None: ...

    @abstractmethod
    async def flush(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class NoopPnLWriter(PnLTickWriter):
    async def write(self, tick: IbkrPnLTick) -> None:
        return

    async def flush(self) -> None:
        return

    async def close(self) -> None:
        return


class ParquetPnLWriter(PnLTickWriter):
    """``{persist_dir}/{date}/pnl_{account_id}.parquet`` — one row per tick."""

    def __init__(self, persist_dir: str, flush_every_n: int = 200) -> None:
        self._dir = Path(persist_dir)
        self._buffer: list[dict] = []
        self._flush_every = max(1, flush_every_n)

    async def write(self, tick: IbkrPnLTick) -> None:
        self._buffer.append(tick.model_dump())
        if len(self._buffer) >= self._flush_every:
            await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            return
        import pandas as pd

        df = pd.DataFrame(self._buffer)
        df["_date"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True).dt.strftime(
            "%Y-%m-%d"
        )
        for (date_str, account_id), part in df.groupby(["_date", "account_id"]):
            out_dir = self._dir / date_str
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"pnl_{account_id}.parquet"
            part = part.drop(columns=["_date"])
            try:
                if out_path.exists():
                    existing = pd.read_parquet(out_path)
                    combined = pd.concat([existing, part], ignore_index=True)
                else:
                    combined = part
                combined.to_parquet(out_path, index=False)
            except Exception as exc:
                logger.error("ParquetPnLWriter flush failed for %s: %s", out_path, exc)
        self._buffer.clear()
        logger.debug("Flushed pnl buffer to %s", self._dir)

    async def close(self) -> None:
        await self.flush()


def make_pnl_writer(persist: bool, persist_dir: str) -> PnLTickWriter:
    """Phase 2c factory — flag-gated, default OFF."""
    if not persist:
        return NoopPnLWriter()
    Path(persist_dir).mkdir(parents=True, exist_ok=True)
    logger.info(
        "ParquetPnLWriter active. Archive: %s (started at %s)",
        persist_dir,
        datetime.now(tz=UTC).isoformat(),
    )
    return ParquetPnLWriter(persist_dir)
