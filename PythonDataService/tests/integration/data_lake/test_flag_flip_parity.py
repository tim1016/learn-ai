"""A window the lake imported costs nothing at the provider (#1839).

The data lake replaced the policy-keyed cache as the market-data authority.
These two tests hold the part of that claim no native-lake test repeats: a
window imported from the old cache is served by the chart split-read and by an
engine backtest without a single provider call.

**Daily infrastructure.** The two-minute pull-request job deliberately has no
Postgres and defers this directory. The chart test reads the imported bytes
without consulting the catalog, so it runs on a plain developer checkout; the
engine test goes through ``ensure_data`` and therefore needs the disposable,
migrated catalog the daily suite supplies.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import settings
from app.data_lake.atomic import atomic_write_and_promote
from app.data_lake.cache_import import verify_and_read_zip
from app.data_lake.path_policy import LeanMinuteBarPath, resolve_lake_root, resolve_staging_root
from app.data_lake.types import PriceAdjustmentMode
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from tests._helpers.lean_store import seed_store_day

SYMBOL = "SPY"
DAY_ONE = date(2026, 1, 5)  # Monday
DAY_TWO = date(2026, 1, 6)
DAY_THREE = date(2026, 1, 7)
WINDOW = [DAY_ONE, DAY_TWO, DAY_THREE]


# ---------------------------------------------------------------------------
# The fixture pair: a pre-import policy cache, and the lake imported from it.
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    """The pre-import state: a policy-keyed cache written by the policy writer."""
    return _seed_cache(tmp_path / "lean-cache" / "polygon-raw", SYMBOL)


def _seed_cache(policy_root: Path, symbol: str) -> Path:
    for trading_date in WINDOW:
        seed_store_day(policy_root, symbol, trading_date)
    return policy_root


@pytest.fixture
def imported_lake(tmp_path: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The post-import state, produced the way ``cache_import`` produces it.

    Byte-for-byte the importer's own path: ``verify_and_read_zip`` to reject
    anything malformed, then ``atomic_write_and_promote`` of ``raw_bytes``.
    What is deliberately absent is the catalog -- the claim/lease bookkeeping,
    which decides *whether* to write and records *that* it was written, and
    which the chart's catalog-free read never consults (see the module
    docstring). Nothing it does changes a byte.
    """
    # Same location the autouse ``_isolate_data_lake_write_root`` guard in
    # tests/conftest.py already pinned; re-stated (and re-pinned) here so the
    # fixture reads on its own terms rather than depending on a default two
    # files away.
    write_root = tmp_path / "lean-data-writer"
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    return _import_cache_bytes(cache_root, "raw")


def _import_cache_bytes(cache_root: Path, mode: PriceAdjustmentMode) -> Path:
    lake_dir = resolve_lake_root(mode)
    staging_dir = resolve_staging_root(mode)
    lake_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    run_id = uuid4()
    for trading_date in WINDOW:
        source = _cache_zip_path(cache_root, trading_date)
        verified = verify_and_read_zip(source, SYMBOL, trading_date)
        atomic_write_and_promote(
            content=verified.raw_bytes,
            lake_root=lake_dir,
            staging_root=staging_dir,
            rel_lake_path=_lake_relative_path(trading_date),
            request_id=run_id,
            worker_id="parity-test",
            attempt=1,
        )
    return lake_dir


def _cache_zip_path(cache_root: Path, trading_date: date) -> Path:
    return cache_root / "equity" / "usa" / "minute" / SYMBOL.lower() / f"{trading_date:%Y%m%d}_trade.zip"


def _lake_relative_path(trading_date: date):
    return LeanMinuteBarPath(
        market="usa", symbol=SYMBOL, trading_date=trading_date, data_type="trade"
    ).relative_path()


# ---------------------------------------------------------------------------
# Zero provider calls over a window the lake already covers.
# ---------------------------------------------------------------------------


