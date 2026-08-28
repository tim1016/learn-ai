"""End-to-end tests for the Observatory read endpoints (issue #1835).

GET /api/data-lake/coverage, /artifacts/{id}, /storage-summary. Same pattern
as test_ensure_data_route.py: build a minimal FastAPI app that includes the
data_lake router directly (via the shared ``make_data_lake_app`` fixture in
conftest.py), so no app.main reload or settings override is needed to
exercise the flag-on behavior. catalog_client's Postgres-backed functions
are monkeypatched at the module level so these tests run without a live
database — the doubles are pinned to the real functions' keyword signatures
(not ``**kwargs``) so a signature drift breaks the test, not just production.
"""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.catalog_client import ArtifactCoverageRow
from app.data_lake.types import (
    MAX_SYMBOL_LENGTH,
    MAX_TRADING_RANGE_DAYS,
    ArtifactDetail,
    StorageKindTotal,
    SymbolCoverageSpan,
    trading_range_span_days,
)
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc

pytestmark = pytest.mark.asyncio


def _artifact_detail_kwargs(**overrides) -> dict:
    """Base ArtifactDetail field set (a complete, non-failed minute-bar row).

    Every field is required by the model; individual tests override only the
    ones the scenario cares about, so adding a new required field (e.g.
    attempt_count/last_error/error_message for #1845 P2-6) means updating
    this one place, not every construction site.
    """
    base = dict(
        id=101,
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date_ms=session_open_ms_utc(date(2024, 5, 20)),
        resolution="minute",
        data_type="trade",
        provider="polygon",
        provider_params={},
        price_adjustment_mode="raw",
        data_contract_hash="a" * 64,
        content_hash="b" * 64,
        file_path="equity/usa/minute/spy/20240520_trade.zip",
        file_size_bytes=123456,
        status="complete",
        row_count=390,
        first_bar_start_ms=1716196200000,
        last_bar_start_ms=1716219540000,
        fetched_at_ms=1716220000000,
        completed_at_ms=1716220050000,
        attempt_count=1,
        last_error=None,
        error_message=None,
    )
    base.update(overrides)
    return base


async def _get(app: FastAPI, url: str) -> tuple[int, dict]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(url)
    return r.status_code, (r.json() if r.content else {})


class _AlreadyInitializedPool:
    """Sentinel standing in for a real asyncpg pool.

    Satisfies close_pool()'s cleanup path (it awaits ``_pool.close()``)
    without ever touching real I/O.
    """

    async def close(self) -> None:
        return None

    def terminate(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _catalog_pool_already_initialized(monkeypatch: pytest.MonkeyPatch):
    """Every GET route now depends on _ensure_catalog_pool (#1845 P1-1),
    which calls catalog_client.init_pool() — a no-op once ``_pool`` is
    already set, but a RuntimeError (no POSTGRES_URL configured in this
    sandbox) if it's still None. Every test in this module *except* the
    "Pool lifecycle" tests below mocks catalog_client's select_* functions
    directly and never needs a real pool at all, so pre-seed a sentinel here
    to keep init_pool() a no-op for them. The pool-lifecycle tests
    explicitly override this back to None to exercise the real init path.
    """
    monkeypatch.setattr(catalog_client, "_pool", _AlreadyInitializedPool())


# ---------------------------------------------------------------------------
# Flag-off behavior — routes 404 when the router is not registered.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/api/data-lake/coverage?symbol=SPY&start_trading_date=2024-05-20&end_trading_date=2024-05-24",
        "/api/data-lake/artifacts/1",
        "/api/data-lake/storage-summary",
    ],
)
async def test_observatory_routes_404_when_flag_off(url: str, make_data_lake_app):
    flag_off_app = make_data_lake_app(include_data_lake=False)
    status_code, _ = await _get(flag_off_app, url)
    assert status_code == 404


