"""Descriptor that parametrises an ``ArtifactStore`` for one phase.

Per ``docs/architecture/research-artifact-seam.md`` decisions 1, 5, 6:
the on-disk layout stays heterogeneous (each phase keeps its own
subdir, filenames, and parent-id extractor), exception classes stay
phase-named (supplied via the descriptor), and the store is bound at
construction time rather than receiving these knobs per call.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from app.research.artifact.errors import (
    ArtifactAlreadyExistsError,
    ArtifactCorruptError,
    ArtifactNotFoundError,
)


@dataclass(frozen=True)
class ArtifactDescriptor:
    """Per-phase configuration consumed by ``ArtifactStore``.

    Fields:
      * ``subdir`` — directory under the artifacts root, e.g.
        ``"monte-carlo"``. Heterogeneous across phases by design
        (decision 1 in the seam doc).
      * ``id_pattern`` — strict regex on the artifact id. Used both
        as the format check and as defence in depth alongside the
        resolved-path containment guard.
      * ``config_filename`` / ``result_filename`` — on-disk names.
        ``runs`` uses ``ledger.json`` as its config; the others use
        ``config.json``. Both phases use ``result.json`` today, but
        the field is split so this can vary later without re-touching
        the store.
      * ``parent_run_id_extractor`` — callable that returns the parent
        run id (if any) from a *config* model. Used by ``list_ids``
        for the parent-run-id filter.
      * ``hash_payload`` — optional callback that returns the
        canonical-JSON hash of the *config* model. Opt-in per phase
        (decision 2). When absent, no hashing happens; when present,
        the store records the hash on save (PR 4 wires this up for
        ``runs``).
      * ``not_found_error`` / ``already_exists_error`` / ``corrupt_error``
        — the exception classes the store raises. Phase-named classes
        inherit from the shared bases so ``except ArtifactError``
        catches them, while existing per-phase ``pytest.raises``
        sites and router exception handlers stay unchanged
        (decision 5).
    """

    subdir: str
    id_pattern: re.Pattern[str]
    config_filename: str
    result_filename: str
    parent_run_id_extractor: Callable[[BaseModel], str | None]
    hash_payload: Callable[[BaseModel], str] | None = None
    not_found_error: type[ArtifactNotFoundError] = ArtifactNotFoundError
    already_exists_error: type[ArtifactAlreadyExistsError] = ArtifactAlreadyExistsError
    corrupt_error: type[ArtifactCorruptError] = ArtifactCorruptError
