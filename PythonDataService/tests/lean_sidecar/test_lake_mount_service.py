"""Orchestrator behavior under ``DATA_LAKE_ENABLED`` (#1834).

Drives the real ``run_trusted_sample`` with only the process boundaries
faked (the launcher HTTP call, the .NET persist call, and — for the
flag-off comparison — the image-metadata extraction and the bar store's
Polygon refill). No container is launched.

The pair of tests is the point: same request, same window, one with the
flag on and one with it off, so "flag on reads the lake and stages
nothing" and "flag off is unchanged" are asserted against each other
rather than in isolation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.lean_sidecar.lake_mount import CONTAINER_LAKE_DATA_MOUNT, LAKE_SUBDIR
from app.lean_sidecar.launcher.models import LAUNCHER_CAPABILITIES, LaunchRequest, LaunchResponse
from app.lean_sidecar.lean_config import CONTAINER_DATA_FOLDER
from app.lean_sidecar.trading_calendar import next_trading_day, session_open_ms_utc
from tests._helpers.lake_fixture import seed_lake_corporate_actions, seed_lake_window
from tests._helpers.lean_store import seed_store_day

DAY_ONE = date(2026, 1, 5)
DAY_TWO = date(2026, 1, 6)
WINDOW = [DAY_ONE, DAY_TWO]
SYMBOL = "SPY"


def _polygon_live_policy() -> Any:
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy

    return DataPolicy(
        source="polygon",
        symbol=SYMBOL,
        adjusted=False,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        provider_kind="live",
        fixture_id=None,
        fixture_sha256=None,
    )


def _request(run_id: str) -> Any:
    from app.services.lean_sidecar_service import TrustedRunRequest

    return TrustedRunRequest(
        run_id=run_id,
        # P2.5 window contract: start is the session open of the first
        # trading day, end is the session open of the next trading day
        # after the last one. Both derived from the canonical calendar.
        start_ms_utc=session_open_ms_utc(DAY_ONE),
        end_ms_utc=session_open_ms_utc(next_trading_day(DAY_TWO)),
        starting_cash=100_000.0,
        data_policy=_polygon_live_policy(),
    )


@pytest.fixture
def orchestrator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Fake every process boundary ``run_trusted_sample`` crosses.

    Returns a namespace carrying the artifacts root and the list of
    launch requests the (faked) launcher received.
    """
    from app.services import lean_sidecar_service as service

    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir(parents=True)
    launch_requests: list[LaunchRequest] = []

    async def fake_post_launch(request: LaunchRequest) -> LaunchResponse:
        launch_requests.append(request)
        # exit_code 1 keeps the orchestrator out of the LEAN-output
        # parser: this test is about staging and mount rendering, not
        # about result normalization.
        return LaunchResponse(
            run_id=request.run_id,
            exit_code=1,
            duration_ms=5,
            timed_out=False,
            log_tail="faked launcher",
            lean_errors={},
            is_clean=False,
        )

    async def fake_persist(**_kwargs: Any) -> int:
        return 1

    monkeypatch.setattr(service, "DEFAULT_ARTIFACTS_ROOT", artifacts_root)
    monkeypatch.setattr(service, "assert_lean_persistence_source_current", lambda: None)
    monkeypatch.setattr(service, "post_launch", fake_post_launch)
    monkeypatch.setattr(service, "_persist_completed_run", fake_persist)
    return SimpleNamespace(artifacts_root=artifacts_root, launch_requests=launch_requests)


@pytest.fixture
def _launcher_supports_lake_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the capability handshake as a current launcher would."""
    from app.lean_sidecar import launcher_client

    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "capabilities": list(LAUNCHER_CAPABILITIES)}

    monkeypatch.setattr(launcher_client, "get_healthz", healthz)


@pytest.fixture
def _launcher_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer as a launcher predating the capabilities field entirely."""
    from app.lean_sidecar import launcher_client

    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": "old"}

    monkeypatch.setattr(launcher_client, "get_healthz", healthz)


