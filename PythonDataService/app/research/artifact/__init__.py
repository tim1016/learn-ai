"""Shared artifact-store seam under the four research-run phases.

See ``docs/architecture/research-artifact-seam.md`` for the design.
Each phase declares an ``ArtifactDescriptor`` at module level and
constructs an ``ArtifactStore(descriptor, root=...)`` at the call
site; the phase's thin ``storage.py`` delegates ``save``/``load``/
``list_ids`` through that store while keeping its public exception
classes and any phase-specific filters of its own.
"""

from __future__ import annotations

from app.research.artifact.descriptor import ArtifactDescriptor
from app.research.artifact.errors import (
    ArtifactAlreadyExistsError,
    ArtifactCorruptError,
    ArtifactError,
    ArtifactNotFoundError,
)
from app.research.artifact.root import default_artifacts_root
from app.research.artifact.store import ArtifactStore

__all__ = [
    "ArtifactAlreadyExistsError",
    "ArtifactCorruptError",
    "ArtifactDescriptor",
    "ArtifactError",
    "ArtifactNotFoundError",
    "ArtifactStore",
    "default_artifacts_root",
]
