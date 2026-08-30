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
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from unittest import mock
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client, run_materialization
from app.data_lake.ensure_data import ensure_data
from app.data_lake.path_policy import lake_subpath
from app.data_lake.run_materialization import (
    EngineRunMaterialization,
    LakeMaterializationError,
    _build_engine_run_spec,
    _materialize_run_data,
    _materialize_run_data_sync,
    materialize_engine_run,
)
from app.data_lake.types import ArtifactFailure, ArtifactRecord, DataAvailabilityResult, DataRunSpec
from app.lean_sidecar import config as sidecar_config

# 2024-05-20 is a Monday and a full NYSE session; 09:30 ET == 13:30 UTC.
TRADING_DAY = date(2024, 5, 20)
# Tuesday of the same week — a second session, so a window ending here
# aggregates a different set of minute artifacts into the daily zip.
WIDER_WINDOW_END = date(2024, 5, 21)
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
        self.rows[artifact_id] = {
            **row,
            "id": artifact_id,
            "status": "fetching",
            "attempt_count": 1,
            "last_error": None,
        }
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

    _BOOKKEEPING_KEYS = frozenset({"status", "attempt_count", "last_error"})

    @classmethod
    def _record(cls, row: dict) -> ArtifactRecord:
        return ArtifactRecord(**{k: v for k, v in row.items() if k not in cls._BOOKKEEPING_KEYS})

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

    async def select_metadata_claim_state(self, data_contract_hash: str) -> catalog_client.ArtifactClaimState | None:
        artifact_id = self.keys.get(("metadata", data_contract_hash))
        if artifact_id is None:
            return None
        row = self.rows[artifact_id]
        return catalog_client.ArtifactClaimState(
            id=artifact_id,
            status=row["status"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
        )

    @staticmethod
    def _minute_key(identity) -> tuple:
        return (
            "minute",
            identity.market,
            identity.symbol,
            identity.trading_date,
            identity.data_type,
            identity.provider,
            identity.price_adjustment_mode,
        )

    async def claim_minute_bar(self, identity, worker_id, lease_ttl_ms, data_contract_hash, file_path) -> int | None:
        return self._claim(self._minute_key(identity), self._identity_row(identity, data_contract_hash, file_path))

    async def select_minute_bar_claim_state(self, identity) -> catalog_client.ArtifactClaimState | None:
        artifact_id = self.keys.get(self._minute_key(identity))
        if artifact_id is None:
            return None
        row = self.rows[artifact_id]
        return catalog_client.ArtifactClaimState(
            id=artifact_id,
            status=row["status"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
        )

    async def steal_or_retry_minute_bar(self, artifact_id, worker_id, lease_ttl_ms, max_retries) -> bool:
        row = self.rows[artifact_id]
        # The fake has no lease clock, so "fetching" always means a live
        # lease held by someone else — nothing to steal, matching the real
        # WHERE clause's "LeaseExpiresAtMs < now" arm never firing here.
        if row["status"] == "failed" and row["attempt_count"] < max_retries:
            row["status"] = "fetching"
            row["attempt_count"] += 1
            row["last_error"] = None
            return True
        return False

    async def select_coverage_minute_bars(
        self, market, symbol, data_type, start_trading_date, end_trading_date, *, price_adjustment_mode
    ) -> list[ArtifactRecord]:
        # Mode is a required filter in the real query (#1832): two modes can
        # coexist for one (market, symbol, date, data_type), and a fake that
        # ignored the distinction would hide exactly the wrong-row bug the
        # required parameter exists to prevent.
        #
        # Both bounds None means unbounded — the daily-trade rollup's
        # symbol-wide source read (#1870) — mirroring the real query's
        # null-safe BETWEEN predicate.
        def _in_window(trading_date) -> bool:
            if start_trading_date is None and end_trading_date is None:
                return True
            return start_trading_date <= trading_date <= end_trading_date

        return [
            self._record(row)
            for row in self.rows.values()
            if row["artifact_kind"] == "time_series_bars"
            and row["resolution"] == "minute"
            and row["market"] == market
            and row["symbol"] == symbol
            and row["data_type"] == data_type
            and row["price_adjustment_mode"] == price_adjustment_mode
            and row["status"] == "complete"
            and _in_window(row["trading_date"])
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

    async def refresh_complete_artifact(self, artifact_id, worker_id, lease_ttl_ms) -> catalog_client.PriorArtifactMetadata | None:
        row = self.rows.get(artifact_id)
        if row is None or row["status"] != "complete":
            return None
        prior = catalog_client.PriorArtifactMetadata(
            prior_file_path=row["file_path"],
            prior_file_sha256=row["file_sha256"],
        )
        row.update(status="fetching")
        return prior

    async def restore_complete_artifact(self, artifact_id, worker_id) -> bool:
        row = self.rows.get(artifact_id)
        if row is None or row["status"] != "fetching":
            return False
        row.update(status="complete")
        return True

    async def complete_artifact(
        self,
        artifact_id,
        row_count,
        first_bar_start_ms,
        last_bar_start_ms,
        file_size_bytes,
        file_sha256,
        data_contract_hash=None,
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
            data_contract_hash=data_contract_hash if data_contract_hash is not None else row["data_contract_hash"],
        )

    async def fail_artifact(self, artifact_id, last_error, error_message=None) -> None:
        self.rows[artifact_id].update(status="failed", last_error=last_error)


@pytest.fixture
def fake_catalog(monkeypatch) -> FakeCatalog:
    catalog = FakeCatalog()
    for name in (
        "init_pool",
        "claim_metadata_artifact",
        "select_complete_metadata_artifact",
        "select_metadata_claim_state",
        "claim_minute_bar",
        "select_minute_bar_claim_state",
        "steal_or_retry_minute_bar",
        "select_coverage_minute_bars",
        "claim_aggregated_bar_artifact",
        "select_complete_aggregated_bar_artifact",
        "refresh_complete_artifact",
        "restore_complete_artifact",
        "complete_artifact",
        "fail_artifact",
    ):
        monkeypatch.setattr(catalog_client, name, getattr(catalog, name))
    return catalog


@pytest.fixture
def ensure_attempts(monkeypatch) -> list[UUID]:
    """Record every ``ensure_data`` pass, keyed by the run that made it.

    The retry loop is the only thing that produces a second pass for one
    request_id, so these counts are what makes a deleted loop visible: without
    it the coalescing and deadline tests would still pass on cache reuse.
    """
    attempts: list[UUID] = []
    real_ensure_data = run_materialization.ensure_data

    async def _counting(spec: DataRunSpec):
        attempts.append(spec.request_id)
        return await real_ensure_data(spec)

    monkeypatch.setattr(run_materialization, "ensure_data", _counting)
    return attempts


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch) -> Path:
    """Point LEAN_DATA_WRITE_ROOT at a tmp_path tree with lake/ + staging/.

    Also points app.lean_sidecar.config.DEFAULT_ARTIFACTS_ROOT at a sibling
    tmp_path tree: Phase 0 reads the launcher's extracted metadata files back
    off that root (app.data_lake.lean_metadata does not trust the launcher's
    HTTP response body, only its own view of the shared mount), so
    _mock_launcher below stages files there instead of under the real repo
    path.
    """
    write_root = tmp_path / "writer-root"
    (write_root / "lake").mkdir(parents=True)
    (write_root / "staging").mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "test-key")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_URL", "http://launcher-mock:8090")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_TOKEN", "test-token")
    monkeypatch.setenv("LEAN_LAUNCHER_TOKEN", "test-token")
    artifacts_root = tmp_path / "artifacts-root"
    artifacts_root.mkdir(parents=True)
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", artifacts_root)
    return write_root


# ---------------------------------------------------------------------------
# HTTP fixtures
# ---------------------------------------------------------------------------


_MARKET_HOURS_JSON = json.dumps(
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
_SYMBOL_PROPERTIES_CSV = b"SPY,equity,usd,1,0\n"


def _stage_workspace_files(artifacts_root: Path, run_id: str) -> None:
    """Pre-place the two files a real launcher run would have written.

    Layout must match app.lean_sidecar.workspace.Workspace.data_dir and
    staging.list_metadata_databases: <root>/<run_id>/workspace/data/...
    """
    data_dir = artifacts_root / run_id / "workspace" / "data"
    (data_dir / "market-hours").mkdir(parents=True, exist_ok=True)
    (data_dir / "symbol-properties").mkdir(parents=True, exist_ok=True)
    (data_dir / "market-hours" / "market-hours-database.json").write_bytes(_MARKET_HOURS_JSON)
    (data_dir / "symbol-properties" / "symbol-properties-database.csv").write_bytes(_SYMBOL_PROPERTIES_CSV)


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


def _responder(payload: dict, *, latency_s: float):
    """A respx side effect that actually suspends the caller.

    ``mock(return_value=…)`` returns without ever awaiting, so two coroutines
    driven by ``asyncio.gather`` run strictly one after the other and a test
    that thinks it proved concurrency has proved sequential cache reuse. A
    non-zero ``latency_s`` puts a real suspension point inside the request, so
    the second ensure runs while the first is still mid-fetch.
    """

    async def _respond(request: httpx.Request) -> httpx.Response:
        if latency_s:
            await asyncio.sleep(latency_s)
        return httpx.Response(200, json=payload)

    return _respond


def _launcher_responder(*, latency_s: float = 0.0):
    """Stand-in for the launcher: stages the files app.data_lake.lean_metadata
    will read back (keyed by the run_id the caller sent), then returns the
    launcher's actual paths-only response shape — see module docstring on
    _stage_workspace_files for the layout contract."""

    async def _respond(request: httpx.Request) -> httpx.Response:
        if latency_s:
            await asyncio.sleep(latency_s)
        body = json.loads(request.content)
        _stage_workspace_files(sidecar_config.DEFAULT_ARTIFACTS_ROOT, body["run_id"])
        return httpx.Response(
            200,
            json={
                "market_hours_db_path": "/launcher-side/market-hours-database.json",
                "symbol_properties_db_path": "/launcher-side/symbol-properties-database.csv",
            },
        )

    return _respond


def _mock_launcher(*, latency_s: float = 0.0):
    return respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_responder(latency_s=latency_s)
    )


def _mock_polygon(ticker: str = "SPY", *, latency_s: float = 0.0):
    return respx.get(url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/{ticker}/range/1/minute/.*").mock(
        side_effect=_responder(_polygon_payload(ticker), latency_s=latency_s)
    )


def _spec() -> DataRunSpec:
    return _build_engine_run_spec(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY, requester="test")


def _narrow_spec() -> DataRunSpec:
    return _build_engine_run_spec(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY)


def _wide_spec() -> DataRunSpec:
    return _build_engine_run_spec(symbol="SPY", start=TRADING_DAY, end=WIDER_WINDOW_END)


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
    spec = _build_engine_run_spec(symbol="spy", start=TRADING_DAY, end=TRADING_DAY)

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

    result = await _materialize_run_data(_spec(), resolution="minute")

    assert result.overall_status == "complete", result.failures
    assert polygon.call_count == 1
    assert result.data_availability_hash


@respx.mock
@pytest.mark.asyncio
async def test_materialize_run_data_writes_lean_bytes_into_the_lake(fake_catalog, tmp_lake):
    _mock_launcher()
    _mock_polygon()

    result = await _materialize_run_data(_spec(), resolution="minute")

    minute = [a for a in result.artifacts if a.resolution == "minute"]
    assert len(minute) == 1
    on_disk = tmp_lake / lake_subpath("raw") / minute[0].file_path
    assert on_disk.is_file()
    assert result.lean_data_root_path == str(tmp_lake / lake_subpath("raw"))


@respx.mock
@pytest.mark.asyncio
async def test_materialize_run_data_reuses_the_bytes_on_a_second_run(fake_catalog, tmp_lake):
    """Delta-fetch: an already-materialized day costs zero provider calls."""
    _mock_launcher()
    polygon = _mock_polygon()

    first = await _materialize_run_data(_spec(), resolution="minute")
    second = await _materialize_run_data(_spec(), resolution="minute")

    assert polygon.call_count == 1
    assert second.overall_status == "complete", second.failures
    assert second.reused_artifact_count > 0
    assert second.fetched_artifact_count == 0
    assert second.data_availability_hash == first.data_availability_hash


@respx.mock
@pytest.mark.asyncio
async def test_two_concurrent_runs_coalesce_onto_one_fetch(fake_catalog, tmp_lake, ensure_attempts):
    """The catalog hands the fetch to one run; the other waits and takes its bytes.

    The provider mock suspends mid-fetch, so the second ensure genuinely runs
    while the first holds the claim — it loses every claim, gets
    ``lease_timeout``, and only reaches ``complete`` because it comes back for
    a second pass. Delete the retry loop and this test fails.
    """
    _mock_launcher()
    polygon = _mock_polygon(latency_s=0.05)

    first, second = await asyncio.gather(
        _materialize_run_data(_spec(), resolution="minute", poll_interval_s=0.01),
        _materialize_run_data(_spec(), resolution="minute", poll_interval_s=0.01),
    )

    assert polygon.call_count == 1, "the same trading day was fetched twice"
    assert first.overall_status == "complete", first.failures
    assert second.overall_status == "complete", second.failures
    # Both runs must be able to name the same bytes, or the fingerprint each
    # records would describe a different lake than the one it read.
    assert first.data_availability_hash == second.data_availability_hash
    # Exactly one of them was blocked and had to come back for the winner's
    # bytes; a run that never retried never contended.
    retried = [rid for rid, count in Counter(ensure_attempts).items() if count > 1]
    assert len(retried) == 1, f"expected one run to wait and retry, saw attempts {Counter(ensure_attempts)}"


@respx.mock
@pytest.mark.asyncio
async def test_concurrent_runs_record_one_artifact_per_identity(fake_catalog, tmp_lake, ensure_attempts):
    """Coalescing is the catalog's unique claim, not a lucky interleaving."""
    _mock_launcher()
    _mock_polygon(latency_s=0.05)

    await asyncio.gather(
        _materialize_run_data(_spec(), resolution="minute", poll_interval_s=0.01),
        _materialize_run_data(_spec(), resolution="minute", poll_interval_s=0.01),
    )

    minute_rows = [
        row
        for row in fake_catalog.rows.values()
        if row["artifact_kind"] == "time_series_bars" and row["resolution"] == "minute"
    ]
    assert len(minute_rows) == 1
    assert minute_rows[0]["status"] == "complete"
    assert len(ensure_attempts) > 2, "neither run contended; the interleaving did not happen"


@respx.mock
@pytest.mark.asyncio
async def test_a_sequential_second_run_never_needs_a_retry(fake_catalog, tmp_lake, ensure_attempts):
    """Contrast with the test above: cache reuse alone costs no second pass.

    This is what the concurrency tests would collapse into if the provider mock
    stopped suspending — one pass each, one fetch, and the retry loop dead
    weight. Pinning it here keeps that collapse visible.
    """
    _mock_launcher()
    polygon = _mock_polygon()

    await _materialize_run_data(_spec(), resolution="minute")
    await _materialize_run_data(_spec(), resolution="minute")

    assert polygon.call_count == 1
    assert len(ensure_attempts) == 2
    assert max(Counter(ensure_attempts).values()) == 1


@respx.mock
@pytest.mark.asyncio
async def test_materialize_run_data_returns_a_real_failure_without_waiting(fake_catalog, tmp_lake):
    """A symbol the provider has no data for is not contention — do not poll."""
    _mock_launcher()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/NODATA/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json={"ticker": "NODATA", "status": "OK", "results": []})
    )
    spec = _build_engine_run_spec(symbol="NODATA", start=TRADING_DAY, end=TRADING_DAY)

    result = await asyncio.wait_for(_materialize_run_data(spec, resolution="minute", poll_interval_s=0.01), timeout=10)

    assert result.overall_status in {"partial", "failed"}
    assert any(f.reason == "provider_no_data" for f in result.failures)


