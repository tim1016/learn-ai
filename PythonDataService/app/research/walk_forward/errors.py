"""Phase-named walk-forward exceptions, rebased onto shared artifact bases.

Existing call sites (router, tests) catch by these specific class
names and continue to work unchanged; new common code can
``except ArtifactError`` and catch this family alongside the other
phases'. See ``docs/architecture/research-artifact-seam.md`` §
"Shared base errors" for the rationale.

The classes used to live in ``walk_forward/storage.py``; they moved
here in PR 3 so ``storage.py`` can import the descriptor without
creating an ``errors.py`` → ``descriptor.py`` → ``storage.py``
circular import.
"""

from __future__ import annotations

from app.research.artifact.errors import (
    ArtifactAlreadyExistsError,
    ArtifactCorruptError,
    ArtifactNotFoundError,
)


class WalkForwardNotFoundError(ArtifactNotFoundError):
    """Raised when a WF cannot be found under the given root."""


class WalkForwardAlreadyExistsError(ArtifactAlreadyExistsError):
    """Raised when ``save_walk_forward`` would overwrite without ``replace=True``."""


class WalkForwardCorruptError(ArtifactCorruptError):
    """Raised when a persisted WF fails Pydantic validation."""
