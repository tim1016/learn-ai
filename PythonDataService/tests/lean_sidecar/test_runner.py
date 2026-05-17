"""Runner argv construction + image-allow-list enforcement tests.

These tests do not spawn podman; they assert on the *constructed*
command and on the launcher's refusal to launch when configuration is
unsafe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.config import DEFAULT_RUN_LIMITS, RunLimits
from app.lean_sidecar.runner import (
    CONTAINER_WORKSPACE_MOUNT,
    RunnerConfigurationError,
    build_command,
)
from app.lean_sidecar.workspace import resolve_workspace

DUMMY_DIGEST = "sha256:0000000000000000000000000000000000000000000000000000000000000001"


@pytest.fixture
def _allow_dummy_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Temporarily widen the image allow-list so runner tests can assert.

    The launcher never sees this allow-list in production; it is
    re-derived from ``config.PINNED_LEAN_IMAGE_DIGEST`` on import. Tests
    monkey-patch the in-module set so they exercise the real check.
    """
    monkeypatch.setattr(
        sidecar_config,
        "ALLOWED_IMAGE_DIGESTS",
        frozenset({DUMMY_DIGEST}),
    )
    # The runner imports the symbol at module-load time; rebind there too.
    from app.lean_sidecar import runner

    monkeypatch.setattr(
        runner,
        "ALLOWED_IMAGE_DIGESTS",
        frozenset({DUMMY_DIGEST}),
    )


