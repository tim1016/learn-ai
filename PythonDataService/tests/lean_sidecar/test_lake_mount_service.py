"""Orchestrator behavior when a run retains admitted lake inputs (#2455).

Drives the real ``run_trusted_sample`` with only the process boundaries
faked (the launcher HTTP call, the .NET persist call, and — for the
flag-off comparison — the image-metadata extraction and the bar store's
Polygon refill). No container is launched.

Lake runs capture catalog-admitted bytes into their private workspace;
fixture runs keep their existing staging path. Shared-file replacement
must never change what a launched run consumes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import respx

from app.data_lake.path_policy import lake_subpath
from app.lean_sidecar.lake_mount import CONTAINER_LAKE_DATA_MOUNT
from app.lean_sidecar.launcher.models import LAUNCHER_CAPABILITIES, LaunchRequest, LaunchResponse
from app.lean_sidecar.trading_calendar import next_trading_day, session_open_ms_utc
from app.research.backtest_runs.service import SaveOutcome
from tests._helpers.lake_fixture import seed_lake_corporate_actions, seed_lake_interest_rate, seed_lake_window

pytestmark = pytest.mark.usefixtures("seeded_lake_catalog")

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
    from app.lean_sidecar import launcher_client
    monkeypatch.setattr(launcher_client, "get_healthz", AsyncMock(return_value={
        "status": "ok", "capabilities": list(LAUNCHER_CAPABILITIES),
    }))
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

    async def fake_persist(**_kwargs: Any) -> SaveOutcome:
        return SaveOutcome(status="saved", run_id=1)

    monkeypatch.setattr(service, "DEFAULT_ARTIFACTS_ROOT", artifacts_root)
    monkeypatch.setattr(service, "assert_lean_persistence_source_current", lambda: None)
    monkeypatch.setattr(service, "post_launch", fake_post_launch)
    monkeypatch.setattr(service, "_persist_completed_run", fake_persist)
    # These orchestration tests formerly faked the launcher's metadata proof.
    # The proof now runs during capture; dedicated tests below exercise it.
    monkeypatch.setattr(service, "verify_lake_metadata_bundle", Mock())
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

    Before #1893 this also patched ``availability.ensure_range``, the policy
    store's fetch-the-missing-range entry point. That function is gone with
    the store, so the client constructor is now the only way a run could
    reach Polygon per-run, and patching it is the whole guard.
    """
    from app.services import polygon_client

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("lake-mode run must not stage from Polygon")

    monkeypatch.setattr(polygon_client, "PolygonClientService", refuse)


def _read_config(workspace_root: Path) -> dict[str, Any]:
    return json.loads((workspace_root / "workspace" / "project" / "config.json").read_text(encoding="utf-8"))


