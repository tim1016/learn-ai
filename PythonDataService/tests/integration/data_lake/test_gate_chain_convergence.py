"""The closure-plan convergence scenario for the #1825 issue family.

``resolve_lake_artifacts`` (app.lean_sidecar.lake_mount) is a linear chain of
gates — mount configured, root mode, trade coverage, quote coverage, daily
artifact, required metadata. Each open issue in the #1825 family was one gate
discovered in review after the previous one was fixed (#1869 is gate 5,
#1859 is past gate 6). Fixing gates in isolation, one PR at a time, does not
prove the chain actually converges — only running the real scenario twice,
back to back, through a real backfill and a real read, does.

Scenario: an operator with a cache_import'd-shaped lake runs a backfill over
window A, then the same symbol over a *different*, wider window B. Both
backfills must complete, the second must cost zero provider calls for A's
already-covered days, and the sidecar's lake-mode resolver must succeed
cleanly over the wider window afterward — no lake_* refusal.

See docs/superpowers/specs/2026-08-29-data-lake-issue-closure-plan.md § 4.
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
from app.data_lake.backfill import run_backfill
from app.data_lake.ensure_data import _metadata_dch
from app.data_lake.path_policy import lake_subpath
from app.data_lake.types import DataRunSpec, trading_date_to_calendar_anchor_ms
from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.lake_mount import resolve_lake_artifacts

pytestmark = pytest.mark.asyncio

# A distinct symbol from every other clean_artifacts-truncating test in this
# directory (SPY: test_ensure_data_all_kinds_complete; QQQ:
# test_ensure_data_second_call_is_cache_hit) so this module's minute-bar/
# daily-trade and corp-action (factor_file/map_file) claims share no
# identity with theirs under pytest-xdist concurrency — see the analogous
# note in test_ensure_data_all_kinds.py's _SECOND_CALL_DAY_OFFSETS_MS.
SYMBOL = "AAPL"
# 2024-05-20..22 is window A (Mon-Wed); 2024-05-20..24 is window B (Mon-Fri) —
# wider, not disjoint, matching the real #1870 reproduction shape.
WINDOW_A_END = date(2024, 5, 22)
WINDOW_B_END = date(2024, 5, 24)
_DAY_OFFSETS_MS = {
    date(2024, 5, 20): 1716211800000,
    date(2024, 5, 21): 1716298200000,
    date(2024, 5, 22): 1716384600000,
    date(2024, 5, 23): 1716471000000,
    date(2024, 5, 24): 1716557400000,
}
# See _spec()'s lean_image_digest note below.
_LEAN_IMAGE_DIGEST = "sha256:test-image-digest-gate-chain"
# The three metadata files Phase 0 always attempts (see ensure_data.py's
# calls into _metadata_dch) -- needed below to scope metadata-row cleanup
# by lean_image_digest, since metadata identity has no Symbol column.
_METADATA_FILE_NAMES = ("market-hours-database.json", "symbol-properties-database.csv", "interest-rate.csv")


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    """Delete only this test's own catalog rows (SYMBOL + its metadata
    contract hashes), instead of a blanket ``TRUNCATE``.

    A table-wide TRUNCATE (every other clean_artifacts fixture in this
    directory still does this) wipes ANY concurrently-running test's
    in-flight rows too, regardless of identity: under pytest-xdist, this
    test's two sequential backfill calls run long enough that another,
    faster test's setup/teardown TRUNCATE reliably lands mid-flight of this
    one. A disjoint SYMBOL/digest (see the module docstring above) prevents
    a claim COLLISION but not a TRUNCATE WIPE, since TRUNCATE carries no
    WHERE clause. Scoping the delete to SYMBOL, plus this test's own
    metadata contract hashes (computed via the same _metadata_dch the app
    itself claims by), makes cleanup identity-scoped instead of table-wide.
    """
    metadata_dchs = [_metadata_dch(_LEAN_IMAGE_DIGEST, name, "raw") for name in _METADATA_FILE_NAMES]

    async def _delete() -> None:
        conn = await asyncpg.connect(_postgres_url())
        try:
            await conn.execute(
                'DELETE FROM "DataLakeArtifacts" WHERE "Symbol" = ANY($1::text[]) OR "DataContractHash" = ANY($2::text[])',
                [SYMBOL],
                metadata_dchs,
            )
        finally:
            await conn.close()

    await _delete()
    yield
    await _delete()


@pytest.fixture
async def pool():
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch):
    write_root = tmp_path / "writer-root"
    (write_root / "lake").mkdir(parents=True)
    (write_root / "staging").mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "test-polygon-key")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_URL", "http://launcher-mock:8090")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_TOKEN", "test-token")
    # Phase 0 resolves the sent token via read_launcher_token(), which reads
    # os.environ directly rather than settings — the setattr above alone is
    # a no-op for what actually reaches the launcher (see
    # test_ensure_data.py's identical fixture for the prior fix this
    # mirrors).
    monkeypatch.setenv("LEAN_LAUNCHER_TOKEN", "test-token")
    artifacts_root = tmp_path / "artifacts-root"
    artifacts_root.mkdir(parents=True)
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", artifacts_root)
    return write_root


def _minimal_market_hours_json() -> bytes:
    return json.dumps(
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


def _minimal_symbol_properties_csv() -> bytes:
    return b"AAPL,equity,usd,1,0\n"


def _stage_workspace_files(artifacts_root: Path, run_id: str) -> None:
    data_dir = artifacts_root / run_id / "workspace" / "data"
    (data_dir / "market-hours").mkdir(parents=True, exist_ok=True)
    (data_dir / "symbol-properties").mkdir(parents=True, exist_ok=True)
    (data_dir / "market-hours" / "market-hours-database.json").write_bytes(_minimal_market_hours_json())
    (data_dir / "symbol-properties" / "symbol-properties-database.csv").write_bytes(_minimal_symbol_properties_csv())


def _launcher_side_effect(artifacts_root: Path):
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


def _polygon_aggs_for(start_ms: int) -> dict:
    return {
        "ticker": SYMBOL,
        "status": "OK",
        "results": [
            {"v": 1000, "vw": 500.0, "o": 500.0, "c": 500.05, "h": 500.10, "l": 499.95, "t": start_ms + i * 60_000, "n": 10}
            for i in range(390)
        ],
    }


_REQUEST_IDS = {
    WINDOW_A_END: UUID("aaaaaaaa-1234-5678-1234-567812345678"),
    WINDOW_B_END: UUID("bbbbbbbb-1234-5678-1234-567812345678"),
}


def _spec(end: date) -> DataRunSpec:
    return DataRunSpec(
        request_id=_REQUEST_IDS[end],
        run_type="python_lab",
        symbols=[SYMBOL],
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(date(2024, 5, 20)),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(end),
        data_types=["trade", "quote"],
        # Distinct from test_ensure_data_all_kinds_complete's
        # "sha256:test-image-digest": the metadata artifact claim is keyed
        # by lean_image_digest ALONE (uq_data_lake_artifacts_metadata is
        # symbol-independent — see ensure_data._metadata_dch), so reusing
        # that digest here would race the two tests' metadata claims under
        # pytest-xdist even after SYMBOL diverged. Shared with clean_artifacts
        # above so its scoped-delete matches what this spec actually claims.
        lean_image_digest=_LEAN_IMAGE_DIGEST,
    )


@respx.mock
async def test_backfill_window_a_then_wider_window_b_converges_through_the_gate_chain(clean_artifacts, pool, tmp_lake):
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake.parent / "artifacts-root")
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
    day_routes = {
        trading_date: respx.get(
            url__regex=(
                rf"https://api\.polygon\.io/v2/aggs/ticker/{SYMBOL}/range/1/minute/{trading_date.isoformat()}.*"
            )
        ).mock(return_value=httpx.Response(200, json=_polygon_aggs_for(start_ms)))
        for trading_date, start_ms in _DAY_OFFSETS_MS.items()
    }

    # --- Window A: 2024-05-20..22 (3 sessions) ---
    result_a = await run_backfill(_spec(WINDOW_A_END))
    assert result_a.overall_status == "complete", result_a.failures
    assert day_routes[date(2024, 5, 20)].call_count == 1
    assert day_routes[date(2024, 5, 21)].call_count == 1
    assert day_routes[date(2024, 5, 22)].call_count == 1

    # --- Window B: 2024-05-20..24 (5 sessions) — wider, not disjoint from A ---
    result_b = await run_backfill(_spec(WINDOW_B_END))
    assert result_b.overall_status == "complete", result_b.failures

    # Zero provider calls for A's already-covered days: call_count must not
    # have incremented past window A's own fetch.
    assert day_routes[date(2024, 5, 20)].call_count == 1
    assert day_routes[date(2024, 5, 21)].call_count == 1
    assert day_routes[date(2024, 5, 22)].call_count == 1
    # The two new days in B were fetched exactly once each.
    assert day_routes[date(2024, 5, 23)].call_count == 1
    assert day_routes[date(2024, 5, 24)].call_count == 1

    # The daily-trade rollup rebuilt onto the wider set (#1870) rather than
    # refusing — surfaced as a refresh, not a fetch, not a failure.
    assert not any(f.reason == "data_contract_mismatch" for f in result_b.failures)

    # --- The sidecar's own gate chain, over the wider window, must now
    # succeed cleanly: mount/root-mode/trade-coverage/quote-coverage/
    # daily-artifact/metadata, gates 1-6 of resolve_lake_artifacts. ---
    lake_root = tmp_lake / lake_subpath("raw")
    artifacts = resolve_lake_artifacts(
        lake_root=lake_root,
        symbol=SYMBOL,
        start=date(2024, 5, 20),
        end=WINDOW_B_END,
    )
    assert artifacts.trading_dates == tuple(sorted(_DAY_OFFSETS_MS))
    assert artifacts.daily_zip_path.exists()
    assert len(artifacts.trade_zip_paths) == 5
    assert len(artifacts.quote_zip_paths) == 5
