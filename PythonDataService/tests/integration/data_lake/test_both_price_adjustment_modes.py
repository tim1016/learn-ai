"""Cross-mode integration proof for issue #1891 (part of PRD #1885, slice 4).

Both price-adjustment roots -- ``raw`` (raw prices plus factor files, the
LEAN-native format carrying this repo's two-engine parity claim) and
``polygon_split_adjusted`` (serves charts and quick analysis) -- are kept
permanently, per PRD #1885's "Price-adjustment modes" decision. This file
proves, end to end and through a real (disposable) Postgres catalog, the two
claims that decision rests on:

1. **No identity collision.** Cataloguing the same ``(market, symbol,
   trading_date, data_type)`` in both modes produces two distinct catalog
   rows, not a claim collision where the second call silently reuses (or
   overwrites) the first mode's row. ``app.data_lake.ensure_data``'s own
   comment on this exact scenario (``_process_minute_trade_artifact``,
   "app.data_lake.cache_import can now put a 'polygon_split_adjusted' row in
   the catalog for the same ... identity") states the risk; this test is the
   proof no regression reopens it.
2. **Mode-correct reads.** The Python engine's own root resolver
   (``app.engine.data.policy_store.resolve_data_roots``) and the LEAN
   sidecar's own artifact resolver (``app.lean_sidecar.lake_mount``) each
   resolve the mode a caller actually requests to the matching physical
   subtree, and the bytes read back there are byte-identical to what
   ``ensure_data`` wrote for that specific mode -- not the other mode's bytes
   silently substituted through a wrong-root fallback.

Prior art and idiom this file follows (per the issue's own instruction):
``test_ensure_data_all_kinds.py`` for the respx/launcher/tmp_lake fixture
shape that drives ``ensure_data`` for real, and
``test_gate_chain_convergence.py`` for calling the real sidecar resolver
(``resolve_lake_artifacts``) against the real artifacts a real ``ensure_data``
run produced. ``test_flag_flip_parity.py`` established the *byte-identical
import* claim for one mode; this file is the two-mode claim that PRD #1885
identifies as the remaining gap.

Two Polygon aggregate payloads (keyed off the vendor's own ``adjusted`` query
parameter, exactly as ``app.data_lake.polygon_fetcher`` sends it) give the two
modes genuinely different bytes on disk -- a wrong-root fallback or a
mode-blind cache hit would read the *other* mode's prices and this test would
catch it, rather than passing on two identical fixtures that could not tell
the modes apart.

No Postgres, no run: every test in this module skips cleanly when
``POSTGRES_URL`` is unset (CI's "Python Tests" job sets none; see
``test_flag_flip_parity.py``'s module docstring for why). Never point
``POSTGRES_URL`` at ``my-postgres`` -- a disposable, migrated-to-head Postgres
only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.ensure_data import ensure_data
from app.data_lake.metadata_bundle import metadata_data_contract_hash as _metadata_dch
from app.data_lake.path_policy import LeanMinuteBarPath, lake_subpath, resolve_lake_root
from app.data_lake.types import DataRunSpec, PriceAdjustmentMode, trading_date_to_calendar_anchor_ms
from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.policy_store import resolve_data_roots
from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.lake_mount import LakeMountError, data_plane_lake_root, resolve_lake_artifacts

pytestmark = pytest.mark.asyncio

# A symbol distinct from every other clean_artifacts-truncating test in this
# directory (see test_gate_chain_convergence.py's identical note) so this
# module's minute-bar/quote/daily/corp-action claims share no identity with
# theirs under pytest-xdist concurrency.
SYMBOL = "ZQPX1891"
TRADING_DATE = date(2024, 5, 20)  # Monday, a plain NYSE session, no early close.
# 2024-05-20 09:30:00 ET = 2024-05-20 13:30:00 UTC (EDT, UTC-4).
_SESSION_OPEN_MS = 1716211800000
_LEAN_IMAGE_DIGEST = "sha256:test-image-digest-1891"
# Phase 0's three metadata files -- needed to scope metadata-row cleanup by
# (lean_image_digest, file_name, mode), since metadata identity has no
# Symbol column and -- since #1866 folded the mode into the metadata DCH
# (metadata_bundle.metadata_data_contract_hash) -- is now mode-scoped too.
_METADATA_FILE_NAMES = ("market-hours-database.json", "symbol-properties-database.csv", "interest-rate.csv")
_MODES: tuple[PriceAdjustmentMode, ...] = ("raw", "polygon_split_adjusted")

# Two genuinely different price bases so the raw and adjusted fetches produce
# non-identical bytes -- a wrong-root fallback (reading the other mode's
# file) or a mode-blind cache hit (reusing the other mode's catalog row)
# would be caught by the cross-mode inequality assertions below, not masked
# by two fixtures that happen to agree.
_RAW_BASE_PRICE = 500.0
_ADJUSTED_BASE_PRICE = 250.0  # simulates a 2:1 split adjustment


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    """Delete only this module's own catalog rows, never a blanket TRUNCATE.

    Scoped to SYMBOL plus this module's own metadata contract hashes for
    *both* modes (see _METADATA_FILE_NAMES above) -- see
    test_ensure_data_all_kinds.py's identical fixture for why a table-wide
    TRUNCATE is unsafe under pytest-xdist concurrency.
    """
    import asyncpg

    metadata_dchs = [_metadata_dch(_LEAN_IMAGE_DIGEST, name, mode) for name in _METADATA_FILE_NAMES for mode in _MODES]

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
def tmp_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point LEAN_DATA_WRITE_ROOT at a scratch tree; see
    test_ensure_data_all_kinds.py's identical fixture for the launcher-side
    artifacts_root rationale."""
    write_root = tmp_path / "writer-root"
    (write_root / "lake").mkdir(parents=True)
    (write_root / "staging").mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "test-polygon-key")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_URL", "http://launcher-mock:8090")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_TOKEN", "test-token")
    # Phase 0 resolves the sent token via read_launcher_token(), which reads
    # os.environ directly rather than settings (see
    # test_gate_chain_convergence.py's identical fixture for the fix this
    # mirrors).
    monkeypatch.setenv("LEAN_LAUNCHER_TOKEN", "test-token")
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
    artifacts_root = tmp_path / "artifacts-root"
    artifacts_root.mkdir(parents=True)
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", artifacts_root)
    return write_root