@respx.mock
@pytest.mark.asyncio
async def test_ensure_data_reclaims_a_failed_minute_artifact_instead_of_polling_it(fake_catalog, tmp_lake):
    """A 'failed' row is a done deal, not a sibling fetch to wait out.

    Before the fix, ``claim_minute_bar`` losing to an existing row of ANY
    status (not just an active lease) was reported as ``lease_timeout`` —
    contention, per ``_CONTENTION_REASONS`` — which sends the bridge into a
    poll loop that re-tries the exact same failed claim every interval until
    ``fetch_timeout_seconds`` elapses, because the row never transitions on
    its own. The fix reclaims a 'failed' row via the same primitive the
    lease-expiry sweep uses, so the very next ``ensure_data`` pass gets a
    fresh attempt at the bytes — no polling required to get there.
    """
    _mock_launcher()
    responses = iter(
        [
            httpx.Response(200, json={"ticker": "SPY", "status": "OK", "results": []}),
            httpx.Response(200, json=_polygon_payload("SPY")),
        ]
    )

    async def _next_response(request: httpx.Request) -> httpx.Response:
        return next(responses)

    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        side_effect=_next_response
    )
    spec = _spec()

    first = await ensure_data(spec)
    minute_failure = next(
        f for f in first.failures if f.artifact_kind == "time_series_bars" and f.trading_date == TRADING_DAY
    )
    assert minute_failure.reason == "provider_no_data"

    # No poll, no wait — one direct second call, and the previously-failed
    # row is what gets fetched: the provider's second response completes it.
    second = await ensure_data(spec)

    assert second.overall_status == "complete", second.failures
    minute = [a for a in second.artifacts if a.resolution == "minute"]
    assert len(minute) == 1


