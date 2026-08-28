"""Which materializer an engine run uses, and what it records afterwards.

The engine has always ensured its bars exist before reading them. With
``DATA_LAKE_ENABLED`` that job moves from the policy store's ``ensure_range``
to the lake's ``ensure_data``, and the run comes back carrying the
fingerprint of the bytes it consumed. With the flag off — the default —
nothing about the run changes.

The lake itself is exercised in ``tests/unit/data_lake/test_run_materialization.py``;
here the materializer is a stand-in, because what is under test is the
engine's choice of it and what the run does with the answer.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import settings
from app.data_lake import run_materialization
from app.data_lake.types import DataAvailabilityResult
from app.routers import engine as engine_router
from app.routers.engine import EngineBacktestRequest, execute_engine_backtest
from tests._helpers.lean_store import seed_store_day

DAY_ONE = date(2026, 1, 5)  # Monday
DAY_THREE = date(2026, 1, 7)
SEEDED_DAYS = (DAY_ONE, date(2026, 1, 6), DAY_THREE)

LAKE_MANIFEST = "f" * 64


def _noop(_: str) -> None:
    return None


def _availability(manifest: str, lake_dir: Path) -> DataAvailabilityResult:
    return DataAvailabilityResult(
        request_id=uuid4(),
        overall_status="complete",
        lean_data_root_path=str(lake_dir),
        data_availability_hash=manifest,
        fetched_artifact_count=3,
        reused_artifact_count=1,
        completed_at_ms=0,
        duration_ms=0,
    )


def _request() -> EngineBacktestRequest:
    return EngineBacktestRequest(
        strategy_name="sma_crossover",
        params={"symbol": "SPY"},
        from_date=DAY_ONE.isoformat(),
        to_date=DAY_THREE.isoformat(),
        auto_fetch=True,
    )


def _run() -> object:
    return execute_engine_backtest(request=_request(), on_phase=_noop, on_log=_noop)


@pytest.fixture(autouse=True)
def offline_persistence(monkeypatch):
    """The .NET study save is best-effort and not what these tests are about."""
    monkeypatch.setattr(engine_router, "_save_study_sync", lambda **kwargs: None)


@pytest.fixture
def seeded_roots(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    """Seed the same SPY days into both the lake and the policy cache.

    Whichever root the run resolves, the engine finds bars — so a
    difference in the response is a difference in materialization, not a
    difference in what happened to be on disk.
    """
    write_root = tmp_path / "writer-root"
    lake_dir = write_root / "lake"
    lake_dir.mkdir(parents=True)
    (write_root / "staging").mkdir()
    policy_root = tmp_path / "store" / "polygon-adjusted"

    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setenv("LEAN_DATA_ROOT", str(tmp_path / "no-reference-mount"))
    monkeypatch.setenv("LEAN_DATA_CACHE", str(tmp_path / "store"))

    for day in SEEDED_DAYS:
        seed_store_day(lake_dir, "SPY", day)
        seed_store_day(policy_root, "SPY", day)
    return {"lake": lake_dir, "policy": policy_root}


def test_flag_off_run_still_materializes_through_the_policy_store(seeded_roots, monkeypatch):
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", False)
    ensured: list[dict] = []
    monkeypatch.setattr(engine_router, "ensure_range", lambda **kwargs: ensured.append(kwargs))

    def _must_not_run(**kwargs):
        raise AssertionError("the lake was consulted with the flag off")

    monkeypatch.setattr(run_materialization, "materialize_engine_run", _must_not_run)

    response = _run()

    assert response.success, response.error
    assert len(ensured) == 1
    assert ensured[0]["symbol"] == "SPY"
    # No lake bytes were read, so the run has no lake fingerprint to claim.
    assert response.lake_data_availability_hash is None


def test_flag_on_run_materializes_through_the_lake(seeded_roots, monkeypatch):
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
    calls: list[dict] = []

    def _fake_materialize(**kwargs):
        calls.append(kwargs)
        return _availability(LAKE_MANIFEST, seeded_roots["lake"])

    monkeypatch.setattr(run_materialization, "materialize_engine_run", _fake_materialize)

    def _must_not_run(**kwargs):
        raise AssertionError("the policy-store exporter ran with the lake on")

    monkeypatch.setattr(engine_router, "ensure_range", _must_not_run)

    response = _run()

    assert response.success, response.error
    assert calls == [
        {
            "symbol": "SPY",
            "start": DAY_ONE,
            "end": DAY_THREE,
            "requester": "sma_crossover",
        }
    ]


def test_flag_on_run_records_the_manifest_of_the_bytes_it_consumed(seeded_roots, monkeypatch):
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
    monkeypatch.setattr(
        run_materialization,
        "materialize_engine_run",
        lambda **kwargs: _availability(LAKE_MANIFEST, seeded_roots["lake"]),
    )

    response = _run()

    assert response.success, response.error
    assert response.lake_data_availability_hash == LAKE_MANIFEST


def test_flag_on_run_reads_the_lake_tree_and_nothing_else(seeded_roots, monkeypatch):
    """The resolved root really is the lake — emptying it starves the run.

    Both roots hold the same three days to start with, so the first run is
    the baseline: it sees bars and trades. Removing the *lake* copy while
    leaving the policy cache untouched must take those bars away. If the
    second run still traded, the flag would not have moved the authority.
    """
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
    monkeypatch.setattr(
        run_materialization,
        "materialize_engine_run",
        lambda **kwargs: _availability(LAKE_MANIFEST, seeded_roots["lake"]),
    )

    with_lake = _run()
    assert with_lake.success, with_lake.error
    assert with_lake.total_trades > 0, "baseline run produced no trades; the test proves nothing"

    for day in SEEDED_DAYS:
        (seeded_roots["lake"] / "equity" / "usa" / "minute" / "spy" / f"{day:%Y%m%d}_trade.zip").unlink()
    without_lake = _run()

    assert without_lake.total_trades == 0


def test_flag_on_run_without_auto_fetch_claims_no_manifest(seeded_roots, monkeypatch):
    """No materialization, no fingerprint — a run must not claim bytes it never asked for."""
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

    def _must_not_run(**kwargs):
        raise AssertionError("auto_fetch=False must not materialize anything")

    monkeypatch.setattr(run_materialization, "materialize_engine_run", _must_not_run)
    request = _request()
    request.auto_fetch = False

    response = execute_engine_backtest(request=request, on_phase=_noop, on_log=_noop)

    assert response.success, response.error
    assert response.lake_data_availability_hash is None


def test_a_lake_that_cannot_materialize_fails_the_run_loudly(seeded_roots, monkeypatch):
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

    def _explode(**kwargs):
        raise run_materialization.LakeMaterializationError("provider_no_data")

    monkeypatch.setattr(run_materialization, "materialize_engine_run", _explode)

    response = _run()

    assert response.success is False
    assert "provider_no_data" in (response.error or "")
