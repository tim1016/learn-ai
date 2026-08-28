"""Launcher service rejection tests.

The launcher's value is rejecting bad input before podman is invoked.
These tests assert each rejection class names the right ``reason`` so
the API contract is stable.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.launcher.models import (
    ExtractMetadataRequest,
    ExtractMetadataResponse,
    LauncherImageReadiness,
    LaunchRequest,
    LaunchResponse,
)
from app.lean_sidecar.launcher.service import LaunchRejectedError, check_pinned_image, launch
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


class TestPinnedImageReadiness:
    def test_check_pinned_image_reports_local_pinned_image_as_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.lean_sidecar.launcher import service as launcher_service

        monkeypatch.setattr(launcher_service, "PINNED_LEAN_IMAGE_DIGEST", DUMMY_DIGEST)
        monkeypatch.setattr(launcher_service, "_require_podman", lambda: "/usr/local/bin/podman")
        completed = subprocess.CompletedProcess([], 0)
        monkeypatch.setattr(launcher_service.subprocess, "run", lambda *args, **kwargs: completed)

        readiness = check_pinned_image()

        assert readiness == LauncherImageReadiness(
            reference=f"{sidecar_config.LEAN_IMAGE_REPO}@{DUMMY_DIGEST}",
            available=True,
            detail="Pinned LEAN image is present in Podman's local image store.",
        )

    def test_check_pinned_image_reports_missing_local_image_without_running_a_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.lean_sidecar.launcher import service as launcher_service

        monkeypatch.setattr(launcher_service, "PINNED_LEAN_IMAGE_DIGEST", DUMMY_DIGEST)
        monkeypatch.setattr(launcher_service, "_require_podman", lambda: "/usr/local/bin/podman")
        completed = subprocess.CompletedProcess([], 1)
        monkeypatch.setattr(launcher_service.subprocess, "run", lambda *args, **kwargs: completed)
        readiness = check_pinned_image()

        assert readiness.reference == f"{sidecar_config.LEAN_IMAGE_REPO}@{DUMMY_DIGEST}"
        assert readiness.available is False
        assert readiness.failure_reason == "missing"
        assert readiness.detail == "Pinned LEAN image is not present in Podman's local image store."

    def test_check_pinned_image_reports_podman_operational_failure_without_calling_it_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.lean_sidecar.launcher import service as launcher_service

        monkeypatch.setattr(launcher_service, "PINNED_LEAN_IMAGE_DIGEST", DUMMY_DIGEST)
        monkeypatch.setattr(launcher_service, "_require_podman", lambda: "/usr/local/bin/podman")
        completed = subprocess.CompletedProcess([], 125)
        monkeypatch.setattr(launcher_service.subprocess, "run", lambda *args, **kwargs: completed)

        readiness = check_pinned_image()

        assert readiness.available is False
        assert readiness.failure_reason == "check_failed"
        assert "exit code 125" in readiness.detail

    def test_check_pinned_image_bounds_probe_below_the_diagnostics_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.lean_sidecar.launcher import service as launcher_service

        monkeypatch.setattr(launcher_service, "PINNED_LEAN_IMAGE_DIGEST", DUMMY_DIGEST)
        monkeypatch.setattr(launcher_service, "_require_podman", lambda: "/usr/local/bin/podman")
        observed_timeout: float | None = None

        def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
            nonlocal observed_timeout
            timeout = kwargs["timeout"]
            assert isinstance(timeout, float)
            observed_timeout = timeout
            return subprocess.CompletedProcess([], 0)

        monkeypatch.setattr(launcher_service.subprocess, "run", run)

        check_pinned_image()

        assert observed_timeout is not None
        assert observed_timeout < 2.0


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
    async def test_healthz_coalesces_concurrent_pinned_image_probes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.lean_sidecar.launcher import app as launcher_app_module

        calls = 0

        def slow_check_pinned_image() -> LauncherImageReadiness:
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            return LauncherImageReadiness(
                reference="localhost/learn-ai/lean-sandbox@sha256:test",
                available=True,
                detail="Pinned LEAN image is present in Podman's local image store.",
            )

        monkeypatch.setattr(launcher_app_module, "check_pinned_image", slow_check_pinned_image)
        monkeypatch.setattr(launcher_app_module, "_pinned_image_readiness", None)
        monkeypatch.setattr(launcher_app_module, "_pinned_image_readiness_at", None)
        monkeypatch.setattr(launcher_app_module, "_pinned_image_probe", None)

        transport = ASGITransport(app=launcher_app_module.app)
        async with AsyncClient(transport=transport, base_url="http://launcher") as client:
            first, second = await asyncio.gather(client.get("/healthz"), client.get("/healthz"))

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert calls == 1

    @pytest.mark.asyncio
    async def test_extract_metadata_responds_while_launch_is_running(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from app.lean_sidecar.launcher import app as launcher_app_module

        launch_started = threading.Event()
        release_launch = threading.Event()

        def slow_launch(
            request: LaunchRequest,
            *,
            artifacts_root: Path,
            lake_root: Path | None = None,
        ) -> LaunchResponse:
            launch_started.set()
            if not release_launch.wait(timeout=2):
                raise AssertionError("test did not release the blocked launch")
            return LaunchResponse(
                run_id=request.run_id,
                exit_code=0,
                duration_ms=1,
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
            assert await asyncio.to_thread(launch_started.wait, 1)

            try:
                metadata_response = await asyncio.wait_for(
                    client.post(
                        "/extract-metadata",
                        json={"run_id": "run_concurrent", "image_digest": DUMMY_DIGEST},
                        headers=headers,
                    ),
                    timeout=1,
                )
                assert not release_launch.is_set()
            finally:
                release_launch.set()
                launch_response = await asyncio.wait_for(launch_task, timeout=1)

        assert metadata_response.status_code == 200, metadata_response.text
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
