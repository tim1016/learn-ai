"""Tests for the engine's run-materialization bridge to the data lake.

Everything here is fixture-based: Polygon and the LEAN launcher are mocked
at the HTTP layer with respx, and the Postgres catalog is replaced by an
in-memory stand-in that reproduces the two properties ensure_data actually
depends on — a claim is a unique-key insert that exactly one caller wins,
and a row is only selectable once it has been completed. Nothing here
needs the lake to have been imported first.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import date
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client, run_materialization
from app.data_lake.run_materialization import (
    LakeMaterializationError,
    build_engine_run_spec,
    materialize_run_data,
    materialize_run_data_sync,
)
from app.data_lake.types import ArtifactRecord, DataAvailabilityResult, DataRunSpec

# 2024-05-20 is a Monday and a full NYSE session; 09:30 ET == 13:30 UTC.
TRADING_DAY = date(2024, 5, 20)
BAR_START_MS = 1716211800000


# ---------------------------------------------------------------------------
# In-memory catalog
# ---------------------------------------------------------------------------


class FakeCatalog:
    """Stand-in for the Postgres catalog, faithful to its claim semantics.

    Each ``claim_*`` mirrors an ``INSERT ... ON CONFLICT DO NOTHING`` against
    the partial unique index for that artifact kind: the first caller for a
    key gets an id, every later caller gets ``None`` until the row is gone.
    Each ``select_complete_*`` mirrors the ``Status = 'complete'`` predicate,
    so a claimed-but-unfinished artifact is invisible to the loser of the
    race — which is what makes contention observable rather than papered
    over. The methods contain no ``await``, so on one event loop a claim is
    as indivisible as the SQL statement it stands in for.
    """

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.keys: dict[tuple, int] = {}
        self._next_id = 1

    # -- claim helpers ------------------------------------------------------

    def _claim(self, key: tuple, row: dict) -> int | None:
        if key in self.keys:
            return None
        artifact_id = self._next_id
        self._next_id += 1
        self.keys[key] = artifact_id
        self.rows[artifact_id] = {**row, "id": artifact_id, "status": "fetching"}
        return artifact_id

    @staticmethod
    def _identity_row(identity, data_contract_hash: str, file_path: str) -> dict:
        return {
            "artifact_kind": identity.artifact_kind,
            "market": identity.market,
            "symbol": identity.symbol,
            "trading_date": identity.trading_date,
            "resolution": identity.resolution,
            "data_type": identity.data_type,
            "provider": identity.provider,
            "price_adjustment_mode": identity.price_adjustment_mode,
            "data_contract_hash": data_contract_hash,
            "file_path": file_path,
            "file_sha256": "",
            "row_count": None,
            "first_bar_start_ms": None,
            "last_bar_start_ms": None,
        }

    @staticmethod
    def _record(row: dict) -> ArtifactRecord:
        return ArtifactRecord(**{k: v for k, v in row.items() if k != "status"})

    # -- catalog_client surface --------------------------------------------

    async def init_pool(self) -> None:
        return None

    async def claim_metadata_artifact(
        self, identity, worker_id, lease_ttl_ms, data_contract_hash, file_path
    ) -> int | None:
        return self._claim(
            ("metadata", data_contract_hash),
            self._identity_row(identity, data_contract_hash, file_path),
        )

    async def select_complete_metadata_artifact(self, data_contract_hash: str) -> ArtifactRecord | None:
        for row in self.rows.values():
            if (
                row["artifact_kind"] == "metadata"
                and row["data_contract_hash"] == data_contract_hash
                and row["status"] == "complete"
            ):
                return self._record(row)
        return None

    async def claim_minute_bar(self, identity, worker_id, lease_ttl_ms, data_contract_hash, file_path) -> int | None:
        key = (
            "minute",
            identity.market,
            identity.symbol,
            identity.trading_date,
            identity.data_type,
            identity.provider,
            identity.price_adjustment_mode,
        )
        return self._claim(key, self._identity_row(identity, data_contract_hash, file_path))

    async def select_coverage_minute_bars(
        self, market, symbol, data_type, start_trading_date, end_trading_date
    ) -> list[ArtifactRecord]:
        return [
            self._record(row)
            for row in self.rows.values()
            if row["artifact_kind"] == "time_series_bars"
            and row["resolution"] == "minute"
            and row["market"] == market
            and row["symbol"] == symbol
            and row["data_type"] == data_type
            and row["status"] == "complete"
            and start_trading_date <= row["trading_date"] <= end_trading_date
        ]

    async def claim_aggregated_bar_artifact(
        self, identity, worker_id, lease_ttl_ms, data_contract_hash, file_path
    ) -> int | None:
        key = (
            "aggregated",
            identity.market,
            identity.symbol,
            identity.resolution,
            identity.data_type,
            identity.provider,
            identity.price_adjustment_mode,
        )
        return self._claim(key, self._identity_row(identity, data_contract_hash, file_path))

    async def select_complete_aggregated_bar_artifact(self, identity) -> ArtifactRecord | None:
        for row in self.rows.values():
            if (
                row["artifact_kind"] == "time_series_bars"
                and row["resolution"] == identity.resolution
                and row["market"] == identity.market
                and row["symbol"] == identity.symbol
                and row["data_type"] == identity.data_type
                and row["status"] == "complete"
            ):
                return self._record(row)
        return None

    async def complete_artifact(
        self, artifact_id, row_count, first_bar_start_ms, last_bar_start_ms, file_size_bytes, file_sha256
    ) -> None:
        row = self.rows[artifact_id]
        if row["status"] != "fetching":
            return
        row.update(
            status="complete",
            row_count=row_count,
            first_bar_start_ms=first_bar_start_ms,
            last_bar_start_ms=last_bar_start_ms,
            file_sha256=file_sha256,
        )

    async def fail_artifact(self, artifact_id, last_error, error_message=None) -> None:
        self.rows[artifact_id]["status"] = "failed"


@pytest.fixture
def fake_catalog(monkeypatch) -> FakeCatalog:
    catalog = FakeCatalog()
    for name in (
        "init_pool",
        "claim_metadata_artifact",
        "select_complete_metadata_artifact",
        "claim_minute_bar",
        "select_coverage_minute_bars",
        "claim_aggregated_bar_artifact",
        "select_complete_aggregated_bar_artifact",
        "complete_artifact",
        "fail_artifact",
    ):
        monkeypatch.setattr(catalog_client, name, getattr(catalog, name))
    return catalog


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch) -> Path:
    """Point LEAN_DATA_WRITE_ROOT at a tmp_path tree with lake/ + staging/."""
    write_root = tmp_path / "writer-root"
    (write_root / "lake").mkdir(parents=True)
    (write_root / "staging").mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "test-key")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_URL", "http://launcher-mock:8090")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_TOKEN", "test-token")
    return write_root


# ---------------------------------------------------------------------------
# HTTP fixtures
# ---------------------------------------------------------------------------


def _launcher_payload() -> dict:
    from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST

    market_hours = json.dumps(
        {
            "entries": {
                "Equity-usa-[*]": {
                    "exchange": "NYSE",
                    "timezone": "America/New_York",
                    "holidays": [],
                    "earlyCloses": {},
                }
            }
        }
    ).encode("utf-8")
    return {
        "market_hours_database_b64": base64.b64encode(market_hours).decode("ascii"),
        "symbol_properties_database_b64": base64.b64encode(b"SPY,equity,usd,1,0\n").decode("ascii"),
        # The extractor rejects a launcher that ran a different image than the
        # spec pinned, so the mock must echo the digest the spec carries.
        "image_digest_used": PINNED_LEAN_IMAGE_DIGEST,
    }


def _polygon_payload(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "status": "OK",
        "results": [
            {
                "v": 1000,
                "vw": 500.0,
                "o": 500.0,
                "c": 500.05,
                "h": 500.10,
                "l": 499.95,
                "t": BAR_START_MS + i * 60_000,
                "n": 10,
            }
            for i in range(390)
        ],
    }


def _mock_launcher() -> None:
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        return_value=httpx.Response(200, json=_launcher_payload())
    )


def _mock_polygon(ticker: str = "SPY"):
    return respx.get(url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/{ticker}/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_payload(ticker))
    )


def _spec() -> DataRunSpec:
    return build_engine_run_spec(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY, requester="test")


# ---------------------------------------------------------------------------
# The spec a Python-engine run asks for
# ---------------------------------------------------------------------------


def test_build_engine_run_spec_asks_only_for_trade_bars():
    spec = _spec()

    assert spec.run_type == "python_lab"
    assert spec.symbols == ["SPY"]
    assert spec.data_types == ["trade"]
    # The Python engine never opens a factor file or a map file; asking for
    # them buys provider round-trips and a window-keyed contract for nothing.
    assert spec.include_factor_files is False
    assert spec.include_map_files is False


def test_build_engine_run_spec_uppercases_the_symbol():
    spec = build_engine_run_spec(symbol="spy", start=TRADING_DAY, end=TRADING_DAY)

    assert spec.symbols == ["SPY"]


def test_build_engine_run_spec_pins_the_lean_image_the_calendar_comes_from():
    from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST

    assert _spec().lean_image_digest == PINNED_LEAN_IMAGE_DIGEST


def test_build_engine_run_spec_refuses_an_unpinned_lean_image(monkeypatch):
    monkeypatch.setattr("app.lean_sidecar.config.PINNED_LEAN_IMAGE_DIGEST", None)

    with pytest.raises(LakeMaterializationError, match="pinned LEAN image digest"):
        _spec()


def test_two_specs_for_the_same_window_get_distinct_request_ids():
    """Staging paths are keyed by request_id; two runs must not share one."""
    assert _spec().request_id != _spec().request_id


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_materialize_run_data_fetches_the_missing_day(fake_catalog, tmp_lake):
    _mock_launcher()
    polygon = _mock_polygon()

    result = await materialize_run_data(_spec())

    assert result.overall_status == "complete", result.failures
    assert polygon.call_count == 1
    assert result.data_availability_hash


@respx.mock
@pytest.mark.asyncio
async def test_materialize_run_data_writes_lean_bytes_into_the_lake(fake_catalog, tmp_lake):
    _mock_launcher()
    _mock_polygon()

    result = await materialize_run_data(_spec())

    minute = [a for a in result.artifacts if a.resolution == "minute"]
    assert len(minute) == 1
    on_disk = tmp_lake / "lake" / minute[0].file_path
    assert on_disk.is_file()
    assert result.lean_data_root_path == str(tmp_lake / "lake")


@respx.mock
@pytest.mark.asyncio
async def test_materialize_run_data_reuses_the_bytes_on_a_second_run(fake_catalog, tmp_lake):
    """Delta-fetch: an already-materialized day costs zero provider calls."""
    _mock_launcher()
    polygon = _mock_polygon()

    first = await materialize_run_data(_spec())
    second = await materialize_run_data(_spec())

    assert polygon.call_count == 1
    assert second.overall_status == "complete", second.failures
    assert second.reused_artifact_count > 0
    assert second.fetched_artifact_count == 0
    assert second.data_availability_hash == first.data_availability_hash


@respx.mock
@pytest.mark.asyncio
async def test_two_concurrent_runs_coalesce_onto_one_fetch(fake_catalog, tmp_lake):
    """The catalog hands the fetch to one run; the other takes its bytes."""
    _mock_launcher()
    polygon = _mock_polygon()

    first, second = await asyncio.gather(
        materialize_run_data(_spec(), poll_interval_s=0.01),
        materialize_run_data(_spec(), poll_interval_s=0.01),
    )

    assert polygon.call_count == 1, "the same trading day was fetched twice"
    assert first.overall_status == "complete", first.failures
    assert second.overall_status == "complete", second.failures
    # Both runs must be able to name the same bytes, or the fingerprint each
    # records would describe a different lake than the one it read.
    assert first.data_availability_hash == second.data_availability_hash


@respx.mock
@pytest.mark.asyncio
async def test_concurrent_runs_record_one_artifact_per_identity(fake_catalog, tmp_lake):
    """Coalescing is the catalog's unique claim, not a lucky interleaving."""
    _mock_launcher()
    _mock_polygon()

    await asyncio.gather(
        materialize_run_data(_spec(), poll_interval_s=0.01),
        materialize_run_data(_spec(), poll_interval_s=0.01),
    )

    minute_rows = [
        row
        for row in fake_catalog.rows.values()
        if row["artifact_kind"] == "time_series_bars" and row["resolution"] == "minute"
    ]
    assert len(minute_rows) == 1
    assert minute_rows[0]["status"] == "complete"


