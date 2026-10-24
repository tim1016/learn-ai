"""The run ledgers a store holds, for the specs that check what a run left on disk."""
from __future__ import annotations

from pathlib import Path

from app.research.artifact.store import ArtifactStore
from app.research.runs.descriptor import RUNS_ARTIFACT
from app.research.runs.ledger import RunLedger


def run_ids(root: Path, *, parent_run_id: str | None = None) -> list[str]:
    """Persisted run ids under ``root``, newest first, optionally those a parent run owns."""
    return ArtifactStore(RUNS_ARTIFACT, root=root).list_ids(parent_run_id=parent_run_id)


def run_ledgers(root: Path, *, parent_run_id: str | None = None) -> list[RunLedger]:
    """The ledgers of those runs, read as they sit on disk (a run whose result never landed still has one)."""
    return [
        RunLedger.model_validate_json((root / run_id / RUNS_ARTIFACT.config_filename).read_text(encoding="utf-8"))
        for run_id in run_ids(root, parent_run_id=parent_run_id)
    ]