@respx.mock
@pytest.mark.asyncio
async def test_ensure_data_reports_a_terminal_failure_once_retries_are_exhausted(fake_catalog, tmp_lake, monkeypatch):
    """A row that keeps failing must eventually say so plainly, not loop.

    ``fetch_timeout`` (not ``lease_timeout``) so the bridge's contention
    classifier does not send this back into the 600s poll it can never win —
    an exhausted retry budget clears no faster the fourth time it is asked.
    """
    from app.data_lake import ensure_data as ensure_data_module

    _mock_launcher()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json={"ticker": "SPY", "status": "OK", "results": []})
    )
    monkeypatch.setattr(ensure_data_module, "_MAX_CLAIM_RETRIES", 2)
    spec = _spec()

    await ensure_data(spec)  # attempt 1: fails, AttemptCount=1
    await ensure_data(spec)  # attempt 2: reclaimed, fails again, AttemptCount=2
    third = await ensure_data(spec)  # attempt 3: exhausted at max_retries=2

    minute_failure = next(
        f for f in third.failures if f.artifact_kind == "time_series_bars" and f.trading_date == TRADING_DAY
    )
    assert minute_failure.reason == "fetch_timeout"
    assert "exhausted" in (minute_failure.detail or "")
    # The terminal reason must not be read as contention, or the bridge would
    # poll a row that will never change again.
    assert not run_materialization._is_blocked_by_a_sibling_fetch(third, resolution="minute")