# ---------------------------------------------------------------------------
# Pool lifecycle — a GET must work on a fresh process, no prior POST.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/api/data-lake/coverage?symbol=SPY&start_trading_date=2024-05-20&end_trading_date=2024-05-24",
        "/api/data-lake/artifacts/1",
        "/api/data-lake/storage-summary",
    ],
)
async def test_get_routes_initialize_the_pool_without_a_prior_post(
    url: str, monkeypatch: pytest.MonkeyPatch, make_data_lake_app
):
    """#1845 P1-1: in a fresh process, all three GET routes used to 500.

    ensure_data() calls catalog_client.init_pool() as its own first step, so
    POST /ensure-data never needed help — but nothing else ever called
    init_pool(), so a GET before any POST hit connection()'s "asyncpg pool
    not initialized" RuntimeError as an unhandled 500. Every other test in
    this module mocks catalog_client's select_* functions directly, which
    bypasses connection()/_pool entirely — exactly why this bug went
    unnoticed. This test does NOT mock the selectors: it resets _pool to
    None (simulating a fresh process), fakes only the asyncpg layer
    (create_pool + a connection whose fetch/fetchrow return empty results,
    no real network I/O), and lets the real select_* functions run through
    the real connection(). Before the fix, connection() would raise
    RuntimeError here — this test would have failed (an unhandled exception
    surfacing through httpx.ASGITransport, not a clean response) without
    the pool-init dependency.
    """

    class _FakeConnection:
        async def fetch(self, query: str, *args: object) -> list:
            return []

        async def fetchrow(self, query: str, *args: object) -> None:
            return None

    class _FakeAcquireContext:
        async def __aenter__(self) -> _FakeConnection:
            return _FakeConnection()

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    class _FakePool:
        def acquire(self) -> _FakeAcquireContext:
            return _FakeAcquireContext()

        async def close(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    async def _fake_create_pool(*args, **kwargs) -> _FakePool:
        return _FakePool()

    await catalog_client.close_pool()
    assert catalog_client._pool is None

    monkeypatch.setattr(settings, "POSTGRES_URL", "postgres://fake-host/fake-db")
    monkeypatch.setattr(asyncpg, "create_pool", _fake_create_pool)

    try:
        app = make_data_lake_app(include_data_lake=True)
        status_code, _ = await _get(app, url)
        # 404 for the artifact-detail URL (id=1, no row in the fake
        # connection's empty result set) is the expected "found nothing"
        # outcome, not the pool failure this test guards against — either
        # way, the request must reach the real selector and its real
        # connection() call, not raise before ever getting there.
        assert status_code in (200, 404)
        assert catalog_client._pool is not None
    finally:
        await catalog_client.close_pool()


# ---------------------------------------------------------------------------
# Coverage — calendar-keyed, honest-missing, honest-empty.
# ---------------------------------------------------------------------------


async def test_coverage_keys_days_by_canonical_calendar_sessions_only(monkeypatch: pytest.MonkeyPatch, make_data_lake_app):
    """2024-05-20 (Mon) .. 2024-05-26 (Sun) has 5 NYSE sessions and a weekend.

    An empty catalog must report every session as "missing" (honest, not
    invented) and must never emit an entry for the weekend days.
    """

    async def _empty_coverage(
        market: str,
        symbol: str,
        data_type: str,
        provider: str,
        price_adjustment_mode: str,
        start_trading_date: date,
        end_trading_date: date,
    ) -> list[ArtifactCoverageRow]:
        return []

    monkeypatch.setattr(catalog_client, "select_artifact_coverage", _empty_coverage)

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(
        app,
        "/api/data-lake/coverage?symbol=SPY&start_trading_date=2024-05-20&end_trading_date=2024-05-26",
    )

    assert status_code == 200
    sessions = expected_sessions(date(2024, 5, 20), date(2024, 5, 26))
    assert sessions == [date(2024, 5, 20), date(2024, 5, 21), date(2024, 5, 22), date(2024, 5, 23), date(2024, 5, 24)]
    assert len(body["days"]) == len(sessions)
    for day, session_date in zip(body["days"], sessions, strict=True):
        assert day["status"] == "missing"
        assert day["artifact_id"] is None
        assert isinstance(day["trading_date_ms"], int)
        assert day["trading_date_ms"] == session_open_ms_utc(session_date)


async def test_coverage_reflects_catalog_status_per_day(monkeypatch: pytest.MonkeyPatch, make_data_lake_app):
    """A day with a catalog row reports that row's real status, not "missing"."""

    async def _mixed_coverage(
        market: str,
        symbol: str,
        data_type: str,
        provider: str,
        price_adjustment_mode: str,
        start_trading_date: date,
        end_trading_date: date,
    ) -> list[ArtifactCoverageRow]:
        return [
            ArtifactCoverageRow(trading_date=date(2024, 5, 20), status="complete", artifact_id=101),
            ArtifactCoverageRow(trading_date=date(2024, 5, 22), status="failed", artifact_id=102),
        ]

    monkeypatch.setattr(catalog_client, "select_artifact_coverage", _mixed_coverage)

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(
        app,
        "/api/data-lake/coverage?symbol=SPY&start_trading_date=2024-05-20&end_trading_date=2024-05-24",
    )

    assert status_code == 200
    by_ms = {d["trading_date_ms"]: d for d in body["days"]}
    complete_day = by_ms[session_open_ms_utc(date(2024, 5, 20))]
    failed_day = by_ms[session_open_ms_utc(date(2024, 5, 22))]
    still_missing_day = by_ms[session_open_ms_utc(date(2024, 5, 21))]

    assert complete_day["status"] == "complete"
    assert complete_day["artifact_id"] == 101
    assert failed_day["status"] == "failed"
    assert failed_day["artifact_id"] == 102
    assert still_missing_day["status"] == "missing"
    assert still_missing_day["artifact_id"] is None


@pytest.mark.parametrize(
    ("data_type", "expected_provider"),
    [
        ("trade", "polygon"),
        ("quote", "learn_ai_derived"),
    ],
)
async def test_coverage_derives_provider_from_data_type(
    data_type: str, expected_provider: str, monkeypatch: pytest.MonkeyPatch, make_data_lake_app
):
    """#1845 P1-2: quote coverage was unfindable.

    expand_required_artifacts catalogs quote minute-bars under
    Provider='learn_ai_derived' (they're synthesized from same-day trade
    bytes, not fetched from Polygon directly) — but the coverage endpoint
    used to accept only provider="polygon" as a query param and feed it
    straight into the filter, so a quote artifact could never match. The
    endpoint no longer takes a provider parameter at all: it derives one
    from data_type via provider_for_data_type, the same function
    expand_required_artifacts calls.
    """
    captured: dict[str, object] = {}

    async def _capturing_coverage(**kwargs) -> list[ArtifactCoverageRow]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(catalog_client, "select_artifact_coverage", _capturing_coverage)

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(
        app,
        f"/api/data-lake/coverage?symbol=SPY&data_type={data_type}"
        "&start_trading_date=2024-05-20&end_trading_date=2024-05-24",
    )

    assert status_code == 200
    assert captured["provider"] == expected_provider
    assert body["provider"] == expected_provider


async def test_coverage_finds_a_seeded_quote_artifact_as_complete(monkeypatch: pytest.MonkeyPatch, make_data_lake_app):
    """A quote artifact recorded under Provider='learn_ai_derived' must report "complete".

    Seeds select_artifact_coverage's return value as if a real
    'learn_ai_derived' quote row exists for 2024-05-21 — this is what the
    live-Postgres equivalent (test_catalog_observatory_reads.py) confirms
    against a real schema; this mocked version pins the router's own
    provider-derivation wiring in isolation.
    """

    async def _quote_coverage(**kwargs) -> list[ArtifactCoverageRow]:
        assert kwargs["provider"] == "learn_ai_derived"
        return [ArtifactCoverageRow(trading_date=date(2024, 5, 21), status="complete", artifact_id=55)]

    monkeypatch.setattr(catalog_client, "select_artifact_coverage", _quote_coverage)

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(
        app,
        "/api/data-lake/coverage?symbol=SPY&data_type=quote&start_trading_date=2024-05-20&end_trading_date=2024-05-24",
    )

    assert status_code == 200
    by_ms = {d["trading_date_ms"]: d for d in body["days"]}
    quote_day = by_ms[session_open_ms_utc(date(2024, 5, 21))]
    assert quote_day["status"] == "complete"
    assert quote_day["artifact_id"] == 55


async def test_coverage_422_when_range_inverted(make_data_lake_app):
    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(
        app,
        "/api/data-lake/coverage?symbol=SPY&start_trading_date=2024-05-24&end_trading_date=2024-05-20",
    )
    assert status_code == 422
    assert body["detail"]["reason"] == "invalid_range"


@pytest.mark.parametrize(
    "symbol",
    [
        "spy",  # lowercase — SYMBOL_RE requires uppercase
        "SP Y",  # whitespace
        "S" * (MAX_SYMBOL_LENGTH + 1),  # exceeds the catalog's storable length
        "1SPY",  # must start with a letter
    ],
)
async def test_coverage_422_when_symbol_invalid(symbol: str, make_data_lake_app):
    """#1845 P2-4: validate at the boundary with DataRunSpec's own pattern + length limit.

    An invalid symbol used to sail straight through to the catalog query
    (which would just find nothing and report every session "missing" —
    honest-empty, but for the wrong reason: not because the query was
    validated, because it was never checked at all).
    """
    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(
        app,
        f"/api/data-lake/coverage?symbol={quote(symbol)}&start_trading_date=2024-05-20&end_trading_date=2024-05-24",
    )
    assert status_code == 422
    assert body["detail"]["reason"] == "invalid_symbol"


async def test_coverage_200_when_symbol_valid(monkeypatch: pytest.MonkeyPatch, make_data_lake_app):
    """The canonical uppercase-letters-digits-dot pattern must still be accepted."""

    async def _empty_coverage(**kwargs) -> list[ArtifactCoverageRow]:
        return []

    monkeypatch.setattr(catalog_client, "select_artifact_coverage", _empty_coverage)

    app = make_data_lake_app(include_data_lake=True)
    status_code, _ = await _get(
        app,
        "/api/data-lake/coverage?symbol=BRK.B&start_trading_date=2024-05-20&end_trading_date=2024-05-24",
    )
    assert status_code == 200


async def test_coverage_422_when_range_exceeds_max_days(monkeypatch: pytest.MonkeyPatch, make_data_lake_app):
    """One day past the shared cap must 422.

    Imports the same MAX_TRADING_RANGE_DAYS + trading_range_span_days that
    both the router and DataRunSpec's validator use, so this boundary can't
    silently drift from the real cap the way the two used to (#1835 review
    round 2, MAJOR 3).
    """

    async def _empty_coverage(**kwargs) -> list[ArtifactCoverageRow]:
        return []

    monkeypatch.setattr(catalog_client, "select_artifact_coverage", _empty_coverage)

    start = date(2020, 1, 1)
    # span_days = trading_range_span_days(start, end) = MAX_TRADING_RANGE_DAYS + 1
    end = start + timedelta(days=MAX_TRADING_RANGE_DAYS)
    assert trading_range_span_days(start, end) == MAX_TRADING_RANGE_DAYS + 1

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(
        app,
        f"/api/data-lake/coverage?symbol=SPY&start_trading_date={start.isoformat()}&end_trading_date={end.isoformat()}",
    )
    assert status_code == 422
    assert body["detail"]["reason"] == "range_too_large"


async def test_coverage_200_at_exactly_max_range_days(monkeypatch: pytest.MonkeyPatch, make_data_lake_app):
    """The cap boundary itself (span_days == MAX_TRADING_RANGE_DAYS) must be accepted.

    Pins the off-by-one boundary from the other side: DataRunSpec's
    validator and this endpoint now share one formula (trading_range_span_days)
    and one constant, so a window right at the cap is treated identically by
    both — this is the case the pre-fix drift got wrong.

    No longer stubs the calendar walk (round-2 review flagged this as a
    concern, #1845 P2-5 made it a real fix): the endpoint now builds the
    whole range's NYSE schedule in one session_windows_ms_utc() call instead
    of one session_open_ms_utc() call per returned day, so even this
    5-year-wide request resolves in well under a second — see
    test_coverage_builds_one_schedule_for_the_whole_range below, which pins
    that call count directly.
    """

    async def _empty_coverage(**kwargs) -> list[ArtifactCoverageRow]:
        return []

    monkeypatch.setattr(catalog_client, "select_artifact_coverage", _empty_coverage)

    start = date(2020, 1, 1)
    # span_days = trading_range_span_days(start, end) == MAX_TRADING_RANGE_DAYS exactly.
    end = start + timedelta(days=MAX_TRADING_RANGE_DAYS - 1)
    assert trading_range_span_days(start, end) == MAX_TRADING_RANGE_DAYS

    app = make_data_lake_app(include_data_lake=True)
    status_code, _ = await _get(
        app,
        f"/api/data-lake/coverage?symbol=SPY&start_trading_date={start.isoformat()}&end_trading_date={end.isoformat()}",
    )
    assert status_code == 200


async def test_coverage_builds_one_schedule_for_the_whole_range(monkeypatch: pytest.MonkeyPatch, make_data_lake_app):
    """#1845 P2-5: coverage must call the calendar's range accessor ONCE,

    not once per returned day. Spies on session_windows_ms_utc (the one
    range-wide accessor) and asserts it's called exactly once per request,
    regardless of how many sessions the range contains — the bug was a
    per-day session_open_ms_utc() call inside the router's day loop, which
    measured ~10s at the 5-year cap before this fix.
    """
    import app.routers.data_lake as data_lake_router_module

    async def _empty_coverage(**kwargs) -> list[ArtifactCoverageRow]:
        return []

    monkeypatch.setattr(catalog_client, "select_artifact_coverage", _empty_coverage)

    call_count = 0
    real_session_windows_ms_utc = data_lake_router_module.session_windows_ms_utc

    def _counting_session_windows_ms_utc(start: date, end: date):
        nonlocal call_count
        call_count += 1
        return real_session_windows_ms_utc(start, end)

    monkeypatch.setattr(data_lake_router_module, "session_windows_ms_utc", _counting_session_windows_ms_utc)

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(
        app,
        "/api/data-lake/coverage?symbol=SPY&start_trading_date=2024-05-20&end_trading_date=2024-05-24",
    )

    assert status_code == 200
    assert len(body["days"]) == 5
    assert call_count == 1


# ---------------------------------------------------------------------------
# Artifact detail — full receipt, int64 ms UTC timestamps, honest 404.
# ---------------------------------------------------------------------------


async def test_artifact_detail_returns_full_receipt_with_int_ms_timestamps(
    monkeypatch: pytest.MonkeyPatch, make_data_lake_app
):
    async def _detail(artifact_id: int) -> ArtifactDetail:
        assert artifact_id == 101
        return ArtifactDetail(**_artifact_detail_kwargs(provider_params={"adjusted": "true"}))

    monkeypatch.setattr(catalog_client, "select_artifact_by_id", _detail)

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(app, "/api/data-lake/artifacts/101")

    assert status_code == 200
    # Receipt completeness: content hash, data-contract hash, size, fetch
    # timestamp, and provider params are all present.
    assert body["content_hash"] == "b" * 64
    assert body["data_contract_hash"] == "a" * 64
    assert body["file_size_bytes"] == 123456
    assert body["provider_params"] == {"adjusted": "true"}
    assert body["status"] == "complete"
    # Every temporal field on the wire is int64 ms UTC — never an ISO string.
    for field in ("trading_date_ms", "fetched_at_ms", "completed_at_ms", "first_bar_start_ms", "last_bar_start_ms"):
        assert isinstance(body[field], int), f"{field} must be int64 ms UTC, got {type(body[field])}"
    assert body["trading_date_ms"] == session_open_ms_utc(date(2024, 5, 20))


async def test_artifact_detail_trading_date_ms_is_none_for_non_day_keyed_artifacts(
    monkeypatch: pytest.MonkeyPatch, make_data_lake_app
):
    """factor_file/map_file/metadata rows carry no TradingDate — must stay None, not fabricated.

    content_hash is also None here, not "" — an empty string would read as
    a real (if empty) hash on a receipt surface (#1835 review round 2,
    MAJOR 4; see test_artifact_detail_content_hash_is_none_until_complete
    below for the status-gated scenario the finding was actually about).
    """

    async def _detail(artifact_id: int) -> ArtifactDetail:
        return ArtifactDetail(
            **_artifact_detail_kwargs(
                id=5,
                artifact_kind="metadata",
                trading_date_ms=None,
                resolution=None,
                data_type=None,
                price_adjustment_mode=None,
                data_contract_hash="c" * 64,
                content_hash=None,
                file_path="equity/usa/metadata/spy.json",
                file_size_bytes=512,
                row_count=None,
                first_bar_start_ms=None,
                last_bar_start_ms=None,
            )
        )

    monkeypatch.setattr(catalog_client, "select_artifact_by_id", _detail)

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(app, "/api/data-lake/artifacts/5")

    assert status_code == 200
    assert body["trading_date_ms"] is None
    assert body["content_hash"] is None


async def test_artifact_detail_content_hash_is_none_until_complete(monkeypatch: pytest.MonkeyPatch, make_data_lake_app):
    """A 'fetching' (not yet complete) artifact reports content_hash=None.

    This is the exact scenario MAJOR 4 flagged: select_artifact_by_id has no
    Status filter, so a still-fetching or failed row must not emit "" as if
    it were a real (empty) hash on a surface documented as a full receipt.
    """

    async def _detail(artifact_id: int) -> ArtifactDetail:
        return ArtifactDetail(
            **_artifact_detail_kwargs(
                id=7,
                trading_date_ms=session_open_ms_utc(date(2024, 5, 21)),
                data_contract_hash="d" * 64,
                content_hash=None,
                file_path="equity/usa/minute/spy/20240521_trade.zip",
                file_size_bytes=None,
                status="fetching",
                row_count=None,
                first_bar_start_ms=None,
                last_bar_start_ms=None,
                completed_at_ms=None,
            )
        )

    monkeypatch.setattr(catalog_client, "select_artifact_by_id", _detail)

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(app, "/api/data-lake/artifacts/7")

    assert status_code == 200
    assert body["status"] == "fetching"
    assert body["content_hash"] is None
    assert body["file_size_bytes"] is None


async def test_artifact_detail_returns_failure_diagnostics_for_failed_artifact(
    monkeypatch: pytest.MonkeyPatch, make_data_lake_app
):
    """#1845 P2-6: a 'failed' row's receipt must carry what fail_artifact() persisted.

    fail_artifact() (catalog_client.py) writes LastError, ErrorMessage, and
    increments AttemptCount, but select_artifact_by_id used to omit all
    three from the projected receipt — an operator looking at a failed
    artifact's detail had no way to see why it failed.
    """

    async def _detail(artifact_id: int) -> ArtifactDetail:
        return ArtifactDetail(
            **_artifact_detail_kwargs(
                id=9,
                content_hash=None,
                file_size_bytes=None,
                status="failed",
                row_count=None,
                first_bar_start_ms=None,
                last_bar_start_ms=None,
                completed_at_ms=None,
                attempt_count=3,
                last_error="provider_rate_limited",
                error_message="429 Too Many Requests from Polygon after 3 attempts",
            )
        )

    monkeypatch.setattr(catalog_client, "select_artifact_by_id", _detail)

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(app, "/api/data-lake/artifacts/9")

    assert status_code == 200
    assert body["status"] == "failed"
    assert body["attempt_count"] == 3
    assert body["last_error"] == "provider_rate_limited"
    assert body["error_message"] == "429 Too Many Requests from Polygon after 3 attempts"


async def test_artifact_detail_404_when_row_does_not_exist(monkeypatch: pytest.MonkeyPatch, make_data_lake_app):
    async def _missing(artifact_id: int) -> None:
        return None

    monkeypatch.setattr(catalog_client, "select_artifact_by_id", _missing)

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(app, "/api/data-lake/artifacts/999")
    assert status_code == 404
    assert body["detail"]["reason"] == "artifact_not_found"


# ---------------------------------------------------------------------------
# Storage summary — counts/bytes by kind, per-symbol span, honest-empty.
# ---------------------------------------------------------------------------


async def test_storage_summary_reports_counts_bytes_and_symbol_spans(
    monkeypatch: pytest.MonkeyPatch, make_data_lake_app
):
    async def _kinds(market: str) -> list[StorageKindTotal]:
        return [
            StorageKindTotal(artifact_kind="time_series_bars", resolution="minute", artifact_count=42, total_bytes=1_048_576),
            StorageKindTotal(artifact_kind="factor_file", resolution=None, artifact_count=1, total_bytes=2048),
        ]

    async def _spans(market: str) -> list[SymbolCoverageSpan]:
        return [
            SymbolCoverageSpan(
                symbol="SPY",
                first_trading_date_ms=session_open_ms_utc(date(2024, 5, 20)),
                last_trading_date_ms=session_open_ms_utc(date(2024, 5, 24)),
                artifact_count=5,
            )
        ]

    monkeypatch.setattr(catalog_client, "select_storage_totals_by_kind", _kinds)
    monkeypatch.setattr(catalog_client, "select_symbol_coverage_spans", _spans)

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(app, "/api/data-lake/storage-summary")

    assert status_code == 200
    assert body["market"] == "usa"
    kinds_by_name = {k["artifact_kind"]: k for k in body["kinds"]}
    assert kinds_by_name["time_series_bars"]["artifact_count"] == 42
    assert kinds_by_name["time_series_bars"]["total_bytes"] == 1_048_576
    assert kinds_by_name["factor_file"]["resolution"] is None

    assert len(body["symbols"]) == 1
    span = body["symbols"][0]
    assert span["symbol"] == "SPY"
    assert span["artifact_count"] == 5
    assert span["first_trading_date_ms"] == session_open_ms_utc(date(2024, 5, 20))
    assert span["last_trading_date_ms"] == session_open_ms_utc(date(2024, 5, 24))


async def test_storage_summary_honest_empty_on_empty_catalog(monkeypatch: pytest.MonkeyPatch, make_data_lake_app):
    async def _no_kinds(market: str) -> list[StorageKindTotal]:
        return []

    async def _no_spans(market: str) -> list[SymbolCoverageSpan]:
        return []

    monkeypatch.setattr(catalog_client, "select_storage_totals_by_kind", _no_kinds)
    monkeypatch.setattr(catalog_client, "select_symbol_coverage_spans", _no_spans)

    app = make_data_lake_app(include_data_lake=True)
    status_code, body = await _get(app, "/api/data-lake/storage-summary")

    assert status_code == 200
    assert body["kinds"] == []
    assert body["symbols"] == []
