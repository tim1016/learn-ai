"""Launcher HTTP request / response models.

Per the ADR, the request payload is intentionally minimal: ``run_id``,
``image``, and ``limits``. The launcher resolves the workspace path
itself; the data plane never sends paths and cannot widen the mount.

Authority: ``docs/architecture/lean-sidecar-lab.md`` §"Launcher
shape".
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from app.lean_sidecar.workspace import RUN_ID_PATTERN


class LaunchRequest(BaseModel):
    """Minimal launch request from the data plane."""

    run_id: str = Field(
        ...,
        description=("Strict slug; resolved to a workspace path under the launcher's configured artifacts root."),
    )
    image_digest: str = Field(
        ...,
        description=("Pinned image digest (sha256:...). Must be in the launcher's allow-list."),
    )
    cpus: float = Field(..., gt=0)
    memory_mb: int = Field(..., gt=0)
    pids_limit: int = Field(..., gt=0)
    wall_clock_timeout_s: int = Field(..., gt=0)
    workspace_max_mb: int = Field(..., gt=0)
    log_tail_bytes: int = Field(..., gt=0)
    hardening_flags: list[str] = Field(
        default_factory=list,
        description=(
            "Optional --read-only / --tmpfs tokens. Every token must be in "
            "ALLOWED_HARDENING_TOKENS; the launcher refuses unknown tokens "
            "to prevent sandbox-widening flags like --privileged. "
            "Mutually exclusive with ``hardening_profile``."
        ),
    )
    hardening_profile: str | None = Field(
        default=None,
        description=(
            "Optional typed alternative to ``hardening_flags`` — accepts "
            "one of the ``HardeningProfile`` enum values "
            "('minimal' / 'with_tmpfs_256m' / 'with_tmpfs_64m'). The "
            "launcher expands it into the same argv tokens as the matching "
            "``hardening_flags`` would produce. Mutually exclusive with "
            "``hardening_flags`` — passing both is a 400."
        ),
    )

    @field_validator("hardening_profile")
    @classmethod
    def _validate_profile_name(cls, v: str | None) -> str | None:
        # Importing inside the validator avoids a circular import:
        # runner.py depends on workspace + config only, but launcher
        # models live above runner in the dependency graph.
        if v is None:
            return v
        from app.lean_sidecar.runner import HardeningProfile

        valid = {p.value for p in HardeningProfile}
        if v not in valid:
            raise ValueError(
                f"hardening_profile must be one of {sorted(valid)}, got {v!r}"
            )
        return v

    @field_validator("run_id")
    @classmethod
    def _validate_run_id(cls, v: str) -> str:
        if not RUN_ID_PATTERN.fullmatch(v):
            raise ValueError("run_id must match ^[a-z0-9][a-z0-9_-]{2,63}$")
        return v

    @field_validator("image_digest")
    @classmethod
    def _validate_image_digest(cls, v: str) -> str:
        bare = v.split("@", 1)[-1]
        if not bare.startswith("sha256:"):
            raise ValueError("image_digest must be pinned (start with sha256:)")
        return v

    @model_validator(mode="after")
    def _reject_mutually_exclusive_hardening(self) -> LaunchRequest:
        """Passing both ``hardening_flags`` and ``hardening_profile`` is
        ambiguous: merge semantics would surprise someone (do they
        concatenate? does profile win? does flags win?). Reject up
        front so the caller picks one."""
        if self.hardening_profile is not None and self.hardening_flags:
            raise ValueError(
                "hardening_profile and hardening_flags are mutually exclusive; pick one"
            )
        return self


class LaunchResponse(BaseModel):
    """Result returned to the data plane after the container exits.

    ``exit_code == 0`` alone is not a "clean" signal — LEAN can crash
    its ResultsAnalyzer, fail data requests, or raise in
    Algorithm.Initialize while still exiting 0. ``lean_errors``
    summarizes any ``ERROR::`` lines in ``output/log.txt``, bucketed by
    category, so callers can decide whether the run is acceptable for
    compatibility (warnings allowed) or reconciliation-grade (no
    analysis failures, no failed data requests).
    """

    run_id: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    log_tail: str
    lean_errors: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Categorized LEAN ERROR:: lines, keyed by category "
            "(analysis_failed | failed_data_requests | runtime_error | other). "
            "Empty dict means LEAN's log.txt had no errors."
        ),
    )
    is_clean: bool = Field(
        ...,
        description=(
            "True iff exit_code == 0 AND lean_errors is empty AND not "
            "timed_out. The single boolean callers should branch on."
        ),
    )


class ExtractMetadataRequest(BaseModel):
    """Request to extract LEAN's bundled metadata into a workspace.

    Hardening-fix for the launcher topology: the data-plane container
    does not have ``podman`` on PATH, so it cannot subprocess-spawn
    ``podman cp`` to pull ``market-hours-database.json``,
    ``symbol-properties-database.csv``, and the ``interest-rate``
    subtree out of the LEAN image. The launcher (a host process with
    podman) owns this work and exposes it via
    ``POST /extract-metadata``; the data plane calls it when its own
    ``shutil.which('podman')`` returns None.
    """

    run_id: str = Field(
        ...,
        description=(
            "Strict slug; resolved to a workspace path under the "
            "launcher's configured artifacts root. The same value the "
            "data plane uses for the matching ``/launch`` request."
        ),
    )
    image_digest: str = Field(
        ...,
        description=(
            "Pinned image digest (``sha256:...``). Must be in the "
            "launcher's allow-list, same as for ``/launch``."
        ),
    )

    @field_validator("run_id")
    @classmethod
    def _validate_run_id(cls, v: str) -> str:
        if not RUN_ID_PATTERN.fullmatch(v):
            raise ValueError("run_id must match ^[a-z0-9][a-z0-9_-]{2,63}$")
        return v

    @field_validator("image_digest")
    @classmethod
    def _validate_image_digest(cls, v: str) -> str:
        bare = v.split("@", 1)[-1]
        if not bare.startswith("sha256:"):
            raise ValueError("image_digest must be pinned (start with sha256:)")
        return v


class ExtractMetadataResponse(BaseModel):
    """Paths the launcher just wrote, as the launcher sees them.

    The data plane sees the same files under its own view of the
    bind-mounted artifacts root, so it does not need these paths to do
    its own manifest hashing — the data plane re-resolves the workspace
    against its in-container ``DEFAULT_ARTIFACTS_ROOT``. The paths are
    returned anyway for log auditability + a launcher-side sanity check.
    """

    market_hours_db_path: str
    symbol_properties_db_path: str
