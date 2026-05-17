"""Launcher HTTP request / response models.

Per the ADR, the request payload is intentionally minimal: ``run_id``,
``image``, and ``limits``. The launcher resolves the workspace path
itself; the data plane never sends paths and cannot widen the mount.

Authority: ``docs/architecture/lean-sidecar-lab.md`` §"Launcher
shape".
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

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
    hardening_flags: list[str] = Field(default_factory=list)
    extra_image_args: list[str] = Field(default_factory=list)

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


class LaunchResponse(BaseModel):
    """Result returned to the data plane after the container exits."""

    run_id: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    log_tail: str
