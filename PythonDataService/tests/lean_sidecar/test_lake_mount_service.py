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

from app.data_lake.path_policy import lake_subpath
from app.lean_sidecar.lake_mount import CONTAINER_LAKE_DATA_MOUNT
from app.lean_sidecar.launcher.models import LAUNCHER_CAPABILITIES, LaunchRequest, LaunchResponse
from app.lean_sidecar.lean_config import CONTAINER_DATA_FOLDER
from app.lean_sidecar.trading_calendar import next_trading_day, session_open_ms_utc
from tests._helpers.lake_fixture import seed_lake_corporate_actions, seed_lake_window
from tests._helpers.lean_store import seed_store_day

DAY_ONE = date(2026, 1, 5)
DAY_TWO = date(2026, 1, 6)
WINDOW = [DAY_ONE, DAY_TWO]
SYMBOL = "SPY"


def _polygon_live_policy(*, adjusted: bool = False) -> Any:
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy

    return DataPolicy(
        source="polygon",
        symbol=SYMBOL,
        adjusted=adjusted,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        provider_kind="live",
        fixture_id=None,
        fixture_sha256=None,
    )


def _request(run_id: str, *, adjusted: bool = False) -> Any:
    from app.services.lean_sidecar_service import TrustedRunRequest

    return TrustedRunRequest(
        run_id=run_id,
        # P2.5 window contract: start is the session open of the first
        # trading day, end is the session open of the next trading day
        # after the last one. Both derived from the canonical calendar.
        start_ms_utc=session_open_ms_utc(DAY_ONE),
        end_ms_utc=session_open_ms_utc(next_trading_day(DAY_TWO)),
        starting_cash=100_000.0,
        data_policy=_polygon_live_policy(adjusted=adjusted),
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
    lake_root = write_root / lake_subpath("raw")
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
    # ``exist_ok`` because tests/conftest.py's autouse
    # ``_isolate_data_lake_write_root`` already created this root under the
    # same tmp_path — the flag is on by default now, so every test gets an
    # isolated lake whether it asked for one or not. What this test needs is
    # that the lake is *empty*, which it is either way.
    (write_root / lake_subpath("raw")).mkdir(parents=True, exist_ok=True)  # a lake with nothing in it
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

    with pytest.raises(service.LeanSidecarServiceError, match="lake_incomplete_trade_coverage"):
        await service.run_trusted_sample(_request("reusable-run-id"))

    assert not (orchestrator.artifacts_root / "reusable-run-id").exists()

    # The same id now works once the lake can serve it — the refusal
    # was about the lake, and nothing about the id was consumed.
    seed_lake_window(write_root / lake_subpath("raw"), SYMBOL, WINDOW)
    result = await service.run_trusted_sample(_request("reusable-run-id"))
    assert result.workspace_root.exists()


@pytest.mark.asyncio
async def test_a_fixture_replay_never_consults_the_lake_even_when_it_has_no_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None,
) -> None:
    """A frozen fixture replay's recording is the data authority — the lake
    preflight must never run for it, regardless of coverage.

    Before this guard, the preflight checked only ``data_policy.source ==
    "polygon"`` and the flag, so with the flag default-on (#1839) a fixture
    replay (parity tests / freshness canary, ``provider_kind="fixture"``)
    over a window the lake has no coverage for refused with
    ``lake_incomplete_trade_coverage`` before ever reaching the fixture
    branch, discarding its frozen bars for an unrelated lake gap.
    """
    from app.config import settings
    from app.lean_sidecar import polygon_canonical
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    (write_root / lake_subpath("raw")).mkdir(parents=True, exist_ok=True)  # empty lake
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

    class _FakeFixtureProvider:
        fixture_id = "fake-fixture-v1"
        fixture_sha256 = None

        def fetch_minute_bars(
            self, *, symbol: str, start_date: date, end_date: date, adjusted: bool
        ) -> list[dict[str, Any]]:
            price = 100.0
            bars = []
            for trading_date in WINDOW:
                bars.append(
                    {
                        "timestamp": session_open_ms_utc(trading_date),
                        "open": price,
                        "high": price + 0.5,
                        "low": price - 0.5,
                        "close": price + 0.1,
                        "volume": 1_000,
                    }
                )
                price += 1.0
            return bars

    monkeypatch.setattr(polygon_canonical, "get_default_provider", lambda: _FakeFixtureProvider())

    resolved: list[object] = []
    real_resolve = service._resolve_lake_artifacts_or_refuse

    async def _spy(request):
        outcome = await real_resolve(request)
        resolved.append(outcome)
        return outcome

    monkeypatch.setattr(service, "_resolve_lake_artifacts_or_refuse", _spy)

    data_policy = DataPolicy(
        source="polygon",
        symbol=SYMBOL,
        adjusted=False,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        provider_kind="fixture",
        fixture_id="fake-fixture-v1",
        fixture_sha256=None,
    )
    request = service.TrustedRunRequest(
        run_id="fixture-skips-lake-preflight",
        start_ms_utc=session_open_ms_utc(DAY_ONE),
        end_ms_utc=session_open_ms_utc(next_trading_day(DAY_TWO)),
        starting_cash=100_000.0,
        data_policy=data_policy,
    )

    result = await service.run_trusted_sample(request)

    assert not resolved, "a fixture replay must never consult the lake"
    assert result.workspace_root.exists()


