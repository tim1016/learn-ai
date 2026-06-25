"""PRD #619-B daemon integration — RunnerProcessManager + create_app lifespan.

Asserts:

- ``RunnerProcessManager`` generates a fresh ``boot_id`` per
  construction; tests can pin a deterministic one.
- ``_build_child_env`` propagates the daemon's ``boot_id`` via the
  ``LIVE_RUNNER_DAEMON_BOOT_ID`` env var (the child watchdog reads
  this in B5).
- The FastAPI lifespan runs orphan classification at startup, starts
  the ``DaemonLeaseWriter``, and flushes a final ``DRAINING`` lease
  on shutdown.
- ``/health`` surfaces the new control-plane diagnostics
  (``daemon_boot_id``, ``lease_status``, ``last_lease_written_at_ms``,
  ``orphan_candidates_count``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from app.engine.live.control_plane import (
    DaemonLease,
    daemon_lease_path,
    read_daemon_lease,
)
from app.engine.live.engine_runtime import (
    BarLoopBlock,
    BrokerBlock,
    CommandLoopBlock,
    ControlPlaneBlock,
    EngineRuntimeSnapshot,
    write_engine_runtime_snapshot,
)
from app.engine.live.host_daemon import RunnerProcessManager, create_app
from app.schemas.live_runs import HostRunnerStartRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(tmp_path: Path, *, boot_id: str = "daemon-test-boot") -> RunnerProcessManager:
    live_runs_root = tmp_path / "live_runs"
    live_runs_root.mkdir()
    # Token file lives at artifacts_root/.host-daemon-token; the manager
    # uses live_runs_root.parent so artifacts_root == tmp_path here.
    return RunnerProcessManager(
        repo_root=tmp_path,
        live_runs_root=live_runs_root,
        boot_id=boot_id,
    )


def _seed_orphan_run(live_runs_root: Path, run_id: str, *, boot_id: str) -> None:
    run_dir = live_runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = EngineRuntimeSnapshot(
        strategy_instance_id="sid",
        run_id=run_id,
        pid=4242,
        process_start_identity="child-001",
        expected_daemon_boot_id=boot_id,
        snapshot_seq=0,
        written_at_ms=1_700_000_000_000,
        command_loop=CommandLoopBlock(heartbeat_at_ms=1_700_000_000_000, state="RUNNING"),
        broker=BrokerBlock(
            identity="PAPER_VERIFIED",
            submission_capability="PAPER_ORDERS_ENABLED",
            effective_posture="PAPER_EXECUTION",
            connection_state="connected",
            connection_epoch=1,
            connected_account="DU1234567",
            port_class="paper_port",
            observation_at_ms=1_700_000_000_000,
            probe_completed_at_ms=1_700_000_000_000 - 100,
            reconnect_attempt=0,
        ),
        bar_loop=BarLoopBlock(heartbeat_at_ms=1_700_000_000_000),
        control_plane=ControlPlaneBlock(
            lease_observed_at_ms=1_700_000_000_000,
            observed_daemon_boot_id=boot_id,
        ),
    )
    write_engine_runtime_snapshot(run_dir, snapshot)


# ---------------------------------------------------------------------------
# RunnerProcessManager — boot_id identity + env propagation
# ---------------------------------------------------------------------------


def test_manager_generates_fresh_boot_id_per_construction(tmp_path: Path) -> None:
    a = RunnerProcessManager(repo_root=tmp_path, live_runs_root=tmp_path / "lr-a")
    (tmp_path / "lr-a").mkdir(exist_ok=True)
    b = RunnerProcessManager(repo_root=tmp_path, live_runs_root=tmp_path / "lr-b")
    (tmp_path / "lr-b").mkdir(exist_ok=True)

    assert a.boot_id != b.boot_id
    assert len(a.boot_id) >= 16


def test_manager_accepts_pinned_boot_id(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, boot_id="pinned-deadbeef")
    assert mgr.boot_id == "pinned-deadbeef"


def test_build_child_env_sets_daemon_boot_id_env(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, boot_id="boot-XYZ")
    request = HostRunnerStartRequest(
        run_id="run-1",
        ibkr_host="127.0.0.1",
    )

    env = mgr._build_child_env(request)

    assert env["LIVE_RUNNER_DAEMON_BOOT_ID"] == "boot-XYZ"
    # Existing fields preserved.
    assert env["IBKR_HOST"] == "127.0.0.1"
    assert "PYTHONPATH" in env


def test_build_child_env_propagates_ibkr_live_runs_root(tmp_path: Path) -> None:
    """Regression test: ``IbkrConfig.live_runs_root`` defaults to the
    container bind-mount path ``/app/artifacts/live_runs``. When the
    daemon spawns the engine on the host that default points at a
    directory that doesn't exist (or, on macOS, exists as a read-only
    system mount), so every ``IbkrClient._record_broker_event`` write
    fails with ENOENT/EROFS. The daemon already knows the resolved
    host path on ``self.live_runs_root``; this test pins that it is
    propagated into the child via the ``IBKR_LIVE_RUNS_ROOT`` env var
    so the IbkrConfig settings load picks it up.
    """
    mgr = _make_manager(tmp_path)
    request = HostRunnerStartRequest(
        run_id="run-1",
        ibkr_host="127.0.0.1",
    )

    env = mgr._build_child_env(request)

    assert env["IBKR_LIVE_RUNS_ROOT"] == str(mgr.live_runs_root)


# ---------------------------------------------------------------------------
# create_app lifespan — orphan classification + lease writer
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_token(tmp_path: Path, monkeypatch) -> str:
    """Pin the daemon token so /health (and protected routes) work in tests."""
    from app.engine.live import host_daemon

    monkeypatch.setattr(host_daemon, "ensure_daemon_token", lambda _root: "test-token")
    return "test-token"


@pytest.fixture
def client_for(tmp_path: Path, fake_token: str):
    """Build an ASGITransport client around a freshly constructed app.

    Returns a factory the test calls to bind an app to a specific
    ``RunnerProcessManager`` instance.
    """

    async def _factory(mgr: RunnerProcessManager) -> AsyncIterator[httpx.AsyncClient]:
        app = create_app(manager=mgr, allowed_origins=["http://localhost"])
        transport = ASGITransport(app=app)
        auth_headers = {"X-Live-Runner-Token": fake_token}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://daemon",
            timeout=5.0,
            headers=auth_headers,
        ) as ac:
            # Trigger lifespan startup.
            async with httpx.AsyncClient(
                transport=transport, base_url="http://daemon", headers=auth_headers
            ) as warmup:
                await warmup.get("/health")
            yield ac

    return _factory


@pytest.mark.asyncio
async def test_health_exposes_daemon_boot_id_and_lease_state(
    tmp_path: Path, fake_token: str
) -> None:
    mgr = _make_manager(tmp_path, boot_id="boot-LIVE")
    app = create_app(manager=mgr, allowed_origins=["http://localhost"])

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport,
        base_url="http://daemon",
        timeout=5.0,
        headers={"X-Live-Runner-Token": fake_token},
    ) as ac:
        response = await ac.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["daemon_boot_id"] == "boot-LIVE"
        # The lease writer started during lifespan; status reflects its
        # current state (CONNECTED before shutdown).
        assert body["lease_status"] == "CONNECTED"
        assert isinstance(body["last_lease_written_at_ms"], int)
        # No orphans seeded → empty count.
        assert body["orphan_candidates_count"] == 0


@pytest.mark.asyncio
async def test_lifespan_classifies_orphan_candidates_on_boot(
    tmp_path: Path, fake_token: str
) -> None:
    """A fresh sidecar owned by a previous boot_id should surface as
    one ORPHANED_CONTROL_PLANE candidate after startup."""
    mgr = _make_manager(tmp_path, boot_id="boot-NEW")
    _seed_orphan_run(mgr.live_runs_root, "run-old", boot_id="boot-PREVIOUS")

    app = create_app(manager=mgr, allowed_origins=["http://localhost"])
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport,
        base_url="http://daemon",
        timeout=5.0,
        headers={"X-Live-Runner-Token": fake_token},
    ) as ac:
        response = await ac.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["orphan_candidates_count"] == 1
        # The classifier ran and populated the manager's in-memory list.
        assert len(mgr._orphan_candidates) == 1


@pytest.mark.asyncio
async def test_lifespan_starts_lease_writer_and_writes_daemon_lease(
    tmp_path: Path, fake_token: str
) -> None:
    """After /health (lifespan startup), the lease file exists and
    carries this daemon's boot_id + ``CONNECTED`` status."""
    mgr = _make_manager(tmp_path, boot_id="boot-LEASE")

    app = create_app(manager=mgr, allowed_origins=["http://localhost"])
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport,
        base_url="http://daemon",
        timeout=5.0,
        headers={"X-Live-Runner-Token": fake_token},
    ) as ac:
        await ac.get("/health")

    # Lease exists and is readable via the canonical helper.
    lease_path = daemon_lease_path(mgr.artifacts_root)
    assert lease_path.exists()
    lease = read_daemon_lease(mgr.artifacts_root)
    assert lease is not None
    assert lease.boot_id == "boot-LEASE"
    # On clean shutdown the final lease is DRAINING.
    assert lease.status == "DRAINING"