def _read_manifest(workspace_root: Path) -> dict[str, Any]:
    return json.loads((workspace_root / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_lake_run_reads_an_admitted_private_snapshot(
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
    # A lake run must not shell out for image metadata either — the
    # lake's own Phase-0 bootstrap owns those files.
    monkeypatch.setattr(
        service,
        "stage_lean_metadata_from_image",
        lambda *_a, **_k: pytest.fail("lake-mode run must not extract image metadata per run"),
    )

    result = await service.run_trusted_sample(_request("lake-mode-run"))

    workspace_data = result.workspace_root / "workspace" / "data"
    assert len(list(workspace_data.rglob("*.zip"))) == 5

    config = _read_config(result.workspace_root)
    assert config["data-folder"] == "/lean-run/data"

    assert [r.mount_lake_read_only for r in orchestrator.launch_requests] == [False]
    assert orchestrator.launch_requests[0].read_only_workspace_data

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
    from app.data_lake import ensure_data as pipeline
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    # ``exist_ok`` because tests/conftest.py's autouse
    # ``_isolate_data_lake_write_root`` already created this root under the
    # same tmp_path — the flag is on by default now, so every test gets an
    # isolated lake whether it asked for one or not. What this test needs is
    # that the lake is *empty*, which it is either way.
    (write_root / lake_subpath("raw")).mkdir(parents=True, exist_ok=True)  # a lake with nothing in it
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))

    prepare = AsyncMock(return_value=SimpleNamespace(overall_status="failed", failures=[]))
    monkeypatch.setattr(pipeline, "ensure_data", prepare)
    with pytest.raises(service.LeanSidecarServiceError, match="lake_preparation_incomplete"):
        await service.run_trusted_sample(_request("reusable-run-id"))

    assert not (orchestrator.artifacts_root / "reusable-run-id").exists()

    # The same id now works once the lake can serve it — the refusal
    # was about the lake, and nothing about the id was consumed.
    seed_lake_window(write_root / lake_subpath("raw"), SYMBOL, WINDOW)
    result = await service.run_trusted_sample(_request("reusable-run-id"))
    assert result.workspace_root.exists()
    prepare.assert_awaited_once()


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
    monkeypatch.setattr(service, "stage_lean_metadata_from_image", lambda *_a, **_k: None)

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
async def test_compatibility_fixture_reads_committed_lake_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    seeded_lake_catalog: dict,
) -> None:
    from app.config import settings
    from app.data_lake.admission import LakeAdmissionError
    from app.engine.data.policy_store import snapshot_minute_trade_zips
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "writer"
    lake_root = write_root / lake_subpath("raw")
    seed_lake_window(lake_root, SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(service, "stage_lean_metadata_from_image", lambda *_a, **_k: None)
    receipt = await asyncio.to_thread(
        snapshot_minute_trade_zips, [lake_root], symbol=SYMBOL,
        start=DAY_ONE, end=DAY_TWO, adjusted=False, session="regular",
    )
    request = _request("committed-compatibility")
    request = replace(request, data_policy=replace(
        request.data_policy, provider_kind="fixture", fixture_id=receipt["fixture_id"],
        fixture_sha256=receipt["fixture_sha256"],
    ))

    result = await service.run_trusted_sample(request)

    assert len(orchestrator.launch_requests) == 1

    assert not orchestrator.launch_requests[0].mount_lake_read_only
    manifest = _read_manifest(result.workspace_root)
    assert manifest["data_policy"]["fixture_sha256"] == receipt["fixture_sha256"]
    for file in receipt["files"]:
        staged = result.workspace_root / "workspace" / "data" / file["path"]
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == file["sha256"]

    # The thread boundary must not turn a frozen hash into an admission bypass.
    seeded_lake_catalog.clear()
    request = replace(request, run_id="uncommitted-compatibility")
    with pytest.raises(LakeAdmissionError, match="committed"):
        await service.run_trusted_sample(request)
    assert len(orchestrator.launch_requests) == 1


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("damage", [
    "quote_receipts", "quote_files", "metadata_scope", "both", "both_trees",
    "trade_files", "daily_file", "required_metadata", "factor_receipt", "map_receipt", "map_bytes",
    "map_window", "map_unreadable",
])
async def test_lake_run_repairs_legacy_inputs_from_verified_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None,
    damage: str,
) -> None:
    from app.data_lake import ensure_data as pipeline
    from app.data_lake.adjustment_versions import companion_path
    from app.data_lake.polygon_fetcher import PolygonBar
    from app.data_lake.types import DataRunSpec, trading_date_to_calendar_anchor_ms
    from app.services import lean_sidecar_service as service
    from tests._helpers.fake_lake_catalog import install_fake_catalog, mock_launcher, point_lake_writer_at_tmp

    point_lake_writer_at_tmp(tmp_path, monkeypatch)
    catalog = install_fake_catalog(monkeypatch)
    mock_launcher()
    fetches: list[date] = []

    async def bars(*, start: date, **_kwargs: Any) -> list[PolygonBar]:
        fetches.append(start)
        return [PolygonBar(t_ms=session_open_ms_utc(start), open=100, high=101, low=99,
                           close=100, volume=100, vwap=100, n=1)]

    monkeypatch.setattr(pipeline, "fetch_minute_trade_aggregates", bars)
    monkeypatch.setattr(pipeline, "fetch_splits", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline, "fetch_dividends", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline, "fetch_ticker_events", AsyncMock(return_value=[]))
    spec = DataRunSpec(
        request_id=uuid4(), run_type="lean_lab", symbols=[SYMBOL],
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(DAY_ONE),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(DAY_TWO),
        data_types=["trade", "quote"], price_adjustment_mode="polygon_split_adjusted",
        include_factor_files=True, include_map_files=True,
        lean_image_digest=service.PINNED_LEAN_IMAGE_DIGEST,
    )
    if damage == "both_trees":
        raw = await pipeline.ensure_data(spec.model_copy(update={"price_adjustment_mode": "raw"}))
        assert raw.overall_status == "complete"
    captured = await pipeline.ensure_data(spec)
    assert captured.overall_status == "complete", captured.failures
    root = Path(captured.lean_data_root_path)
    quotes = [r for r in captured.artifacts if r.data_type == "quote"]
    assert len(quotes) == 2
    if damage in {"quote_receipts", "quote_files", "both"}:
        for record in quotes:
            companion_path(root / record.file_path).unlink()
            catalog.rows[record.id]["corporate_action_version"] = None
            if damage == "quote_files":
                (root / record.file_path).unlink()
    if damage in {"metadata_scope", "both", "both_trees"}:
        for row in catalog.rows.values():
            if row["artifact_kind"] == "metadata":
                row["price_adjustment_mode"] = None
    if damage in {"trade_files", "daily_file", "required_metadata"}:
        target = {
            "trade_files": "equity/usa/minute/spy/20260105_trade.zip",
            "daily_file": "equity/usa/daily/spy.zip",
            "required_metadata": "market-hours/market-hours-database.json",
        }[damage]
        (root / target).unlink()
    if damage in {"factor_receipt", "map_receipt"}:
        kind = "factor_file" if damage == "factor_receipt" else "map_file"
        for row in catalog.rows.values():
            if row["artifact_kind"] == kind:
                row["status"] = "failed"
    if damage == "map_bytes":
        (root / "equity/usa/map_files/spy.csv").write_text("uncommitted replacement\n")
    if damage in {"map_window", "map_unreadable"}:
        # These bytes have a valid catalog receipt, so the semantic map
        # refusal (not the admission hash check) must trigger preparation.
        payload = (b"20260105,spy,nyse\n20260105,spy,nyse\n" if damage == "map_window"
                   else b"not a map file\n")
        (root / "equity/usa/map_files/spy.csv").write_bytes(payload)
        for row in catalog.rows.values():
            if row["artifact_kind"] == "map_file":
                row.update(file_sha256=hashlib.sha256(payload).hexdigest(), file_size_bytes=len(payload))
    if damage == "both_trees":
        await service.run_trusted_sample(_request("repair-raw-metadata"))
    fetches.clear()
    if damage in {"map_window", "map_unreadable"}:
        ensure = pipeline.ensure_data

        async def prepare_map(repair_spec: DataRunSpec) -> Any:
            assert repair_spec.include_map_files
            assert not (orchestrator.artifacts_root / "repair-adjusted-quotes").exists()
            assert orchestrator.launch_requests == []
            return await ensure(repair_spec)

        monkeypatch.setattr(pipeline, "ensure_data", prepare_map)

    # This must exercise the real writer before acquiring the run's adjusted
    # read lock; nesting ensure_data's capture lock would deadlock the run.
    result = await asyncio.wait_for(
        service.run_trusted_sample(_request("repair-adjusted-quotes", adjusted=True)), timeout=5,
    )

    assert len(orchestrator.launch_requests) == (2 if damage == "both_trees" else 1)
    assert fetches == ([DAY_ONE] if damage == "trade_files" else []), "reuse admitted trade bars"
    assert _read_manifest(result.workspace_root)["staged_data"]["corporate_action_versions"] == captured.corporate_action_versions
    assert all(companion_path(root / record.file_path).exists() for record in quotes)
    if damage in {"map_window", "map_unreadable"}:
        from app.data_lake.map_files import map_file_coverage

        assert map_file_coverage((root / "equity/usa/map_files/spy.csv").read_bytes()) == (DAY_ONE, DAY_TWO)


