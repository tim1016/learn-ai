"""Phase-named baseline exceptions, rebased onto shared artifact bases.

Existing call sites (router, tests) catch by these specific class
names and continue to work unchanged; new common code can
``except ArtifactError`` and catch this family alongside the other
phases'. See ``docs/architecture/research-artifact-seam.md`` §
"Shared base errors" for the rationale.
"""

from __future__ import annotations

from app.research.artifact.errors import (
    ArtifactAlreadyExistsError,
    ArtifactCorruptError,
    ArtifactNotFoundError,
)


class BaselineNotFoundError(ArtifactNotFoundError):
    """Raised when a baseline cannot be found under the given root."""


class BaselineAlreadyExistsError(ArtifactAlreadyExistsError):
    """Raised when ``save_baseline`` would overwrite without ``replace=True``."""


class BaselineCorruptError(ArtifactCorruptError):
    """Raised when a persisted baseline fails Pydantic validation."""
