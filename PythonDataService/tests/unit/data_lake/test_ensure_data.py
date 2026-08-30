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
from pathlib import Path
from uuid import UUID

import asyncpg
import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import ArtifactIdentity, DataRunSpec
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


def _spec(symbols: list[str], *, lean_image_digest: str = "sha256:test") -> DataRunSpec:
    return DataRunSpec(
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=symbols,
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 24),
        lean_image_digest=lean_image_digest,
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
_INTEREST_RATE_CSV = b"date,rate\n2024-05-20,0.0525\n"


def _stage_workspace_files(artifacts_root: Path, run_id: str, *, include_interest_rate: bool = False) -> None:
    """Pre-place the files a real launcher run would have written.

    Layout must match app.lean_sidecar.workspace.Workspace.data_dir and
    staging.list_metadata_databases: <root>/<run_id>/workspace/data/...

    ``include_interest_rate`` defaults False: most tests exercise a
    launcher build (or an image variant) that doesn't produce the
    optional alternative/interest-rate subtree, which is the common case
    every test but the dedicated interest-rate ones needs.
    """
    data_dir = artifacts_root / run_id / "workspace" / "data"
    (data_dir / "market-hours").mkdir(parents=True, exist_ok=True)
    (data_dir / "symbol-properties").mkdir(parents=True, exist_ok=True)
    (data_dir / "market-hours" / "market-hours-database.json").write_bytes(_MARKET_HOURS_JSON)
    (data_dir / "symbol-properties" / "symbol-properties-database.csv").write_bytes(_SYMBOL_PROPERTIES_CSV)
    if include_interest_rate:
        (data_dir / "alternative" / "interest-rate" / "usa").mkdir(parents=True, exist_ok=True)
        (data_dir / "alternative" / "interest-rate" / "usa" / "interest-rate.csv").write_bytes(_INTEREST_RATE_CSV)


def _launcher_side_effect(artifacts_root: Path, *, include_interest_rate: bool = False):
    """respx side_effect standing in for a real launcher: stages the files
    app.data_lake.lean_metadata will read back, keyed by the run_id the
    caller sent, then returns the launcher's actual (paths-only) response
    shape — not the base64-bytes shape a prior version of the caller
    expected but the launcher has never sent."""

    def _mock(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        _stage_workspace_files(artifacts_root, body["run_id"], include_interest_rate=include_interest_rate)
        response: dict[str, str] = {
            "market_hours_db_path": "/launcher-side/market-hours-database.json",
            "symbol_properties_db_path": "/launcher-side/symbol-properties-database.csv",
        }
        if include_interest_rate:
            response["interest_rate_db_path"] = "/launcher-side/interest-rate.csv"
        return httpx.Response(200, json=response)

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
    """A launcher outage leaves no partial catalog state at all (#1879, PR C
    of #1861 rewrites Phase 0 into a single bundle call: catalog claims only
    ever happen *after* a successful extraction, so a failed round has
    nothing to reclaim). A second call, once the launcher recovers, must
    still succeed cleanly rather than being permanently jammed by whatever
    the first round left behind."""
    succeed_from_call = 1  # round 1 fails; round 2 succeeds
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
    assert len(first_metadata_failures) == 3, "all three metadata kinds fail together — one bundle, one launcher call"

    second = await ensure_data(_spec(["SPY"]))
    second_metadata_failures = [f for f in second.failures if f.artifact_kind == "metadata"]
    assert second_metadata_failures == [], (
        f"expected the recovered launcher to be retried, not permanently jammed: {second_metadata_failures}"
    )
    assert launcher_route.call_count == 2, "one launcher call per ensure_data call, not one per metadata kind"


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

    assert launcher_route.call_count == 1  # one call for the whole bundle (#1879)
    for call in launcher_route.calls:
        assert call.request.headers["X-Launcher-Token"] == "test-token"


# ---------------------------------------------------------------------------
# #1859: interest-rate is a third, optional metadata artifact
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_interest_rate_metadata_bootstraps_when_the_launcher_produces_it(clean_artifacts, pool, tmp_lake):
    """A launcher build that stages the alternative/interest-rate subtree
    (every current build does — see staging.py's podman-cp loop) gets it
    promoted into the lake as a third metadata artifact, closing the
    lake_mount.py-documented input divergence for this run."""
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake, include_interest_rate=True)
    )
    _mock_corpus_actions_and_events()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    result = await ensure_data(_spec(["SPY"]))

    assert result.overall_status == "complete"
    assert result.failures == []
    metadata = [a for a in result.artifacts if a.artifact_kind == "metadata"]
    assert len(metadata) == 3, f"expected 3 metadata artifacts (mh, sp, interest-rate), got {len(metadata)}"
    interest_rate = next(a for a in metadata if a.file_path == "alternative/interest-rate/usa/interest-rate.csv")
    assert interest_rate.file_size_bytes == len(_INTEREST_RATE_CSV)


