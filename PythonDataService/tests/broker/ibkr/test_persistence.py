"""Tests for app.broker.ibkr.persistence — Parquet writer."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrChainSnapshot, IbkrOptionQuote
from app.broker.ibkr.persistence import (
    NoopTickWriter,
    ParquetTickWriter,
    make_writer,
)


def _snapshot(symbol: str, as_of_ms: int) -> IbkrChainSnapshot:
    return IbkrChainSnapshot(
        symbol=symbol,
        expiry_ms=1_800_000_000_000,
        underlying_price=420.0,
        quotes=[
            IbkrOptionQuote(
                symbol=symbol,
                expiry_ms=1_800_000_000_000,
                strike=420.0,
                right="C",
                bid=1.20,
                ask=1.25,
                iv=0.21,
                delta=0.55,
                greeks_source="model",
                ts_ms=as_of_ms,
            )
        ],
        as_of_ms=as_of_ms,
    )


def test_make_writer_returns_noop_when_persist_off(tmp_path: Path) -> None:
    w = make_writer(persist=False, persist_dir=str(tmp_path))
    assert isinstance(w, NoopTickWriter)


def test_make_writer_returns_parquet_when_persist_on(tmp_path: Path) -> None:
    w = make_writer(persist=True, persist_dir=str(tmp_path / "ticks"))
    assert isinstance(w, ParquetTickWriter)
    assert (tmp_path / "ticks").exists()


@pytest.mark.asyncio
async def test_noop_writer_swallows_writes() -> None:
    w = NoopTickWriter()
    await w.write(_snapshot("SPY", 1_800_000_000_500))
    await w.flush()
    await w.close()


@pytest.mark.asyncio
async def test_parquet_writer_creates_partitioned_file(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")

    w = ParquetTickWriter(str(tmp_path), flush_every_n=10)
    # 2026-05-02 19:00:00 UTC
    from datetime import UTC, datetime

    as_of_ms = int(datetime(2026, 5, 2, 19, 0, tzinfo=UTC).timestamp() * 1000)
    await w.write(_snapshot("SPY", as_of_ms))
    await w.flush()

    out = tmp_path / "2026-05-02" / "SPY.parquet"
    assert out.exists()


@pytest.mark.asyncio
async def test_parquet_writer_appends_to_existing_file(tmp_path: Path) -> None:
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")

    w = ParquetTickWriter(str(tmp_path), flush_every_n=1)
    from datetime import UTC, datetime

    base_ms = int(datetime(2026, 5, 2, 19, 0, tzinfo=UTC).timestamp() * 1000)
    await w.write(_snapshot("SPY", base_ms))
    await w.flush()
    await w.write(_snapshot("SPY", base_ms + 250))
    await w.flush()

    out = tmp_path / "2026-05-02" / "SPY.parquet"
    df = pd.read_parquet(out)
    assert len(df) == 2


# ── B-07: atomic, serialized partition writes ──────────────────────────


def test_write_parquet_partition_leaves_no_tmp_debris(tmp_path: Path) -> None:
    """The atomic write goes through a sibling .tmp + rename and must leave no
    leftover .tmp file behind on success."""
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    from app.broker.ibkr.persistence import _write_parquet_partition

    out = tmp_path / "p.parquet"
    _write_parquet_partition(out, pd.DataFrame({"a": [1]}))
    _write_parquet_partition(out, pd.DataFrame({"a": [2]}))

    assert sorted(pd.read_parquet(out)["a"].tolist()) == [1, 2]
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_parquet_partition_failed_rename_keeps_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (B-07, crash-atomicity): if the write fails, the durable file
    must keep its prior rows — never be left truncated/corrupt."""
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    from app.broker.ibkr import persistence

    out = tmp_path / "p.parquet"
    persistence._write_parquet_partition(out, pd.DataFrame({"a": [1, 2]}))

    monkeypatch.setattr(
        persistence.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    )
    with pytest.raises(OSError):
        persistence._write_parquet_partition(out, pd.DataFrame({"a": [3]}))

    monkeypatch.undo()
    # The original file is untouched and fully readable.
    assert sorted(pd.read_parquet(out)["a"].tolist()) == [1, 2]


def test_write_parquet_partition_concurrent_appends_lose_no_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (B-07, concurrency): two threads appending to the same
    partition must not clobber each other.

    A small sleep injected into the read widens the read→write window. Without
    the per-path lock every writer reads the same baseline and the last write
    wins, dropping the others' rows. The lock serializes them so all survive.
    """
    import threading
    import time

    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    from app.broker.ibkr import persistence

    out = tmp_path / "p.parquet"
    real_read = pd.read_parquet

    def slow_read(path, *a, **k):
        time.sleep(0.02)
        return real_read(path, *a, **k)

    # Seed so every concurrent writer hits the read-modify-write path.
    persistence._write_parquet_partition(out, pd.DataFrame({"a": [0]}))
    monkeypatch.setattr(pd, "read_parquet", slow_read)

    def append(i: int) -> None:
        persistence._write_parquet_partition(out, pd.DataFrame({"a": [i]}))

    threads = [threading.Thread(target=append, args=(i,)) for i in range(1, 6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    monkeypatch.undo()
    assert sorted(real_read(out)["a"].tolist()) == [0, 1, 2, 3, 4, 5]