def test_chart_serves_a_covered_completed_window_with_zero_provider_calls(
    imported_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: a previously-fetched window costs nothing at the provider.

    The chart split-read over imported coverage, flag on, with every session
    in the window long closed. The provider callable is not mocked to return
    something cheap -- it raises. A composition that reached for it at all
    would fail loudly rather than pass with a suspiciously small call count.

    This also covers the "chart split-read verified against real (imported)
    coverage" criterion: the bars below come out of the artifacts the import
    produced, not out of a lake seeded by the chart tests' own writer.
    """
    from app.services.chart_bar_source import compose_chart_bars

    def _provider_must_not_be_called(from_date: str, to_date: str):
        raise AssertionError(f"the provider was called for {from_date}..{to_date} over a fully-covered window")

    composed = compose_chart_bars(
        ticker=SYMBOL,
        from_date=DAY_ONE.isoformat(),
        to_date=DAY_THREE.isoformat(),
        adjusted=False,
        fetch_provider=_provider_must_not_be_called,
        session="rth",
        # Well past the window's last close, so no session is still forming
        # and there is no live tail to fetch. Pinned rather than "now" so the
        # test does not change meaning as the calendar moves.
        now_ms=_ms_after_the_window(),
        lake_root=imported_lake,
    )

    assert len(composed.bars) == 3 * 390
    assert [span.source for span in composed.spans] == ["lake"]
    assert composed.notice_code is None


def _ms_after_the_window() -> int:
    """An instant safely after the window's last scheduled close.

    Derived from the canonical calendar rather than a literal, so a half-day
    in the window would move it too.
    """
    from app.lean_sidecar.trading_calendar import session_close_ms_utc

    return session_close_ms_utc(DAY_THREE) + 60 * 60 * 1000


# ---------------------------------------------------------------------------
# End-to-end, through the catalog. Gated: needs a live Postgres.
# ---------------------------------------------------------------------------


def _requires_postgres() -> None:
    import os

    if not (settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")):
        pytest.skip("POSTGRES_URL not configured — the catalog-backed half of the parity proof")


def test_engine_backtest_over_an_imported_window_makes_zero_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: end to end, through the catalog, with no Polygon call at all.

    The catalog-backed companion to the chart test above, and the
    production sequence in miniature: write a policy cache, import it, then
    back-test the window it covers. This is the only assertion in this file
    that needs Postgres, and the one that cannot be made without it -- "zero
    provider calls" is a statement about what ``ensure_data`` decides after
    consulting the catalog, and a fake catalog would be making that decision
    for us: the exact shape of test double that hides the bug it was written
    to catch.

    The import is real (``import_cache_root``), not simulated. That matters
    for a reason the chart fixture above deliberately does not cover:
    artifacts on disk with no catalog rows are **invisible** to the lake.
    ``ensure_data`` asks the catalog what exists, so a lake populated by
    copying files -- which is what the fixture above does, correctly, for the
    chart's catalog-free read -- would send this run straight to Polygon for every
    day. Only the import makes the bytes findable.

    ``respx`` asserts at the transport layer rather than by mocking the
    Polygon client, so a call through any code path -- the fetcher, the
    corp-action endpoints, a future one nobody has written yet -- fails this.

    The launcher is mocked rather than counted. Phase 0 bootstraps the two
    LEAN metadata databases through it, which is a call to *our own* host
    process, not to a market-data vendor: it costs no quota and fetches no
    bars. "Zero provider calls" is a claim about Polygon, and conflating the
    two would fail this test for a reason that has nothing to do with bars.
    """
    _requires_postgres()

    import base64

    import httpx
    import respx

    from app.data_lake import catalog_client, root_identity
    from app.data_lake.cache_import import import_cache_root
    from app.data_lake.run_materialization import materialize_engine_run

    # A symbol of this run's own. The scratch Postgres outlives tmp_path, so
    # a fixed ticker would inherit catalog rows from the previous run of this
    # test -- including a half-written one from a run that failed -- and the
    # import would report ``in_flight_or_incomplete`` for a day whose bytes
    # are right there. Uniqueness makes the test independent of the database's
    # history instead of requiring it to be clean.
    symbol = f"T{uuid4().hex[:10].upper()}"
    cache_root = _seed_cache(tmp_path / "lean-cache" / "polygon-raw", symbol)
    write_root = tmp_path / "lean-data-writer"
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    # import_cache_root requires any --lake-root to carry a valid marker
    # (issue #1878, PR B of #1861) -- stamped with the service's own default
    # active root so materialize_engine_run's later catalog reads (which
    # default to that same root) actually find what this import writes.
    root_identity.init_empty_root(write_root, root_identity.active_root_id())
    _write_cache_provenance(cache_root, symbol)

    try:
        report = asyncio.run(import_cache_root(cache_root, write_root))
        assert len(report.imported) == len(WINDOW), report

        with respx.mock(assert_all_called=False) as router:
            polygon = router.route(host="api.polygon.io")
            router.post(path="/extract-metadata").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "market_hours_database_b64": base64.b64encode(b'{"entries": {}}\n').decode(),
                        "symbol_properties_database_b64": base64.b64encode(
                            b"usa,spy,equity,SPY,USD,1,0.01,1\n"
                        ).decode(),
                    },
                )
            )
            materialization = materialize_engine_run(
                symbol=symbol,
                start=DAY_ONE,
                end=DAY_THREE,
                resolution="minute",
                requester="flag-flip-parity",
            )

        assert polygon.call_count == 0, "a window the import already covered reached the provider"
        # Every minute-trade artifact the run reads was already catalogued.
        # Not asserted as zero fetches overall: Phase 0's metadata artifacts
        # and the derived quote/daily artifacts are the lake's own
        # bookkeeping, produced without touching a provider, and counting
        # them would make the assertion about the wrong thing.
        assert materialization.reused_artifact_count >= len(WINDOW)
        assert materialization.availability_hash
    finally:
        _close_materialization_pool(catalog_client)