@respx.mock
@pytest.mark.asyncio
async def test_interest_rate_metadata_bootstrap_is_optional_not_a_failure(clean_artifacts, pool, tmp_lake):
    """A launcher/workspace with no interest-rate subtree — every other
    test's `_launcher_side_effect` — must not degrade the run: LEAN falls
    back to its built-in risk-free rate (lake_mount.py's module
    docstring), so this is logged, never an ArtifactFailure."""
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)  # include_interest_rate defaults False
    )
    _mock_corpus_actions_and_events()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    result = await ensure_data(_spec(["SPY"]))

    assert result.overall_status == "complete"
    assert result.failures == [], "interest-rate absence must never surface as an ArtifactFailure"
    metadata = [a for a in result.artifacts if a.artifact_kind == "metadata"]
    assert len(metadata) == 2, f"expected exactly market-hours + symbol-properties, got {len(metadata)}"


@respx.mock
@pytest.mark.asyncio
async def test_interest_rate_genuine_extraction_failure_surfaces_as_artifact_failure(
    clean_artifacts, pool, tmp_lake
):
    """#1859 review fix: only confirmed absence (provider_no_data) is
    optional. A genuine extraction failure — the launcher 500s specifically
    on interest-rate's own independent request, unlike a workspace that
    simply never had the file — must surface exactly like a market-hours/
    symbol-properties failure, so the run does not silently claim input
    parity it doesn't have."""

    # Every extraction attempt fails at the HTTP layer, including the third
    # (interest-rate) call — this is the "the check itself broke" case, not
    # "confirmed no data".
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        return_value=httpx.Response(500, json={"detail": "launcher internal error"})
    )
    _mock_corpus_actions_and_events()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    result = await ensure_data(_spec(["SPY"]))

    metadata_failures = [f for f in result.failures if f.artifact_kind == "metadata"]
    assert len(metadata_failures) == 3, f"expected all three metadata kinds to fail, got {metadata_failures}"
    assert all(f.reason == "io_error" for f in metadata_failures)
    interest_rate_failure = next(f for f in metadata_failures if "interest-rate" in f.detail)
    assert interest_rate_failure.reason == "io_error"


