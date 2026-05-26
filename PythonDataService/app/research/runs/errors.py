"""Phase-named runs/ exceptions, rebased onto shared artifact bases.

Existing call sites (router, monte_carlo/baselines runners, tests)
catch by these specific class names and continue to work unchanged;
new common code can ``except ArtifactError`` and catch this family
alongside the other phases'. See
``docs/architecture/research-artifact-seam.md`` § "Shared base errors"
for the rationale.

The classes used to live in ``runs/storage.py``; they moved here in
PR 4 so ``storage.py`` can import the descriptor without creating
an ``errors.py`` → ``descriptor.py`` → ``storage.py`` circular
import. ``runs/storage.py`` re-exports the names so callers that
imported them from that module path keep working.
"""

from __future__ import annotations

from app.research.artifact.errors import (
    ArtifactAlreadyExistsError,
    ArtifactCorruptError,
    ArtifactNotFoundError,
)


class RunNotFoundError(ArtifactNotFoundError):
    """Raised when ``load_run`` cannot find a ``run_id`` under the given root."""


class RunAlreadyExistsError(ArtifactAlreadyExistsError):
    """Raised when ``save_run`` would overwrite a ``run_id`` without ``replace=True``."""


class RunCorruptError(ArtifactCorruptError):
    """Raised when a persisted ledger or result fails Pydantic validation."""