@pytest.fixture
def artifacts_root(tmp_lake: Path, tmp_path: Path) -> Path:
    return tmp_path / "artifacts-root"


# ---------------------------------------------------------------------------
# Polygon / launcher mocks
# ---------------------------------------------------------------------------


def _polygon_aggs_for(base_price: float, count: int = 390) -> dict:
    return {
        "ticker": SYMBOL,
        "status": "OK",
        "results": [
            {
                "v": 1000 + i,
                "vw": base_price,
                "o": base_price + i * 0.01,
                "c": base_price + 0.05 + i * 0.01,
                "h": base_price + 0.10 + i * 0.01,
                "l": base_price - 0.05 + i * 0.01,
                "t": _SESSION_OPEN_MS + i * 60_000,
                "n": 10,
            }
            for i in range(count)
        ],
    }


def _aggs_side_effect(request: httpx.Request) -> httpx.Response:
    """Return genuinely different bars for adjusted=true vs adjusted=false --
    the same vendor query parameter app.data_lake.polygon_fetcher sends
    (see PolygonFetchError-free path in fetch_aggregate_bars)."""
    adjusted = request.url.params.get("adjusted")
    base_price = _ADJUSTED_BASE_PRICE if adjusted == "true" else _RAW_BASE_PRICE
    return httpx.Response(200, json=_polygon_aggs_for(base_price))


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
    return f"{SYMBOL},equity,usd,1,0\n".encode()


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


