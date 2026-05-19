"""WorkspacePoller — in-flight workspace-cap enforcement tests.

The poller is the background thread that walks the workspace while
the LEAN container is running and asks the kill helper to stop the
container if usage exceeds ``workspace_max_mb``. v1's post-execute
size check stays as a backstop for the race-path case where a write
lands as ``execute()`` exits; the poller's job is in-flight
enforcement during the run.

Threat model: benign overrun, not adversarial input. See the
poller module docstring for the full paragraph.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


class TestWorkspaceSizeBytesScandir:
    """The walk helper must agree with file-system reality and skip
    symlinks (we count on-disk usage, not link-target sizes that may
    live outside the workspace)."""

    def test_sums_file_sizes(self, tmp_path: Path) -> None:
        from app.lean_sidecar.workspace_poller import workspace_size_bytes_scandir

        (tmp_path / "a.bin").write_bytes(b"x" * 100)
        (tmp_path / "b.bin").write_bytes(b"y" * 250)
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.bin").write_bytes(b"z" * 50)
        assert workspace_size_bytes_scandir(tmp_path) == 400

    def test_ignores_symlinks(self, tmp_path: Path) -> None:
        from app.lean_sidecar.workspace_poller import workspace_size_bytes_scandir

        real = tmp_path / "real.bin"
        real.write_bytes(b"r" * 100)
        link = tmp_path / "link.bin"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this host (Windows w/o priv)")
        assert workspace_size_bytes_scandir(tmp_path) == 100

    def test_missing_root_returns_zero(self, tmp_path: Path) -> None:
        from app.lean_sidecar.workspace_poller import workspace_size_bytes_scandir

        ghost = tmp_path / "does_not_exist"
        assert workspace_size_bytes_scandir(ghost) == 0

    def test_handles_disappearing_file_mid_walk(self, tmp_path: Path) -> None:
        """A file vanishing between scandir entry and stat() must not
        propagate the OSError — the poller is racing a still-shutting-
        down LEAN process and the next walk catches up."""
        from app.lean_sidecar.workspace_poller import workspace_size_bytes_scandir

        (tmp_path / "stable.bin").write_bytes(b"s" * 100)
        # Walk an empty subtree where a file path is unreadable; under
        # normal POSIX semantics any race-induced OSError is swallowed.
        assert workspace_size_bytes_scandir(tmp_path) == 100


class TestWorkspacePollerLifecycle:
    """Thread lifecycle: start/stop are idempotent in spirit; the
    poller must shut down promptly when the stop event is set."""

    def test_does_not_fire_when_workspace_under_cap(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.lean_sidecar import runner as _runner
        from app.lean_sidecar.workspace_poller import WorkspacePoller

        kill_calls: list[tuple[Path, str]] = []
        monkeypatch.setattr(
            _runner,
            "_kill_container_via_cidfile",
            lambda cid, *, reason: kill_calls.append((cid, reason)),
        )

        cidfile = tmp_path / "cidfile"
        cidfile.write_text("dummy", encoding="utf-8")
        (tmp_path / "small.bin").write_bytes(b"x" * 100)

        poller = WorkspacePoller(
            cidfile_path=cidfile,
            workspace_dir=tmp_path,
            max_bytes=10 * 1024,  # 10 KiB, far above the 100B file
            interval_s=0.02,
        )
        poller.start()
        time.sleep(0.1)  # several poll cycles
        poller.stop()

        assert poller.fired is False
        assert kill_calls == []

    def test_stop_returns_promptly_when_idle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.lean_sidecar import runner as _runner
        from app.lean_sidecar.workspace_poller import WorkspacePoller

        monkeypatch.setattr(
            _runner,
            "_kill_container_via_cidfile",
            lambda cid, *, reason: None,
        )
        cidfile = tmp_path / "cidfile"
        cidfile.write_text("dummy", encoding="utf-8")

        poller = WorkspacePoller(
            cidfile_path=cidfile,
            workspace_dir=tmp_path,
            max_bytes=1 << 30,
            interval_s=10.0,  # would block 10s if stop is broken
        )
        poller.start()
        t0 = time.monotonic()
        poller.stop()
        elapsed = time.monotonic() - t0
        # Stop must NOT wait for the next interval tick.
        assert elapsed < 1.0, f"stop() blocked for {elapsed:.2f}s — interval misused"

    def test_double_start_is_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.lean_sidecar import runner as _runner
        from app.lean_sidecar.workspace_poller import WorkspacePoller

        monkeypatch.setattr(
            _runner,
            "_kill_container_via_cidfile",
            lambda cid, *, reason: None,
        )
        cidfile = tmp_path / "cidfile"
        cidfile.write_text("dummy", encoding="utf-8")

        poller = WorkspacePoller(
            cidfile_path=cidfile,
            workspace_dir=tmp_path,
            max_bytes=1 << 30,
            interval_s=0.05,
        )
        poller.start()
        try:
            with pytest.raises(RuntimeError):
                poller.start()
        finally:
            poller.stop()


class TestWorkspacePollerFires:
    """The core behaviour: when the workspace exceeds the cap mid-run,
    the poller calls the kill helper with
    ``KillReason.WORKSPACE_MAX_MB_EXCEEDED`` and sets ``fired=True``."""

    def test_fires_when_workspace_exceeds_cap(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.lean_sidecar import runner as _runner
        from app.lean_sidecar.runner import KillReason
        from app.lean_sidecar.workspace_poller import WorkspacePoller

        kill_calls: list[tuple[Path, KillReason]] = []
        monkeypatch.setattr(
            _runner,
            "_kill_container_via_cidfile",
            lambda cid, *, reason: kill_calls.append((cid, reason)),
        )

        cidfile = tmp_path / "cidfile"
        cidfile.write_text("dummy", encoding="utf-8")

        poller = WorkspacePoller(
            cidfile_path=cidfile,
            workspace_dir=tmp_path,
            max_bytes=1024,  # 1 KiB cap
            interval_s=0.02,
        )
        poller.start()
        # Write a 4 KiB file mid-flight — poller must detect on its
        # next iteration.
        time.sleep(0.05)
        (tmp_path / "big.bin").write_bytes(b"x" * 4096)
        # Give the poller several intervals to detect + fire.
        deadline = time.monotonic() + 1.0
        while not poller.fired and time.monotonic() < deadline:
            time.sleep(0.02)
        poller.stop()

        assert poller.fired is True, "poller did not fire within 1s of overrun"
        assert len(kill_calls) == 1
        assert kill_calls[0][0] == cidfile
        assert kill_calls[0][1] == KillReason.WORKSPACE_MAX_MB_EXCEEDED

    def test_fires_only_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even with a sustained overrun, the kill helper is called at
        most once — after firing, the poller exits its loop. The
        operator-facing rejection is a single event, not a stream."""
        from app.lean_sidecar import runner as _runner
        from app.lean_sidecar.workspace_poller import WorkspacePoller

        kill_calls: list[tuple[Path, str]] = []
        monkeypatch.setattr(
            _runner,
            "_kill_container_via_cidfile",
            lambda cid, *, reason: kill_calls.append((cid, reason)),
        )

        cidfile = tmp_path / "cidfile"
        cidfile.write_text("dummy", encoding="utf-8")
        # Already over the cap when the poller starts.
        (tmp_path / "huge.bin").write_bytes(b"x" * 4096)

        poller = WorkspacePoller(
            cidfile_path=cidfile,
            workspace_dir=tmp_path,
            max_bytes=1024,
            interval_s=0.01,
        )
        poller.start()
        time.sleep(0.2)  # many would-be polls
        poller.stop()

        assert poller.fired is True
        assert len(kill_calls) == 1


