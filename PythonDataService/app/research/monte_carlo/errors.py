"""Phase-named Monte Carlo exceptions, rebased onto shared artifact bases.

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


class MonteCarloNotFoundError(ArtifactNotFoundError):
    """Raised when an MC cannot be found under the given root."""


class MonteCarloAlreadyExistsError(ArtifactAlreadyExistsError):
    """Raised when ``save_monte_carlo`` would overwrite without ``replace=True``."""


class MonteCarloCorruptError(ArtifactCorruptError):
    """Raised when a persisted MC fails Pydantic validation."""
