"""Shared test fixtures and helpers"""

import os
from collections.abc import Iterable
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# Patch env before importing app
os.environ.setdefault("POLYGON_API_KEY", "test-key-for-testing")
# Router tests exercise control endpoints without modeling the local
# data-plane shared-secret hop; opt out explicitly here while dedicated
# security tests monkeypatch this off to prove production fail-closed behavior.
# Assign rather than setdefault so a developer's real .env secret cannot make
# otherwise isolated ASGI tests fail with 403 responses.
os.environ["DATA_PLANE_CONTROL_SECRET"] = ""
os.environ["DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL"] = "true"

@pytest.fixture(autouse=True)
def _clerk_market_liveness_defaults_tradable(monkeypatch: pytest.MonkeyPatch):
    """The Alpaca Clerk's submission-boundary liveness recheck (#1671,
    ``runtime.py::_execute_effect``) fails closed by default — correct in
    production, but it silently stalls any pre-existing test that drives a
    real ENTER through ``SqliteAlpacaClerkFacade`` without first configuring
    live market-liveness evidence (the unconfigured process-global store
    resolves to UNKNOWN, so every such ENTER is rejected before it ever
    reaches the broker). Default every test to a fresh TRADABLE fact; a test
    that specifically exercises the gate overrides this with its own
    ``monkeypatch.setattr(clerk_runtime, "market_liveness_fact", ...)``,
    which always wins over this fixture (later patches win)."""
    import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
    from app.schemas.market_liveness import MarketClockLivenessEvidence
    from app.services.market_liveness import compose_market_liveness

    def _tradable(symbol: str, observed_at_ms: int):
        return compose_market_liveness(
            symbol,
            now_ms=observed_at_ms,
            market_clock=MarketClockLivenessEvidence(
                state="OPEN",
                source="test.default-tradable-clock",
                observed_at_ms=observed_at_ms,
                vendor_timestamp_ms=observed_at_ms,
            ),
            connected=True,
            connection_changed_at_ms=observed_at_ms,
            symbol_status=None,
        )

    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", _tradable)


@pytest.fixture(autouse=True)
def _isolate_strategy_validation_flag_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1739: the strategy-validation flag ledger
    (``artifacts/strategy_validation/flag_events.json``) is gitignored and
    machine-local. A developer who has ever used the flag UI has real
    entries there that CI never sees, so any reader that falls through to
    ``DEFAULT_FLAG_EVENTS_PATH`` silently makes local pytest diverge from
    CI (see PR #1733, where this produced a locally-green, CI-red run).
    Every test gets an isolated, empty-by-default ledger location instead;
    a test that wants a populated ledger builds its own fixture under its
    own ``tmp_path`` and passes ``flag_events_path`` explicitly (several
    already do, e.g. ``tests/broker/v2panel/conftest.py``).

    Patching this one module attribute is enough for every reader:
    ``strategy_validation_manifest._resolve_flag_events_path`` re-reads it
    at call time (not at import time) for every caller that omits
    ``flag_events_path``, including
    ``app/services/broker_v2_panel/panel_data_source.py`` and
    ``app/services/strategy_validation_admission.py``, and the FastAPI
    dependency in ``app/routers/strategy_validation.py`` reads the same
    module attribute dynamically rather than a name frozen at router-import
    time. A test that needs a *different* ledger through the HTTP layer
    can still use ``app.dependency_overrides`` per-test, as
    ``tests/routers/test_strategy_validation.py`` already does — that
    always takes priority over this fixture's default.

    Production behavior is untouched: DEFAULT_FLAG_EVENTS_PATH and the
    router's default dependency still resolve to the real ledger outside
    pytest.
    """
    import app.services.strategy_validation_manifest as strategy_validation_manifest

    isolated_path = tmp_path / "strategy_validation_flag_events.json"
    monkeypatch.setattr(strategy_validation_manifest, "DEFAULT_FLAG_EVENTS_PATH", isolated_path)


@pytest.fixture(autouse=True)
def _isolate_data_lake_write_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep every test's lake root inside its own tmp_path.

    ``DATA_LAKE_ENABLED`` defaults ON since #1839, so root resolution now
    reaches the lake by default: ``policy_store.resolve_data_roots`` for a raw
    request, the chart split-read, and the sidecar preflight all resolve
    ``LEAN_DATA_WRITE_ROOT`` unless a test pins it. Left unpinned that is the
    deployment path (``/lean-data-writer``) -- a directory a test would create
    if it could and read a developer's real lake out of if it existed. Either
    outcome makes a test's result depend on the host rather than on its
    fixture, which is the same class of local-vs-CI divergence
    ``_isolate_strategy_validation_flag_ledger`` above exists to prevent.

    ``staging/`` is created alongside ``lake/`` because the writer's atomic
    promote asserts both exist on the same filesystem.

    A test that wants a specific lake overrides this with its own
    ``monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", ...)``; later
    patches win, so this is a floor, not a ceiling.
    """
    from app.config import settings

    write_root = tmp_path / "lean-data-writer"
    (write_root / "lake").mkdir(parents=True, exist_ok=True)
    (write_root / "staging").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))