@pytest.mark.asyncio
async def test_a_wedged_metadata_lease_does_not_stall_a_run_that_reads_no_metadata():
    """Regression: one crashed bootstrap must not cost every run 600 seconds.

    The Phase-0 metadata artifacts are a process-global catalog identity --
    no symbol, no trading date, one row shared by every run on the
    deployment. A worker that dies mid-bootstrap leaves it ``fetching``, and
    before this fix every later engine run classified the resulting
    ``lease_timeout`` as sibling contention and slept out its whole
    ``fetch_timeout_seconds`` before proceeding -- only to pass the coverage
    gate immediately, because metadata withholds no bars at any resolution.

    Reproduced for real on scratch Postgres while writing #1839's parity
    suite: a metadata row left ``fetching`` by an earlier failed run hung the
    next run for the full ten minutes.
    """
    metadata_contention = DataAvailabilityResult(
        request_id=uuid4(),
        overall_status="partial",
        lean_data_root_path="/lake",
        data_availability_hash="a" * 64,
        artifacts=[],
        failures=[
            ArtifactFailure(
                artifact_kind="metadata",
                symbol=None,
                trading_date=None,
                data_type=None,
                reason="lease_timeout",
                detail="another worker holds the metadata claim",
                attempt_count=1,
            )
        ],
        non_sessions=[],
        fetched_artifact_count=0,
        reused_artifact_count=3,
        started_at_ms=0,
        completed_at_ms=1,
        duration_ms=1,
    )

    assert not run_materialization._is_blocked_by_a_sibling_fetch(metadata_contention, resolution="minute")
    assert not run_materialization._is_blocked_by_a_sibling_fetch(metadata_contention, resolution="daily")

    # And the wait loop therefore returns on the first pass rather than
    # polling: one ensure_data call, no sleep.
    calls: list[DataRunSpec] = []

    async def _one_pass(spec: DataRunSpec) -> DataAvailabilityResult:
        calls.append(spec)
        return metadata_contention

    with mock.patch.object(run_materialization, "ensure_data", _one_pass):
        result = await run_materialization._materialize_run_data(_spec(), resolution="minute")

    assert result is metadata_contention
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_wedged_lease_on_a_bar_the_run_reads_is_still_waited_on():
    """The counterpart, so the fix above is a narrowing and not a removal.

    Contention on a minute artifact this run's reader will actually open is
    exactly what the wait exists for, and it must still poll.
    """
    bar_contention = DataAvailabilityResult(
        request_id=uuid4(),
        overall_status="partial",
        lean_data_root_path="/lake",
        data_availability_hash="b" * 64,
        artifacts=[],
        failures=[
            ArtifactFailure(
                artifact_kind="time_series_bars",
                symbol="SPY",
                trading_date=TRADING_DAY,
                data_type="trade",
                reason="lease_timeout",
                detail="another worker holds this day's claim",
                attempt_count=1,
            )
        ],
        non_sessions=[],
        fetched_artifact_count=0,
        reused_artifact_count=0,
        started_at_ms=0,
        completed_at_ms=1,
        duration_ms=1,
    )

    assert run_materialization._is_blocked_by_a_sibling_fetch(bar_contention, resolution="minute")

    calls: list[DataRunSpec] = []

    async def _always_contended(spec: DataRunSpec) -> DataAvailabilityResult:
        calls.append(spec)
        return bar_contention

    ticks = iter([0.0, 0.0, 99.0])
    with mock.patch.object(run_materialization, "ensure_data", _always_contended):
        await run_materialization._materialize_run_data(
            _spec().model_copy(update={"fetch_timeout_seconds": 10}),
            resolution="minute",
            poll_interval_s=0.0,
            now=lambda: next(ticks),
        )

    assert len(calls) == 2