async def test_renew_control_plane_lease_writes_daemon_lease_now(
    tmp_path: Path, fake_token: str
) -> None:
    """The cockpit recovery action nudges the reachable daemon to refresh
    its lease immediately, without restarting the child or bypassing auth."""
    mgr = _make_manager(tmp_path, boot_id="boot-RENEW")

    app = create_app(manager=mgr, allowed_origins=["http://localhost"])
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport,
        base_url="http://daemon",
        timeout=5.0,
        headers={"X-Live-Runner-Token": fake_token},
    ) as ac:
        writer = mgr._lease_writer
        assert writer is not None
        original_writer = writer._writer
        writes: list[int] = []

        def spy_writer(path: Path, lease: DaemonLease) -> None:
            writes.append(lease.written_at_ms)
            original_writer(path, lease)

        writer._writer = spy_writer

        response = await ac.post("/control-plane/renew-lease")

        assert response.status_code == 200
        body = response.json()
        for _ in range(20):
            if len(writes) >= 2:
                break
            await asyncio.sleep(0.01)
        lease = read_daemon_lease(mgr.artifacts_root)
        assert lease is not None
        assert lease.boot_id == "boot-RENEW"
        assert lease.status == "CONNECTED"
        assert body["daemon_boot_id"] == "boot-RENEW"
        assert isinstance(body["last_lease_written_at_ms"], int)
        # One synchronous renew write plus one cadence-loop write proves
        # the threadpool caller woke the asyncio task safely.
        assert len(writes) >= 2


