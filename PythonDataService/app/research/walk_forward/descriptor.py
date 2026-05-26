"""Artifact descriptor for the walk-forward phase.

Declared at module level (decision 6 of the seam doc) so the phase's
on-disk identity is grep-able from one place. Consumed by
``app/research/walk_forward/storage.py`` to construct an
``ArtifactStore`` at each call site.
"""

from __future__ import annotations

import re

from app.research.artifact.descriptor import ArtifactDescriptor
from app.research.walk_forward.errors import (
    WalkForwardAlreadyExistsError,
    WalkForwardCorruptError,
    WalkForwardNotFoundError,
)
from app.research.walk_forward.result import WalkForwardConfig

# Same alphabet as ``run_id`` / ``monte_carlo_id`` — exactly what
# ``uuid.uuid4().hex`` emits: 32 lowercase hex chars, no separators.
_WF_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


WALK_FORWARD_ARTIFACT = ArtifactDescriptor(
    subdir="walk-forward",
    id_field="walk_forward_id",
    id_pattern=_WF_ID_PATTERN,
    config_filename="config.json",
    result_filename="result.json",
    parent_run_id_extractor=lambda cfg: getattr(cfg, "parent_run_id", None),
    log_tag="WF",
    not_found_error=WalkForwardNotFoundError,
    already_exists_error=WalkForwardAlreadyExistsError,
    corrupt_error=WalkForwardCorruptError,
)
"""On-disk identity of the walk-forward phase, consumed by ``ArtifactStore``."""


__all__ = ["WALK_FORWARD_ARTIFACT", "WalkForwardConfig"]