@respx.mock
@pytest.mark.asyncio
async def test_materialize_run_data_stops_waiting_at_the_fetch_deadline(
    fake_catalog, tmp_lake, monkeypatch, ensure_attempts
):
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

    result = await _materialize_run_data(spec, resolution="minute", poll_interval_s=0.0, now=lambda: next(ticks))

    assert result.overall_status in {"partial", "failed"}
    assert any(f.reason == "lease_timeout" for f in result.failures)
    # One pass, one wait, one more pass, then the deadline. Without the loop
    # there would be exactly one attempt and this test would prove nothing.
    assert len(ensure_attempts) == 2


# ---------------------------------------------------------------------------
# Phase-0 metadata: contention is not a launcher failure
#
# The wait above keys off ``lease_timeout``, so ``ensure_data`` reporting the
# two Phase-0 outcomes under one reason would make it un-waitable: a run that
# lost the metadata race would look exactly like a run whose launcher is dead.
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_metadata_leased_elsewhere_reports_contention(fake_catalog, tmp_lake):
    _mock_launcher(latency_s=0.05)
    _mock_polygon()

    # Raw ensure_data, not the bridge: the bridge's retry would erase the
    # loser's report, and the loser's report is the subject here.
    both = await asyncio.gather(ensure_data(_spec()), ensure_data(_spec()))

    metadata_failures = [f for result in both for f in result.failures if f.artifact_kind == "metadata"]
    assert metadata_failures, "the two runs did not race for the Phase-0 claim"
    assert all(f.reason == "lease_timeout" for f in metadata_failures)


