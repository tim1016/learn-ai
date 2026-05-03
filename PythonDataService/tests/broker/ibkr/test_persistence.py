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
