"""The daily-rollup claim-miss race, against a real Postgres catalog (#2496).

The fake-catalog leg (tests/unit/data_lake/test_daily_rollup_claim_miss.py)
pins the same protocol; this leg proves the real ``refresh_complete_artifact``
compare-and-swap does what the fake models — a stale build publishes nothing
over a sibling's wider rollup. Skips without ``POSTGRES_URL``.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.ensure_data import _process_daily_trade_artifact, minute_bar_identity
from app.data_lake.run_materialization import _build_engine_run_spec
from app.data_lake.types import ArtifactIdentity
from app.engine.data.lean_format import write_lean_day_zip
from app.engine.data.trade_bar import TradeBar
from tests._helpers.fake_lake_catalog import point_lake_writer_at_tmp

SYMBOL = "DRYCAS"
DAY_ONE = date(2024, 5, 20)
DAY_TWO = date(2024, 5, 21)
DAY_THREE = date(2024, 5, 22)


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


async def _delete_scoped_rows() -> None:
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('DELETE FROM "DataLakeArtifacts" WHERE "Symbol" = $1', SYMBOL)
    finally:
        await conn.close()


@pytest.fixture
async def postgres_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(settings, "POSTGRES_URL", _postgres_url())
    await _delete_scoped_rows()
    root = point_lake_writer_at_tmp(tmp_path, monkeypatch)
    (root / "staging" / "raw").mkdir(parents=True, exist_ok=True)
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    yield root
    await catalog_client.close_pool()
    await _delete_scoped_rows()


def _daily_identity() -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol=SYMBOL,
        trading_date=None,
        resolution="daily",
        data_type="trade",
        provider="learn_ai_derived",
        price_adjustment_mode="raw",
    )


async def _seed_minute_day(spec, day: date, lake_root: Path):
    from app.lean_sidecar.trading_calendar import session_windows_ms_utc

    window_open = session_windows_ms_utc(day, day)[0].open_ms_utc
    price = Decimal("100")
    write_lean_day_zip(
        lake_root,
        SYMBOL,
        day,
        [TradeBar(symbol=SYMBOL, open=price, high=price, low=price, close=price, volume=10,
                  start_ms=window_open, end_ms=window_open + 60_000)],
    )
    file_path = f"equity/usa/minute/{SYMBOL.lower()}/{day:%Y%m%d}_trade.zip"
    payload = (lake_root / file_path).read_bytes()
    identity = minute_bar_identity(spec, symbol=SYMBOL, trading_date=day, data_type="trade")
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id="seed-worker",
        lease_ttl_ms=60_000,
        data_contract_hash="seed-contract",
        file_path=file_path,
    )
    assert artifact_id is not None
    completed = await catalog_client.complete_artifact(
        artifact_id,
        row_count=1,
        first_bar_start_ms=window_open,
        last_bar_start_ms=window_open,
        file_size_bytes=len(payload),
        file_sha256=hashlib.sha256(payload).hexdigest(),
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    assert completed


def _daily_row_count(lake_root: Path) -> int:
    import zipfile

    with zipfile.ZipFile(lake_root / "equity" / "usa" / "daily" / f"{SYMBOL.lower()}.zip") as zf:
        rows = zf.read(zf.namelist()[0]).decode("ascii").strip().splitlines()
    return len(rows)


@pytest.mark.asyncio
async def test_the_real_cas_keeps_the_siblings_wider_rollup(postgres_catalog: Path) -> None:
    spec = _build_engine_run_spec(symbol=SYMBOL, start=DAY_ONE, end=DAY_ONE, requester="test")
    identity = _daily_identity()
    lake_root = postgres_catalog

    await _seed_minute_day(spec, DAY_ONE, lake_root)
    sources = await catalog_client.select_coverage_minute_bars(
        market="usa", symbol=SYMBOL, data_type="trade",
        start_trading_date=None, end_trading_date=None, price_adjustment_mode="raw",
    )
    record, failure, _ = await _process_daily_trade_artifact(identity, sources, spec, lake_root)
    assert failure is None and record is not None
    narrow_snapshot = sources

    await _seed_minute_day(spec, DAY_TWO, lake_root)
    sources = await catalog_client.select_coverage_minute_bars(
        market="usa", symbol=SYMBOL, data_type="trade",
        start_trading_date=None, end_trading_date=None, price_adjustment_mode="raw",
    )
    wide_record, failure, _ = await _process_daily_trade_artifact(identity, sources, spec, lake_root)
    assert failure is None and wide_record is not None
    wide_sha = wide_record.file_sha256
    assert _daily_row_count(lake_root) == 2

    # A third capture moves the sources under every in-flight build.
    await _seed_minute_day(spec, DAY_THREE, lake_root)

    # The stale build (read before DAY_TWO existed) must publish nothing.
    record, failure, outcome = await _process_daily_trade_artifact(identity, narrow_snapshot, spec, lake_root)

    assert record is None
    assert failure is not None and failure.reason == "lease_timeout"
    assert "daily-trade sources changed while this build ran" in (failure.detail or "")
    assert _daily_row_count(lake_root) == 2
    assert hashlib.sha256(
        (lake_root / "equity" / "usa" / "daily" / f"{SYMBOL.lower()}.zip").read_bytes()
    ).hexdigest() == wide_sha

    # The retry rebuilds from the current sources onto all three sessions.
    current = await catalog_client.select_coverage_minute_bars(
        market="usa", symbol=SYMBOL, data_type="trade",
        start_trading_date=None, end_trading_date=None, price_adjustment_mode="raw",
    )
    record, failure, outcome = await _process_daily_trade_artifact(identity, current, spec, lake_root)
    assert failure is None and record is not None
    assert outcome == "refreshed"
    assert _daily_row_count(lake_root) == 3
