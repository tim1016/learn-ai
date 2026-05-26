"""Artifact descriptor for the baselines phase.

Declared at module level (decision 6 of the seam doc) so the phase's
on-disk identity is grep-able from one place. Consumed by
``app/research/baselines/storage.py`` to construct an
``ArtifactStore`` at each call site.
"""

from __future__ import annotations

import re

from app.research.artifact.descriptor import ArtifactDescriptor
from app.research.baselines.errors import (
    BaselineAlreadyExistsError,
    BaselineCorruptError,
    BaselineNotFoundError,
)
from app.research.baselines.result import BaselineConfig

# Strict whitelist for ``baseline_id`` — exactly what
# ``uuid.uuid4().hex`` emits: 32 lowercase hex chars, no separators.
# Same shape every other phase uses.
_BASELINE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


BASELINES_ARTIFACT = ArtifactDescriptor(
    subdir="baselines",
    id_field="baseline_id",
    id_pattern=_BASELINE_ID_PATTERN,
    config_filename="config.json",
    result_filename="result.json",
    parent_run_id_extractor=lambda cfg: getattr(cfg, "parent_run_id", None),
    log_tag="BASELINES",
    not_found_error=BaselineNotFoundError,
    already_exists_error=BaselineAlreadyExistsError,
    corrupt_error=BaselineCorruptError,
)
"""On-disk identity of the baselines phase, consumed by ``ArtifactStore``."""


# Re-export the config type so other modules can import descriptor +
# config from one place if they want to. Not strictly required, but
# matches the pattern other phases adopt.
__all__ = ["BASELINES_ARTIFACT", "BaselineConfig"]
