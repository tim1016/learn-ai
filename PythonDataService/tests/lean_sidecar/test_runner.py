"""Runner argv construction + image-allow-list enforcement tests.

These tests do not spawn podman; they assert on the *constructed*
command and on the launcher's refusal to launch when configuration is
unsafe.
"""

from __future__ import annotations

import subprocess
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
        # ``--user`` is dynamic per host (matches host UID on Linux,
        # falls back to ``10001:10001`` on Windows where ``os.getuid``
        # is unavailable). Pattern-match rather than literal-match.
        user_args = [a for a in argv if a.startswith("--user=")]
        assert len(user_args) == 1, f"expected exactly one --user= flag, got {user_args}"
        uid_str, gid_str = user_args[0].removeprefix("--user=").split(":", 1)
        assert int(uid_str) > 0, f"container UID must not be root, got {uid_str}"
        assert int(gid_str) > 0, f"container GID must not be root, got {gid_str}"
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

    def test_argv_contains_cidfile_flag_pointing_into_workspace(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        """Review-fix (P1.1): the argv must include ``--cidfile=<path>``
        so podman writes the container id to a host-side file on
        creation, and ``execute`` can issue ``podman stop`` against it
        on wall-clock timeout. Without the cidfile, the
        ``subprocess.run(..., timeout=...)`` outer kill switch only
        kills the podman CLIENT — the LEAN container keeps running."""
        ws = resolve_workspace("run_cid", tmp_artifacts_root)
        ws.ensure_layout()
        plan = build_command(ws, DUMMY_DIGEST)
        cidfile_args = [a for a in plan.argv if a.startswith("--cidfile=")]
        assert len(cidfile_args) == 1, f"expected exactly one --cidfile= flag, got {cidfile_args}"
        cidfile_path_str = cidfile_args[0].removeprefix("--cidfile=")
        # Lives under the workspace's launcher_dir alongside
        # launcher.log; cleanup is then a single ``rm -rf
        # <workspace>/launcher/`` for the operator.
        assert cidfile_path_str.startswith(str(ws.launcher_dir)), (
            f"cidfile {cidfile_path_str} should live under launcher_dir ({ws.launcher_dir})"
        )
        # ``RunnerPlan.cidfile_path`` carries the same path so
        # ``execute`` can read it without re-parsing argv.
        assert str(plan.cidfile_path) == cidfile_path_str

    def test_build_command_clears_stale_cidfile(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        """A stale cidfile from a prior run on the same workspace would
        make podman refuse to start with ``error opening cidfile: file
        exists``. build_command must clear it up-front so a retry on
        the same workspace works."""
        ws = resolve_workspace("run_cid_stale", tmp_artifacts_root)
        ws.ensure_layout()
        stale_cid = ws.launcher_dir / "cidfile"
        stale_cid.write_text("old-container-id\n", encoding="utf-8")
        assert stale_cid.exists()
        plan = build_command(ws, DUMMY_DIGEST)
        assert not plan.cidfile_path.exists(), "build_command should have removed the stale cidfile"

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

    def test_container_user_spec_matches_host_uid_on_posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reviewer P1: on Linux, the container UID must match the
        launcher's host UID so the container can write to workspace
        files the launcher just created. Otherwise POSIX permissions
        reject the cross-UID write and backtests fail."""
        from app.lean_sidecar import runner

        monkeypatch.setattr(runner.os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setattr(runner.os, "getgid", lambda: 1000, raising=False)
        assert runner._container_user_spec() == "1000:1000"

    def test_container_user_spec_falls_back_when_getuid_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On Windows where ``os.getuid`` does not exist, the helper
        returns ``10001:10001`` — non-root, fixed, audit-explicit. The
        WSL2 mount layer doesn't enforce host UID so this just works."""
        from app.lean_sidecar import runner

        # Simulate Windows native Python by removing getuid/getgid.
        monkeypatch.delattr(runner.os, "getuid", raising=False)
        monkeypatch.delattr(runner.os, "getgid", raising=False)
        spec = runner._container_user_spec()
        assert spec == "10001:10001"

    def test_argv_includes_userns_keep_id_on_rootless_podman(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: on rootless podman, ``--user=<host-uid>`` alone
        runs LEAN as a sub-UID with no write access to the workspace
        (the workspace files are owned by the launcher's host UID,
        which maps to container UID 0; the requested container UID
        maps to a sub-UID from /etc/subuid). Without ``--userns=keep-id``
        LEAN crashes inside ``BacktestingResultHandler.Exit()`` with
        ``UnauthorizedAccessException`` on every ``output/*`` write —
        the run looks "finished" but produces no log.txt or summary.

        Pin both the flag presence AND its position before the
        ``--user=`` flag so a future refactor cannot silently reorder
        them (podman parses flags positionally for some interactions).
        """
        from app.lean_sidecar import runner

        monkeypatch.setattr(runner, "_is_rootless_podman", lambda _path: True)
        ws = resolve_workspace("run_userns_rootless", tmp_artifacts_root)
        ws.ensure_layout()
        argv = build_command(ws, DUMMY_DIGEST).argv

        assert "--userns=keep-id" in argv
        user_arg = next(a for a in argv if a.startswith("--user="))
        assert argv.index("--userns=keep-id") < argv.index(user_arg), (
            "--userns=keep-id must precede --user=<host-uid> so the user-namespace "
            "mapping is in effect when podman resolves the requested container UID"
        )

    def test_argv_omits_userns_keep_id_on_rootful_podman(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rootful podman rejects ``--userns=keep-id`` at parse time
        ("keep-id is only supported in rootless mode"). Omitting the
        flag is the safe path; the default user-namespace already
        gives identity mapping between host and container UIDs there.
        """
        from app.lean_sidecar import runner

        monkeypatch.setattr(runner, "_is_rootless_podman", lambda _path: False)
        ws = resolve_workspace("run_userns_rootful", tmp_artifacts_root)
        ws.ensure_layout()
        argv = build_command(ws, DUMMY_DIGEST).argv

        assert "--userns=keep-id" not in argv

    def test_is_rootless_podman_returns_false_on_probe_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Defensive: any failure of the ``podman info`` probe (binary
        missing, non-zero exit, timeout, malformed output) must fall
        back to ``False`` so the omit-the-flag path runs. A True
        default would corrupt rootful argvs."""
        from app.lean_sidecar import runner

        def _fail(*_args: object, **_kwargs: object) -> object:
            raise subprocess.SubprocessError("simulated probe failure")

        monkeypatch.setattr(runner.subprocess, "run", _fail)
        assert runner._is_rootless_podman("/usr/bin/podman") is False

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


class TestExecuteTimeoutKillsContainer:
    """Review-fix (P1.1): on wall-clock timeout, ``execute`` must read
    the cidfile podman wrote on container creation and issue
    ``podman stop`` + ``podman rm`` against the container id. Without
    this, ``subprocess.run(..., timeout=...)`` only kills the podman
    CLIENT — the LEAN container keeps running past the deadline.

    Subprocess is mocked: the real ``podman run`` is replaced with
    a TimeoutExpired raise so we exercise the kill path without
    spawning a container. The cidfile is written manually so the kill
    path has a valid id to stop.
    """

    def test_timeout_invokes_podman_stop_and_rm(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import subprocess as _subprocess

        from app.lean_sidecar import runner as _runner

        ws = resolve_workspace("run_kill", tmp_artifacts_root)
        ws.ensure_layout()
        plan = _runner.build_command(ws, DUMMY_DIGEST)
        # Simulate podman writing the cidfile at container creation.
        fake_cid = "1234567890abcdef" * 4
        plan.cidfile_path.write_text(fake_cid + "\n", encoding="utf-8")

        recorded_calls: list[list[str]] = []

        def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
            recorded_calls.append(list(argv))
            # First call is the ``podman run ...`` argv → raise
            # TimeoutExpired. Subsequent calls are ``podman stop`` and
            # ``podman rm`` → return success.
            if len(recorded_calls) == 1:
                raise _subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
            return _subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(_subprocess, "run", fake_run)
        # ``execute`` looks up podman via shutil.which inside the
        # kill helper too — make it deterministic regardless of host.
        import shutil as _shutil

        monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/podman")

        result = _runner.execute(plan)

        assert result.timed_out is True
        assert result.exit_code == -1
        # First recorded call is the original run; the next two should
        # be ``podman stop --time=5 <cid>`` and ``podman rm <cid>``.
        assert len(recorded_calls) == 3
        stop_call = recorded_calls[1]
        rm_call = recorded_calls[2]
        assert stop_call[0] == "/usr/bin/podman"
        assert stop_call[1] == "stop"
        assert "--time=5" in stop_call
        assert fake_cid in stop_call
        assert rm_call[0] == "/usr/bin/podman"
        assert rm_call[1] == "rm"
        assert fake_cid in rm_call

    def test_timeout_handles_missing_cidfile_gracefully(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the timeout fires before podman wrote the cidfile (e.g.,
        podman startup itself hung), there's no container to kill —
        the helper should log + return without raising."""
        import subprocess as _subprocess

        from app.lean_sidecar import runner as _runner

        ws = resolve_workspace("run_kill_nocid", tmp_artifacts_root)
        ws.ensure_layout()
        plan = _runner.build_command(ws, DUMMY_DIGEST)
        # Intentionally do NOT write the cidfile — simulate startup
        # hang before podman touched it. build_command pre-cleared it
        # so we know it's absent.
        assert not plan.cidfile_path.exists()

        def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
            raise _subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

        monkeypatch.setattr(_subprocess, "run", fake_run)
        import shutil as _shutil

        monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/podman")

        result = _runner.execute(plan)  # MUST NOT raise
        assert result.timed_out is True
        assert result.exit_code == -1


class TestKillReason:
    """Reviewer-tightening (P1.4 v2): ``_kill_container_via_cidfile``
    must surface *why* it killed the container so the launcher can
    return the correct ``RunResult``/``LaunchRejectedError`` reason.

    A single "killed" signal collapses two distinct failure modes
    (wall-clock timeout vs workspace cap overrun) into one — the
    discriminator is the enum threaded through the helper.
    """

    def test_enum_has_expected_members(self) -> None:
        from app.lean_sidecar.runner import KillReason

        assert KillReason.WALL_CLOCK_TIMEOUT == "wall_clock_timeout"
        assert KillReason.WORKSPACE_MAX_MB_EXCEEDED == "workspace_max_mb_exceeded"
        # Stable string values for log / payload routing.
        assert {m.value for m in KillReason} == {
            "wall_clock_timeout",
            "workspace_max_mb_exceeded",
        }

    def test_kill_helper_accepts_reason_kwarg_and_logs_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        import subprocess as _subprocess

        from app.lean_sidecar import runner as _runner
        from app.lean_sidecar.runner import KillReason, _kill_container_via_cidfile

        cidfile = tmp_path / "cidfile"
        fake_cid = "deadbeef" * 8
        cidfile.write_text(fake_cid + "\n", encoding="utf-8")

        def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
            return _subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(_subprocess, "run", fake_run)
        import shutil as _shutil

        monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/podman")

        with caplog.at_level(logging.INFO, logger=_runner.__name__):
            _kill_container_via_cidfile(cidfile, reason=KillReason.WORKSPACE_MAX_MB_EXCEEDED)

        # The reason string must appear in at least one log record so an
        # operator scanning launcher.log can tell the two kill paths apart.
        assert any("workspace_max_mb_exceeded" in record.getMessage() for record in caplog.records), (
            f"reason missing from logs: {[r.getMessage() for r in caplog.records]}"
        )

    def test_timeout_path_threads_wall_clock_reason(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """End-to-end through ``execute`` on TimeoutExpired:
        ``_kill_container_via_cidfile`` must be called with
        ``KillReason.WALL_CLOCK_TIMEOUT`` (not defaulted, not omitted)."""
        import logging
        import subprocess as _subprocess

        from app.lean_sidecar import runner as _runner

        ws = resolve_workspace("run_kill_reason_t", tmp_artifacts_root)
        ws.ensure_layout()
        plan = _runner.build_command(ws, DUMMY_DIGEST)
        fake_cid = "fedcba9876543210" * 4
        plan.cidfile_path.write_text(fake_cid + "\n", encoding="utf-8")

        call_count = {"n": 0}

        def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
            call_count["n"] += 1
            # First call is the ``podman run …`` argv from execute();
            # subsequent calls are ``podman stop`` and ``podman rm``
            # from the kill helper.
            if call_count["n"] == 1:
                raise _subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
            return _subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(_subprocess, "run", fake_run)
        import shutil as _shutil

        monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/podman")

        with caplog.at_level(logging.INFO, logger=_runner.__name__):
            _runner.execute(plan)

        assert any("wall_clock_timeout" in record.getMessage() for record in caplog.records), (
            f"wall_clock_timeout reason missing: {[r.getMessage() for r in caplog.records]}"
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
