"""Map files describe listing history, not the first capture window (#2453).

The regression cases drive the real ``_process_map_file_artifact`` against the
fake catalog: extending a capture window must extend the map's rows (on master
the existing map is an unconditional cache hit and keeps the first window's
end), and the final map must not depend on the order windows were requested
in. The sidecar range check lives in tests/lean_sidecar.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from app.data_lake import catalog_client
from app.data_lake.ensure_data import _process_map_file_artifact, minute_bar_identity
from app.data_lake.path_policy import LeanMapFilePath
from app.data_lake.run_materialization import _build_engine_run_spec
from app.data_lake.types import DataRunSpec
from tests._helpers.fake_lake_catalog import (
    FakeCatalog,
    install_fake_catalog,
    mock_launcher,
    point_lake_writer_at_tmp,
)

# A Monday and the Tuesday of the same NYSE week.
NARROW_START, NARROW_END = date(2024, 5, 20), date(2024, 5, 21)
# Friday of the same week — the widened window.
WIDE_END = date(2024, 5, 24)

_NARROW_SESSIONS = [date(2024, 5, 20), date(2024, 5, 21)]
_WIDE_SESSIONS = [date(2024, 5, 20), date(2024, 5, 21), date(2024, 5, 22), date(2024, 5, 23), date(2024, 5, 24)]


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    return install_fake_catalog(monkeypatch)


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return point_lake_writer_at_tmp(tmp_path, monkeypatch)


def _spec(start: date, end: date) -> DataRunSpec:
    return _build_engine_run_spec(symbol="SPY", start=start, end=end, requester="test")


def _map_identity(spec: DataRunSpec):
    from app.data_lake.types import ArtifactIdentity

    return ArtifactIdentity(
        artifact_kind="map_file",
        market=spec.market,
        symbol="SPY",
        trading_date=None,
        resolution=None,
        data_type=None,
        provider="polygon",
        price_adjustment_mode=spec.price_adjustment_mode,
    )


async def _seed_captured_sessions(catalog: FakeCatalog, spec: DataRunSpec, sessions: list[date]) -> None:
    """Complete minute-trade rows in the catalog, as a prior capture would."""
    for trading_date in sessions:
        identity = minute_bar_identity(spec, symbol="SPY", trading_date=trading_date, data_type="trade")
        artifact_id = await catalog.claim_minute_bar(
            identity=identity,
            worker_id="seed-worker",
            lease_ttl_ms=60_000,
            data_contract_hash="seed-contract",
            file_path=f"equity/usa/minute/spy/{trading_date.strftime('%Y%m%d')}_trade.zip",
        )
        assert artifact_id is not None
        completed = await catalog.complete_artifact(
            artifact_id,
            row_count=390,
            first_bar_start_ms=0,
            last_bar_start_ms=0,
            file_size_bytes=1,
            file_sha256=f"seed-{trading_date.isoformat()}",
            lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
        )
        assert completed


def _mock_ticker_events() -> None:
    respx.get(url__regex=r"https://api\.polygon\.io/v3/reference/tickers/SPY/events.*").mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )


def _map_file(tmp_lake: Path) -> Path:
    return tmp_lake / "lake" / "raw" / Path(*LeanMapFilePath(market="usa", symbol="SPY").relative_path().parts)


def _map_row_dates(tmp_lake: Path) -> list[str]:
    body = _map_file(tmp_lake).read_bytes().decode("ascii").strip().split("\n")
    return [row.split(",")[0] for row in body]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("start,end,expected_skipped,expected_bar_failures", [
    (date(2024, 5, 25), date(2024, 5, 26), 2, 0),
    (date(2024, 5, 27), date(2024, 5, 27), 1, 0),
    (NARROW_START, NARROW_END, 0, 2),
])
async def test_capture_without_complete_minutes_finishes_map_claim(
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    monkeypatch: pytest.MonkeyPatch,
    start: date,
    end: date,
    expected_skipped: int,
    expected_bar_failures: int,
) -> None:
    """Non-sessions and provider failures still return a structured result."""
    from app.data_lake import ensure_data as pipeline
    from app.data_lake.map_files import map_file_coverage
    from app.data_lake.polygon_fetcher import PolygonFetchError

    mock_launcher()
    _mock_ticker_events()
    fetch = AsyncMock(side_effect=PolygonFetchError("minute fetch unavailable"))
    monkeypatch.setattr(pipeline, "fetch_minute_trade_aggregates", fetch)
    spec = _spec(start, end).model_copy(update={"include_map_files": True})

    result = await pipeline.ensure_data(spec)

    assert result.overall_status == "partial"
    assert len(result.skipped_non_sessions) == expected_skipped
    assert sum(f.reason == "provider_api_error" for f in result.failures) == expected_bar_failures
    assert fetch.await_count == expected_bar_failures
    assert any(f.reason == "internal_error" and "no complete minute-trade sources" in f.detail
               for f in result.failures)
    assert map_file_coverage(_map_file(tmp_lake).read_bytes()) == (start, end)
    assert [r for r in result.artifacts if r.artifact_kind == "map_file"]
    assert all(row["status"] != "fetching" for row in fake_catalog.rows.values())


@respx.mock
@pytest.mark.asyncio
async def test_extending_the_window_extends_the_map(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    """The #2453 regression: backfill narrow, extend the window — the map's
    last row must move to the new end, not keep the first window's end date
    (which LEAN reads as a delisting mid-run)."""
    _mock_ticker_events()
    narrow, wide = _spec(NARROW_START, NARROW_END), _spec(NARROW_START, WIDE_END)

    await _seed_captured_sessions(fake_catalog, narrow, _NARROW_SESSIONS)
    record, failure, _ = await _process_map_file_artifact(_map_identity(narrow), narrow)
    assert failure is None and record is not None
    assert _map_row_dates(tmp_lake) == ["20240520", "20240521"]

    # A later, wider capture adds its sessions and re-asks.
    await _seed_captured_sessions(fake_catalog, wide, [d for d in _WIDE_SESSIONS if d not in _NARROW_SESSIONS])
    record, failure, reused = await _process_map_file_artifact(_map_identity(wide), wide)
    assert failure is None and record is not None
    assert reused is False, "a map that no longer covers the union must rebuild, not cache-hit"

    assert _map_row_dates(tmp_lake) == ["20240520", "20240524"]


@respx.mock
@pytest.mark.asyncio
async def test_a_covering_map_remains_a_cache_hit(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    """A map whose coverage already spans the union is reused byte-for-byte."""
    _mock_ticker_events()
    spec = _spec(NARROW_START, WIDE_END)

    await _seed_captured_sessions(fake_catalog, spec, _WIDE_SESSIONS)
    first, failure, _ = await _process_map_file_artifact(_map_identity(spec), spec)
    assert failure is None and first is not None
    events_mock_calls = respx.calls

    second, failure, reused = await _process_map_file_artifact(_map_identity(spec), spec)
    assert failure is None
    assert reused is True
    assert second is not None and second.file_sha256 == first.file_sha256
    assert len(respx.calls) == len(events_mock_calls), "a covering cache hit must not re-fetch ticker events"


@respx.mock
@pytest.mark.asyncio
async def test_map_content_is_independent_of_request_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Requesting [narrow, wide] must land on the same map as [wide] alone."""
    _mock_ticker_events()

    # Path A: narrow backfill first, then extend.
    lake_a = point_lake_writer_at_tmp(tmp_path_factory.mktemp("map-order-a"), monkeypatch)
    catalog_a = install_fake_catalog(monkeypatch)
    await _seed_captured_sessions(catalog_a, _spec(NARROW_START, NARROW_END), _NARROW_SESSIONS)
    await _process_map_file_artifact(_map_identity(_spec(NARROW_START, NARROW_END)), _spec(NARROW_START, NARROW_END))
    await _seed_captured_sessions(
        catalog_a, _spec(NARROW_START, WIDE_END), [d for d in _WIDE_SESSIONS if d not in _NARROW_SESSIONS]
    )
    await _process_map_file_artifact(_map_identity(_spec(NARROW_START, WIDE_END)), _spec(NARROW_START, WIDE_END))

    # Path B: the wide window first, with every session already captured.
    lake_b = point_lake_writer_at_tmp(tmp_path_factory.mktemp("map-order-b"), monkeypatch)
    catalog_b = install_fake_catalog(monkeypatch)
    await _seed_captured_sessions(catalog_b, _spec(NARROW_START, WIDE_END), _WIDE_SESSIONS)
    await _process_map_file_artifact(_map_identity(_spec(NARROW_START, WIDE_END)), _spec(NARROW_START, WIDE_END))

    assert _map_file(lake_a).read_bytes() == _map_file(lake_b).read_bytes()
