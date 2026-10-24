"""Where study records live: one directory per study under the research artifact root.

``<root>/<study>/<record_id>/config.json`` and ``result.json``, written and read through ``ArtifactStore`` like the
walk-forward, Monte Carlo and baseline stores beside them. The new directories hold nothing the older readers look at:
the runs store lists only 32-hex directory names, and each of the others reads only its own subdirectory, so adding these
changes no existing listing, and a build without them ignores them.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from app.research.artifact import (
    ArtifactAlreadyExistsError,
    ArtifactCorruptError,
    ArtifactDescriptor,
    ArtifactNotFoundError,
    ArtifactStore,
)
from app.research.study_records.models import (
    RECORD_ID_PATTERN,
    STUDY_TYPES,
    StudyRecordConfig,
    StudyRecordResult,
    StudyType,
)


class StudyRecordNotFoundError(ArtifactNotFoundError):
    """No record of this study has this id."""


class StudyRecordAlreadyExistsError(ArtifactAlreadyExistsError):
    """A record with this id exists; records are written once."""


class StudyRecordCorruptError(ArtifactCorruptError):
    """A record's file cannot be read as a record of this version."""


def _descriptor(study: StudyType) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        subdir=study,
        id_field="record_id",
        id_pattern=RECORD_ID_PATTERN,
        config_filename="config.json",
        result_filename="result.json",
        parent_run_id_extractor=lambda cfg: getattr(cfg, "rerun_of", None),
        log_tag=f"STUDY:{study}",
        not_found_error=StudyRecordNotFoundError,
        already_exists_error=StudyRecordAlreadyExistsError,
        corrupt_error=StudyRecordCorruptError,
    )


STUDY_RECORD_ARTIFACTS: Mapping[StudyType, ArtifactDescriptor] = {study: _descriptor(study) for study in STUDY_TYPES}
"""On-disk identity of each study's records, consumed by ``ArtifactStore``."""


class StudyRecordStore:
    """One study's records. ``root`` is for tests; ``None`` is the real artifact root."""

    def __init__(self, study: StudyType, *, root: Path | None = None) -> None:
        self.study: StudyType = study
        self._store = ArtifactStore(STUDY_RECORD_ARTIFACTS[study], root=root)

    def save(self, config: StudyRecordConfig, result: StudyRecordResult) -> Path:
        """Write a finished run. Raises if the id is taken, and if the two files name different records or studies."""
        if config.study != self.study:
            raise ValueError(f"a {config.study} record cannot be saved in the {self.study} store")
        if config.record_id != result.record_id:
            raise ValueError(f"config.record_id {config.record_id!r} does not match result.record_id {result.record_id!r}")
        return self._store.save(config, result)

    def load(self, record_id: str) -> tuple[StudyRecordConfig, StudyRecordResult]:
        """The record, or ``StudyRecordNotFoundError`` / ``StudyRecordCorruptError``. A malformed id raises ``ValueError``."""
        config, result = self._store.load(record_id, config_type=StudyRecordConfig, result_type=StudyRecordResult)
        if config.study != self.study or config.record_id != record_id or result.record_id != record_id:
            raise StudyRecordCorruptError(f"the files under {record_id} do not describe that {self.study} record")
        return config, result

    def exists(self, record_id: str) -> bool:
        return any(found == record_id for found, _ in self._store.config_paths())

    def config_paths(self) -> list[tuple[str, Path]]:
        return self._store.config_paths()