@pytest.mark.asyncio
async def test_cancellation_stops_lake_preparation_before_launch(
    monkeypatch: pytest.MonkeyPatch, orchestrator: SimpleNamespace,
) -> None:
    """A job's cancellation flag interrupts a pending provider/capture await."""
    from app.data_lake import ensure_data as pipeline
    from app.services import lean_sidecar_service as service

    entered = asyncio.Event()
    stopped = asyncio.Event()
    cancelled = False

    async def prepare(_spec: Any) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(service, "_resolve_lake_artifacts_or_refuse", AsyncMock(
        side_effect=service.LeanSidecarServiceError("lake_incomplete_quote_coverage: SPY"),
    ))
    monkeypatch.setattr(service, "_CANCEL_ACK_POLL_SECONDS", 0.005)
    monkeypatch.setattr(pipeline, "ensure_data", prepare)
    task = asyncio.create_task(service.run_trusted_sample(
        _request("cancel-preparation"), cancel_requested=lambda: cancelled,
    ))
    await entered.wait()
    cancelled = True
    with pytest.raises(service.LeanRunCancelled):
        await asyncio.wait_for(task, timeout=0.2)
    assert stopped.is_set()
    assert not orchestrator.launch_requests
    assert not (orchestrator.artifacts_root / "cancel-preparation").exists()


