"""Artifact descriptor for persisted Exhaustive Run studies."""

from __future__ import annotations

import re

from app.research.artifact.descriptor import ArtifactDescriptor
from app.research.exhaustive_run.errors import (
    ExhaustiveRunAlreadyExistsError,
    ExhaustiveRunCorruptError,
    ExhaustiveRunNotFoundError,
)

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

EXHAUSTIVE_RUN_ARTIFACT = ArtifactDescriptor(
    subdir="exhaustive-run",
    id_field="exhaustive_run_id",
    id_pattern=_ID_PATTERN,
    config_filename="config.json",
    result_filename="result.json",
    parent_run_id_extractor=lambda config: getattr(
        config,
        "source_walk_forward_id",
        None,
    ),
    log_tag="EXHAUSTIVE",
    not_found_error=ExhaustiveRunNotFoundError,
    already_exists_error=ExhaustiveRunAlreadyExistsError,
    corrupt_error=ExhaustiveRunCorruptError,
)
