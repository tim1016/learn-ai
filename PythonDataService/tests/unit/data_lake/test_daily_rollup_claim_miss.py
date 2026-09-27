"""A daily-trade rollup never overwrites a newer one a sibling published (#2496).

``_process_daily_trade_artifact`` builds from the sources it read before
claiming. When a sibling publishes a WIDER rollup between this build's source
read and its claim miss, the stale build must return ``lease_timeout`` and
publish nothing — the wider zip survives, and the retry rebuilds from the
current sources. On master the claim miss refreshed the row without
re-reading the sources and without the compare-and-swap, so the older build
published its narrower zip over the sibling's wider one.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.data_lake import catalog_client
from app.data_lake.ensure_data import _process_daily_trade_artifact, minute_bar_identity
from app.data_lake.run_materialization import _build_engine_run_spec
from app.data_lake.types import ArtifactIdentity, ArtifactRecord
from app.engine.data.lean_format import write_lean_day_zip
from app.engine.data.trade_bar import TradeBar
from tests._helpers.fake_lake_catalog import (
    FakeCatalog,
    install_fake_catalog,
    point_lake_writer_at_tmp,
)

DAY_ONE = date(2024, 5, 20)
DAY_TWO = date(2024, 5, 21)


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    return install_fake_catalog(monkeypatch)


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = point_lake_writer_at_tmp(tmp_path, monkeypatch)
    # Driving the daily processor directly skips _ensure_data's Phase-0
    # layout bootstrap; the staging segment it publishes through must exist.
    (root / "staging" / "raw").mkdir(parents=True, exist_ok=True)
    return root


def _daily_identity() -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=None,
        resolution="daily",
        data_type="trade",
        provider="learn_ai_derived",
        price_adjustment_mode="raw",
    )


async def _seed_minute_day(catalog: FakeCatalog, spec, day: date, lake_root: Path) -> ArtifactRecord:
    """Complete minute row + on-disk zip with the catalog honest about the bytes."""
    from app.lean_sidecar.trading_calendar import session_windows_ms_utc

    window_open = session_windows_ms_utc(day, day)[0].open_ms_utc
    price = Decimal("100")
    write_lean_day_zip(
        lake_root,
        "SPY",
        day,
        [TradeBar(symbol="SPY", open=price, high=price, low=price, close=price, volume=10,
                  start_ms=window_open, end_ms=window_open + 60_000)],
    )
    zip_path = lake_root / "equity" / "usa" / "minute" / "spy" / f"{day:%Y%m%d}_trade.zip"
    payload = zip_path.read_bytes()
    identity = minute_bar_identity(spec, symbol="SPY", trading_date=day, data_type="trade")
    artifact_id = await catalog.claim_minute_bar(
        identity=identity,
        worker_id="seed-worker",
        lease_ttl_ms=60_000,
        data_contract_hash="seed-contract",
        file_path=f"equity/usa/minute/spy/{day:%Y%m%d}_trade.zip",
    )
    assert artifact_id is not None
    completed = await catalog.complete_artifact(
        artifact_id,
        row_count=1,
        first_bar_start_ms=window_open,
        last_bar_start_ms=window_open,
        file_size_bytes=len(payload),
        file_sha256=hashlib.sha256(payload).hexdigest(),
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    assert completed
    record = catalog._record(catalog.rows[artifact_id])
    assert record is not None
    return record


def _daily_zip(lake_root: Path) -> Path:
    return lake_root / "equity" / "usa" / "daily" / "spy.zip"


def _daily_row_count(lake_root: Path) -> int:
    import zipfile

    with zipfile.ZipFile(_daily_zip(lake_root)) as zf:
        name = zf.namelist()[0]
        rows = zf.read(name).decode("ascii").strip().splitlines()
    return len(rows)


@pytest.mark.asyncio
async def test_a_stale_daily_build_never_overwrites_a_siblings_wider_rollup(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    DAY_THREE = date(2024, 5, 22)
    spec = _build_engine_run_spec(symbol="SPY", start=DAY_ONE, end=DAY_ONE, requester="test")
    identity = _daily_identity()

    # First capture: one day, one daily row.
    narrow_sources = [await _seed_minute_day(fake_catalog, spec, DAY_ONE, tmp_lake)]
    record, failure, _ = await _process_daily_trade_artifact(identity, narrow_sources, spec, tmp_lake)
    assert failure is None and record is not None
    assert _daily_row_count(tmp_lake) == 1

    # A sibling captures DAY_TWO and publishes the wider rollup; a third
    # capture then adds DAY_THREE's minutes without a daily rebuild — the
    # sources have now moved under every build in flight.
    wide_sources = [*narrow_sources, await _seed_minute_day(fake_catalog, spec, DAY_TWO, tmp_lake)]
    wide_record, failure, _ = await _process_daily_trade_artifact(identity, wide_sources, spec, tmp_lake)
    assert failure is None and wide_record is not None
    assert _daily_row_count(tmp_lake) == 2
    wide_sha = hashlib.sha256(_daily_zip(tmp_lake).read_bytes()).hexdigest()
    await _seed_minute_day(fake_catalog, spec, DAY_THREE, tmp_lake)

    # This build read its sources before any of that: its stale narrow
    # snapshot must publish nothing over the sibling's wider rollup.
    record, failure, outcome = await _process_daily_trade_artifact(identity, narrow_sources, spec, tmp_lake)

    assert record is None
    assert failure is not None
    assert failure.reason == "lease_timeout"
    assert "daily-trade sources changed while this build ran" in (failure.detail or "")
    assert outcome == "fetched"
    # The wider rollup survived byte-for-byte, and the row still vouches for it.
    assert _daily_row_count(tmp_lake) == 2
    assert hashlib.sha256(_daily_zip(tmp_lake).read_bytes()).hexdigest() == wide_sha
    daily_rows = [
        row for row in fake_catalog.rows.values()
        if row["artifact_kind"] == "time_series_bars" and row["resolution"] == "daily"
    ]
    assert [row["file_sha256"] for row in daily_rows] == [wide_sha]

    # The retry rebuilds from the current sources: the refresh wins by
    # compare-and-swap and the rollup grows to every captured session.
    current = await catalog_client.select_coverage_minute_bars(
        market="usa", symbol="SPY", data_type="trade",
        start_trading_date=None, end_trading_date=None, price_adjustment_mode="raw",
    )
    record, failure, outcome = await _process_daily_trade_artifact(identity, current, spec, tmp_lake)
    assert failure is None and record is not None
    assert outcome == "refreshed"
    assert _daily_row_count(tmp_lake) == 3


@pytest.mark.asyncio
async def test_a_stale_daily_build_reuses_a_row_that_is_already_current(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The sibling's rollup is exactly what the current sources imply: the
    stale build reuses it and publishes nothing — never a narrower rewrite."""
    spec = _build_engine_run_spec(symbol="SPY", start=DAY_ONE, end=DAY_ONE, requester="test")
    identity = _daily_identity()

    narrow_sources = [await _seed_minute_day(fake_catalog, spec, DAY_ONE, tmp_lake)]
    await _process_daily_trade_artifact(identity, narrow_sources, spec, tmp_lake)
    wide_sources = [*narrow_sources, await _seed_minute_day(fake_catalog, spec, DAY_TWO, tmp_lake)]
    wide_record, failure, _ = await _process_daily_trade_artifact(identity, wide_sources, spec, tmp_lake)
    assert failure is None and wide_record is not None
    wide_sha = wide_record.file_sha256

    record, failure, outcome = await _process_daily_trade_artifact(identity, narrow_sources, spec, tmp_lake)

    assert failure is None
    assert outcome == "reused"
    assert record is not None and record.file_sha256 == wide_sha
    assert _daily_row_count(tmp_lake) == 2


@pytest.mark.asyncio
async def test_a_current_daily_build_still_refreshes_onto_grown_sources(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The CAS must not refuse a legitimate rebuild: the row is stale AND this
    build's snapshot is the current one — the refresh wins and publishes."""
    spec = _build_engine_run_spec(symbol="SPY", start=DAY_ONE, end=DAY_ONE, requester="test")
    identity = _daily_identity()

    narrow_sources = [await _seed_minute_day(fake_catalog, spec, DAY_ONE, tmp_lake)]
    record, failure, _ = await _process_daily_trade_artifact(identity, narrow_sources, spec, tmp_lake)
    assert failure is None and record is not None

    wide_sources = [*narrow_sources, await _seed_minute_day(fake_catalog, spec, DAY_TWO, tmp_lake)]
    record, failure, outcome = await _process_daily_trade_artifact(identity, wide_sources, spec, tmp_lake)

    assert failure is None and record is not None
    assert outcome == "refreshed"
    assert _daily_row_count(tmp_lake) == 2
