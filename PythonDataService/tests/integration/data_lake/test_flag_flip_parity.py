"""Flagship parity: the lake serves the same run the policy cache did (#1839).

This is the slice's central claim, made executable. The data lake replaces the
policy-keyed cache as the market-data authority, and everything downstream --
two backtest engines, a chart, a manifest fingerprint on a run receipt --
depends on the replacement being an identity rather than an approximation.

Equivalence levels, per ``.claude/rules/numerical-rigor.md``, and they are not
all the same level. Stating which claim is which is the point:

* **Bit-exact, across the import.** ``cache_import`` promotes the cache zip's
  bytes verbatim (``atomic_write_and_promote(content=verified.raw_bytes)``), so
  an imported lake artifact is byte-identical to the cache zip it came from --
  same SHA-256, therefore same ``data_availability_hash``. This is the claim
  the acceptance criterion ("identical manifest fingerprint for lake-read vs
  cache-read of the same run spec") actually rests on, and it is provable
  rather than approximate.
* **Bit-exact, for the bars.** Both readers decode the same integers off the
  same deci-cent grid into ``Decimal``, so the bar streams are compared with
  exact equality. There is no ``atol`` here and there should not be: a
  tolerance would be admitting a difference that cannot arise, and would hide
  one that could.
* **Row-exact, NOT byte-exact, writer-to-writer.** For a day each path fetches
  *fresh*, the two writers produce identical rows in non-identical archives:
  the lake writer terminates its CSV with a newline and the policy writer does
  not. Byte-equality between the two writers is claimed nowhere in this repo.
  ``tests/unit/data_lake/test_deci_cent_canonical.py`` owns that weaker claim
  in full -- both halves of it, since only that file builds the two zips from
  the same bars and can therefore assert the inequality for its real reason.
  This file owns the strong claim, and shows the row-level agreement on the
  imported day for continuity.

**No Postgres.** The CI "Python Tests" job sets no ``POSTGRES_URL``, so every
live-catalog test skips there (carry-forward A8). A parity proof that only ran
on a developer's scratch database would be a parity proof that never ran. So
this file exercises the byte path -- the importer's own verification and
promote primitives, the readers, the fingerprint function -- and leaves the
catalog bookkeeping to the gated import tests. What the catalog contributes to
the fingerprint is the artifact's identity and its ``file_sha256``; both are
reconstructed here from the bytes actually on disk, which is where the catalog
gets them from too.
"""

from __future__ import annotations