@pytest.fixture(autouse=True)
def _isolate_canary_admission_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep a developer's machine-local canary approvals out of every test.

    Production derives exact-pair admission from a gitignored local ledger.
    Patching the service default at call time gives each test an empty ledger
    while tests for the mechanism can still pass their own explicit path.
    """
    import app.services.canary_admission as canary_admission

    isolated_path = tmp_path / "canary_admission_events.json"
    monkeypatch.setattr(
        canary_admission,
        "DEFAULT_CANARY_ADMISSION_LEDGER_PATH",
        isolated_path,
    )


_CATALOG_TRUNCATING_FIXTURE_PREFIX = "clean_artifacts"

# A developer or CI job must set this to explicitly attest, out of band,
# that POSTGRES_URL points at a database safe to wipe. It cannot be inferred
# from POSTGRES_URL itself: CI's per-job postgres:16 service (ci.yml) and a
# developer's real my-postgres dev container (compose.yaml) both resolve to
# postgres://postgres:...@localhost:5432/postgres -- host, port, user, and
# database name are identical either way. Only an explicit, separate signal
# can tell them apart.
_POSTGRES_TARGET_EPHEMERAL_ENV_VAR = "POSTGRES_URL_IS_EPHEMERAL"


def _postgres_target_is_ephemeral() -> bool:
    return os.getenv(_POSTGRES_TARGET_EPHEMERAL_ENV_VAR, "").strip().lower() in ("1", "true")


def _postgres_target_url() -> str:
    """Same lookup every per-file ``_postgres_url()`` helper in
    tests/{unit,integration}/data_lake already does (``settings.POSTGRES_URL``
    falling back to the raw env var). Imported lazily, matching this file's
    other fixtures, so importing conftest doesn't require app.config to be
    importable before pytest has set up sys.path.
    """
    from app.config import settings

    return settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")


def _raise_if_catalog_truncation_is_unsafe(fixturenames: Iterable[str]) -> None:
    """Core decision behind ``_guard_data_lake_catalog_truncation`` below,
    factored out so it is unit-testable without going through pytest's
    fixture-injection machinery -- see
    tests/unit/data_lake/test_catalog_truncation_guard.py.

    Matches by *prefix*, not an exhaustive name list: every current
    ``clean_artifacts``-style fixture across tests/unit/data_lake and
    tests/integration/data_lake follows this naming convention (including
    test_ensure_data_all_kinds.py's ``clean_artifacts_all_kinds_complete``
    and ``clean_artifacts_second_call``), and a future one that follows it
    too is covered without editing this file again.

    A test with no POSTGRES_URL configured is left alone: the fixture's own
    ``_postgres_url()`` helper will ``pytest.skip`` before issuing any SQL,
    same as it always has, and this guard only has something to protect once
    there is a real database on the other end of the connection.
    """
    truncates_catalog = any(
        name.startswith(_CATALOG_TRUNCATING_FIXTURE_PREFIX) for name in fixturenames
    )
    if not truncates_catalog or not _postgres_target_url() or _postgres_target_is_ephemeral():
        return
    raise RuntimeError(
        "Refusing to run a catalog-truncating fixture (name starting with "
        f"'{_CATALOG_TRUNCATING_FIXTURE_PREFIX}'): {_POSTGRES_TARGET_EPHEMERAL_ENV_VAR} "
        "is not set, so POSTGRES_URL has not been demonstrated to point at a "
        "disposable database. A prior incident pointed POSTGRES_URL at the "
        "shared dev container (my-postgres) and these fixtures truncated "
        "DataLakeArtifacts to zero rows on every test that used them (#1887). "
        f"Set {_POSTGRES_TARGET_EPHEMERAL_ENV_VAR}=1 only when POSTGRES_URL "
        "names a database you can afford to lose -- a fresh postgres:16 "
        "container you stood up for this run, never my-postgres."
    )


@pytest.fixture(autouse=True)
def _guard_data_lake_catalog_truncation(request: pytest.FixtureRequest) -> None:
    """Block any ``clean_artifacts``-style fixture (tests/unit/data_lake,
    tests/integration/data_lake) from truncating DataLakeArtifacts /
    DataLakeRuns unless POSTGRES_URL has been explicitly attested as
    ephemeral.

    Same class of bug as ``_isolate_data_lake_write_root`` and
    ``_isolate_strategy_validation_flag_ledger`` above -- a developer's real
    resource getting hit by a test that assumed it owned the whole
    environment -- except here there is no ``tmp_path`` to silently redirect
    writes to: Postgres is out-of-process infrastructure this test process
    doesn't control. So this fails loudly instead of isolating quietly.
    """
    _raise_if_catalog_truncation_is_unsafe(request.fixturenames)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    """Async HTTP client for testing FastAPI endpoints."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def make_sample_bars(count: int = 30) -> list[dict]:
    """Create sample OHLCV bars for indicator tests."""
    bars = []
    base_price = 150.0
    for i in range(count):
        price = base_price + i * 0.5
        bars.append(
            {
                "timestamp": 1704067200000 + i * 86400000,  # daily from 2024-01-01
                "open": price,
                "high": price + 2.0,
                "low": price - 1.0,
                "close": price + 1.0,
                "volume": 1000000.0 + i * 10000,
            }
        )
    return bars