def _write_cache_provenance(cache_root: Path, symbol: str) -> None:
    """Give the cache the provenance document the importer requires.

    This used to call ``policy_store.record_fetch`` so the document would be
    the shape a real cache carries rather than one invented for a test. #1893
    deleted that writer with the rest of the store's write path, so the
    document is written literally here.

    The shape is not invented even so: it is what ``cache_import`` validates
    on the way in (``_read_provenance``: schema_version, a ``symbol`` matching
    the directory, ``policy.source == "polygon"``, a boolean
    ``policy.adjusted``, and a ``fetches`` list). The importer is the only
    reader that still exists, so its contract is the definition now -- and if
    that contract changes, this test fails on the import rather than drifting
    quietly against a writer nothing runs.
    """
    document = {
        "schema_version": 1,
        "symbol": symbol,
        "policy": {"source": "polygon", "adjusted": False},
        "fetches": [
            {
                "resolution": "minute",
                "from_date": DAY_ONE.isoformat(),
                "to_date": DAY_THREE.isoformat(),
                "fetched_at_ms": session_open_ms_utc(DAY_ONE),
            }
        ],
    }
    prov_path = cache_root / "provenance" / f"{symbol.lower()}.json"
    prov_path.parent.mkdir(parents=True, exist_ok=True)
    prov_path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def _close_materialization_pool(catalog_client) -> None:
    """Close the pool ``materialize_engine_run`` opened on its own loop.

    It runs on the process-wide background loop (``app.utils.background_loop``),
    so the pool it created belongs to that loop and cannot be closed from here
    by awaiting; the close has to be submitted back onto the same loop.
    """
    import asyncio
    import contextlib

    from app.utils import background_loop as shared_loop

    loop = shared_loop._loop
    if loop is None or loop.is_closed():
        return
    with contextlib.suppress(Exception):
        asyncio.run_coroutine_threadsafe(catalog_client.close_pool(), loop).result(timeout=10)
