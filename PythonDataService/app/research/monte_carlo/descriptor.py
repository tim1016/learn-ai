"""Artifact descriptor for the Monte Carlo phase.

Declared at module level (decision 6 of the seam doc) so the phase's
on-disk identity is grep-able from one place. Consumed by
``app/research/monte_carlo/storage.py`` to construct an
``ArtifactStore`` at each call site.
"""

from __future__ import annotations

import re

from app.research.artifact.descriptor import ArtifactDescriptor
from app.research.monte_carlo.errors import (
    MonteCarloAlreadyExistsError,
    MonteCarloCorruptError,
    MonteCarloNotFoundError,
)
from app.research.monte_carlo.result import MonteCarloConfig

# Strict whitelist for ``monte_carlo_id`` — exactly what
# ``uuid.uuid4().hex`` emits: 32 lowercase hex chars, no separators.
# Tighter than ``[0-9a-fA-F-]{8,64}`` per the same nitpick the runs
# storage applied (PR #107 follow-up).
_MC_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


MONTE_CARLO_ARTIFACT = ArtifactDescriptor(
    subdir="monte-carlo",
    id_field="monte_carlo_id",
    id_pattern=_MC_ID_PATTERN,
    config_filename="config.json",
    result_filename="result.json",
    parent_run_id_extractor=lambda cfg: getattr(cfg, "parent_run_id", None),
    not_found_error=MonteCarloNotFoundError,
    already_exists_error=MonteCarloAlreadyExistsError,
    corrupt_error=MonteCarloCorruptError,
)
"""On-disk identity of the Monte Carlo phase, consumed by ``ArtifactStore``."""


# Re-export the config type so other modules can import descriptor +
# config from one place if they want to. Not strictly required, but
# matches the pattern other phases will adopt in PRs 2-4.
__all__ = ["MONTE_CARLO_ARTIFACT", "MonteCarloConfig"]