@respx.mock
@pytest.mark.asyncio
async def test_materialize_run_data_returns_a_real_failure_without_waiting(fake_catalog, tmp_lake):
    """A symbol the provider has no data for is not contention — do not poll."""
    _mock_launcher()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/NODATA/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json={"ticker": "NODATA", "status": "OK", "results": []})
    )
    spec = build_engine_run_spec(symbol="NODATA", start=TRADING_DAY, end=TRADING_DAY)

    result = await asyncio.wait_for(materialize_run_data(spec, poll_interval_s=0.01), timeout=10)

    assert result.overall_status in {"partial", "failed"}
    assert any(f.reason == "provider_no_data" for f in result.failures)


@respx.mock
@pytest.mark.asyncio
async def test_materialize_run_data_stops_waiting_at_the_fetch_deadline(fake_catalog, tmp_lake, monkeypatch):
    """A lease that never clears must not hang the run forever."""
    _mock_launcher()
    _mock_polygon()

    # Every claim loses, and nothing ever completes: permanent contention.
    async def _always_taken(*args, **kwargs):
        return None

    monkeypatch.setattr(catalog_client, "claim_metadata_artifact", _always_taken)
    monkeypatch.setattr(catalog_client, "claim_minute_bar", _always_taken)
    monkeypatch.setattr(catalog_client, "claim_aggregated_bar_artifact", _always_taken)

    spec = _spec().model_copy(update={"fetch_timeout_seconds": 10})
    # Clock: start at 0 (deadline 10), one wait still inside the window,
    # then past it. Without the deadline this loop would never terminate.
    ticks = iter([0.0, 0.0, 11.0])

    result = await materialize_run_data(spec, poll_interval_s=0.0, now=lambda: next(ticks))

    assert result.overall_status in {"partial", "failed"}
    assert any(f.reason == "lease_timeout" for f in result.failures)