@pytest.fixture
def _polygon_is_off_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any per-run Polygon staging a loud failure, not a slow test.

    ``run_trusted_sample`` imports both of these inside the live-Polygon
    branch, so patching the defining modules is enough to catch the
    branch being taken.
    """
    from app.engine.data import availability
    from app.services import polygon_client

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("lake-mode run must not stage from Polygon")

    monkeypatch.setattr(availability, "ensure_range", refuse)
    monkeypatch.setattr(polygon_client, "PolygonClientService", refuse)


def _read_config(workspace_root: Path) -> dict[str, Any]:
    return json.loads((workspace_root / "workspace" / "project" / "config.json").read_text(encoding="utf-8"))


def _read_manifest(workspace_root: Path) -> dict[str, Any]:
    return json.loads((workspace_root / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_lake_run_reads_the_mount_and_stages_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _polygon_is_off_limits: None,
    _launcher_supports_lake_mount: None,
) -> None:
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    lake_root = write_root / LAKE_SUBDIR
    seed_lake_window(lake_root, SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
    # A lake run must not shell out for image metadata either — the
    # lake's own Phase-0 bootstrap owns those files.
    monkeypatch.setattr(
        service,
        "stage_lean_metadata_from_image",
        lambda *_a, **_k: pytest.fail("lake-mode run must not extract image metadata per run"),
    )

    result = await service.run_trusted_sample(_request("lake-mode-run"))

    workspace_data = result.workspace_root / "workspace" / "data"
    assert list(workspace_data.rglob("*.zip")) == [], "lake mode must stage no bar zips into the workspace"

    config = _read_config(result.workspace_root)
    assert config["data-folder"] == CONTAINER_LAKE_DATA_MOUNT

    assert [r.mount_lake_read_only for r in orchestrator.launch_requests] == [True]

    manifest = _read_manifest(result.workspace_root)
    staged = manifest["staged_zip_sha256"]
    assert sorted(staged) == [
        "equity/usa/daily/spy.zip",
        "equity/usa/minute/spy/20260105_quote.zip",
        "equity/usa/minute/spy/20260105_trade.zip",
        "equity/usa/minute/spy/20260106_quote.zip",
        "equity/usa/minute/spy/20260106_trade.zip",
    ]
    for relative, digest in staged.items():
        lake_file = lake_root / relative
        assert lake_file.exists(), f"manifest names {relative}, which is not in the lake"
        assert hashlib.sha256(lake_file.read_bytes()).hexdigest() == digest


@pytest.mark.asyncio
async def test_flag_off_run_still_stages_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
) -> None:
    from app.config import settings
    from app.engine.data import availability, policy_store
    from app.services import lean_sidecar_service as service

    store_root = tmp_path / "polygon-raw"
    for trading_date in WINDOW:
        seed_store_day(store_root, SYMBOL, trading_date)

    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", False)
    monkeypatch.setattr(policy_store, "resolve_data_roots", lambda **_k: [store_root])
    monkeypatch.setattr(
        availability,
        "ensure_range",
        lambda **_k: SimpleNamespace(available_days=len(WINDOW), expected_days=len(WINDOW)),
    )
    monkeypatch.setattr(service, "stage_lean_metadata_from_image", lambda *_a, **_k: None)

    result = await service.run_trusted_sample(_request("staged-mode-run"))

    workspace_data = result.workspace_root / "workspace" / "data"
    staged_zips = sorted(p.name for p in workspace_data.rglob("*.zip"))
    assert staged_zips == [
        "20260105_quote.zip",
        "20260105_trade.zip",
        "20260106_quote.zip",
        "20260106_trade.zip",
        "spy.zip",
    ]

    config = _read_config(result.workspace_root)
    assert config["data-folder"] == CONTAINER_DATA_FOLDER

    assert [r.mount_lake_read_only for r in orchestrator.launch_requests] == [False]

    manifest = _read_manifest(result.workspace_root)
    for relative in manifest["staged_zip_sha256"]:
        assert (workspace_data / relative).exists()


@pytest.mark.asyncio
async def test_lake_refusal_leaves_the_run_id_reusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None,
) -> None:
    """An unserveable lake must not burn the run_id on its way out.

    The lake preflight runs before the workspace exists precisely so a
    "fix the lake and re-submit" refusal does not leave a directory
    behind — which the duplicate-run_id guard would then report as a
    stale-id problem, pointing the operator at the wrong thing.
    """
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    (write_root / LAKE_SUBDIR).mkdir(parents=True)  # a lake with nothing in it
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

    with pytest.raises(service.LeanSidecarServiceError, match="lake_incomplete_trade_coverage"):
        await service.run_trusted_sample(_request("reusable-run-id"))

    assert not (orchestrator.artifacts_root / "reusable-run-id").exists()

    # The same id now works once the lake can serve it — the refusal
    # was about the lake, and nothing about the id was consumed.
    seed_lake_window(write_root / LAKE_SUBDIR, SYMBOL, WINDOW)
    result = await service.run_trusted_sample(_request("reusable-run-id"))
    assert result.workspace_root.exists()


@pytest.mark.asyncio
async def test_stale_launcher_refuses_before_the_workspace_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _launcher_is_stale: None,
) -> None:
    """A launcher that would silently drop the mount is caught up front.

    Pydantic ignores unknown request fields, so a stale launcher would
    accept ``mount_lake_read_only=True`` and run the container with no
    lake volume — LEAN then reads an empty data folder and fails
    somewhere that says nothing about launcher versions.
    """
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    seed_lake_window(write_root / LAKE_SUBDIR, SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

    with pytest.raises(service.LeanSidecarServiceError, match="lake_mount_unsupported_by_launcher") as excinfo:
        await service.run_trusted_sample(_request("stale-launcher-run"))

    assert "Restart the launcher" in str(excinfo.value)
    assert orchestrator.launch_requests == []
    assert not (orchestrator.artifacts_root / "stale-launcher-run").exists()


@pytest.mark.asyncio
async def test_factor_files_move_the_input_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _polygon_is_off_limits: None,
    _launcher_supports_lake_mount: None,
) -> None:
    """A factor-file change must change ``input_snapshot_sha256``.

    LEAN reads factor and map files off the mounted lake and they alter
    split/dividend handling. Leaving them out of the snapshot would let
    a corporate-action revision change a run's results while its
    reproducibility fingerprint stayed constant — the exact claim the
    manifest exists to make.
    """
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    lake_root = write_root / LAKE_SUBDIR
    seed_lake_window(lake_root, SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

    seed_lake_corporate_actions(lake_root, SYMBOL, factor_rows="20260105,1,1\n")
    first = await service.run_trusted_sample(_request("factor-snapshot-a"))
    manifest_a = _read_manifest(first.workspace_root)

    seed_lake_corporate_actions(lake_root, SYMBOL, factor_rows="20260105,0.5,1\n")
    second = await service.run_trusted_sample(_request("factor-snapshot-b"))
    manifest_b = _read_manifest(second.workspace_root)

    assert manifest_a["staged_data"]["factor_files"], "factor file present in the lake must be hashed"
    assert manifest_a["staged_data"]["map_files"], "map file present in the lake must be hashed"
    assert manifest_a["input_snapshot_sha256"] != manifest_b["input_snapshot_sha256"]