import asyncio
import hashlib
import zipfile
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import settings
from app.data_lake.atomic import atomic_write_and_promote
from app.data_lake.cache_import import verify_and_read_zip
from app.data_lake.ensure_data import _compute_data_availability_hash
from app.data_lake.lean_writer import build_minute_trade_zip_bytes
from app.data_lake.path_policy import LeanMinuteBarPath
from app.data_lake.types import ArtifactRecord
from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.policy_store import resolve_data_roots, snapshot_minute_trade_zips
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from tests._helpers.lean_store import make_minute_bars, seed_store_day

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
    which needs a live Postgres this test must run without (see the module
    docstring). Nothing it does changes a byte.
    """
    # Same location the autouse ``_isolate_data_lake_write_root`` guard in
    # tests/conftest.py already pinned; re-stated (and re-pinned) here so the
    # fixture reads on its own terms rather than depending on a default two
    # files away.
    write_root = tmp_path / "lean-data-writer"
    lake_dir = write_root / "lake"
    staging_dir = write_root / "staging"
    lake_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))

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
            price_adjustment_mode="raw",
        )
    return lake_dir


def _cache_zip_path(cache_root: Path, trading_date: date) -> Path:
    return cache_root / "equity" / "usa" / "minute" / SYMBOL.lower() / f"{trading_date:%Y%m%d}_trade.zip"


def _lake_relative_path(trading_date: date):
    return LeanMinuteBarPath(
        market="usa", symbol=SYMBOL, trading_date=trading_date, data_type="trade"
    ).relative_path()


def _artifact_records(root: Path) -> list[ArtifactRecord]:
    """Reconstruct the catalog rows a run's fingerprint is computed over.

    Every field the fingerprint consumes comes off the bytes on disk, which is
    where ``complete_artifact`` gets them from as well -- so a record built
    here and a record the catalog wrote for the same file agree by
    construction, not by coincidence.
    """
    records: list[ArtifactRecord] = []
    for artifact_id, trading_date in enumerate(WINDOW, start=1):
        relative = _lake_relative_path(trading_date)
        payload = (root / Path(*relative.parts)).read_bytes()
        bars = list(LeanMinuteDataReader([root], session="extended").read_day(SYMBOL, trading_date))
        records.append(
            ArtifactRecord(
                id=artifact_id,
                artifact_kind="time_series_bars",
                market="usa",
                symbol=SYMBOL,
                trading_date=trading_date,
                resolution="minute",
                data_type="trade",
                provider="polygon",
                price_adjustment_mode="raw",
                data_contract_hash="c" * 64,
                file_path=str(relative),
                file_sha256=hashlib.sha256(payload).hexdigest(),
                row_count=len(bars),
                first_bar_start_ms=bars[0].start_ms,
                last_bar_start_ms=bars[-1].start_ms,
            )
        )
    return records


# ---------------------------------------------------------------------------
# The claim.
# ---------------------------------------------------------------------------


def test_imported_lake_artifacts_are_byte_identical_to_their_cache_zips(
    cache_root: Path, imported_lake: Path
) -> None:
    """The strong claim, and the one everything below rests on.

    The importer promotes ``verified.raw_bytes`` -- it does not re-encode --
    so the lake artifact and the cache zip are the same file under two names.
    Everything downstream (the SHA the catalog records, the fingerprint the
    run receipt carries, the bytes LEAN reads off the mount) inherits that.
    """
    for trading_date in WINDOW:
        cache_bytes = _cache_zip_path(cache_root, trading_date).read_bytes()
        lake_bytes = (imported_lake / Path(*_lake_relative_path(trading_date).parts)).read_bytes()

        assert lake_bytes == cache_bytes, trading_date
        assert hashlib.sha256(lake_bytes).hexdigest() == hashlib.sha256(cache_bytes).hexdigest()


def test_lake_read_and_cache_read_produce_the_same_manifest_fingerprint(
    cache_root: Path, imported_lake: Path
) -> None:
    """The acceptance criterion, stated as the equality it actually is.

    ``data_availability_hash`` is a SHA-256 over each artifact's identity and
    its ``file_sha256``. The identity is the same on both sides because the
    lake's path policy and the policy store's layout agree on
    ``equity/usa/minute/<symbol>/<date>_trade.zip``; the hash is the same
    because the import copied bytes. So the fingerprints match, and a run that
    switched from one root to the other would record the same receipt.
    """
    lake_hash = _compute_data_availability_hash(_artifact_records(imported_lake))
    cache_hash = _compute_data_availability_hash(_artifact_records(cache_root))

    assert lake_hash == cache_hash
    # Not a degenerate equality: the hash is over real content, so mutating
    # one byte of one day must break it. Without this the test would pass on
    # two empty record lists.
    tampered = _artifact_records(imported_lake)
    tampered[0] = tampered[0].model_copy(update={"file_sha256": "0" * 64})
    assert _compute_data_availability_hash(tampered) != cache_hash


def test_lake_read_and_cache_read_produce_identical_bar_streams(
    cache_root: Path, imported_lake: Path
) -> None:
    """Exact equality, no tolerance -- and the tolerance choice is the claim.

    Both sides decode the same integers off LEAN's deci-cent grid into
    ``Decimal``, so no floating-point step exists between the bytes and these
    values. An ``atol`` here would admit a difference that cannot arise and
    conceal one that could.
    """
    lake_bars = list(LeanMinuteDataReader([imported_lake], session="regular").iter_bars(SYMBOL, DAY_ONE, DAY_THREE))
    cache_bars = list(LeanMinuteDataReader([cache_root], session="regular").iter_bars(SYMBOL, DAY_ONE, DAY_THREE))

    assert len(lake_bars) == 3 * 390
    assert lake_bars == cache_bars


def test_both_engines_resolve_the_same_artifact_hashes_for_one_run(
    imported_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: the Python engine and the LEAN sidecar consume identical bytes.

    Asserted at the config/manifest seam, the way #1834's tests do -- no
    container is launched. The Python engine resolves its roots through
    ``policy_store.resolve_data_roots`` and hashes what it will read
    (``snapshot_minute_trade_zips``); the sidecar resolves the same window
    through the lake mount. Both must name the same files with the same
    digests, because "both engines read the same bytes" is the property the
    whole two-engine parity programme is built on.
    """
    from app.lean_sidecar.lake_mount import resolve_lake_artifacts

    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
    _seed_lake_run_prerequisites(imported_lake)

    engine_roots = resolve_data_roots(source="polygon", adjusted=False)
    assert engine_roots == [imported_lake]
    engine_receipt = snapshot_minute_trade_zips(
        engine_roots,
        symbol=SYMBOL,
        start=DAY_ONE,
        end=DAY_THREE,
        adjusted=False,
        session="regular",
    )

    sidecar_artifacts = resolve_lake_artifacts(
        lake_root=imported_lake,
        symbol=SYMBOL,
        start=DAY_ONE,
        end=DAY_THREE,
    )

    engine_digests = {entry["path"]: entry["sha256"] for entry in engine_receipt["files"]}
    sidecar_digests = {
        str(path.relative_to(imported_lake)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sidecar_artifacts.trade_zip_paths
    }

    assert engine_digests == sidecar_digests
    assert len(engine_digests) == len(WINDOW)


def test_the_two_writers_agree_on_rows_for_the_imported_day(imported_lake: Path) -> None:
    """The weaker claim: same rows out of two different writers.

    A day the *lake* fetches fresh goes through ``lean_writer``; the same day
    fetched through the pre-lake path goes through ``lean_format``. Since
    #1839 both encode prices with the one canonical deci-cent rule, so the
    decoded rows are identical even though the archives are not.

    **The byte-INEQUALITY is guarded elsewhere, deliberately.** It cannot be
    asserted here: this compares a five-bar lake zip against the imported
    390-bar day, so the bytes differ because the row counts differ, and the
    assertion would still pass if the writers became byte-identical -- a
    guard that cannot fail for its stated reason is worse than none. The real
    guard builds both zips from the *same* bars and lives in
    ``tests/unit/data_lake/test_deci_cent_canonical.py``
    (``test_both_writers_encode_identically_on_sub_grid_prices``); that is
    where a future change making the writers byte-identical gets caught.
    """
    bars = make_minute_bars(SYMBOL, DAY_ONE, count=5)
    lake_written = build_minute_trade_zip_bytes(
        SYMBOL,
        DAY_ONE.strftime("%Y%m%d"),
        [_as_lake_bar(bar) for bar in bars],
    )
    store_written = (imported_lake / Path(*_lake_relative_path(DAY_ONE).parts)).read_bytes()

    assert _csv_rows(lake_written)[:5] == _csv_rows(store_written)[:5]


def _as_lake_bar(bar):
    from app.data_lake.lean_writer import MinuteTradeBar
    from app.utils.timestamps import datetime_at_ms
    from tests._helpers.lean_store import EASTERN

    return MinuteTradeBar(
        bar_start_et=datetime_at_ms(bar.start_ms, tz=EASTERN),
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


def _csv_rows(zip_bytes: bytes) -> list[str]:
    import io

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        body = zf.read(zf.namelist()[0]).decode("ascii")
    return [line for line in body.split("\n") if line]


def _seed_lake_run_prerequisites(lake_root: Path) -> None:
    """Everything a lake-mode sidecar run needs *besides* the trade zips.

    Quote artifacts, the daily artifact, and the two metadata databases --
    exactly what ``resolve_lake_artifacts`` demands before it will serve a
    run. The trade zips are deliberately not (re)written here: they are the
    imported bytes under test, and the shared ``seed_lake_minute_day`` helper
    would overwrite them with the lake writer's own encoding, quietly
    destroying the byte-identity the assertions above depend on.
    """
    from app.data_lake.derived_quote import build_minute_quote_zip_bytes
    from tests._helpers.lake_fixture import seed_lake_daily, seed_lake_metadata, to_lake_bars

    for trading_date in WINDOW:
        relative = LeanMinuteBarPath(
            market="usa", symbol=SYMBOL, trading_date=trading_date, data_type="quote"
        ).relative_path()
        destination = lake_root / Path(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            build_minute_quote_zip_bytes(
                SYMBOL,
                trading_date.strftime("%Y%m%d"),
                to_lake_bars(make_minute_bars(SYMBOL, trading_date)),
            )
        )
    seed_lake_daily(lake_root, SYMBOL, WINDOW, count=390)
    seed_lake_metadata(lake_root)


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

    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

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


def test_chart_falls_back_to_the_provider_for_an_adjusted_request(
    imported_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of A2, from the chart's side.

    The same covered window, asked for adjusted: the lake holds raw bytes and
    must not serve them. The provider answers instead and the receipt says
    why, so the operator sees a named reason rather than silently different
    prices.
    """
    from app.services.chart_bar_source import compose_chart_bars

    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
    calls: list[tuple[str, str]] = []

    def _provider(from_date: str, to_date: str):
        calls.append((from_date, to_date))
        return []

    composed = compose_chart_bars(
        ticker=SYMBOL,
        from_date=DAY_ONE.isoformat(),
        to_date=DAY_THREE.isoformat(),
        adjusted=True,
        fetch_provider=_provider,
        session="rth",
        now_ms=_ms_after_the_window(),
        lake_root=imported_lake,
    )

    assert calls == [(DAY_ONE.isoformat(), DAY_THREE.isoformat())]
    assert composed.notice_code == "adjusted_prices_provider_only"


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

    The catalog-backed companion to the byte-level claims above, and the
    production sequence in miniature: write a policy cache, import it, then
    back-test the window it covers. This is the only assertion in this file
    that needs Postgres, and the one that cannot be made without it -- "zero
    provider calls" is a statement about what ``ensure_data`` decides after
    consulting the catalog, and a fake catalog would be making that decision
    for us: the exact shape of test double that hides the bug it was written
    to catch.

    The import is real (``import_cache_root``), not simulated. That matters
    for a reason the byte-level fixture above deliberately does not cover:
    artifacts on disk with no catalog rows are **invisible** to the lake.
    ``ensure_data`` asks the catalog what exists, so a lake populated by
    copying files -- which is what the fixture above does, correctly, for a
    claim about bytes -- would send this run straight to Polygon for every
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

    from app.data_lake import catalog_client
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
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
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

    Written through ``policy_store.record_fetch``, the same function the
    pre-lake fetch path uses, so the document is the shape a real cache
    carries rather than one invented for this test.
    """
    from app.engine.data.policy_store import record_fetch

    record_fetch(
        cache_root,
        symbol,
        source="polygon",
        adjusted=False,
        resolution="minute",
        from_date=DAY_ONE.isoformat(),
        to_date=DAY_THREE.isoformat(),
        fetched_at_ms=session_open_ms_utc(DAY_ONE),
    )


def _close_materialization_pool(catalog_client) -> None:
    """Close the pool ``materialize_engine_run`` opened on its own loop.

    It runs on a process-wide background loop of its own (see
    ``run_materialization._materialization_loop``), so the pool it created
    belongs to that loop and cannot be closed from here by awaiting; the close
    has to be submitted back onto the same loop.
    """
    import asyncio
    import contextlib

    from app.data_lake import run_materialization

    loop = run_materialization._loop
    if loop is None or loop.is_closed():
        return
    with contextlib.suppress(Exception):
        asyncio.run_coroutine_threadsafe(catalog_client.close_pool(), loop).result(timeout=10)
