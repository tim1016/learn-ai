"""End-to-end test: data-plane router → real launcher → real LEAN.

Gated on ``requires_lean_image`` so it only runs on hosts with the
pinned LEAN image. Stands up an in-process launcher FastAPI app on a
loopback port via ``uvicorn``, points the data plane's
``LEAN_LAUNCHER_URL`` at it, then hits the router exactly the way an
external caller would.

Failure mode this test catches: anything in the wiring between the
router, the orchestrator, and the launcher HTTP boundary that the
mocked router test cannot exercise — particularly the real
``LeanConfig`` + ``stage_lean_metadata_from_image`` + manifest write
sequence triggered by the live launcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import httpx
import pytest
import uvicorn
from httpx import ASGITransport, AsyncClient

from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST
from app.lean_sidecar.launcher.app import app as launcher_app
from app.lean_sidecar.workspace import resolve_workspace
from app.main import app as data_plane_app

pytestmark = [
    pytest.mark.requires_lean_image,
    pytest.mark.slow,
    pytest.mark.asyncio,
]


@pytest.fixture
def patched_artifacts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "artifacts").resolve()
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", root)
    from app.routers import lean_sidecar as lean_sidecar_router
    from app.services import lean_sidecar_service

    monkeypatch.setattr(lean_sidecar_service, "DEFAULT_ARTIFACTS_ROOT", root)
    monkeypatch.setattr(lean_sidecar_router, "DEFAULT_ARTIFACTS_ROOT", root)
    # Launcher reads its own artifacts root from env so the same
    # ``run_id`` resolves to the same workspace path on both sides.
    monkeypatch.setenv("LEAN_LAUNCHER_ARTIFACTS_ROOT", str(root))
    return root


@contextlib.asynccontextmanager
async def _running_launcher(port: int):
    """Stand up the launcher uvicorn server on ``port``, yield, stop.

    Uses ``uvicorn.Server`` directly so the test owns the lifecycle
    and a port conflict surfaces as a test failure, not a hung
    process. Picks an ephemeral port by binding to 0 first.
    """
    config = uvicorn.Config(
        launcher_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    # Wait for the server to bind. ``server.started`` flips to True
    # after the protocol accepts connections.
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        raise RuntimeError("launcher did not start within 2.5s")
    try:
        yield
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)


def _pick_free_port() -> int:
    """Pick a TCP port the OS confirms is free *right now*.

    There's a TOCTOU between "OS gave me 49152" and "uvicorn binds
    49152" but in practice on Windows + WSL2 dev hosts this is fine.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestRouterToLauncherToLeanEndToEnd:
    async def test_trusted_run_round_trip(
        self,
        patched_artifacts_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if PINNED_LEAN_IMAGE_DIGEST is None:
            pytest.skip("PINNED_LEAN_IMAGE_DIGEST not set")

        port = _pick_free_port()
        monkeypatch.setenv("LEAN_LAUNCHER_URL", f"http://127.0.0.1:{port}")

        async with _running_launcher(port):
            transport = ASGITransport(app=data_plane_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/lean-sidecar/trusted-runs",
                    json={
                        "run_id": "e2e_router_real",
                        "symbol": "SPY",
                        # 2025-01-06 .. 2025-01-10 (Mon-Fri), represented
                        # by the session-open ms of the first session and
                        # next trading day.
                        "start_ms_utc": 1_736_173_800_000,
                        "end_ms_utc": 1_736_778_600_000,
                        "starting_cash": 100000.0,
                    },
                    timeout=httpx.Timeout(300.0),
                )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["run_id"] == "e2e_router_real"
        assert body["exit_code"] == 0
        assert not body["timed_out"]
        # ``is_clean`` may be False because the trusted sample emits
        # known-noise (quote.zip) — see the lean_sidecar test module.
        # What MUST hold is that the only error category is
        # ``failed_data_requests`` and every entry is a quote.zip line.
        # A genuinely clean run omits ``lean_errors`` or returns {};
        # ``.get`` handles either shape so this assertion can't false-
        # fail when ``is_clean is True``.
        for cat, lines in body.get("lean_errors", {}).items():
            for line in lines:
                if cat == "failed_data_requests" and "_quote.zip" in line:
                    continue
                pytest.fail(f"unexpected error category {cat} with line: {line!r}")

        # Manifest + observations + log must be reachable through the
        # inspection endpoints.
        ws = resolve_workspace("e2e_router_real", patched_artifacts_root)
        async with AsyncClient(transport=ASGITransport(app=data_plane_app), base_url="http://test") as client:
            manifest_r = await client.get("/api/lean-sidecar/runs/e2e_router_real/manifest")
            obs_r = await client.get("/api/lean-sidecar/runs/e2e_router_real/observations")
            log_r = await client.get("/api/lean-sidecar/runs/e2e_router_real/log")

        assert manifest_r.status_code == 200
        manifest = manifest_r.json()
        assert manifest["lean_image_digest"] == PINNED_LEAN_IMAGE_DIGEST
        assert manifest["algorithm_type_name"] == "MyAlgorithm"
        # Manifest hashes a non-empty list of staged bar zips.
        assert manifest["staged_data"]["bar_zips"], "no staged bars in manifest"

        assert obs_r.status_code == 200
        assert obs_r.text.startswith("ms_utc,close")

        assert log_r.status_code == 200
        assert "LEAN ALGORITHMIC TRADING ENGINE" in log_r.text
        # Sanity: the on-disk manifest matches the manifest endpoint.
        assert json.loads(ws.manifest_path.read_text(encoding="utf-8")) == manifest