async def test_renew_control_plane_lease_surfaces_write_failure(
    tmp_path: Path, fake_token: str
) -> None:
    mgr = _make_manager(tmp_path, boot_id="boot-RENEW-FAIL")

    app = create_app(manager=mgr, allowed_origins=["http://localhost"])
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport,
        base_url="http://daemon",
        timeout=5.0,
        headers={"X-Live-Runner-Token": fake_token},
    ) as ac:
        writer = mgr._lease_writer
        assert writer is not None

        def failing_writer(_path: Path, _lease: DaemonLease) -> None:
            raise OSError("disk full")

        writer._writer = failing_writer

        response = await ac.post("/control-plane/renew-lease")

        assert response.status_code == 503
        assert response.json()["detail"] == "daemon lease renewal failed"


@pytest.mark.asyncio
async def test_lifespan_flushes_draining_lease_on_shutdown(
    tmp_path: Path, fake_token: str
) -> None:
    """The lifespan ``finally`` calls ``set_draining()`` then ``stop()``
    so the watchdog sees the planned transition without waiting for
    the next cadence tick."""
    mgr = _make_manager(tmp_path, boot_id="boot-DRAIN")

    app = create_app(manager=mgr, allowed_origins=["http://localhost"])
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport,
        base_url="http://daemon",
        timeout=5.0,
        headers={"X-Live-Runner-Token": fake_token},
    ) as ac:
        await ac.get("/health")

    # After the ``async with`` exits, lifespan shutdown has run.
    lease = read_daemon_lease(mgr.artifacts_root)
    assert lease is not None
    assert lease.status == "DRAINING"