# ---------------------------------------------------------------------------
# Synchronous boundary
# ---------------------------------------------------------------------------


def _sentinel_result(spec: DataRunSpec) -> DataAvailabilityResult:
    return DataAvailabilityResult(
        request_id=spec.request_id,
        overall_status="complete",
        lean_data_root_path="/lean-data-writer/lake",
        data_availability_hash="a" * 64,
        completed_at_ms=0,
        duration_ms=0,
    )


def test_materialize_run_data_sync_runs_the_coroutine_off_thread(monkeypatch):
    spec = _spec()
    seen: list[UUID] = []

    async def _fake(passed_spec, **kwargs):
        seen.append(passed_spec.request_id)
        return _sentinel_result(passed_spec)

    monkeypatch.setattr(run_materialization, "materialize_run_data", _fake)

    result = materialize_run_data_sync(spec)

    assert seen == [spec.request_id]
    assert result.data_availability_hash == "a" * 64


def test_materialize_run_data_sync_reuses_one_loop_for_the_process(monkeypatch):
    """The catalog pool belongs to a loop; a per-call loop would orphan it."""

    async def _fake(passed_spec, **kwargs):
        return _sentinel_result(passed_spec)

    monkeypatch.setattr(run_materialization, "materialize_run_data", _fake)

    materialize_run_data_sync(_spec())
    first_loop = run_materialization._materialization_loop()
    materialize_run_data_sync(_spec())

    assert run_materialization._materialization_loop() is first_loop
    assert not first_loop.is_closed()


@pytest.mark.asyncio
async def test_materialize_run_data_sync_refuses_to_block_an_event_loop():
    with pytest.raises(RuntimeError, match="running event loop"):
        materialize_run_data_sync(_spec())
