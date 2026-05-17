"""Launcher service rejection tests.

The launcher's value is rejecting bad input before podman is invoked.
These tests assert each rejection class names the right ``reason`` so
the API contract is stable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
        with pytest.raises(Exception):
            _make_request("../escape")

    def test_pydantic_rejects_unpinned_image(self) -> None:
        with pytest.raises(Exception):
            _make_request("run_a", digest="quantconnect/lean:latest")

    def test_pydantic_rejects_nonpositive_limit(self) -> None:
        with pytest.raises(Exception):
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