@pytest.mark.asyncio
async def test_an_adjusted_request_also_reaches_the_lake_with_the_flag_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None,
) -> None:
    """Retain the adjusted root under its basis lock, without a raw fallback."""
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    (write_root / lake_subpath("polygon_split_adjusted")).mkdir(parents=True, exist_ok=True)
    seed_lake_window(write_root / lake_subpath("polygon_split_adjusted"), SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))

    resolved: list[object] = []
    real_resolve = service._resolve_lake_artifacts_or_refuse

    async def _spy(request):
        outcome = await real_resolve(request)
        resolved.append(outcome)
        return outcome

    monkeypatch.setattr(service, "_resolve_lake_artifacts_or_refuse", _spy)

    from app.data_lake.adjustment_versions import current_snapshot_path, current_version
    from app.utils.advisory_lock import try_advisory_file_lock

    root = write_root / lake_subpath("polygon_split_adjusted")
    launch = service.post_launch

    async def check_basis_is_locked(request: LaunchRequest) -> LaunchResponse:
        with try_advisory_file_lock(root / current_snapshot_path(SYMBOL)) as acquired:
            assert not acquired, "an adjusted LEAN run must exclude a concurrent cache rebuild"
        return await launch(request)

    monkeypatch.setattr(service, "post_launch", check_basis_is_locked)
    result = await service.run_trusted_sample(_request("adjusted-reaches-the-lake", adjusted=True))
    assert _read_manifest(result.workspace_root)["staged_data"]["corporate_action_versions"] == {
        SYMBOL: current_version(root, SYMBOL),
    }
    with try_advisory_file_lock(root / current_snapshot_path(SYMBOL)) as acquired:
        assert acquired, "the basis lock must release after the run"


    assert resolved, "an adjusted run never consulted the lake"
    assert orchestrator.launch_requests
    assert not orchestrator.launch_requests[-1].mount_lake_read_only


@pytest.mark.asyncio
async def test_a_raw_request_does_reach_the_lake_with_the_flag_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None,
) -> None:
    """A raw run retains bytes from its own root, not the adjusted root."""
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    (write_root / lake_subpath("raw")).mkdir(parents=True, exist_ok=True)
    seed_lake_window(write_root / lake_subpath("raw"), SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))

    await service.run_trusted_sample(_request("raw-reaches-the-lake", adjusted=False))

    assert orchestrator.launch_requests
    assert not orchestrator.launch_requests[-1].mount_lake_read_only


@pytest.mark.asyncio
async def test_retained_inputs_refuse_stale_launcher_before_creating_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _launcher_is_stale: None,
) -> None:
    """An old launcher must not silently drop the read-only data flag."""
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    seed_lake_window(write_root / lake_subpath("raw"), SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))

    with pytest.raises(service.LeanSidecarServiceError, match="workspace_data_read_only_unsupported_by_launcher"):
        await service.run_trusted_sample(_request("stale-launcher-run"))
    assert not (orchestrator.artifacts_root / "stale-launcher-run").exists()
    assert not orchestrator.launch_requests


