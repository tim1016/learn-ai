"""Domain errors for persisted Exhaustive Run research artifacts."""

from __future__ import annotations

from app.research.artifact.errors import (
    ArtifactAlreadyExistsError,
    ArtifactCorruptError,
    ArtifactNotFoundError,
)


class ExhaustiveRunNotFoundError(ArtifactNotFoundError):
    """Raised when an Exhaustive Run artifact cannot be found."""


class ExhaustiveRunAlreadyExistsError(ArtifactAlreadyExistsError):
    """Raised when an Exhaustive Run id collides with an existing artifact."""


class ExhaustiveRunCorruptError(ArtifactCorruptError):
    """Raised when a persisted Exhaustive Run fails schema validation."""


class ExhaustiveRunSourceError(ValueError):
    """Raised when the source walk-forward is not valid canonical evidence."""
