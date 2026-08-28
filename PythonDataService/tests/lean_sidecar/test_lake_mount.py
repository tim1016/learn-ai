"""The LEAN sidecar's read-only data-lake mount (flag-gated, #1834).

Four claims are pinned here, all at the config-rendering / staging seam
— no container is ever launched:

1. The mount the runner renders is read-only, and there is no way to
   construct a writable one.
2. With the flag off, the rendered podman argv and ``config.json`` are
   byte-identical to what they were before the lake existed.
3. Config rendering points LEAN at the mounted lake subtree.
4. On identical fixture lake artifacts, the bar stream the sidecar
   hands LEAN and the bar stream the Python LEAN readers produce are
   the same bars off the same bytes.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from app.data_lake.derived_quote import build_minute_quote_zip_bytes
from app.data_lake.lean_writer import build_minute_trade_zip_bytes
from app.engine.data.lean_format import LeanMinuteDataReader
from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.lake_mount import (
    CONTAINER_LAKE_DATA_MOUNT,
    LAKE_SUBDIR,
    LAKE_VOLUME_HOST_PATH_ENV,
    LakeBarStream,
    LakeMount,
    LakeMountError,
    data_plane_lake_root,
    lake_metadata_paths,
    lake_mount_enabled,
    launcher_host_lake_root,
    read_lake_bar_stream,
)
from app.lean_sidecar.launcher.models import LaunchRequest
from app.lean_sidecar.launcher.service import LaunchRejectedError, launch
from app.lean_sidecar.lean_config import CONTAINER_DATA_FOLDER, LeanConfig
from app.lean_sidecar.runner import (
    CONTAINER_WORKSPACE_MOUNT,
    RunnerConfigurationError,
    build_command,
)
from app.lean_sidecar.workspace import SymbolValidationError, resolve_workspace
from tests._helpers.lake_fixture import (
    seed_lake_metadata,
    seed_lake_minute_day,
    seed_lake_window,
    to_lake_bars,
)
from tests._helpers.lean_store import make_minute_bars

DUMMY_DIGEST = "sha256:0000000000000000000000000000000000000000000000000000000000000003"
DAY_ONE = date(2026, 1, 5)
DAY_TWO = date(2026, 1, 6)
WINDOW = [DAY_ONE, DAY_TWO]


@pytest.fixture
def _allow_dummy_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the image allow-list so argv assertions can run.

    Mirrors ``tests/lean_sidecar/test_runner.py`` — the runner imports
    the set at module load, so both bindings are patched.
    """
    from app.lean_sidecar import runner

    monkeypatch.setattr(sidecar_config, "ALLOWED_IMAGE_DIGESTS", frozenset({DUMMY_DIGEST}))
    monkeypatch.setattr(runner, "ALLOWED_IMAGE_DIGESTS", frozenset({DUMMY_DIGEST}))


@pytest.fixture
def fixture_lake(tmp_path: Path) -> Path:
    """A complete, runnable fixture lake: 2 days of SPY + daily + metadata."""
    lake_root = tmp_path / "lean-data" / LAKE_SUBDIR
    seed_lake_window(lake_root, "SPY", WINDOW)
    return lake_root


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _volume_args(argv: tuple[str, ...]) -> list[str]:
    """Every value token that followed a ``-v`` flag, in argv order."""
    return [argv[i + 1] for i, token in enumerate(argv) if token == "-v"]