def _mock_polygon_and_launcher(artifacts_root: Path) -> None:
    """Register the respx routes both tests in this module need: the
    launcher's metadata extraction, the mode-sensitive aggs mock, and the
    three corp-action endpoints (empty -- SYMBOL is a synthetic ticker with
    no real splits/dividends/events)."""
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(artifacts_root)
    )
    respx.get(url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/{SYMBOL}/range/1/minute/.*").mock(
        side_effect=_aggs_side_effect
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/dividends.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(rf"https://api\.polygon\.io/v3/reference/tickers/{SYMBOL}/events.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )


_REQUEST_IDS: dict[PriceAdjustmentMode, UUID] = {
    "raw": UUID("11111111-1891-1891-1891-111111111891"),
    "polygon_split_adjusted": UUID("22222222-1891-1891-1891-222222222891"),
}


def _spec(price_adjustment_mode: PriceAdjustmentMode) -> DataRunSpec:
    return DataRunSpec(
        request_id=_REQUEST_IDS[price_adjustment_mode],
        run_type="python_lab",
        symbols=[SYMBOL],
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(TRADING_DATE),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(TRADING_DATE),
        data_types=["trade", "quote"],
        price_adjustment_mode=price_adjustment_mode,
        lean_image_digest=_LEAN_IMAGE_DIGEST,
    )


def _lake_relative_trade_path(symbol: str = SYMBOL) -> Path:
    return Path(
        *LeanMinuteBarPath(market="usa", symbol=symbol, trading_date=TRADING_DATE, data_type="trade")
        .relative_path()
        .parts
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# The proof.
# ---------------------------------------------------------------------------


@respx.mock
async def test_both_price_adjustment_modes_catalogue_independently_and_read_back_correctly(
    clean_artifacts, pool, tmp_lake: Path, artifacts_root: Path
) -> None:
    """AC 1: raw and polygon_split_adjusted both catalogue the same
    (market, symbol, trading_date, data_type) without identity collision.
    AC 2: the Python engine and the LEAN sidecar each read back the mode
    they actually requested, byte-identical to what ensure_data wrote for
    that mode -- never the other mode's bytes.
    """
    _mock_polygon_and_launcher(artifacts_root)

    # --- Write: catalogue the SAME symbol/date range in both modes. ---
    raw_result = await ensure_data(_spec("raw"))
    assert raw_result.overall_status == "complete", f"raw run failed: {raw_result.failures}"

    adjusted_result = await ensure_data(_spec("polygon_split_adjusted"))
    assert adjusted_result.overall_status == "complete", f"adjusted run failed: {adjusted_result.failures}"

    # A collision (the adjusted call silently reusing the raw call's minute-
    # trade row) would report this as a cache hit with zero fetches, exactly
    # the failure mode app.data_lake.ensure_data's own comment on this
    # scenario warns about. It must have fetched fresh.
    assert adjusted_result.fetched_artifact_count > 0, (
        "the adjusted-mode run reused artifacts instead of fetching its own -- "
        "the two modes' identities collided"
    )

    # --- AC 1: two distinct catalog rows, not one, for the same identity
    # dimensions modulo mode. ---
    raw_rows = await catalog_client.select_coverage_minute_bars(
        market="usa",
        symbol=SYMBOL,
        data_type="trade",
        start_trading_date=TRADING_DATE,
        end_trading_date=TRADING_DATE,
        price_adjustment_mode="raw",
    )
    adjusted_rows = await catalog_client.select_coverage_minute_bars(
        market="usa",
        symbol=SYMBOL,
        data_type="trade",
        start_trading_date=TRADING_DATE,
        end_trading_date=TRADING_DATE,
        price_adjustment_mode="polygon_split_adjusted",
    )
    assert len(raw_rows) == 1, raw_rows
    assert len(adjusted_rows) == 1, adjusted_rows
    raw_row, adjusted_row = raw_rows[0], adjusted_rows[0]

    # Distinct catalog rows: different primary keys, different content.
    assert raw_row.id != adjusted_row.id
    assert raw_row.file_sha256 != adjusted_row.file_sha256, (
        "raw and adjusted rows recorded the same bytes -- the mocks did not "
        "actually distinguish the two Polygon requests"
    )
    # Not distinguished by physical root identity or file path -- both land
    # under the same DataRootId (one physical root, per PRD #1885: "both
    # roots stay", meaning both mode subtrees, not two DataRootIds) at the
    # same root-relative FilePath, matching the docstring on
    # app.data_lake.path_policy.resolve_lake_root: "their catalog FilePath
    # stays byte-identical, because FilePath is root-relative and carries no
    # root identity of its own." The only column that tells them apart is
    # PriceAdjustmentMode -- proving that column, not FilePath, is what
    # keeps the two modes from colliding.
    assert raw_row.data_root_id == adjusted_row.data_root_id
    assert raw_row.file_path == adjusted_row.file_path
    assert raw_row.price_adjustment_mode == "raw"
    assert adjusted_row.price_adjustment_mode == "polygon_split_adjusted"

    # --- On disk: two separate mode subtrees hold two different byte
    # sequences at the identical root-relative path. ---
    relative = _lake_relative_trade_path()
    raw_file = tmp_lake / lake_subpath("raw") / relative
    adjusted_file = tmp_lake / lake_subpath("polygon_split_adjusted") / relative
    assert raw_file.is_file()
    assert adjusted_file.is_file()
    assert raw_file.read_bytes() != adjusted_file.read_bytes()
    assert _sha256(raw_file) == raw_row.file_sha256
    assert _sha256(adjusted_file) == adjusted_row.file_sha256

    # --- AC 2a: the Python engine's own root resolver reads back the mode
    # it requested, byte-identical to what ensure_data wrote for that mode. ---
    engine_raw_roots = resolve_data_roots(source="polygon", adjusted=False)
    engine_adjusted_roots = resolve_data_roots(source="polygon", adjusted=True)
    assert engine_raw_roots == [resolve_lake_root("raw")]
    assert engine_adjusted_roots == [resolve_lake_root("polygon_split_adjusted")]
    assert engine_raw_roots != engine_adjusted_roots

    engine_raw_bars = LeanMinuteDataReader(engine_raw_roots, session="regular").read_day(SYMBOL, TRADING_DATE)
    engine_adjusted_bars = LeanMinuteDataReader(engine_adjusted_roots, session="regular").read_day(
        SYMBOL, TRADING_DATE
    )
    assert len(engine_raw_bars) == 390
    assert len(engine_adjusted_bars) == 390
    # The requested mode's own bars, not the other mode's -- decoded prices
    # cluster around each mode's distinct base price.
    assert abs(float(engine_raw_bars[0].open) - _RAW_BASE_PRICE) < 1.0
    assert abs(float(engine_adjusted_bars[0].open) - _ADJUSTED_BASE_PRICE) < 1.0
    assert engine_raw_bars != engine_adjusted_bars
    # Byte-identical to what the catalog recorded for that mode's row.
    assert _sha256(engine_raw_roots[0] / relative) == raw_row.file_sha256
    assert _sha256(engine_adjusted_roots[0] / relative) == adjusted_row.file_sha256

    # --- AC 2b: the LEAN sidecar's own artifact resolver reads back the
    # mode it requested, byte-identical to what ensure_data wrote. ---
    sidecar_raw_root = data_plane_lake_root("raw")
    sidecar_adjusted_root = data_plane_lake_root("polygon_split_adjusted")
    assert sidecar_raw_root == tmp_lake / lake_subpath("raw")
    assert sidecar_adjusted_root == tmp_lake / lake_subpath("polygon_split_adjusted")
    assert sidecar_raw_root != sidecar_adjusted_root

    sidecar_raw_artifacts = resolve_lake_artifacts(
        lake_root=sidecar_raw_root, symbol=SYMBOL, start=TRADING_DATE, end=TRADING_DATE
    )
    sidecar_adjusted_artifacts = resolve_lake_artifacts(
        lake_root=sidecar_adjusted_root, symbol=SYMBOL, start=TRADING_DATE, end=TRADING_DATE
    )
    assert len(sidecar_raw_artifacts.trade_zip_paths) == 1
    assert len(sidecar_adjusted_artifacts.trade_zip_paths) == 1

    sidecar_raw_sha = _sha256(sidecar_raw_artifacts.trade_zip_paths[0])
    sidecar_adjusted_sha = _sha256(sidecar_adjusted_artifacts.trade_zip_paths[0])
    assert sidecar_raw_sha == raw_row.file_sha256
    assert sidecar_adjusted_sha == adjusted_row.file_sha256
    # The negative control: the sidecar's raw-mode read is not, even
    # incidentally, the adjusted row's bytes (and vice versa) -- ruling out
    # a wrong-root fallback that happens to still hash-match something.
    assert sidecar_raw_sha != adjusted_row.file_sha256
    assert sidecar_adjusted_sha != raw_row.file_sha256


@respx.mock
async def test_sidecar_refuses_a_mode_with_no_coverage_rather_than_falling_back(
    clean_artifacts, pool, tmp_lake: Path, artifacts_root: Path
) -> None:
    """The other half of "you get the mode you asked for": a mode this test
    never catalogues must refuse rather than silently serve the other mode's
    (fully covered) bytes. Only the raw mode is written here.
    """
    _mock_polygon_and_launcher(artifacts_root)

    raw_result = await ensure_data(_spec("raw"))
    assert raw_result.overall_status == "complete", f"raw run failed: {raw_result.failures}"

    with pytest.raises(LakeMountError):
        resolve_lake_artifacts(
            lake_root=data_plane_lake_root("polygon_split_adjusted"),
            symbol=SYMBOL,
            start=TRADING_DATE,
            end=TRADING_DATE,
        )