@respx.mock
@pytest.mark.asyncio
async def test_metadata_launcher_failure_is_not_contention(fake_catalog, tmp_lake):
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        return_value=httpx.Response(500, json={"detail": "launcher internal error"})
    )
    _mock_polygon()

    result = await ensure_data(_spec())

    metadata_failures = [f for f in result.failures if f.artifact_kind == "metadata"]
    assert metadata_failures
    assert all(f.reason == "io_error" for f in metadata_failures)


# ---------------------------------------------------------------------------
# Partial coverage the run cannot survive
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_a_second_window_rebuilds_the_daily_artifact_onto_the_wider_source_set(fake_catalog, tmp_lake):
    """#1870: a second, wider window rebuilds the daily artifact instead of
    refusing.

    The derived daily artifact's data contract is keyed by the set of minute
    artifacts it aggregated, so widening the window makes the newly computed
    hash describe a different (larger) set than the narrow call's. Rather
    than refuse with a stale-daily failure, ensure_data now rebuilds the
    artifact onto the current full set — the daily artifact's job is to
    always reflect everything currently catalogued for the symbol, not one
    call's requested window.
    """
    _mock_launcher()
    _mock_polygon()

    narrow = await _materialize_run_data(_narrow_spec(), resolution="minute")
    wider = await _materialize_run_data(_wide_spec(), resolution="minute")

    assert wider.overall_status == "complete", wider.failures
    mismatch_failures = [f for f in wider.failures if f.reason == "data_contract_mismatch"]
    assert not mismatch_failures, wider.failures

    def _daily_artifact(result):
        daily = [a for a in result.artifacts if a.artifact_kind == "time_series_bars" and a.resolution == "daily"]
        assert len(daily) == 1, result.artifacts
        return daily[0]

    narrow_daily = _daily_artifact(narrow)
    wide_daily = _daily_artifact(wider)
    assert wide_daily.data_contract_hash != narrow_daily.data_contract_hash, (
        "the rebuilt daily artifact must reflect the wider source set, not the narrow call's cached hash"
    )
    assert wider.refreshed_artifact_count == 1, "the rebuild must be counted as refreshed, not fetched or reused"


def test_materialize_engine_run_refuses_when_the_daily_bars_it_reads_are_stale(monkeypatch):
    """A daily-resolution run must not read the previous window's zip."""
    monkeypatch.setattr(
        run_materialization,
        "_materialize_run_data_sync",
        lambda spec, **_kwargs: _partial_with_stale_daily(spec),
    )

    with pytest.raises(LakeMaterializationError, match="incomplete daily coverage"):
        materialize_engine_run(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY, resolution="daily")


def test_materialize_engine_run_ignores_a_stale_daily_for_a_minute_run(monkeypatch):
    """The same partial is harmless to a run that never opens the daily zip."""
    monkeypatch.setattr(
        run_materialization,
        "_materialize_run_data_sync",
        lambda spec, **_kwargs: _partial_with_stale_daily(spec),
    )

    result = materialize_engine_run(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY, resolution="minute")

    # Reported to the caller, not raised: the minute run reads none of it.
    assert result.incomplete_summary == "time_series_bars/data_contract_mismatch"


def test_materialize_engine_run_refuses_a_missing_session_for_a_minute_run(monkeypatch):
    """A day the provider could not supply is a hole in the backtest, not a note."""

    def _missing_day(spec: DataRunSpec, **_kwargs) -> DataAvailabilityResult:
        return _partial(
            spec,
            ArtifactFailure(
                artifact_kind="time_series_bars",
                symbol="SPY",
                trading_date=TRADING_DAY,
                data_type="trade",
                reason="provider_no_data",
            ),
        )

    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", _missing_day)

    with pytest.raises(LakeMaterializationError, match="incomplete minute coverage"):
        materialize_engine_run(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY, resolution="minute")


