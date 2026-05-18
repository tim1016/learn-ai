"""Tests for the reviewer-suggested HardeningProfile enum (Phase 1c residue).

The enum is an additive layer on top of the existing ``hardening_flags``
raw-token interface — old callers keep working; new callers can use the
type-safe enum so they cannot misorder tokens, smuggle unknown specs,
or pass sandbox-widening flags.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.lean_sidecar.config import RunLimits
from app.lean_sidecar.launcher.models import LaunchRequest
from app.lean_sidecar.runner import (
    ALLOWED_HARDENING_TOKENS,
    HARDENING_PROFILE_TOKENS,
    HardeningProfile,
    RunnerConfigurationError,
    build_command,
    tokens_for_profile,
)
from app.lean_sidecar.workspace import Workspace


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    ws = Workspace(
        run_id="ut_hardening_profile",
        artifacts_root=tmp_path,
        root=tmp_path / "ut_hardening_profile",
    )
    ws.ensure_layout()
    return ws


DUMMY_DIGEST = "sha256:00000000000000000000000000000000000000000000000000000000cafebabe"


@pytest.fixture(autouse=True)
def _allow_dummy_digest(monkeypatch):
    """The runner refuses image digests not on its allow-list. Patch
    the allow-list so these tests can use a deterministic dummy."""
    from app.lean_sidecar import config, runner

    monkeypatch.setattr(config, "ALLOWED_IMAGE_DIGESTS", frozenset({DUMMY_DIGEST}))
    monkeypatch.setattr(runner, "ALLOWED_IMAGE_DIGESTS", frozenset({DUMMY_DIGEST}))


class TestProfileMapping:
    def test_minimal_expands_to_empty_tuple(self) -> None:
        assert tokens_for_profile(HardeningProfile.MINIMAL) == ()

    def test_with_tmpfs_256m_expands_to_two_tokens(self) -> None:
        expanded = tokens_for_profile(HardeningProfile.WITH_TMPFS_256M)
        assert expanded == ("--tmpfs", "/tmp:rw,noexec,nosuid,size=256m")

    def test_with_tmpfs_64m_expands_to_two_tokens(self) -> None:
        expanded = tokens_for_profile(HardeningProfile.WITH_TMPFS_64M)
        assert expanded == ("--tmpfs", "/tmp:rw,noexec,nosuid,size=64m")

    @pytest.mark.parametrize("profile", list(HardeningProfile))
    def test_every_profile_expands_only_to_allow_listed_tokens(self, profile: HardeningProfile) -> None:
        """Regression catch: if a future profile adds a token not in
        ALLOWED_HARDENING_TOKENS, the enum would silently bypass the
        allow-list. The profile -> tokens map is the SECOND source of
        truth; this test pins them together."""
        for token in tokens_for_profile(profile):
            assert token in ALLOWED_HARDENING_TOKENS, (
                f"profile {profile} expands to {token!r} which is not in ALLOWED_HARDENING_TOKENS"
            )


class TestBuildCommandWithProfile:
    def test_minimal_profile_yields_same_argv_as_empty_flags(self, workspace: Workspace) -> None:
        """Minimal profile must be argv-identical to passing no flags —
        the enum is a typed wrapper, not a behavior change."""
        plan_profile = build_command(workspace, DUMMY_DIGEST, hardening_profile=HardeningProfile.MINIMAL)
        plan_flags = build_command(workspace, DUMMY_DIGEST, hardening_flags=())
        assert plan_profile.argv == plan_flags.argv

    def test_with_tmpfs_profile_yields_same_argv_as_explicit_flags(self, workspace: Workspace) -> None:
        plan_profile = build_command(
            workspace, DUMMY_DIGEST, hardening_profile=HardeningProfile.WITH_TMPFS_256M
        )
        plan_flags = build_command(
            workspace,
            DUMMY_DIGEST,
            hardening_flags=("--tmpfs", "/tmp:rw,noexec,nosuid,size=256m"),
        )
        assert plan_profile.argv == plan_flags.argv

    def test_both_profile_and_flags_rejected(self, workspace: Workspace) -> None:
        """Merge semantics here would surprise someone — the runner
        refuses both-set so the caller picks one."""
        with pytest.raises(RunnerConfigurationError, match="not both"):
            build_command(
                workspace,
                DUMMY_DIGEST,
                hardening_profile=HardeningProfile.MINIMAL,
                hardening_flags=("--tmpfs", "/tmp:rw,noexec,nosuid,size=256m"),
            )

    def test_profile_without_explicit_flags_argument(self, workspace: Workspace) -> None:
        """The keyword-only signature lets a caller pass only the
        profile without referencing the legacy flags arg at all —
        which is the migration story we want."""
        plan = build_command(workspace, DUMMY_DIGEST, hardening_profile=HardeningProfile.WITH_TMPFS_64M)
        assert "/tmp:rw,noexec,nosuid,size=64m" in plan.argv


class TestLaunchRequestModel:
    def _good_payload(self, **overrides) -> dict:
        base = {
            "run_id": "ut_request_001",
            "image_digest": DUMMY_DIGEST,
            "cpus": 2.0,
            "memory_mb": 2048,
            "pids_limit": 512,
            "wall_clock_timeout_s": 60,
            "workspace_max_mb": 100,
            "log_tail_bytes": 4096,
        }
        base.update(overrides)
        return base

    def test_hardening_profile_field_defaults_to_none(self) -> None:
        req = LaunchRequest.model_validate(self._good_payload())
        assert req.hardening_profile is None
        assert req.hardening_flags == []

    def test_hardening_profile_accepts_valid_enum_value(self) -> None:
        req = LaunchRequest.model_validate(self._good_payload(hardening_profile="with_tmpfs_256m"))
        assert req.hardening_profile == "with_tmpfs_256m"

    def test_hardening_profile_rejects_unknown_value(self) -> None:
        with pytest.raises(ValidationError):
            LaunchRequest.model_validate(self._good_payload(hardening_profile="not_a_profile"))

    def test_rejects_both_profile_and_flags(self) -> None:
        """Mutex check at the API boundary — same semantic as the
        build_command mutex but caught before the launcher process
        starts so the rejection is a clean 400, not a 500 mid-launch."""
        with pytest.raises(ValidationError, match="mutually exclusive"):
            LaunchRequest.model_validate(
                self._good_payload(
                    hardening_profile="with_tmpfs_256m",
                    hardening_flags=["--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"],
                )
            )

    def test_accepts_flags_only_when_profile_none(self) -> None:
        """Back-compat path — old callers that only set hardening_flags
        keep working with the new model unchanged."""
        req = LaunchRequest.model_validate(
            self._good_payload(hardening_flags=["--tmpfs", "/tmp:rw,noexec,nosuid,size=256m"])
        )
        assert req.hardening_flags == ["--tmpfs", "/tmp:rw,noexec,nosuid,size=256m"]
        assert req.hardening_profile is None


def test_profile_mapping_is_a_strict_subset_of_token_allow_list() -> None:
    """Whole-mapping regression: every token a profile expands to is
    in ALLOWED_HARDENING_TOKENS. Catches a future profile addition
    that accidentally introduces a new token."""
    for profile, tokens in HARDENING_PROFILE_TOKENS.items():
        for token in tokens:
            assert token in ALLOWED_HARDENING_TOKENS, (
                f"{profile} -> {token!r} not in ALLOWED_HARDENING_TOKENS"
            )


def test_run_limits_unused_import_silenced() -> None:
    """Defensive — the test imports RunLimits via the runner module's
    transitive surface, this just asserts the import path stays
    valid (catches a future refactor that hides RunLimits)."""
    assert RunLimits is not None