class TestBuildCommand:
    def test_contains_mandatory_security_flags(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        ws = resolve_workspace("run_x1", tmp_artifacts_root)
        ws.ensure_layout()
        plan = build_command(ws, DUMMY_DIGEST)
        argv = plan.argv

        assert "run" in argv
        assert "--rm" in argv
        assert "--network=none" in argv
        assert "--security-opt=no-new-privileges" in argv
        assert "--cap-drop=ALL" in argv
        # Phase 1c — promoted to mandatory after E2E proved viable.
        assert "--read-only" in argv
        assert "--user=10001:10001" in argv
        assert any(a.startswith("--cpus=") for a in argv)
        assert any(a.startswith("--memory=") for a in argv)
        assert any(a.startswith("--pids-limit=") for a in argv)
        # The LEAN launcher arg ``--config <workspace-path>`` is always
        # appended after the image; without it LEAN runs the image-baked
        # default config silently.
        assert "--config" in argv
        assert "/lean-run/project/config.json" in argv
        # Workspace mount is exactly the workspace directory.
        mount_arg_idx = argv.index("-v")
        mount_spec = argv[mount_arg_idx + 1]
        assert mount_spec.startswith(str(ws.workspace_dir))
        assert mount_spec.endswith(f":{CONTAINER_WORKSPACE_MOUNT}:rw")

    def test_refuses_unpinned_image(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        ws = resolve_workspace("run_x2", tmp_artifacts_root)
        ws.ensure_layout()
        with pytest.raises(RunnerConfigurationError):
            build_command(ws, "quantconnect/lean:latest")

    def test_refuses_unknown_digest(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        ws = resolve_workspace("run_x3", tmp_artifacts_root)
        ws.ensure_layout()
        other = "sha256:dead000000000000000000000000000000000000000000000000000000000000"
        with pytest.raises(RunnerConfigurationError):
            build_command(ws, other)

    def test_refuses_missing_workspace(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        ws = resolve_workspace("run_x4", tmp_artifacts_root)
        # Note: ensure_layout NOT called.
        with pytest.raises(RunnerConfigurationError):
            build_command(ws, DUMMY_DIGEST)

    def test_refuses_workspace_that_is_a_file(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        ws = resolve_workspace("run_x5", tmp_artifacts_root)
        # Place a regular file where the workspace directory should be.
        # This is a misconfiguration we want to fail fast on.
        ws.root.mkdir(parents=True, exist_ok=True)
        ws.workspace_dir.write_text("not a directory", encoding="utf-8")
        with pytest.raises(RunnerConfigurationError, match="not a directory"):
            build_command(ws, DUMMY_DIGEST)

    @pytest.mark.parametrize(
        "bad_token",
        [
            "--privileged",
            "--cap-add=SYS_ADMIN",
            "--security-opt=seccomp=unconfined",
            "--network=host",
            "--user=0:0",
            "--volume=/etc:/host-etc",
        ],
    )
    def test_refuses_sandbox_widening_hardening_flag(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
        bad_token: str,
    ) -> None:
        """Hardening flags from a caller can never widen the sandbox.

        The launcher boundary forwards ``hardening_flags`` straight into
        the ``podman run`` argv; a caller passing ``--privileged`` or
        anything similarly permissive must be rejected with a clear
        error before any container is spawned.
        """
        ws = resolve_workspace("run_x6", tmp_artifacts_root)
        ws.ensure_layout()
        with pytest.raises(RunnerConfigurationError, match="not on the allow-list"):
            build_command(ws, DUMMY_DIGEST, hardening_flags=(bad_token,))

    def test_accepts_allow_listed_hardening_tokens(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        ws = resolve_workspace("run_x7", tmp_artifacts_root)
        ws.ensure_layout()
        # All tokens here must be in ALLOWED_HARDENING_TOKENS. Phase
        # 1c moved --read-only and --user out of the allow-list and
        # into the mandatory shape, so the only opt-in surface left
        # is --tmpfs + spec.
        plan = build_command(
            ws,
            DUMMY_DIGEST,
            hardening_flags=(
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=256m",
            ),
        )
        argv = plan.argv
        # --read-only is in the mandatory shape, not the allow-list;
        # still expect it in the constructed argv.
        assert "--read-only" in argv
        assert "--tmpfs" in argv
        assert "/tmp:rw,noexec,nosuid,size=256m" in argv

    def test_rejects_tmpfs_without_spec(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        """``--tmpfs`` on its own must fail before podman sees it.

        Without this check the next token would be the image
        reference, which podman would treat as the tmpfs spec — a
        confusing failure that's hard to diagnose from outside.
        """
        ws = resolve_workspace("run_x8", tmp_artifacts_root)
        ws.ensure_layout()
        with pytest.raises(RunnerConfigurationError, match="requires a value"):
            build_command(ws, DUMMY_DIGEST, hardening_flags=("--tmpfs",))

    def test_rejects_tmpfs_followed_by_another_flag(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        """``--tmpfs`` must be followed by a value token, not another
        flag. The other-flag check has to fire AFTER allow-list
        membership; using a token CodeQL-allow-listed but flag-shaped
        (``--tmpfs --tmpfs``) exercises the structural validator
        rather than the allow-list."""
        ws = resolve_workspace("run_x9", tmp_artifacts_root)
        ws.ensure_layout()
        with pytest.raises(RunnerConfigurationError, match="expects a value"):
            build_command(
                ws,
                DUMMY_DIGEST,
                hardening_flags=("--tmpfs", "--tmpfs"),
            )


class TestRunLimits:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("cpus", 0),
            ("memory_mb", -1),
            ("pids_limit", 0),
            ("wall_clock_timeout_s", 0),
            ("workspace_max_mb", 0),
            ("log_tail_bytes", 0),
        ],
    )
    def test_rejects_nonpositive(self, field: str, value: int | float) -> None:
        kwargs = {
            "cpus": 2.0,
            "memory_mb": 2048,
            "pids_limit": 512,
            "wall_clock_timeout_s": 120,
            "workspace_max_mb": 512,
            "log_tail_bytes": 1024,
        }
        kwargs[field] = value
        with pytest.raises(ValueError):
            RunLimits(**kwargs).validate()

    def test_default_run_limits_validates(self) -> None:
        DEFAULT_RUN_LIMITS.validate()