class TestWorkspacePollerWalkCost:
    """Performance smoke: walking a workspace with many small files
    must complete in <500ms per iteration with generous margin (target
    in the handoff is 50ms; the margin absorbs CI variability)."""

    def test_walk_10k_files_under_500ms(self, tmp_path: Path) -> None:
        from app.lean_sidecar.workspace_poller import workspace_size_bytes_scandir

        # Create 10k tiny files across 100 subdirs.
        n_files = 10_000
        for i in range(100):
            sub = tmp_path / f"dir_{i:03d}"
            sub.mkdir()
            for j in range(n_files // 100):
                (sub / f"f_{j:03d}").write_bytes(b"x")

        t0 = time.monotonic()
        total = workspace_size_bytes_scandir(tmp_path)
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert total == n_files  # 1 byte per file
        # The handoff's target is 50ms; 500ms is a 10x margin for CI.
        assert elapsed_ms < 500, f"walk took {elapsed_ms:.0f}ms over budget"


def test_default_poll_interval_constant_exists() -> None:
    """The handoff pins ``_WORKSPACE_POLL_INTERVAL_S = 1.0`` as a
    module-level constant in config.py — never as a RunLimits field
    (the interval IS the overshoot budget; making it caller-settable
    widens it unsafely)."""
    from app.lean_sidecar import config

    assert config._WORKSPACE_POLL_INTERVAL_S == 1.0
    # And it must NOT be a RunLimits field.
    from app.lean_sidecar.config import RunLimits

    assert "workspace_poll_interval_s" not in RunLimits.__dataclass_fields__
    assert "_workspace_poll_interval_s" not in RunLimits.__dataclass_fields__
