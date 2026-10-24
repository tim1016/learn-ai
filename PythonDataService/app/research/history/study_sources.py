"""The runs the five studies record themselves, as History stores (#3520).

Feature, Signal, Cross-sectional, Indicator reliability and Realized vs IV each keep one record per run under
``<artifacts>/<study>/<record_id>/`` (``app.research.study_records``). These sources read the small ``config.json`` of
each and never its result: how a run ended, its headline and its inputs are all there, so a listing opens no result.

A run's question, its instruments and its window are read from its inputs by the study's spec. A rerun's record names the
record it was prefilled from, shown as its parent. A directory whose config cannot be read as a record of this version
is counted in ``unreadable`` like any other store's, so a record from a newer build is reported, not silently dropped.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.research.history.artifact_sources import _ArtifactSource, _Own, _read_text, _Settled
from app.research.study_records.models import StudyRecordConfig, StudyType
from app.research.study_records.specs import STUDY_SPECS, StudySpec
from app.research.study_records.store import STUDY_RECORD_ARTIFACTS
from app.schemas.research_history import ResearchHistoryRef

_DIGEST_SHOWN = 12


def _provenance(config: StudyRecordConfig) -> str | None:
    """The data the run read, as far as the record knows it; ``None`` reads "not recorded", never a guess."""
    block = config.data_source
    if block is not None:
        digest = block.get("snapshot_digest") or block.get("identity_digest")
        version = block.get("calculation_version")
        parts = [str(block.get("kind") or "data source")]
        if digest:
            parts.append(f"snapshot {str(digest)[:_DIGEST_SHOWN]}")
        if version:
            parts.append(str(version))
        return "; ".join(parts)
    if config.data_identity:
        return f"data {config.data_identity[:_DIGEST_SHOWN]}"
    return None


def _own_reader(spec: StudySpec) -> Callable[[str], _Own]:
    def read(path: str) -> _Own:
        config = StudyRecordConfig.model_validate_json(_read_text(path))
        if config.study != spec.study:
            raise ValueError(f"a {config.study} record under the {spec.study} store")
        try:
            question, symbols, window = spec.question(config.inputs), spec.symbols(config.inputs), spec.window(config.inputs)
        except (KeyError, TypeError) as exc:  # the inputs lack a field the spec reads: a record this build cannot index
            raise ValueError(f"inputs the {spec.study} spec cannot read: {exc!r}") from exc
        return _Own(
            native_id=config.record_id,
            created_at_ms=config.created_at_ms,
            question=question,
            provenance=_provenance(config),
            parent_run_id=config.rerun_of,
            symbols=symbols,
            window=window,
            settled=_Settled(config.status, config.status_detail, config.completed_at_ms),
            headline=config.headline,
        )

    return read


_OWN_READERS: dict[StudyType, Callable[[str], _Own]] = {study: _own_reader(spec) for study, spec in STUDY_SPECS.items()}
"""Built once: the parsed configs are kept per reader and file, so a reader rebuilt per request would keep none."""


class StudyRecords(_ArtifactSource):
    """One study's records, read through the same ``ArtifactStore`` that wrote them."""

    def __init__(self, spec: StudySpec, root: Path | None = None) -> None:
        super().__init__(root)
        self.type = spec.study
        self.label = spec.label
        self.descriptor = STUDY_RECORD_ARTIFACTS[spec.study]
        self.own_reader = _OWN_READERS[spec.study]

    def _parent_of(self, own: _Own) -> ResearchHistoryRef | None:
        """A rerun's parent is the record it was prefilled from, of the same study."""
        return None if own.parent_run_id is None else ResearchHistoryRef(type=self.type, native_id=own.parent_run_id)


def study_record_sources(root: Path | None = None) -> tuple[StudyRecords, ...]:
    """One source per study that keeps records, in the order the studies are listed."""
    return tuple(StudyRecords(spec, root) for spec in STUDY_SPECS.values())