@pytest.mark.asyncio
async def test_an_adjusted_request_also_reaches_the_lake_with_the_flag_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None,
) -> None:
    """An adjusted run mounts the lake too, asserted at the service seam.

    This used to assert the opposite: before #1866 the lake's live pipeline
    was raw-only, so an adjusted run stayed on the pre-lake staging path
    (bug #1839 found and fixed a case where that fell through and served raw
    bytes under an adjusted policy). #1866 made the adjustment mode a path
    segment, so the lake now holds a real ``polygon_split_adjusted`` root and
    an adjusted run resolves it exactly like a raw run resolves its own.

    The lake here is fully serveable in the adjusted mode, so a run that
    consulted it should succeed and mount it: ``mount_lake_read_only`` on the
    launch request, which is what a lake-mode run sets and a staging run
    leaves alone.
    """
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    (write_root / lake_subpath("polygon_split_adjusted")).mkdir(parents=True, exist_ok=True)
    seed_lake_window(write_root / lake_subpath("polygon_split_adjusted"), SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

    resolved: list[object] = []
    real_resolve = service._resolve_lake_artifacts_or_refuse

    async def _spy(request):
        outcome = await real_resolve(request)
        resolved.append(outcome)
        return outcome

    monkeypatch.setattr(service, "_resolve_lake_artifacts_or_refuse", _spy)

    await service.run_trusted_sample(_request("adjusted-reaches-the-lake", adjusted=True))

    assert resolved, "an adjusted run never consulted the lake"
    assert orchestrator.launch_requests
    assert orchestrator.launch_requests[-1].mount_lake_read_only


@pytest.mark.asyncio
async def test_a_raw_request_does_reach_the_lake_with_the_flag_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None,
) -> None:
    """The counterpart, pinning that raw resolves its own root, not the adjusted one.

    Identical setup, raw policy, against a lake that only holds an adjusted
    root: this one must still mount the lake once its own raw root is
    seeded, and the two tests together pin that each mode resolves its own
    root rather than one mode leaking into the other's.
    """
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    (write_root / lake_subpath("raw")).mkdir(parents=True, exist_ok=True)
    seed_lake_window(write_root / lake_subpath("raw"), SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

    await service.run_trusted_sample(_request("raw-reaches-the-lake", adjusted=False))

    assert orchestrator.launch_requests
    assert orchestrator.launch_requests[-1].mount_lake_read_only


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
    seed_lake_window(write_root / lake_subpath("raw"), SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

    with pytest.raises(service.LeanSidecarServiceError, match="lake_mount_unsupported_by_launcher") as excinfo:
        await service.run_trusted_sample(_request("stale-launcher-run"))

    assert "Restart the launcher" in str(excinfo.value)
    assert orchestrator.launch_requests == []
    assert not (orchestrator.artifacts_root / "stale-launcher-run").exists()


@pytest.mark.asyncio
async def test_unreachable_launcher_refuses_before_the_workspace_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
) -> None:
    """A launcher that is down must fail the run without consuming the ID.

    Sibling of the stale-launcher case above, and the distinction is the
    point: "reachable but too old" is a data-plane refusal
    (``LeanSidecarServiceError``), whereas "not reachable at all" is a
    transport failure the preflight deliberately does NOT translate — it
    propagates as ``LauncherUnreachable`` so the router's existing
    mapping renders a 503, rather than dressing an outage up as a lake
    coverage problem.

    That 503 mapping was already covered, but only on the non-lake
    ``/launch`` path, where the workspace is fully staged by the time
    the launcher is called. Reaching it from the lake preflight happens
    *before* the workspace exists — so this pins that an outage leaves
    the run_id reusable too.
    """
    from app.config import settings
    from app.lean_sidecar import launcher_client
    from app.lean_sidecar.launcher_client import LauncherUnreachable
    from app.services import lean_sidecar_service as service

    async def unreachable() -> dict[str, Any]:
        raise LauncherUnreachable("launcher at http://127.0.0.1:8090/healthz unreachable: connection refused")

    monkeypatch.setattr(launcher_client, "get_healthz", unreachable)

    write_root = tmp_path / "lean-data-writer"
    seed_lake_window(write_root / lake_subpath("raw"), SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)

    with pytest.raises(LauncherUnreachable, match="unreachable"):
        await service.run_trusted_sample(_request("unreachable-launcher-run"))

    # Must not be swallowed into the service's own error type: the
    # router keys its 503 arm off this class, and anything caught into
    # LeanSidecarServiceError would render as a run-level failure instead.
    assert not issubclass(LauncherUnreachable, service.LeanSidecarServiceError)

    assert orchestrator.launch_requests == []
    assert not (orchestrator.artifacts_root / "unreachable-launcher-run").exists()


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
    lake_root = write_root / lake_subpath("raw")
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