class TestReadOnlyMountIsRendered:
    """AC: the mount is read-only — asserted on the rendered spec."""

    def test_rendered_lake_volume_is_read_only(
        self,
        tmp_artifacts_root: Path,
        fixture_lake: Path,
        _allow_dummy_digest: None,
    ) -> None:
        workspace = resolve_workspace("run_lake_ro", tmp_artifacts_root)
        workspace.ensure_layout()

        plan = build_command(
            workspace,
            DUMMY_DIGEST,
            lake_mount=LakeMount(host_lake_root=fixture_lake),
        )

        volumes = _volume_args(plan.argv)
        assert volumes == [
            f"{workspace.workspace_dir}:{CONTAINER_WORKSPACE_MOUNT}:rw",
            f"{fixture_lake}:{CONTAINER_LAKE_DATA_MOUNT}:ro",
        ]
        lake_volume = volumes[1]
        assert lake_volume.endswith(":ro")
        assert not lake_volume.endswith(":rw")

    def test_lake_mount_has_no_writable_form(self, fixture_lake: Path) -> None:
        """A caller cannot ask for a writable lake: mode is not an input."""
        mount = LakeMount(host_lake_root=fixture_lake)

        assert mount.mode == "ro"
        assert mount.container_target == CONTAINER_LAKE_DATA_MOUNT
        with pytest.raises(TypeError):
            LakeMount(host_lake_root=fixture_lake, mode="rw")  # type: ignore[call-arg]

    def test_missing_lake_root_refuses_the_launch(
        self,
        tmp_artifacts_root: Path,
        tmp_path: Path,
        _allow_dummy_digest: None,
    ) -> None:
        """Podman would create the missing source as an empty directory."""
        workspace = resolve_workspace("run_lake_absent", tmp_artifacts_root)
        workspace.ensure_layout()

        with pytest.raises(RunnerConfigurationError, match="lake root does not exist"):
            build_command(
                workspace,
                DUMMY_DIGEST,
                lake_mount=LakeMount(host_lake_root=tmp_path / "never-mounted"),
            )