@respx.mock
@pytest.mark.asyncio
async def test_interest_rate_confirmed_absence_never_exhausts_the_retry_budget(clean_artifacts, pool, tmp_lake):
    """Confirmed absence (the receipt's ``interest_rate`` entry is ``null``)
    is a fact recorded once, in the bundle's own on-disk receipt (#1879, PR C
    of #1861) — not a Postgres claim row with a retry budget to exhaust.
    Repeating the same call (same ``lean_image_digest``) any number of times
    must never surface a failure and never touch the launcher again after
    the first call, because the receipt is a verified cache hit.

    Per the issue's own acceptance criterion ("changing lean_image_digest
    triggers re-extraction"), a "launcher upgrade" under the *same* digest is
    no longer implicitly retried the way the pre-#1879 per-kind bootstrap
    did — only a digest change re-extracts. That is proven separately by
    ``test_changing_the_digest_triggers_re_extraction_and_stales_the_old_row``
    in ``test_metadata_bundle.py``; here a fresh digest stands in for
    exactly that operator action and demonstrates the file becomes
    available once taken.
    """
    launcher_route = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)  # never stages interest-rate
    )
    _mock_corpus_actions_and_events()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    for _ in range(6):
        result = await ensure_data(_spec(["SPY"]))
        ir_failures = [f for f in result.failures if "interest-rate" in f.detail]
        assert ir_failures == [], f"confirmed absence must never surface as a failure: {ir_failures}"
        metadata = [a for a in result.artifacts if a.artifact_kind == "metadata"]
        assert len(metadata) == 2, "interest-rate stays confirmed-absent; only market-hours + symbol-properties complete"

    assert launcher_route.call_count == 1, "a verified receipt for the same digest is a pure cache hit, no re-extraction"

    conn = await asyncpg.connect(_postgres_url())
    try:
        count = await conn.fetchval(
            """SELECT count(*) FROM "DataLakeArtifacts"
               WHERE "ArtifactKind" = 'metadata' AND "FilePath" LIKE '%interest-rate%'"""
        )
        assert count == 0, "confirmed absence needs no catalog row at all -- it never reaches the claim step"
    finally:
        await conn.close()

    # A new pinned image (the operator action the acceptance criteria name)
    # forces re-extraction; this mock now produces the interest-rate file.
    respx.routes.clear()
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake, include_interest_rate=True)
    )
    _mock_corpus_actions_and_events()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    final = await ensure_data(_spec(["SPY"], lean_image_digest="sha256:test-v2"))
    assert final.failures == [], f"expected the now-available file to complete cleanly: {final.failures}"
    metadata = [a for a in final.artifacts if a.artifact_kind == "metadata"]
    assert len(metadata) == 3, "market-hours + symbol-properties + interest-rate, all under the new digest"


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
    # 2, not 1: the factor_file's DCH is also window-scoped (_factor_file_dch)
    # and rebuilds onto the wider history window alongside the daily-trade
    # artifact — see test_factor_file_rebuilds_onto_a_wider_window for that
    # rebuild in isolation.
    assert result_wide.refreshed_artifact_count == 2, "both the daily and factor-file rebuilds must count as refreshed"

    daily_artifacts_wide = [
        a for a in result_wide.artifacts if a.artifact_kind == "time_series_bars" and a.resolution == "daily"
    ]
    assert len(daily_artifacts_wide) == 1
    h2 = daily_artifacts_wide[0].data_contract_hash
    assert h1 != h2, "the rebuilt daily artifact must reflect the wider source set, not the narrow call's cached hash"

    # Third call: identical wide window again. If the rebuild above had not
    # persisted the new hash to the DataContractHash column, this call would
    # see the same "mismatch" forever and rebuild a third time — the row
    # must now be a clean cache hit (#1873 review fix).
    result_wide_again = await ensure_data(_spec_wide(["SPY"]))
    assert result_wide_again.overall_status == "complete", f"repeat wide call failed: {result_wide_again.failures}"
    assert result_wide_again.refreshed_artifact_count == 0, "an unchanged source set must not rebuild again"
    daily_artifacts_again = [
        a for a in result_wide_again.artifacts if a.artifact_kind == "time_series_bars" and a.resolution == "daily"
    ]
    assert len(daily_artifacts_again) == 1
    assert daily_artifacts_again[0].data_contract_hash == h2