@pytest.mark.asyncio
async def test_unreachable_launcher_refuses_before_the_workspace_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
) -> None:
    """A launcher that is down must fail the run without consuming the ID.

    The transport failure propagates as ``LauncherUnreachable`` for the
    router's existing 503 mapping. Preflight happens before creating the
    workspace, so an outage leaves the run ID reusable.
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

    seed_lake_corporate_actions(lake_root, SYMBOL, factor_rows="20260105,1,1\n")
    first = await service.run_trusted_sample(_request("factor-snapshot-a"))
    manifest_a = _read_manifest(first.workspace_root)

    seed_lake_corporate_actions(lake_root, SYMBOL, factor_rows="20260105,0.5,1\n")
    second = await service.run_trusted_sample(_request("factor-snapshot-b"))
    manifest_b = _read_manifest(second.workspace_root)

    assert manifest_a["staged_data"]["factor_files"], "factor file present in the lake must be hashed"
    assert manifest_a["staged_data"]["map_files"], "map file present in the lake must be hashed"
    assert manifest_a["input_snapshot_sha256"] != manifest_b["input_snapshot_sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["replace", "delete"])
@pytest.mark.parametrize("relative, manifest_group", [
    ("equity/usa/factor_files/spy.csv", "factor_files"),
    ("equity/usa/daily/spy.zip", "bar_zips"),
])
async def test_retained_inputs_changed_during_launch_keep_original_receipt_and_refuse_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orchestrator: SimpleNamespace,
    _polygon_is_off_limits: None, _launcher_supports_lake_mount: None,
    relative: str, manifest_group: str, mutation: str,
) -> None:
    """A mid-run replacement must not be hashed as if LEAN consumed it."""
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    lake_root = write_root / lake_subpath("raw")
    seed_lake_window(lake_root, SYMBOL, WINDOW)
    seed_lake_corporate_actions(lake_root, SYMBOL, factor_rows="20260105,1,1\n")
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    path = lake_root / relative
    original_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    original_launch = service.post_launch
    persist = AsyncMock()
    monkeypatch.setattr(service, "_persist_completed_run", persist)

    async def replace_during_launch(request: LaunchRequest) -> LaunchResponse:
        retained = orchestrator.artifacts_root / request.run_id / "workspace/data" / relative
        assert hashlib.sha256(retained.read_bytes()).hexdigest() == original_digest
        response = await original_launch(request)
        if mutation == "replace":
            replacement = retained.with_suffix(".replacement")
            replacement.write_bytes(b"a later file generation")
            replacement.replace(retained)
        else:
            retained.unlink()
        return response.model_copy(update={"exit_code": 0, "is_clean": True})

    monkeypatch.setattr(service, "post_launch", replace_during_launch)
    request = _request("replaced-input")
    with pytest.raises(service.LeanSidecarServiceError, match="data_snapshot_changed"):
        await service.run_trusted_sample(request)
    manifest = _read_manifest(orchestrator.artifacts_root / request.run_id)
    receipt = next(item for item in manifest["staged_data"][manifest_group] if item["path_in_workspace"] == relative)
    assert receipt["sha256"] == original_digest
    assert any("data_snapshot_changed" in note for note in manifest["notes"])
    assert "is_clean=False" in manifest["notes"]
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_interest_rate_file_moves_the_input_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _polygon_is_off_limits: None,
    _launcher_supports_lake_mount: None,
) -> None:
    """#1859 review fix (CodeRabbit-P1): a changed interest-rate CSV must
    change ``input_snapshot_sha256``, the same way a factor-file change
    does above.

    LEAN reads the interest-rate file and it changes computed statistics
    (Sharpe and relatives). Before this fix, ``_build_manifest`` discarded
    the path entirely — the rate could change while the reproducibility
    fingerprint stayed constant, exactly the gap the manifest exists to
    close.
    """
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    lake_root = write_root / lake_subpath("raw")
    seed_lake_window(lake_root, SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))

    seed_lake_interest_rate(lake_root, rows="date,rate\n20260105,0.0525\n")
    first = await service.run_trusted_sample(_request("interest-rate-snapshot-a"))
    manifest_a = _read_manifest(first.workspace_root)

    seed_lake_interest_rate(lake_root, rows="date,rate\n20260105,0.0475\n")
    second = await service.run_trusted_sample(_request("interest-rate-snapshot-b"))
    manifest_b = _read_manifest(second.workspace_root)

    assert manifest_a["staged_data"]["interest_rate_database"], (
        "interest-rate file present in the lake must be hashed"
    )
    assert manifest_a["input_snapshot_sha256"] != manifest_b["input_snapshot_sha256"]


@pytest.mark.asyncio
async def test_lake_run_preserves_its_rate_input_for_statistics_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None,
) -> None:
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "writer"
    lake_root = write_root / lake_subpath("raw")
    seed_lake_window(lake_root, SYMBOL, WINDOW)
    source = seed_lake_interest_rate(lake_root)
    original = source.read_bytes()
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))

    result = await service.run_trusted_sample(_request("lake-rate-verification"))

    recorded = _read_manifest(result.workspace_root)["staged_data"]["interest_rate_database"]
    verification_input = result.workspace_root / "workspace" / "data" / recorded["path_in_workspace"]
    assert verification_input.read_bytes() == original
    assert hashlib.sha256(verification_input.read_bytes()).hexdigest() == recorded["sha256"]
    seed_lake_interest_rate(lake_root, rows="date,rate\n20260105,0.01\n")
    assert verification_input.read_bytes() == original, "historical verification must retain this run's input"


@pytest.mark.asyncio
async def test_rate_copy_failure_leaves_run_id_reusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None,
) -> None:
    """A failed input read or disk copy cannot strand a pre-launch workspace."""
    from app.config import settings
    from app.services import lean_sidecar_service as service

    root = tmp_path / "writer"
    lake = root / lake_subpath("raw")
    seed_lake_window(lake, SYMBOL, WINDOW)
    seed_lake_interest_rate(lake)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(root))
    preserve = service._retain_lake_inputs

    def fail_copy(*args: Any) -> None:
        preserve(*args)
        raise OSError("rate copy interrupted")

    monkeypatch.setattr(service, "_retain_lake_inputs", fail_copy)
    with pytest.raises(service.LeanSidecarServiceError, match=r"data_snapshot_unavailable.*rate copy interrupted"):
        await service.run_trusted_sample(_request("retry-rate-copy"))
    assert not (orchestrator.artifacts_root / "retry-rate-copy").exists()
    assert not orchestrator.launch_requests
    monkeypatch.setattr(service, "_retain_lake_inputs", preserve)
    await service.run_trusted_sample(_request("retry-rate-copy"))
    assert len(orchestrator.launch_requests) == 1


@pytest.mark.asyncio
async def test_lake_metadata_generation_is_pinned_through_launch_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None,
) -> None:
    """A concurrent canonical metadata publication waits until evidence is saved."""
    from app.config import settings
    from app.data_lake.metadata_bundle import metadata_bundle_lock
    from app.services import lean_sidecar_service as service

    root = tmp_path / "writer"
    lake = root / lake_subpath("raw")
    seed_lake_window(lake, SYMBOL, WINDOW)
    rate = seed_lake_interest_rate(lake)
    original = rate.read_bytes()
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(root))
    pending: list[asyncio.Task[None]] = []
    published = asyncio.Event()
    post_launch = service.post_launch

    async def publish() -> None:
        async with metadata_bundle_lock(lake):
            seed_lake_interest_rate(lake, rows="date,rate\n20260105,0.01\n")
            published.set()

    async def launch(request: LaunchRequest) -> LaunchResponse:
        pending.append(asyncio.create_task(publish()))
        await asyncio.sleep(0.02)
        assert not published.is_set(), "LEAN's mounted rate generation changed during execution"
        return await post_launch(request)

    monkeypatch.setattr(service, "post_launch", launch)
    try:
        result = await service.run_trusted_sample(_request("pinned-rate-generation"))
        recorded = _read_manifest(result.workspace_root)["staged_data"]["interest_rate_database"]
        retained = result.workspace_root / "workspace" / "data" / recorded["path_in_workspace"]
        assert retained.read_bytes() == original
        assert recorded["sha256"] == hashlib.sha256(original).hexdigest()
    finally:
        await asyncio.gather(*pending)
    assert published.is_set()
    assert rate.read_bytes() != original


@pytest.mark.asyncio
async def test_absent_interest_rate_file_still_produces_a_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: SimpleNamespace,
    _polygon_is_off_limits: None,
    _launcher_supports_lake_mount: None,
) -> None:
    """The optional file's absence must not break manifest construction —
    unlike market-hours/symbol-properties, which require_lake_metadata
    already refuses the run over."""
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    lake_root = write_root / lake_subpath("raw")
    seed_lake_window(lake_root, SYMBOL, WINDOW)  # no seed_lake_interest_rate call
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))

    result = await service.run_trusted_sample(_request("interest-rate-absent"))
    manifest = _read_manifest(result.workspace_root)

    assert manifest["staged_data"]["interest_rate_database"] is None


@pytest.mark.asyncio
async def test_adjusted_run_can_cancel_while_waiting_for_a_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orchestrator: SimpleNamespace,
) -> None:
    from app.config import settings
    from app.data_lake.adjustment_versions import capture_lock
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "lean-data-writer"
    lake_root = write_root / lake_subpath("polygon_split_adjusted")
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    cancelled = False
    async with capture_lock(lake_root, [SYMBOL], 1):
        task = asyncio.create_task(service.run_trusted_sample(
            _request("cancel-during-capture", adjusted=True), cancel_requested=lambda: cancelled,
        ))
        await asyncio.sleep(0)
        cancelled = True
        with pytest.raises(service.LeanRunCancelled):
            await asyncio.wait_for(task, timeout=1)
    assert orchestrator.launch_requests == []
    assert not (orchestrator.artifacts_root / "cancel-during-capture").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("relative, group", [
    ("equity/usa/factor_files/spy.csv", "factor_files"),
    ("equity/usa/daily/spy.zip", "bar_zips"),
])
async def test_lean_consumes_retained_inputs_during_shared_lake_aba(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None, relative: str, group: str,
) -> None:
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "writer"
    lake_root = write_root / lake_subpath("raw")
    seed_lake_window(lake_root, SYMBOL, WINDOW)
    seed_lake_corporate_actions(lake_root, SYMBOL, factor_rows="20260105,1,1\n")
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    path = lake_root / relative
    original = path.read_bytes()
    consumed: list[bytes] = []
    original_launch = service.post_launch

    async def replace_read_restore(request: LaunchRequest) -> LaunchResponse:
        root = orchestrator.artifacts_root / request.run_id
        config = _read_config(root)
        actual_root = lake_root if config["data-folder"] == CONTAINER_LAKE_DATA_MOUNT else root / "workspace/data"
        path.write_bytes(b"generation-B")
        consumed.append((actual_root / relative).read_bytes())
        path.write_bytes(original)

        def catalog_unavailable(**_kwargs: Any) -> None:
            from app.data_lake.catalog_client import CatalogUnavailableError
            raise CatalogUnavailableError("catalog lost after capture")

        monkeypatch.setattr(service, "resolve_lake_artifacts", catalog_unavailable)
        return await original_launch(request)

    monkeypatch.setattr(service, "post_launch", replace_read_restore)
    result = await service.run_trusted_sample(_request("aba-input"))
    manifest = _read_manifest(result.workspace_root)
    receipt = next(item for item in manifest["staged_data"][group] if item["path_in_workspace"] == relative)
    assert consumed == [original]
    assert receipt["sha256"] == hashlib.sha256(consumed[0]).hexdigest()


@pytest.mark.asyncio
async def test_capture_rejects_uncommitted_generation_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None,
) -> None:
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "writer"
    lake_root = write_root / lake_subpath("raw")
    seed_lake_window(lake_root, SYMBOL, WINDOW)
    seed_lake_corporate_actions(lake_root, SYMBOL, factor_rows="20260105,1,1\n")
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    resolve = service._resolve_lake_artifacts_or_refuse
    original_launch = service.post_launch

    async def promote_uncommitted(request: Any) -> Any:
        artifacts = await resolve(request)
        artifacts.factor_file_paths[0].write_bytes(b"20260105,0.5,1\n")
        return artifacts

    async def commit_after_launch(request: LaunchRequest) -> LaunchResponse:
        seed_lake_corporate_actions(lake_root, SYMBOL, factor_rows="20260105,0.5,1\n")
        return await original_launch(request)

    monkeypatch.setattr(service, "_resolve_lake_artifacts_or_refuse", promote_uncommitted)
    monkeypatch.setattr(service, "post_launch", commit_after_launch)
    with pytest.raises(service.LeanSidecarServiceError, match="data_snapshot_unavailable"):
        await service.run_trusted_sample(_request("uncommitted-capture"))
    assert not orchestrator.launch_requests
    assert not (orchestrator.artifacts_root / "uncommitted-capture").exists()


@pytest.mark.asyncio
async def test_prelaunch_snapshot_failure_leaves_run_id_reusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None,
) -> None:
    from app.config import settings
    from app.services import lean_sidecar_service as service

    write_root = tmp_path / "writer"
    seed_lake_window(write_root / lake_subpath("raw"), SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    snapshot = service._snapshot_staged_data

    def unavailable(**_kwargs: Any) -> None:
        raise OSError("input disappeared during capture")

    monkeypatch.setattr(service, "_snapshot_staged_data", unavailable)
    with pytest.raises(service.LeanSidecarServiceError, match="data_snapshot_unavailable"):
        await service.run_trusted_sample(_request("retry-capture"))
    assert not (orchestrator.artifacts_root / "retry-capture").exists()
    assert not orchestrator.launch_requests
    monkeypatch.setattr(service, "_snapshot_staged_data", snapshot)
    await service.run_trusted_sample(_request("retry-capture"))
    assert len(orchestrator.launch_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["image", "root", "mode", "hash", "missing"])
async def test_retained_inputs_preserve_metadata_identity_and_image_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orchestrator: SimpleNamespace,
    _launcher_supports_lake_mount: None, damage: str,
) -> None:
    from app.config import active_root_id, settings
    from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST
    from app.lean_sidecar.lake_mount import verify_lake_metadata_bundle
    from app.services import lean_sidecar_service as service
    from tests._helpers.lake_fixture import seed_lean_metadata_receipt

    root = tmp_path / "writer"
    lake = root / lake_subpath("raw")
    seed_lake_window(lake, SYMBOL, WINDOW)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(root))
    receipt_path = seed_lean_metadata_receipt(
        lake, data_root_id=active_root_id(), price_adjustment_mode="raw",
        lean_image_digest=PINNED_LEAN_IMAGE_DIGEST,
    )
    original = receipt_path.read_bytes()
    receipt = json.loads(original)
    if damage == "image":
        receipt["lean_image_digest"] = "sha256:" + "f" * 64
    elif damage == "root":
        receipt["data_root_id"] = str(uuid4())
    elif damage == "mode":
        receipt["price_adjustment_mode"] = "polygon_split_adjusted"
    elif damage == "hash":
        receipt["files"]["market_hours"]["sha256"] = "f" * 64
    receipt_path.write_text(json.dumps(receipt))
    if damage == "missing":
        receipt_path.unlink()
    monkeypatch.setattr(service, "verify_lake_metadata_bundle", verify_lake_metadata_bundle)

    with pytest.raises(service.LeanSidecarServiceError, match="lake_metadata_receipt_invalid"):
        await service.run_trusted_sample(_request("metadata-proof"))
    assert not orchestrator.launch_requests
    assert not (orchestrator.artifacts_root / "metadata-proof").exists()

    receipt_path.write_bytes(original)
    await service.run_trusted_sample(_request("metadata-proof"))
    assert len(orchestrator.launch_requests) == 1
