"""Launcher service rejection tests.

The launcher's value is rejecting bad input before podman is invoked.
These tests assert each rejection class names the right ``reason`` so
the API contract is stable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.launcher.models import LaunchRequest
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