class TestFlagOffIsUnchanged:
    """AC: with the flag off, the current staging path is unchanged."""

    def test_argv_without_lake_mount_has_only_the_workspace_volume(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        workspace = resolve_workspace("run_no_lake", tmp_artifacts_root)
        workspace.ensure_layout()

        plan = build_command(workspace, DUMMY_DIGEST)

        assert _volume_args(plan.argv) == [f"{workspace.workspace_dir}:{CONTAINER_WORKSPACE_MOUNT}:rw"]
        assert CONTAINER_LAKE_DATA_MOUNT not in " ".join(plan.argv)

    def test_default_config_still_renders_the_workspace_data_folder(self) -> None:
        assert LeanConfig().to_payload()["data-folder"] == CONTAINER_DATA_FOLDER
        assert f"{CONTAINER_WORKSPACE_MOUNT}/data" == CONTAINER_DATA_FOLDER

    def test_flag_ships_default_off(self) -> None:
        """Asserted on the field default, not on the ambient environment.

        A developer with ``DATA_LAKE_ENABLED=true`` in their ``.env``
        should not fail this; what this slice promises is that the
        *shipped* default is off.
        """
        from app.config import Settings

        assert Settings.model_fields["DATA_LAKE_ENABLED"].default is False

    def test_lake_mode_follows_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", False)
        assert lake_mount_enabled() is False

        monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
        assert lake_mount_enabled() is True

    def test_launch_request_does_not_ask_for_the_lake_by_default(self) -> None:
        request = LaunchRequest(
            run_id="run_default_flags",
            image_digest=DUMMY_DIGEST,
            cpus=2.0,
            memory_mb=1024,
            pids_limit=256,
            wall_clock_timeout_s=60,
            workspace_max_mb=256,
            log_tail_bytes=4096,
        )
        assert request.mount_lake_read_only is False


class TestConfigPointsAtTheMountedLake:
    """AC: config rendering points LEAN at the mounted lake subtree."""

    def test_lake_mode_config_data_folder_is_the_mount_target(self) -> None:
        payload = LeanConfig(data_folder=CONTAINER_LAKE_DATA_MOUNT).to_payload()

        assert payload["data-folder"] == CONTAINER_LAKE_DATA_MOUNT
        # Results and object store stay in the read-write workspace; only
        # the data folder moves to the read-only mount.
        assert payload["results-destination-folder"].startswith(CONTAINER_WORKSPACE_MOUNT)
        assert payload["object-store-root"].startswith(CONTAINER_WORKSPACE_MOUNT)

    def test_lake_mount_target_is_outside_the_workspace_mount(self) -> None:
        """Nesting under the workspace mount would shadow ``workspace/data``."""
        assert not CONTAINER_LAKE_DATA_MOUNT.startswith(f"{CONTAINER_WORKSPACE_MOUNT}/")
        assert CONTAINER_LAKE_DATA_MOUNT != CONTAINER_WORKSPACE_MOUNT


class TestLauncherResolvesTheHostPath:
    """The data plane states intent; the launcher owns the path."""

    def test_launch_request_carries_intent_not_a_path(self) -> None:
        fields = set(LaunchRequest.model_fields)

        assert "mount_lake_read_only" in fields
        assert not any("path" in name or "root" in name for name in fields)

    def test_launcher_refuses_a_lake_run_it_cannot_satisfy(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        workspace = resolve_workspace("run_lake_unconfigured", tmp_artifacts_root)
        workspace.ensure_layout()
        request = LaunchRequest(
            run_id="run_lake_unconfigured",
            image_digest=DUMMY_DIGEST,
            cpus=2.0,
            memory_mb=1024,
            pids_limit=256,
            wall_clock_timeout_s=60,
            workspace_max_mb=256,
            log_tail_bytes=4096,
            mount_lake_read_only=True,
        )

        with pytest.raises(LaunchRejectedError) as excinfo:
            launch(request, artifacts_root=tmp_artifacts_root, lake_root=None)

        assert excinfo.value.reason == "lake_mount_not_configured"

    def test_host_lake_root_comes_from_deploy_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(LAKE_VOLUME_HOST_PATH_ENV, raising=False)
        assert launcher_host_lake_root() is None

        volume = tmp_path / "learn_ai_lean_data"
        volume.mkdir()
        monkeypatch.setenv(LAKE_VOLUME_HOST_PATH_ENV, str(volume))
        assert launcher_host_lake_root() == volume.resolve() / LAKE_SUBDIR

    def test_data_plane_lake_root_matches_the_writers_lake_root(self) -> None:
        """Reader and writer must name the same directory.

        ``app.data_lake.ensure_data._lake_roots`` is the writer-side
        authority; this pins the sidecar's read view to it so the two
        cannot drift into pointing at different subdirectories of the
        same volume.
        """
        from uuid import uuid4

        from app.data_lake.ensure_data import _lake_roots
        from app.data_lake.types import DataRunSpec

        spec = DataRunSpec(
            request_id=uuid4(),
            run_type="lean_lab",
            symbols=["SPY"],
            start_trading_date=DAY_ONE,
            end_trading_date=DAY_TWO,
            lean_image_digest=DUMMY_DIGEST,
        )
        writer_lake_root, _staging_root = _lake_roots(spec)

        assert data_plane_lake_root() == writer_lake_root


class TestSameBytesAsThePythonReaders:
    """AC: the sidecar's bar stream matches the Python readers' stream."""

    def test_lake_bar_stream_equals_lean_reader_stream(self, fixture_lake: Path) -> None:
        stream = read_lake_bar_stream(
            lake_root=fixture_lake,
            symbol="SPY",
            start=DAY_ONE,
            end=DAY_TWO,
            session="regular",
        )
        reader = LeanMinuteDataReader([fixture_lake], session="regular")
        reader_bars = list(reader.iter_bars("SPY", DAY_ONE, DAY_TWO))

        sidecar_bars = [bar for _day, day_bars in stream.bars_by_date for bar in day_bars]
        assert sidecar_bars == reader_bars
        assert len(sidecar_bars) == 2 * 390

    def test_exposed_files_are_the_lake_files_themselves(self, fixture_lake: Path) -> None:
        """No copy, no re-encode: the sidecar exposes the lake's inodes."""
        stream = read_lake_bar_stream(
            lake_root=fixture_lake,
            symbol="SPY",
            start=DAY_ONE,
            end=DAY_TWO,
            session="regular",
        )

        assert [p.name for p in stream.trade_zip_paths] == [
            "20260105_trade.zip",
            "20260106_trade.zip",
        ]
        for path in stream.trade_zip_paths:
            assert path.is_relative_to(fixture_lake)
            # The bytes on disk are exactly what the lake writer produced.
            trading_date = date(int(path.name[:4]), int(path.name[4:6]), int(path.name[6:8]))
            expected = build_minute_trade_zip_bytes(
                "SPY",
                trading_date.strftime("%Y%m%d"),
                to_lake_bars(make_minute_bars("SPY", trading_date)),
            )
            assert path.read_bytes() == expected

    def test_quote_artifacts_are_reported_when_the_lake_has_them(self, fixture_lake: Path) -> None:
        stream = read_lake_bar_stream(
            lake_root=fixture_lake,
            symbol="SPY",
            start=DAY_ONE,
            end=DAY_TWO,
            session="regular",
        )

        assert [p.name for p in stream.quote_zip_paths] == [
            "20260105_quote.zip",
            "20260106_quote.zip",
        ]
        expected = build_minute_quote_zip_bytes(
            "SPY",
            DAY_ONE.strftime("%Y%m%d"),
            to_lake_bars(make_minute_bars("SPY", DAY_ONE)),
        )
        assert _sha256(stream.quote_zip_paths[0]) == hashlib.sha256(expected).hexdigest()

    def test_trade_only_lake_reports_no_quote_artifacts(self, tmp_path: Path) -> None:
        """``data_types=['trade']`` is a valid lake spec; absence is honest."""
        lake_root = tmp_path / LAKE_SUBDIR
        seed_lake_window(lake_root, "SPY", WINDOW, with_quote=False)

        stream = read_lake_bar_stream(
            lake_root=lake_root,
            symbol="SPY",
            start=DAY_ONE,
            end=DAY_TWO,
            session="regular",
        )

        assert stream.quote_zip_paths == ()
        assert len(stream.trade_zip_paths) == 2


class TestLakeCoverageFailsLoudly:
    """A gap in the lake must surface, never fall back to a Polygon fetch."""

    def test_empty_window_raises(self, tmp_path: Path) -> None:
        lake_root = tmp_path / LAKE_SUBDIR
        lake_root.mkdir(parents=True)

        with pytest.raises(LakeMountError, match="lake_window_empty"):
            read_lake_bar_stream(
                lake_root=lake_root,
                symbol="SPY",
                start=DAY_ONE,
                end=DAY_TWO,
                session="regular",
            )

    def test_missing_daily_artifact_raises(self, tmp_path: Path) -> None:
        lake_root = tmp_path / LAKE_SUBDIR
        seed_lake_minute_day(lake_root, "SPY", DAY_ONE)
        seed_lake_metadata(lake_root)

        with pytest.raises(LakeMountError, match="lake_missing_daily_artifact"):
            read_lake_bar_stream(
                lake_root=lake_root,
                symbol="SPY",
                start=DAY_ONE,
                end=DAY_TWO,
                session="regular",
            )

    def test_partial_coverage_returns_only_the_days_present(self, tmp_path: Path) -> None:
        lake_root = tmp_path / LAKE_SUBDIR
        seed_lake_window(lake_root, "SPY", [DAY_ONE])

        stream = read_lake_bar_stream(
            lake_root=lake_root,
            symbol="SPY",
            start=DAY_ONE,
            end=DAY_TWO,
            session="regular",
        )

        assert stream.trading_dates == (DAY_ONE,)

    def test_path_unsafe_symbol_is_rejected_before_any_path_join(self, fixture_lake: Path) -> None:
        with pytest.raises(SymbolValidationError):
            read_lake_bar_stream(
                lake_root=fixture_lake,
                symbol="../../etc/passwd",
                start=DAY_ONE,
                end=DAY_TWO,
                session="regular",
            )


class TestLakeMetadataPaths:
    def test_present_metadata_is_returned(self, fixture_lake: Path) -> None:
        market_hours, symbol_properties = lake_metadata_paths(fixture_lake)

        assert market_hours is not None and market_hours.is_relative_to(fixture_lake)
        assert symbol_properties is not None and symbol_properties.is_relative_to(fixture_lake)

    def test_absent_metadata_is_reported_as_absent_not_invented(self, tmp_path: Path) -> None:
        lake_root = tmp_path / LAKE_SUBDIR
        seed_lake_window(lake_root, "SPY", WINDOW, with_metadata=False)

        assert lake_metadata_paths(lake_root) == (None, None)


def test_lake_bar_stream_is_immutable(fixture_lake: Path) -> None:
    """The stream is a frozen record of what LEAN was given, not a buffer."""
    stream = read_lake_bar_stream(
        lake_root=fixture_lake,
        symbol="SPY",
        start=DAY_ONE,
        end=DAY_TWO,
        session="regular",
    )

    assert isinstance(stream, LakeBarStream)
    with pytest.raises(AttributeError):
        stream.lake_root = fixture_lake  # type: ignore[misc]