def test_materialize_engine_run_refuses_a_missing_session_for_a_daily_run(monkeypatch):
    """The derived daily zip IS every source session, aggregated.

    ``ensure_data`` builds the daily artifact from whatever minute artifacts
    materialized, so a source session Polygon could not supply is silently
    absent from an otherwise "complete" daily zip — nothing about the
    aggregate's own data contract ever flags it, because the contract is
    keyed by the sessions that DID materialize, and that set is stable if
    the missing day never gets fetched. Refuse on the underlying per-day
    failure directly, the same way a minute run would, rather than trusting
    the aggregate to notice on its own.
    """

    def _missing_day(spec: DataRunSpec, **_kwargs) -> DataAvailabilityResult:
        return _partial(
            spec,
            ArtifactFailure(
                artifact_kind="time_series_bars",
                symbol="SPY",
                trading_date=TRADING_DAY,
                data_type="trade",
                reason="provider_no_data",
            ),
        )

    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", _missing_day)

    with pytest.raises(LakeMaterializationError, match="incomplete daily coverage") as exc_info:
        materialize_engine_run(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY, resolution="daily")

    # The refusal names which session is missing, not just that the window is
    # short one — critical once a window can span more than a single day.
    assert str(TRADING_DAY) in str(exc_info.value)


def test_materialize_engine_run_lets_a_metadata_note_through(monkeypatch):
    """Metadata failures degrade the calendar; they withhold no bars."""

    def _metadata_only(spec: DataRunSpec, **_kwargs) -> DataAvailabilityResult:
        return _partial(
            spec,
            ArtifactFailure(
                artifact_kind="metadata",
                symbol=None,
                trading_date=None,
                data_type=None,
                reason="io_error",
            ),
        )

    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", _metadata_only)

    result = materialize_engine_run(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY)

    assert result.incomplete_summary == "metadata/io_error"


def _minute_record(rel_path: str, *, file_size_bytes: int | None) -> ArtifactRecord:
    return ArtifactRecord(
        id=1,
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=TRADING_DAY,
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
        data_contract_hash="c" * 64,
        file_path=rel_path,
        file_sha256="d" * 64,
        row_count=390,
        first_bar_start_ms=0,
        last_bar_start_ms=0,
        file_size_bytes=file_size_bytes,
    )


def _complete_with_artifacts(spec: DataRunSpec, *, lake_root: Path, artifacts: list[ArtifactRecord]):
    return DataAvailabilityResult(
        request_id=spec.request_id,
        overall_status="complete",
        lean_data_root_path=str(lake_root),
        data_availability_hash="e" * 64,
        artifacts=artifacts,
        fetched_artifact_count=0,
        reused_artifact_count=len(artifacts),
        completed_at_ms=0,
        duration_ms=0,
    )


def test_materialize_engine_run_refuses_when_a_reused_artifacts_file_is_gone(monkeypatch, tmp_path: Path):
    """Codex finding 6: a catalog row marked complete does not mean the
    bytes are still there.

    ``ensure_data``'s claim machinery only ever checks the catalog once a
    row is 'complete' — a reused artifact is never re-touched, so a volume
    restored from an older snapshot (or a manual prune) can leave a
    'complete' row naming a file that is no longer on disk. Without this
    check, the LEAN reader would silently treat the missing day as an
    ordinary hole in the series rather than a torn promise.
    """
    lake_root = tmp_path / "lake"
    rel_path = f"equity/usa/minute/spy/{TRADING_DAY:%Y%m%d}_trade.zip"
    file_path = lake_root / rel_path
    file_path.parent.mkdir(parents=True)
    payload = b"stand-in for a real zip; only the byte count matters here"
    file_path.write_bytes(payload)
    record = _minute_record(rel_path, file_size_bytes=len(payload))

    monkeypatch.setattr(
        run_materialization,
        "_materialize_run_data_sync",
        lambda spec, **_kwargs: _complete_with_artifacts(spec, lake_root=lake_root, artifacts=[record]),
    )

    file_path.unlink()  # the gap this check exists to catch

    with pytest.raises(LakeMaterializationError, match="not on disk"):
        materialize_engine_run(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY, resolution="minute")


