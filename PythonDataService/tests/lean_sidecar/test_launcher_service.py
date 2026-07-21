"""Launcher service rejection tests.

The launcher's value is rejecting bad input before podman is invoked.
These tests assert each rejection class names the right ``reason`` so
the API contract is stable.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.launcher.models import (
    ExtractMetadataRequest,
    ExtractMetadataResponse,
    LaunchRequest,
    LaunchResponse,
)
from app.lean_sidecar.launcher.service import LaunchRejectedError, launch
from app.lean_sidecar.workspace import resolve_workspace

DUMMY_DIGEST = "sha256:0000000000000000000000000000000000000000000000000000000000000002"


def _make_request(run_id: str, digest: str = DUMMY_DIGEST) -> LaunchRequest:
    return LaunchRequest(
        run_id=run_id,
        image_digest=digest,
        cpus=2.0,
        memory_mb=1024,
        pids_limit=256,
        wall_clock_timeout_s=60,
        workspace_max_mb=256,
        log_tail_bytes=4096,
    )


class TestLaunchValidation:
    def test_pydantic_rejects_bad_run_id(self) -> None:
        with pytest.raises(ValidationError):
            _make_request("../escape")

    def test_pydantic_rejects_unpinned_image(self) -> None:
        with pytest.raises(ValidationError):
            _make_request("run_a", digest="quantconnect/lean:latest")

    def test_pydantic_rejects_nonpositive_limit(self) -> None:
        with pytest.raises(ValidationError):
            LaunchRequest(
                run_id="run_a",
                image_digest=DUMMY_DIGEST,
                cpus=0,
                memory_mb=1,
                pids_limit=1,
                wall_clock_timeout_s=1,
                workspace_max_mb=1,
                log_tail_bytes=1,
            )

    def test_rejects_when_workspace_not_staged(
        self,
        tmp_artifacts_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            sidecar_config,
            "ALLOWED_IMAGE_DIGESTS",
            frozenset({DUMMY_DIGEST}),
        )
        from app.lean_sidecar import runner

        monkeypatch.setattr(runner, "ALLOWED_IMAGE_DIGESTS", frozenset({DUMMY_DIGEST}))
        req = _make_request("run_unstaged")
        with pytest.raises(LaunchRejectedError) as ei:
            launch(req, artifacts_root=tmp_artifacts_root)
        assert ei.value.reason == "workspace_not_staged"

    def test_rejects_runner_misconfiguration(
        self,
        tmp_artifacts_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Empty allow-list -> runner refuses.
        monkeypatch.setattr(sidecar_config, "ALLOWED_IMAGE_DIGESTS", frozenset())
        from app.lean_sidecar import runner

        monkeypatch.setattr(runner, "ALLOWED_IMAGE_DIGESTS", frozenset())
        # Stage the workspace so we get past that check and land on
        # runner config.
        ws = resolve_workspace("run_misconf", tmp_artifacts_root)
        ws.ensure_layout()
        req = _make_request("run_misconf")
        with pytest.raises(LaunchRejectedError) as ei:
            launch(req, artifacts_root=tmp_artifacts_root)
        assert ei.value.reason == "runner_configuration_error"


class TestLauncherAppConcurrency:
    """Endpoint handlers must not block the launcher event loop.

    A real ``/launch`` call runs a synchronous ``podman run``. If the
    FastAPI handler executes it directly, concurrent ``/extract-metadata``
    requests queue behind the running LEAN container and the data plane
    surfaces them as ``LauncherUnreachable: timed out``.
    """

    @pytest.mark.asyncio
    async def test_lifespan_materializes_token_before_first_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from app.lean_sidecar.launcher import app as launcher_app_module

        monkeypatch.delenv("LEAN_LAUNCHER_TOKEN", raising=False)
        monkeypatch.setattr(launcher_app_module, "_artifacts_root", lambda: tmp_path)

        async with launcher_app_module.app.router.lifespan_context(launcher_app_module.app):
            assert (tmp_path / ".launcher-token").is_file()

    @pytest.mark.asyncio
    async def test_extract_metadata_responds_while_launch_is_running(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from app.lean_sidecar.launcher import app as launcher_app_module

        def slow_launch(request: LaunchRequest, *, artifacts_root: Path) -> LaunchResponse:
            time.sleep(0.3)
            return LaunchResponse(
                run_id=request.run_id,
                exit_code=0,
                duration_ms=300,
                timed_out=False,
                log_tail="ok",
                lean_errors={},
                is_clean=True,
            )

        def fast_extract_metadata(request: ExtractMetadataRequest, *, artifacts_root: Path) -> ExtractMetadataResponse:
            return ExtractMetadataResponse(
                market_hours_db_path=str(
                    artifacts_root / request.run_id / "workspace/data/market-hours/market-hours-database.json"
                ),
                symbol_properties_db_path=str(
                    artifacts_root / request.run_id / "workspace/data/symbol-properties/symbol-properties-database.csv"
                ),
            )

        monkeypatch.setattr(launcher_app_module, "_artifacts_root", lambda: tmp_path)
        monkeypatch.setattr(launcher_app_module, "_expected_token", lambda: "token")
        monkeypatch.setattr(launcher_app_module, "launch", slow_launch)
        monkeypatch.setattr(launcher_app_module, "extract_metadata", fast_extract_metadata)

        headers = {"X-Launcher-Token": "token"}
        transport = ASGITransport(app=launcher_app_module.app)
        async with AsyncClient(transport=transport, base_url="http://launcher") as client:
            launch_task = asyncio.create_task(
                client.post(
                    "/launch",
                    json=_make_request("run_concurrent").model_dump(mode="json"),
                    headers=headers,
                )
            )
            await asyncio.sleep(0)

            started = time.perf_counter()
            metadata_response = await client.post(
                "/extract-metadata",
                json={"run_id": "run_concurrent", "image_digest": DUMMY_DIGEST},
                headers=headers,
            )
            elapsed = time.perf_counter() - started
            launch_response = await launch_task

        assert metadata_response.status_code == 200, metadata_response.text
        assert elapsed < 0.2
        assert launch_response.status_code == 200, launch_response.text


class TestWorkspaceSizeEnforcement:
    """Post-run ``workspace_max_mb`` enforcement.

    Tests against the helper, not a real ``execute()`` — exercising the
    enforcement contract without spawning a container. The integration
    of helper + enforcement is covered by the E2E tests where the LEAN
    container actually writes output.
    """

    def test_under_cap_passes(self, tmp_path: Path) -> None:
        from app.lean_sidecar.launcher.service import _workspace_size_bytes

        (tmp_path / "small.bin").write_bytes(b"x" * 1024)
        assert _workspace_size_bytes(tmp_path) == 1024

    def test_over_cap_detectable(self, tmp_path: Path) -> None:
        from app.lean_sidecar.launcher.service import _workspace_size_bytes

        # Write 3 MiB; cap test in launch() then catches > 2 * (1 << 20).
        (tmp_path / "big.bin").write_bytes(b"y" * (3 * (1 << 20)))
        assert _workspace_size_bytes(tmp_path) > 2 * (1 << 20)

    def test_ignores_symlinks(self, tmp_path: Path) -> None:
        from app.lean_sidecar.launcher.service import _workspace_size_bytes

        target = tmp_path / "real.bin"
        target.write_bytes(b"z" * 100)
        link = tmp_path / "link.bin"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this host (Windows w/o priv)")
        # The link is skipped; only the real file is counted.
        assert _workspace_size_bytes(tmp_path) == 100


class TestWorkspacePollerIntegration:
    """The launcher must run the poller alongside ``execute()`` and
    surface a workspace-cap overrun as
    ``LaunchRejectedError("workspace_max_mb_exceeded")`` — same envelope
    callers already handle for the post-execute backstop.

    The race path (overrun lands as ``execute()`` exits, poller didn't
    catch it) is the backstop's responsibility — it must still fire.
    """

    def test_poller_fires_mid_run_returns_rejected(
        self,
        tmp_artifacts_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Poller detects an overrun while ``execute()`` is in-flight,
        kills the container, launcher returns
        ``workspace_max_mb_exceeded``."""
        import time

        from app.lean_sidecar import runner as _runner
        from app.lean_sidecar.launcher import service as _service
        from app.lean_sidecar.runner import RunResult

        monkeypatch.setattr(
            sidecar_config,
            "ALLOWED_IMAGE_DIGESTS",
            frozenset({DUMMY_DIGEST}),
        )
        monkeypatch.setattr(_runner, "ALLOWED_IMAGE_DIGESTS", frozenset({DUMMY_DIGEST}))

        ws = resolve_workspace("run_poller_fires", tmp_artifacts_root)
        ws.ensure_layout()

        # Fake execute(): simulate a LEAN container that writes a file
        # exceeding the cap and then "runs" long enough for the poller
        # to detect it. Returns an exit_code that looks like the
        # container was killed.
        def fake_execute(plan, *, limits):  # type: ignore[no-untyped-def]
            (ws.workspace_dir / "fat.bin").write_bytes(b"x" * (limits.workspace_max_mb * (1 << 20) + 4096))
            # Loop until the poller fires (kill triggered).
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                time.sleep(0.05)
            return RunResult(exit_code=-1, duration_ms=200, timed_out=False, log_tail="")

        # Tighten the poll interval so the test doesn't have to wait 1s.
        from app.lean_sidecar import workspace_poller as _wp

        monkeypatch.setattr(_wp, "_WORKSPACE_POLL_INTERVAL_S", 0.02)
        monkeypatch.setattr(_service, "execute", fake_execute)

        req = _make_request("run_poller_fires")
        with pytest.raises(LaunchRejectedError) as ei:
            launch(req, artifacts_root=tmp_artifacts_root)
        assert ei.value.reason == "workspace_max_mb_exceeded"

    def test_race_path_post_execute_backstop_still_catches(
        self,
        tmp_artifacts_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Overrun lands AS execute() exits — poller never sees it,
        post-execute backstop must still raise."""
        from app.lean_sidecar import runner as _runner
        from app.lean_sidecar.launcher import service as _service
        from app.lean_sidecar.runner import RunResult

        monkeypatch.setattr(
            sidecar_config,
            "ALLOWED_IMAGE_DIGESTS",
            frozenset({DUMMY_DIGEST}),
        )
        monkeypatch.setattr(_runner, "ALLOWED_IMAGE_DIGESTS", frozenset({DUMMY_DIGEST}))

        ws = resolve_workspace("run_race_backstop", tmp_artifacts_root)
        ws.ensure_layout()

        def fake_execute(plan, *, limits):  # type: ignore[no-untyped-def]
            # Write the over-cap file AFTER no time has passed — the
            # poller won't have had a chance to tick at any sane
            # interval before we return.
            (ws.workspace_dir / "race.bin").write_bytes(b"y" * (limits.workspace_max_mb * (1 << 20) + 4096))
            return RunResult(exit_code=0, duration_ms=10, timed_out=False, log_tail="")

        # Poll interval far longer than the simulated execute() so the
        # poller cannot fire — backstop is the only enforcement.
        from app.lean_sidecar import workspace_poller as _wp

        monkeypatch.setattr(_wp, "_WORKSPACE_POLL_INTERVAL_S", 60.0)
        monkeypatch.setattr(_service, "execute", fake_execute)

        req = _make_request("run_race_backstop")
        with pytest.raises(LaunchRejectedError) as ei:
            launch(req, artifacts_root=tmp_artifacts_root)
        assert ei.value.reason == "workspace_max_mb_exceeded"

    def test_happy_path_poller_does_not_fire(
        self,
        tmp_artifacts_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Workspace stays under cap; poller never fires; launch
        returns a normal LaunchResponse."""
        from app.lean_sidecar import runner as _runner
        from app.lean_sidecar.launcher import service as _service
        from app.lean_sidecar.runner import RunResult

        monkeypatch.setattr(
            sidecar_config,
            "ALLOWED_IMAGE_DIGESTS",
            frozenset({DUMMY_DIGEST}),
        )
        monkeypatch.setattr(_runner, "ALLOWED_IMAGE_DIGESTS", frozenset({DUMMY_DIGEST}))

        ws = resolve_workspace("run_happy", tmp_artifacts_root)
        ws.ensure_layout()

        def fake_execute(plan, *, limits):  # type: ignore[no-untyped-def]
            # Tiny write — well under the cap.
            (ws.workspace_dir / "ok.bin").write_bytes(b"z" * 100)
            return RunResult(exit_code=0, duration_ms=10, timed_out=False, log_tail="")

        from app.lean_sidecar import workspace_poller as _wp

        monkeypatch.setattr(_wp, "_WORKSPACE_POLL_INTERVAL_S", 0.02)
        monkeypatch.setattr(_service, "execute", fake_execute)

        req = _make_request("run_happy")
        # MUST NOT raise — workspace is under the cap.
        resp = launch(req, artifacts_root=tmp_artifacts_root)
        assert resp.exit_code == 0
