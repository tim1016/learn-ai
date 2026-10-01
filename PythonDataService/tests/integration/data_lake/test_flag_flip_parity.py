"""A window the lake imported costs nothing at the provider (#1839).

The data lake replaced the policy-keyed cache as the market-data authority.
These two tests hold the part of that claim no native-lake test repeats: a
window imported from the old cache is served by the chart split-read and by an
engine backtest without a single provider call.

**Daily infrastructure.** The pull-request shards have no Postgres, so both
tests skip there and run daily against the disposable, migrated catalog the
daily suite supplies. Both run the real import: since #2456 a reader admits a
lake file only against a root identity and the committed catalog receipt the
import writes, and ``ensure_data`` finds imported days only through their
catalog rows.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import settings
from app.data_lake.path_policy import resolve_lake_root
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from tests._helpers.lean_store import seed_store_day

DAY_ONE = date(2026, 1, 5)  # Monday
DAY_TWO = date(2026, 1, 6)
DAY_THREE = date(2026, 1, 7)
WINDOW = [DAY_ONE, DAY_TWO, DAY_THREE]


# ---------------------------------------------------------------------------
# The fixture pair: a pre-import policy cache, and the lake imported from it.
# ---------------------------------------------------------------------------


def _run_symbol() -> str:
    """A symbol of this run's own.

    The scratch Postgres outlives tmp_path, so a fixed ticker would inherit
    catalog rows from the previous run of a test -- including a half-written
    one from a run that failed -- and the import would report
    ``in_flight_or_incomplete`` for a day whose bytes are right there.
    Uniqueness makes the tests independent of the database's history instead
    of requiring it to be clean.
    """
    return f"T{uuid4().hex[:10].upper()}"


def _seed_cache(policy_root: Path, symbol: str) -> Path:
    for trading_date in WINDOW:
        seed_store_day(policy_root, symbol, trading_date)
    return policy_root


def _import_cache_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symbol: str) -> Path:
    """Write a policy cache for ``symbol``, import it for real, return the raw lake root.

    The import is ``import_cache_root`` itself, not a copy of its byte path:
    the catalog rows it commits are both what lake admission verifies a read
    against (#2456) and what makes the days findable to ``ensure_data``.
    """
    from app.data_lake import catalog_client, root_identity
    from app.data_lake.cache_import import import_cache_root

    cache_root = _seed_cache(tmp_path / "lean-cache" / "polygon-raw", symbol)
    write_root = tmp_path / "lean-data-writer"
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    # import_cache_root requires any --lake-root to carry a valid marker
    # (issue #1878, PR B of #1861) -- stamped with the service's own default
    # active root so the readers' later catalog reads (which default to that
    # same root) actually find what this import writes.
    root_identity.init_empty_root(write_root, root_identity.active_root_id())
    _write_cache_provenance(cache_root, symbol)

    async def _import():
        try:
            return await import_cache_root(cache_root, write_root)
        finally:
            await catalog_client.close_pool()

    report = asyncio.run(_import())
    assert len(report.imported) == len(WINDOW), report
    return resolve_lake_root("raw")


# ---------------------------------------------------------------------------
# Zero provider calls over a window the lake already covers.
# ---------------------------------------------------------------------------


def test_chart_serves_a_covered_completed_window_with_zero_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    _requires_postgres()

    from app.data_lake import catalog_client
    from app.services.chart_bar_source import compose_chart_bars

    symbol = _run_symbol()
    imported_lake = _import_cache_window(tmp_path, monkeypatch, symbol)

    def _provider_must_not_be_called(from_date: str, to_date: str):
        raise AssertionError(f"the provider was called for {from_date}..{to_date} over a fully-covered window")

    try:
        composed = compose_chart_bars(
            ticker=symbol,
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
    finally:
        # Admission checks each read's receipt on the shared background loop.
        _close_materialization_pool(catalog_client)

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
        pytest.skip("POSTGRES_URL not configured — the import commits its days to the catalog")


def test_engine_backtest_over_an_imported_window_makes_zero_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: end to end, through the catalog, with no Polygon call at all.

    The engine companion to the chart test above, and the production
    sequence in miniature: write a policy cache, import it, then back-test
    the window it covers. "Zero provider calls" is a statement about what
    ``ensure_data`` decides after consulting the catalog, and a fake catalog
    would be making that decision for us: the exact shape of test double that
    hides the bug it was written to catch.

    The import is real (``import_cache_root``), not simulated: artifacts on
    disk with no catalog rows are **invisible** to the lake. ``ensure_data``
    asks the catalog what exists, so a lake populated by copying files would
    send this run straight to Polygon for every day. Only the import makes the
    bytes findable.

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

    from app.data_lake import catalog_client
    from app.data_lake.run_materialization import materialize_engine_run

    symbol = _run_symbol()
    _import_cache_window(tmp_path, monkeypatch, symbol)

    try:
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