def test_materialize_engine_run_refuses_when_a_reused_artifacts_size_has_drifted(monkeypatch, tmp_path: Path):
    """A file that exists but no longer matches the catalog's recorded size
    is refused too — not just a missing file."""
    lake_root = tmp_path / "lake"
    rel_path = f"equity/usa/minute/spy/{TRADING_DAY:%Y%m%d}_trade.zip"
    file_path = lake_root / rel_path
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"the wrong number of bytes")
    record = _minute_record(rel_path, file_size_bytes=999_999)  # does not match what's on disk

    monkeypatch.setattr(
        run_materialization,
        "_materialize_run_data_sync",
        lambda spec, **_kwargs: _complete_with_artifacts(spec, lake_root=lake_root, artifacts=[record]),
    )

    with pytest.raises(LakeMaterializationError, match="the catalog recorded"):
        materialize_engine_run(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY, resolution="minute")


def test_materialize_engine_run_does_not_verify_a_file_this_run_never_opens(monkeypatch, tmp_path: Path):
    """Scoped like the coverage gate: a missing daily zip is not this
    function's business on a minute run."""
    lake_root = tmp_path / "lake"
    minute_rel = f"equity/usa/minute/spy/{TRADING_DAY:%Y%m%d}_trade.zip"
    minute_path = lake_root / minute_rel
    minute_path.parent.mkdir(parents=True)
    minute_payload = b"the only file this minute run actually reads"
    minute_path.write_bytes(minute_payload)
    minute_record = _minute_record(minute_rel, file_size_bytes=len(minute_payload))

    daily_record = ArtifactRecord(
        id=2,
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=None,  # the aggregate identity
        resolution="daily",
        data_type="trade",
        provider="learn_ai_derived",
        price_adjustment_mode="raw",
        data_contract_hash="f" * 64,
        file_path="equity/usa/daily/spy.zip",  # never written to disk in this test
        file_sha256="a" * 64,
        row_count=1,
        first_bar_start_ms=0,
        last_bar_start_ms=0,
        file_size_bytes=123,
    )

    monkeypatch.setattr(
        run_materialization,
        "_materialize_run_data_sync",
        lambda spec, **_kwargs: _complete_with_artifacts(spec, lake_root=lake_root, artifacts=[minute_record, daily_record]),
    )

    result = materialize_engine_run(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY, resolution="minute")

    assert result.incomplete_summary is None


def test_materialize_engine_run_hands_back_only_what_a_run_may_act_on(monkeypatch):
    """The four facts, and no failure list left for a caller to re-judge."""

    def _clean(spec: DataRunSpec, **_kwargs) -> DataAvailabilityResult:
        return DataAvailabilityResult(
            request_id=spec.request_id,
            overall_status="complete",
            lean_data_root_path="/lean-data-writer/lake",
            data_availability_hash="d" * 64,
            fetched_artifact_count=3,
            reused_artifact_count=1,
            completed_at_ms=0,
            duration_ms=0,
        )

    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", _clean)

    assert materialize_engine_run(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY) == EngineRunMaterialization(
        availability_hash="d" * 64,
        fetched_artifact_count=3,
        reused_artifact_count=1,
        incomplete_summary=None,
    )


def _partial(spec: DataRunSpec, *failures: ArtifactFailure) -> DataAvailabilityResult:
    return DataAvailabilityResult(
        request_id=spec.request_id,
        overall_status="partial",
        lean_data_root_path="/lean-data-writer/lake",
        data_availability_hash="b" * 64,
        failures=list(failures),
        completed_at_ms=0,
        duration_ms=0,
    )


def _partial_with_stale_daily(spec: DataRunSpec) -> DataAvailabilityResult:
    """The exact failure shape the end-to-end test above produces."""
    return _partial(
        spec,
        ArtifactFailure(
            artifact_kind="time_series_bars",
            symbol="SPY",
            trading_date=None,
            data_type="trade",
            reason="data_contract_mismatch",
        ),
    )


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

    monkeypatch.setattr(run_materialization, "_materialize_run_data", _fake)

    result = _materialize_run_data_sync(spec, resolution="minute")

    assert seen == [spec.request_id]
    assert result.data_availability_hash == "a" * 64


def test_materialize_run_data_sync_reuses_one_loop_for_the_process(monkeypatch):
    """The catalog pool belongs to a loop; a per-call loop would orphan it."""

    async def _fake(passed_spec, **kwargs):
        return _sentinel_result(passed_spec)

    monkeypatch.setattr(run_materialization, "_materialize_run_data", _fake)

    _materialize_run_data_sync(_spec(), resolution="minute")
    first_loop = run_materialization._materialization_loop()
    _materialize_run_data_sync(_spec(), resolution="minute")

    assert run_materialization._materialization_loop() is first_loop
    assert not first_loop.is_closed()


@pytest.mark.asyncio
async def test_materialize_run_data_sync_refuses_to_block_an_event_loop():
    with pytest.raises(RuntimeError, match="running event loop"):
        _materialize_run_data_sync(_spec(), resolution="minute")
