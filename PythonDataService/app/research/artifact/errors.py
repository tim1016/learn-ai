"""Shared base exceptions for the artifact seam.

Each research phase keeps its own phase-named exception classes
(``MonteCarloNotFoundError``, ``WalkForwardNotFoundError``, ...) so
existing ``pytest.raises(...)`` sites and routers that map specific
exceptions to specific HTTP codes keep working unchanged. Those
phase-named classes now inherit from these shared bases, so new
common code can ``except ArtifactError`` without enumerating every
phase. See ``docs/architecture/research-artifact-seam.md`` §
"Shared base errors".
"""

from __future__ import annotations


class ArtifactError(Exception):
    """Base class for any artifact-store failure."""


class ArtifactNotFoundError(ArtifactError, LookupError):
    """Raised when ``ArtifactStore.load`` cannot find an artifact id."""


class ArtifactAlreadyExistsError(ArtifactError, FileExistsError):
    """Raised when ``ArtifactStore.save`` would overwrite without ``replace=True``."""


class ArtifactCorruptError(ArtifactError, RuntimeError):
    """Raised when a persisted artifact fails Pydantic validation on load."""
