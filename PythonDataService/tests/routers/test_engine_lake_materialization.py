"""Which materializer an engine run uses, and what it records afterwards.

The engine has always ensured its bars exist before reading them. With
``DATA_LAKE_ENABLED`` that job moves from the policy store's ``ensure_range``
to the lake's ``ensure_data``, and the run comes back carrying the lake's
fingerprint for what it materialized. With the flag off — the default —
nothing about the run changes.

The lake itself is exercised in ``tests/unit/data_lake/test_run_materialization.py``;
here the materializer is a stand-in, because what is under test is the
engine's choice of it and what the run does with the answer.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from app.config import settings
from app.data_lake import run_materialization
from app.data_lake.ensure_data import _compute_data_availability_hash
from app.data_lake.run_materialization import EngineRunMaterialization
from app.data_lake.types import ArtifactRecord
from app.routers import engine as engine_router
from app.routers.engine import EngineBacktestRequest, execute_engine_backtest
from tests._helpers.lean_store import seed_store_day

DAY_ONE = date(2026, 1, 5)  # Monday
DAY_THREE = date(2026, 1, 7)
SEEDED_DAYS = (DAY_ONE, date(2026, 1, 6), DAY_THREE)


def _noop(_: str) -> None:
    return None


def _lake_artifacts(lake_dir: Path) -> list[ArtifactRecord]:
    """One catalog record per seeded zip, hashed from the bytes on disk.

    The fingerprint under test is content-derived, so the stub result carries
    records that describe real files rather than invented ones — otherwise the
    assertion could not tell a plumbed hash from a hard-coded one.
    """
    records: list[ArtifactRecord] = []
    for artifact_id, day in enumerate(SEEDED_DAYS, start=1):
        rel = f"equity/usa/minute/spy/{day:%Y%m%d}_trade.zip"
        payload = (lake_dir / rel).read_bytes()
        records.append(
            ArtifactRecord(
                id=artifact_id,
                artifact_kind="time_series_bars",
                market="usa",
                symbol="SPY",
                trading_date=day,
                resolution="minute",
                data_type="trade",
                provider="polygon",
                price_adjustment_mode="raw",
                data_contract_hash="c" * 64,
                file_path=rel,
                file_sha256=hashlib.sha256(payload).hexdigest(),
                row_count=390,
                first_bar_start_ms=0,
                last_bar_start_ms=0,
            )
        )
    return records


def _materialization(lake_dir: Path, *, incomplete_summary: str | None = None) -> EngineRunMaterialization:
    """What the bridge hands a run for the seeded tree.

    Note what the run does *not* receive: the failure list. Whether the run may
    proceed was decided inside ``materialize_engine_run``; all that survives is
    a line to show the operator.
    """
    records = _lake_artifacts(lake_dir)
    return EngineRunMaterialization(
        availability_hash=_compute_data_availability_hash(records),
        fetched_artifact_count=len(records),
        reused_artifact_count=0,
        incomplete_summary=incomplete_summary,
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
        return _materialization(seeded_roots["lake"])

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
            # The lake's partial-coverage gate is resolution-specific: a daily
            # run must refuse a stale daily zip that a minute run may ignore.
            "resolution": "minute",
            "requester": "sma_crossover",
        }
    ]


def test_flag_on_run_records_the_manifest_of_what_it_materialized(seeded_roots, monkeypatch):
    """The response carries the content-derived fingerprint, not a placeholder."""
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
    records = _lake_artifacts(seeded_roots["lake"])
    monkeypatch.setattr(
        run_materialization,
        "materialize_engine_run",
        lambda **kwargs: _materialization(seeded_roots["lake"]),
    )

    response = _run()

    assert response.success, response.error
    assert response.lake_data_availability_hash == _compute_data_availability_hash(records)
    # And it really is derived from the artifact content: change one byte hash
    # and the fingerprint the run would have carried is a different one.
    mutated = [records[0].model_copy(update={"file_sha256": "0" * 64}), *records[1:]]
    assert response.lake_data_availability_hash != _compute_data_availability_hash(mutated)


def test_flag_on_run_surfaces_incomplete_coverage_to_the_operator(seeded_roots, monkeypatch):
    """A run reading a lake that reports itself incomplete must say so in the log."""
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
    partial = _materialization(seeded_roots["lake"], incomplete_summary="metadata/io_error")
    monkeypatch.setattr(run_materialization, "materialize_engine_run", lambda **kwargs: partial)
    log: list[str] = []

    execute_engine_backtest(request=_request(), on_phase=_noop, on_log=log.append)

    assert any("incomplete" in line and "metadata/io_error" in line for line in log), log


def test_a_lake_refusal_reaches_the_operator_log(seeded_roots, monkeypatch):
    """The reason the lake refused must not be buried in the service log."""
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

    def _refuse(**kwargs):
        raise run_materialization.LakeMaterializationError("incomplete minute coverage for SPY")

    monkeypatch.setattr(run_materialization, "materialize_engine_run", _refuse)
    log: list[str] = []

    response = execute_engine_backtest(request=_request(), on_phase=_noop, on_log=log.append)

    assert response.success is False
    assert any("Lake refused this run" in line and "incomplete minute coverage" in line for line in log), log


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
        lambda **kwargs: _materialization(seeded_roots["lake"]),
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
