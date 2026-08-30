"""Unit tests for ensure_data dispatch logic.

Slice 1a: these tests were fixture-backed (no catalog, no Polygon).
Slice 1b: ensure_data now dispatches minute-trade through the real pipeline,
so tests that include minute-trade artifacts need pool management and a
respx-mocked Polygon response. The Slice 1a assertion invariants are
preserved; only the test infrastructure is updated.
Slice 1c: ensure_data now performs Phase 0 metadata bootstrap (calls the LEAN
launcher) and dispatches all artifact kinds through real implementations.
Tests updated to mock the launcher endpoint + corp-action endpoints.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path, PurePosixPath
from uuid import UUID

import asyncpg
import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.catalog_client import ArtifactClaimState
from app.data_lake.ensure_data import _bootstrap_metadata_artifact, ensure_data
from app.data_lake.types import DataRunSpec
from app.lean_sidecar import config as sidecar_config


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()


@pytest.fixture
async def pool():
    # Force-reset any stale pool left by a prior test (different event loop).
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch):
    """Point LEAN_DATA_WRITE_ROOT at a tmp_path tree with lake/ + staging/.

    Also points app.lean_sidecar.config.DEFAULT_ARTIFACTS_ROOT at a sibling
    tmp_path tree: Phase 0 reads the launcher's extracted metadata files back
    off that root (see app.data_lake.lean_metadata — it does not trust the
    launcher's HTTP response body, only its own view of the shared mount),
    so tests must stage files there rather than under the real repo path.
    Returns that artifacts root for tests to stage files into.
    """
    write_root = tmp_path / "writer-root"
    (write_root / "lake").mkdir(parents=True)
    (write_root / "staging").mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "test-key")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_URL", "http://launcher-mock:8090")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_TOKEN", "test-token")
    # Phase 0 resolves the token via app.lean_sidecar.launcher_auth.read_launcher_token
    # (env-or-file, matching the launcher's mandatory-auth contract), not by
    # reading settings.LEAN_LAUNCHER_TOKEN directly — so the env var is what
    # actually needs to be set for that resolution to be deterministic here
    # instead of falling through to whatever .launcher-token happens to sit
    # on the machine running the test.
    monkeypatch.setenv("LEAN_LAUNCHER_TOKEN", "test-token")
    artifacts_root = tmp_path / "artifacts-root"
    artifacts_root.mkdir(parents=True)
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", artifacts_root)
    return artifacts_root


def _spec(symbols: list[str]) -> DataRunSpec:
    return DataRunSpec(
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=symbols,
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 24),
        lean_image_digest="sha256:test",
    )


def _polygon_ok_payload(ticker: str) -> dict:
    # 2024-05-20 09:30:00 ET (DST) = 1716211800000 ms UTC (09:30 ET = 13:30 UTC = 13:30 * 3600 * 1000 + epoch)
    bar_start_ms = 1716211800000
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
                "t": bar_start_ms + i * 60_000,
                "n": 10,
            }
            for i in range(390)
        ],
    }


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


def _launcher_side_effect(artifacts_root: Path):
    """respx side_effect standing in for a real launcher: stages the files
    app.data_lake.lean_metadata will read back, keyed by the run_id the
    caller sent, then returns the launcher's actual (paths-only) response
    shape — not the base64-bytes shape a prior version of the caller
    expected but the launcher has never sent."""

    def _mock(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        _stage_workspace_files(artifacts_root, body["run_id"])
        return httpx.Response(
            200,
            json={
                "market_hours_db_path": "/launcher-side/market-hours-database.json",
                "symbol_properties_db_path": "/launcher-side/symbol-properties-database.csv",
            },
        )

    return _mock


def _mock_corpus_actions_and_events() -> None:
    """Register respx mocks for splits, dividends, ticker-events (all empty)."""
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/dividends.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/tickers/.*/events.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )


@respx.mock
@pytest.mark.asyncio
async def test_known_symbol_produces_complete_result(clean_artifacts, pool, tmp_lake):
    # Slice 1c: mock launcher + corp-action endpoints in addition to Polygon aggs.
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    _mock_corpus_actions_and_events()
    # Catch-all mock: any Polygon aggs call for SPY returns 390 bars.
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    result = await ensure_data(_spec(["SPY"]))
    assert result.overall_status == "complete"
    assert result.failures == []
    assert len(result.artifacts) > 0
    assert all(a.symbol in {None, "SPY"} for a in result.artifacts)


@respx.mock
@pytest.mark.asyncio
async def test_unknown_symbol_produces_partial_with_failures(clean_artifacts, pool, tmp_lake):
    # Slice 1c: mock launcher + corp-action endpoints.
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    _mock_corpus_actions_and_events()
    # UNKNOWN symbol: Polygon returns no bars → provider_no_data failure.
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/UNKNOWN/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json={"ticker": "UNKNOWN", "status": "OK", "results": []})
    )

    result = await ensure_data(_spec(["UNKNOWN"]))
    assert result.overall_status in {"partial", "failed"}
    assert len(result.failures) > 0
    # Slice 1b/1c: unknown symbols fail with provider_no_data (Polygon returns empty).
    assert any(f.reason in {"unknown_symbol", "provider_no_data"} for f in result.failures)


@respx.mock
@pytest.mark.asyncio
async def test_two_identical_calls_produce_same_availability_hash(clean_artifacts, pool, tmp_lake):
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    _mock_corpus_actions_and_events()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    a = await ensure_data(_spec(["SPY"]))
    # Second call: same artifacts (cache hits) → same hash.
    spec2 = _spec(["SPY"])
    b = await ensure_data(spec2)
    assert a.data_availability_hash == b.data_availability_hash


# ---------------------------------------------------------------------------
# P1 #1: Metadata bootstrap failure surfaces as ArtifactFailure
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_metadata_bootstrap_failure_surfaces_as_artifact_failure(clean_artifacts, pool, tmp_lake):
    """When the launcher returns 500, ensure_data must include metadata ArtifactFailure entries."""
    # Launcher returns 500 for both metadata extractions.
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        return_value=httpx.Response(500, json={"detail": "launcher internal error"})
    )
    _mock_corpus_actions_and_events()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    result = await ensure_data(_spec(["SPY"]))

    metadata_failures = [f for f in result.failures if f.artifact_kind == "metadata"]
    assert len(metadata_failures) >= 1, "expected at least one metadata ArtifactFailure when launcher returns 500"
    assert all(f.reason == "io_error" for f in metadata_failures)
    assert result.overall_status in {"partial", "failed"}


@respx.mock
@pytest.mark.asyncio
async def test_metadata_bootstrap_retries_a_prior_failure_instead_of_jamming(clean_artifacts, pool, tmp_lake):
    """Regression: claim_metadata_artifact's ON CONFLICT DO NOTHING has no
    reclaim path of its own (unlike claim_minute_bar), so a settled 'failed'
    row used to look identical to live contention on every later call —
    ensure_data would report lease_timeout on this data_contract_hash
    forever, even once the launcher was healthy again. A second call must
    reclaim the failed row via steal_or_retry_minute_bar and succeed."""
    succeed_from_call = 2  # round 1 (calls 0, 1) fails; round 2 (calls 2, 3) succeeds
    stage = _launcher_side_effect(tmp_lake)
    calls = {"n": 0}

    def _flaky_then_recovers(request: httpx.Request) -> httpx.Response:
        n = calls["n"]
        calls["n"] += 1
        if n < succeed_from_call:
            return httpx.Response(500, json={"detail": "launcher internal error"})
        return stage(request)

    launcher_route = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_flaky_then_recovers
    )
    _mock_corpus_actions_and_events()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    first = await ensure_data(_spec(["SPY"]))
    first_metadata_failures = [f for f in first.failures if f.artifact_kind == "metadata"]
    assert len(first_metadata_failures) == 2, "both metadata files should fail while the launcher is down"

    second = await ensure_data(_spec(["SPY"]))
    second_metadata_failures = [f for f in second.failures if f.artifact_kind == "metadata"]
    assert second_metadata_failures == [], (
        f"expected the recovered launcher to be retried, not permanently jammed: {second_metadata_failures}"
    )
    assert launcher_route.call_count == 4


@pytest.mark.asyncio
async def test_metadata_bootstrap_does_not_misreport_a_lost_reclaim_race_as_exhausted(
    monkeypatch,
):
    """Regression (review round on #1867): a caller that loses a reclaim race
    used to trust `row_state` — a snapshot taken *before* the reclaim attempt
    — instead of re-reading current state. Two callers can both see the same
    settled 'failed' row, both attempt `steal_or_retry_minute_bar`, and only
    one wins; the loser's `row_state.status` is still `'failed'` even though
    the winner just flipped the real row to `'fetching'` under a live lease.
    Trusting the stale snapshot reported a terminal, exhausted-retries
    `fetch_timeout` for a row someone else was actively completing — this
    must report the transient `lease_timeout` instead.

    No Postgres needed: `catalog_client` is faked directly so the race is
    deterministic rather than relying on real concurrent connections.
    """
    stale_snapshot = ArtifactClaimState(id=42, status="failed", attempt_count=1, last_error="boom")
    fresh_after_race = ArtifactClaimState(id=42, status="fetching", attempt_count=2, last_error=None)
    claim_state_calls = {"n": 0}

    async def fake_claim_metadata_artifact(**_kwargs):
        return None  # lost the initial insert — the row already exists

    async def fake_select_complete_metadata_artifact(_dch):
        return None  # not a cache hit

    async def fake_select_metadata_claim_state(_dch):
        claim_state_calls["n"] += 1
        # 1st read: the stale snapshot both racing callers observe.
        # 2nd read: this caller re-checking after losing the reclaim below —
        # must see what the winner actually left behind.
        return stale_snapshot if claim_state_calls["n"] == 1 else fresh_after_race

    async def fake_steal_or_retry_minute_bar(**_kwargs):
        return False  # this caller lost the race

    monkeypatch.setattr(catalog_client, "claim_metadata_artifact", fake_claim_metadata_artifact)
    monkeypatch.setattr(catalog_client, "select_complete_metadata_artifact", fake_select_complete_metadata_artifact)
    monkeypatch.setattr(catalog_client, "select_metadata_claim_state", fake_select_metadata_claim_state)
    monkeypatch.setattr(catalog_client, "steal_or_retry_minute_bar", fake_steal_or_retry_minute_bar)

    record, is_reused, failure_reason = await _bootstrap_metadata_artifact(
        file_name="market-hours-database.json",
        metadata_kind="market_hours",
        rel_path=PurePosixPath("metadata/market-hours-database.json"),
        lean_image_digest="sha256:test",
        spec=_spec(["SPY"]),
        lake_root=Path("/unused-lake-root"),
        staging_root=Path("/unused-staging-root"),
    )

    assert record is None
    assert is_reused is False
    assert failure_reason == "lease_timeout", (
        f"lost a reclaim race must read as transient contention, not exhausted retries: got {failure_reason!r}"
    )
    assert claim_state_calls["n"] == 2, "must re-read claim state after losing the reclaim, not trust the stale snapshot"


@respx.mock
@pytest.mark.asyncio
async def test_metadata_bootstrap_sends_the_resolved_launcher_token(
    clean_artifacts, pool, tmp_lake, monkeypatch
):
    """Regression: Phase 0 used to read settings.LEAN_LAUNCHER_TOKEN directly,
    which is empty unless an operator sets the env var — skipping the
    launcher's auto-generated file-backed token entirely
    (app.lean_sidecar.launcher_auth.read_launcher_token, the same resolution
    app.lean_sidecar.launcher_client already uses). Every request under the
    launcher's mandatory auth then got a 401 it could never recover from.

    The realistic bug shape is settings empty / env set — the `tmp_lake`
    fixture sets both to "test-token", which the old direct-settings-read
    code would also send, so it would pass this test either way. Un-set
    settings' copy here so the two implementations actually diverge: the old
    code sends no header at all (settings.LEAN_LAUNCHER_TOKEN == ""), the
    fixed one still resolves "test-token" from the environment.
    """
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_TOKEN", "")
    launcher_route = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    _mock_corpus_actions_and_events()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    await ensure_data(_spec(["SPY"]))

    assert launcher_route.call_count == 2  # market-hours + symbol-properties
    for call in launcher_route.calls:
        assert call.request.headers["X-Launcher-Token"] == "test-token"


# ---------------------------------------------------------------------------
# P1 #2: Factor-file DCH varies with history window
# ---------------------------------------------------------------------------


def test_factor_file_dch_differs_across_windows():
    """Two ensure_data calls with different windows must produce different factor-file DCHs."""
    from app.data_lake.ensure_data import _factor_file_dch

    dch_narrow = _factor_file_dch(date(2024, 5, 20), date(2024, 5, 22), "raw")
    dch_wide = _factor_file_dch(date(2024, 5, 20), date(2024, 5, 24), "raw")
    assert dch_narrow != dch_wide, "factor-file data_contract_hash must differ when history windows differ"


# ---------------------------------------------------------------------------
# P1 #3: Stale daily artifact detected via DCH mismatch
# ---------------------------------------------------------------------------


def _spec_narrow(symbols: list[str]) -> DataRunSpec:
    return DataRunSpec(
        request_id=UUID("aaaaaaaa-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=symbols,
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 22),
        lean_image_digest="sha256:test",
    )


def _spec_wide(symbols: list[str]) -> DataRunSpec:
    return DataRunSpec(
        request_id=UUID("bbbbbbbb-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=symbols,
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 24),
        lean_image_digest="sha256:test",
    )


def _polygon_ok_payload_date(ticker: str, bar_start_ms: int) -> dict:
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
                "t": bar_start_ms + i * 60_000,
                "n": 10,
            }
            for i in range(390)
        ],
    }


@respx.mock
@pytest.mark.asyncio
async def test_daily_artifact_rebuilds_onto_a_wider_window(clean_artifacts, pool, tmp_lake):
    """Narrower window creates a daily artifact; wider window rebuilds it onto
    the larger source set instead of refusing (#1870)."""
    launcher_mock = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/dividends.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/tickers/.*/events.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )
    # 2024-05-20 09:30 ET = 1716211800000 ms UTC
    # 2024-05-21 09:30 ET = 1716298200000 ms UTC
    # 2024-05-22 09:30 ET = 1716384600000 ms UTC
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/2024-05-20.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload_date("SPY", 1716211800000))
    )
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/2024-05-21.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload_date("SPY", 1716298200000))
    )
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/2024-05-22.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload_date("SPY", 1716384600000))
    )
    # 2024-05-23 is a Thursday; 2024-05-24 09:30 ET = 1716557400000 ms UTC
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/2024-05-23.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload_date("SPY", 1716470400000))
    )
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/2024-05-24.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload_date("SPY", 1716557400000))
    )
    _ = launcher_mock  # suppress unused-variable warning

    # First call: narrow window (May 20–22) — daily artifact created with hash H1.
    result_narrow = await ensure_data(_spec_narrow(["SPY"]))
    assert result_narrow.overall_status == "complete", f"narrow call failed: {result_narrow.failures}"
    daily_artifacts_narrow = [
        a for a in result_narrow.artifacts if a.artifact_kind == "time_series_bars" and a.resolution == "daily"
    ]
    assert len(daily_artifacts_narrow) == 1
    h1 = daily_artifacts_narrow[0].data_contract_hash

    # Second call: wide window (May 20–24). The daily artifact's source set
    # is read from the catalog (all of SPY's complete minute-trade coverage),
    # not this call's own window, so it now includes the narrow call's days
    # too — producing daily hash H2 != H1, and a rebuild rather than a
    # data_contract_mismatch failure.
    result_wide = await ensure_data(_spec_wide(["SPY"]))
    assert result_wide.overall_status == "complete", f"wide call failed: {result_wide.failures}"
    mismatch_failures = [
        f
        for f in result_wide.failures
        if f.reason == "data_contract_mismatch" and f.artifact_kind == "time_series_bars"
    ]
    assert not mismatch_failures, f"expected no data_contract_mismatch failure, got: {result_wide.failures}"
    assert result_wide.refreshed_artifact_count == 1, "the daily rebuild must be counted as refreshed"

    daily_artifacts_wide = [
        a for a in result_wide.artifacts if a.artifact_kind == "time_series_bars" and a.resolution == "daily"
    ]
    assert len(daily_artifacts_wide) == 1
    h2 = daily_artifacts_wide[0].data_contract_hash
    assert h1 != h2, "the rebuilt daily artifact must reflect the wider source set, not the narrow call's cached hash"
