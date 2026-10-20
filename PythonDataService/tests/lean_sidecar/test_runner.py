"""Runner argv construction + image-allow-list enforcement tests.

These tests do not spawn podman; they assert on the *constructed*
command and on the launcher's refusal to launch when configuration is
unsafe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.config import RunLimits
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
    def test_retained_data_is_read_only_inside_writable_workspace(
        self, tmp_artifacts_root: Path, _allow_dummy_digest: None,
    ) -> None:
        ws = resolve_workspace("retained_data", tmp_artifacts_root)
        ws.ensure_layout()
        plan = build_command(ws, DUMMY_DIGEST, read_only_workspace_data=True)
        mounts = [plan.argv[i + 1] for i, token in enumerate(plan.argv) if token == "-v"]
        assert mounts == [
            f"{ws.workspace_dir}:/lean-run:rw",
            f"{ws.data_dir}:/lean-run/data:ro",
        ]

    def test_default_command_security_flags_and_cidfile(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        ws = resolve_workspace("run_x1", tmp_artifacts_root)
        ws.ensure_layout()
        plan = build_command(ws, DUMMY_DIGEST)
        argv = plan.argv

        assert "run" in argv, "argv runs the container"
        assert "--rm" in argv, "mandatory flag --rm"
        assert "--network=none" in argv, "mandatory flag --network=none"
        assert "--security-opt=no-new-privileges" in argv, "mandatory flag --security-opt=no-new-privileges"
        assert "--cap-drop=ALL" in argv, "mandatory flag --cap-drop=ALL"
        # Phase 1c — promoted to mandatory after E2E proved viable.
        assert "--read-only" in argv, "mandatory flag --read-only (Phase 1c)"
        # ``--user`` is dynamic per host (matches host UID on Linux,
        # falls back to ``10001:10001`` on Windows where ``os.getuid``
        # is unavailable). Pattern-match rather than literal-match.
        user_args = [a for a in argv if a.startswith("--user=")]
        assert len(user_args) == 1, f"expected exactly one --user= flag, got {user_args}"
        uid_str, gid_str = user_args[0].removeprefix("--user=").split(":", 1)
        assert int(uid_str) > 0, f"container UID must not be root, got {uid_str}"
        assert int(gid_str) > 0, f"container GID must not be root, got {gid_str}"
        assert any(a.startswith("--cpus=") for a in argv), "mandatory --cpus= limit"
        assert any(a.startswith("--memory=") for a in argv), "mandatory --memory= limit"
        assert any(a.startswith("--pids-limit=") for a in argv), "mandatory --pids-limit= limit"
        # The LEAN launcher arg ``--config <workspace-path>`` is always
        # appended after the image; without it LEAN runs the image-baked
        # default config silently.
        assert "--config" in argv, "LEAN launcher arg --config is appended after the image"
        assert "/lean-run/project/config.json" in argv, "--config points at the workspace project config"
        # Workspace mount is exactly the workspace directory.
        mount_arg_idx = argv.index("-v")
        mount_spec = argv[mount_arg_idx + 1]
        assert mount_spec.startswith(str(ws.workspace_dir)), "workspace mount starts at the workspace directory"
        assert mount_spec.endswith(f":{CONTAINER_WORKSPACE_MOUNT}:rw"), "workspace mount is read-write at the container mount"
        # Review-fix (P1.1): the argv must include ``--cidfile=<path>`` so
        # podman writes the container id to a host-side file on creation, and
        # ``execute`` can issue ``podman stop`` against it on wall-clock
        # timeout. Without the cidfile, the ``subprocess.run(..., timeout=...)``
        # outer kill switch only kills the podman CLIENT — the LEAN container
        # keeps running.
        cidfile_args = [a for a in argv if a.startswith("--cidfile=")]
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
        assert str(plan.cidfile_path) == cidfile_path_str, "RunnerPlan.cidfile_path carries the --cidfile= path"

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

    @pytest.mark.parametrize(
        "image",
        [
            pytest.param("quantconnect/lean:latest", id="unpinned_image"),
            pytest.param(
                "sha256:dead000000000000000000000000000000000000000000000000000000000000",
                id="unknown_digest",
            ),
        ],
    )
    def test_refuses_image_not_on_allow_list(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
        image: str,
    ) -> None:
        ws = resolve_workspace("run_x2", tmp_artifacts_root)
        ws.ensure_layout()
        with pytest.raises(RunnerConfigurationError):
            build_command(ws, image)

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
        ("hardening_flags", "match"),
        [
            # ``--tmpfs`` / ``--env`` on their own must fail before podman sees
            # them: the next token would be the image reference, which podman
            # would treat as the tmpfs / env spec — a confusing failure that's
            # hard to diagnose from outside.
            pytest.param(("--tmpfs",), "requires a value", id="tmpfs_without_spec"),
            pytest.param(("--env",), "requires a value", id="env_without_value"),
            # ``--tmpfs`` / ``--env`` must be followed by a value token, not
            # another flag. The other-flag check has to fire AFTER allow-list
            # membership; using a token CodeQL-allow-listed but flag-shaped
            # (``--tmpfs --tmpfs``) exercises the structural validator rather
            # than the allow-list.
            pytest.param(("--tmpfs", "--tmpfs"), "expects a value", id="tmpfs_followed_by_another_flag"),
            pytest.param(("--env", "--env"), "expects a value", id="env_followed_by_another_flag"),
            # Hardening flags from a caller can never widen the sandbox. The
            # launcher boundary forwards ``hardening_flags`` straight into the
            # ``podman run`` argv; a caller passing ``--privileged`` or
            # anything similarly permissive must be rejected with a clear
            # error before any container is spawned.
            pytest.param(("--privileged",), "not on the allow-list", id="sandbox_widening_privileged"),
            pytest.param(("--cap-add=SYS_ADMIN",), "not on the allow-list", id="sandbox_widening_cap_add_sys_admin"),
            pytest.param(
                ("--security-opt=seccomp=unconfined",),
                "not on the allow-list",
                id="sandbox_widening_seccomp_unconfined",
            ),
            pytest.param(("--network=host",), "not on the allow-list", id="sandbox_widening_network_host"),
            pytest.param(("--user=0:0",), "not on the allow-list", id="sandbox_widening_user_root"),
            pytest.param(("--volume=/etc:/host-etc",), "not on the allow-list", id="sandbox_widening_volume_host_etc"),
            # ``--env`` is more powerful than ``--tmpfs`` (it can set any
            # container-side env var). Safety is preserved by pinning literal
            # KEY=VAL strings in the allow-list, NOT patterns. Other DOTNET_*
            # knobs, generic env smuggling and the right key with the wrong
            # value must all be rejected before podman runs.
            pytest.param(
                ("--env", "DOTNET_EnableDiagnostics=1"),
                "not on the allow-list",
                id="env_unpinned_dotnet_enable_diagnostics",
            ),
            pytest.param(
                ("--env", "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=0"),
                "not on the allow-list",
                id="env_unpinned_dotnet_globalization_invariant",
            ),
            pytest.param(
                ("--env", "LD_PRELOAD=/tmp/evil.so"),
                "not on the allow-list",
                id="env_smuggled_ld_preload",
            ),
            pytest.param(("--env", "PATH=/tmp"), "not on the allow-list", id="env_smuggled_path"),
            pytest.param(
                ("--env", "DOTNET_ReadyToRun=1"),
                "not on the allow-list",
                id="env_right_key_wrong_value_ready_to_run",
            ),
            pytest.param(
                ("--env", "DOTNET_TieredCompilation=1"),
                "not on the allow-list",
                id="env_right_key_wrong_value_tiered_compilation",
            ),
        ],
    )
    def test_refuses_invalid_hardening_flags(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
        hardening_flags: tuple[str, ...],
        match: str,
    ) -> None:
        ws = resolve_workspace("run_refuse_hardening", tmp_artifacts_root)
        ws.ensure_layout()
        with pytest.raises(RunnerConfigurationError, match=match):
            build_command(ws, DUMMY_DIGEST, hardening_flags=hardening_flags)

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

    def test_container_user_spec_matches_host_uid_on_posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reviewer P1: on Linux, the container UID must match the
        launcher's host UID so the container can write to workspace
        files the launcher just created. Otherwise POSIX permissions
        reject the cross-UID write and backtests fail."""
        from app.lean_sidecar import runner

        monkeypatch.setattr(runner.os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setattr(runner.os, "getgid", lambda: 1000, raising=False)
        assert runner._container_user_spec() == "1000:1000"

    def test_build_command_refuses_to_launch_under_a_root_euid(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression (#2769): driving ``build_command`` under a root
        launcher refuses with ``RunnerConfigurationError`` — the error
        class the launcher service maps to a
        ``runner_configuration_error`` launch rejection — instead of
        emitting ``--user=0:0``."""
        from app.lean_sidecar import runner

        monkeypatch.setattr(runner.os, "getuid", lambda: 0, raising=False)
        monkeypatch.setattr(runner.os, "getgid", lambda: 0, raising=False)
        monkeypatch.setattr(runner.os, "geteuid", lambda: 0, raising=False)
        ws = resolve_workspace("run_root_launcher", tmp_artifacts_root)
        ws.ensure_layout()

        with pytest.raises(RunnerConfigurationError, match="must not run as root"):
            build_command(ws, DUMMY_DIGEST)

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

    @pytest.mark.parametrize(
        ("euid", "expected"),
        [
            # The launcher's host UID being non-root is the canonical signal
            # for "rootless podman" on Linux: the same UID identity is what
            # ``--userns=keep-id`` maps into the container. The previous
            # ``podman info`` subprocess probe was abandoned because its 15s
            # wall-clock timeout intermittently fired under concurrent podman
            # load (e.g., while a prior LEAN container was exiting and
            # releasing storage locks), silently misclassifying rootless as
            # rootful and dropping ``--userns=keep-id`` from the argv. The
            # euid check is microseconds and has no race window.
            pytest.param(1000, True, id="true_when_euid_non_root"),
            # Launcher running as root means rootful podman; the keep-id flag
            # is not valid there (podman rejects it at parse time with
            # "keep-id is only supported in rootless mode"). Omit the flag.
            pytest.param(0, False, id="false_when_euid_root"),
        ],
    )
    def test_is_rootless_podman_follows_euid(
        self,
        monkeypatch: pytest.MonkeyPatch,
        euid: int,
        expected: bool,
    ) -> None:
        from app.lean_sidecar import runner

        monkeypatch.setattr(runner.os, "geteuid", lambda: euid)
        assert runner._is_rootless_podman("/usr/bin/podman") is expected

    def test_is_rootless_podman_false_on_non_posix(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Windows hosts have no ``os.geteuid``; WSL2 handles UID
        mapping transparently, so ``--userns=keep-id`` is not needed.
        Omit the flag when the geteuid attribute is absent."""
        from app.lean_sidecar import runner

        monkeypatch.delattr(runner.os, "geteuid", raising=False)
        assert runner._is_rootless_podman("/usr/bin/podman") is False

    def test_accepts_pinned_dotnet_env_pair(
        self,
        tmp_artifacts_root: Path,
        _allow_dummy_digest: None,
    ) -> None:
        """The AppleHV-podman work-around: ``--env DOTNET_ReadyToRun=0``
        and ``--env DOTNET_TieredCompilation=0`` must pass the allow-
        list + pair validator and reach the constructed argv.

        Backend's ``compose.yaml`` documents the same pair as the fix
        for csc SIGILL (exit 132) on Apple Silicon; the LEAN sidecar
        exhibits the identical crash on wider trade-zip windows.
        """
        ws = resolve_workspace("run_dotnet_env", tmp_artifacts_root)
        ws.ensure_layout()
        plan = build_command(
            ws,
            DUMMY_DIGEST,
            hardening_flags=(
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=256m",
                "--env",
                "DOTNET_ReadyToRun=0",
                "--env",
                "DOTNET_TieredCompilation=0",
            ),
        )
        argv = plan.argv
        assert "--env" in argv
        assert "DOTNET_ReadyToRun=0" in argv
        assert "DOTNET_TieredCompilation=0" in argv


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