@respx.mock
@pytest.mark.asyncio
async def test_factor_file_rebuilds_onto_a_wider_window(clean_artifacts, pool, tmp_lake):
    """Same #1870 rebuild-on-mismatch model as the daily-trade artifact,
    applied to factor_file: a wider window changes _factor_file_dch (see
    test_factor_file_dch_differs_across_windows), and the existing complete
    row must rebuild onto it instead of silently keeping the narrower
    window's split/dividend history bounds (#1873 review fix — this gap
    would have produced incorrect adjusted results after a widened
    backfill)."""
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    _mock_corpus_actions_and_events()
    for trading_date, start_ms in {
        date(2024, 5, 20): 1716211800000,
        date(2024, 5, 21): 1716298200000,
        date(2024, 5, 22): 1716384600000,
        date(2024, 5, 23): 1716470400000,
        date(2024, 5, 24): 1716557400000,
    }.items():
        respx.get(
            url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/{trading_date.isoformat()}.*"
        ).mock(return_value=httpx.Response(200, json=_polygon_ok_payload_date("SPY", start_ms)))

    result_narrow = await ensure_data(_spec_narrow(["SPY"]))
    assert result_narrow.overall_status == "complete", f"narrow call failed: {result_narrow.failures}"
    factor_narrow = [a for a in result_narrow.artifacts if a.artifact_kind == "factor_file"]
    assert len(factor_narrow) == 1
    h1 = factor_narrow[0].data_contract_hash

    result_wide = await ensure_data(_spec_wide(["SPY"]))
    assert result_wide.overall_status == "complete", f"wide call failed: {result_wide.failures}"
    assert result_wide.refreshed_artifact_count >= 1, "the factor-file rebuild must be counted as refreshed"
    factor_wide = [a for a in result_wide.artifacts if a.artifact_kind == "factor_file"]
    assert len(factor_wide) == 1
    h2 = factor_wide[0].data_contract_hash
    assert h1 != h2, "the rebuilt factor file must reflect the wider history window, not the narrow call's cached hash"

    # Repeat wide call: clean cache hit, no third rebuild.
    result_wide_again = await ensure_data(_spec_wide(["SPY"]))
    assert result_wide_again.overall_status == "complete"
    factor_again = [a for a in result_wide_again.artifacts if a.artifact_kind == "factor_file"]
    assert len(factor_again) == 1
    assert factor_again[0].data_contract_hash == h2


@respx.mock
@pytest.mark.asyncio
async def test_daily_artifact_rebuild_failure_restores_the_prior_complete_state(clean_artifacts, pool, tmp_lake, monkeypatch):
    """A rebuild that fails while reading source trade bars must not strand
    a previously-working daily artifact 'failed' with no retry path —
    steal_or_retry_minute_bar does not cover aggregated-bar artifacts.
    Restore it to 'complete' instead (#1873 review fix)."""
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    _mock_corpus_actions_and_events()
    for trading_date, start_ms in {
        date(2024, 5, 20): 1716211800000,
        date(2024, 5, 21): 1716298200000,
        date(2024, 5, 22): 1716384600000,
        date(2024, 5, 23): 1716470400000,
        date(2024, 5, 24): 1716557400000,
    }.items():
        respx.get(
            url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/{trading_date.isoformat()}.*"
        ).mock(return_value=httpx.Response(200, json=_polygon_ok_payload_date("SPY", start_ms)))

    result_narrow = await ensure_data(_spec_narrow(["SPY"]))
    assert result_narrow.overall_status == "complete", f"narrow call failed: {result_narrow.failures}"
    daily_narrow = next(
        a for a in result_narrow.artifacts if a.artifact_kind == "time_series_bars" and a.resolution == "daily"
    )

    from app.data_lake import ensure_data as ensure_data_module

    def _boom(*args, **kwargs):
        raise OSError("simulated corrupt source zip")

    monkeypatch.setattr(ensure_data_module, "_read_minute_trade_bars", _boom)

    result_wide = await ensure_data(_spec_wide(["SPY"]))
    io_failures = [
        f for f in result_wide.failures if f.reason == "io_error" and f.artifact_kind == "time_series_bars"
    ]
    assert len(io_failures) == 1, f"expected exactly one daily-trade io_error, got: {result_wide.failures}"

    identity = ArtifactIdentity(
        artifact_kind="time_series_bars",
        market=daily_narrow.market,
        symbol="SPY",
        trading_date=None,
        resolution="daily",
        data_type="trade",
        provider="learn_ai_derived",
        price_adjustment_mode=daily_narrow.price_adjustment_mode,
    )
    restored = await catalog_client.select_complete_aggregated_bar_artifact(identity)
    assert restored is not None, "the row must still be 'complete' — a failed rebuild must not strand it"
    assert restored.data_contract_hash == daily_narrow.data_contract_hash
    assert restored.file_sha256 == daily_narrow.file_sha256
