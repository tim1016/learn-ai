"""Atomic persistence for Exhaustive Run research artifacts."""

from __future__ import annotations

from pathlib import Path

from app.research.artifact.store import ArtifactStore
from app.research.exhaustive_run.descriptor import EXHAUSTIVE_RUN_ARTIFACT
from app.research.exhaustive_run.errors import ExhaustiveRunNotFoundError
from app.research.exhaustive_run.result import ExhaustiveRunConfig, ExhaustiveRunResult


def _store(root: Path | None) -> ArtifactStore:
    return ArtifactStore(EXHAUSTIVE_RUN_ARTIFACT, root=root)


def save_exhaustive_run(
    config: ExhaustiveRunConfig,
    result: ExhaustiveRunResult,
    *,
    root: Path | None = None,
) -> Path:
    """Persist a matching Exhaustive Run config/result pair atomically."""
    if config.exhaustive_run_id != result.exhaustive_run_id:
        raise ValueError("Exhaustive Run config and result ids do not match")
    if config.source_walk_forward_id != result.source_walk_forward_id:
        raise ValueError("Exhaustive Run config and result source ids do not match")
    return _store(root).save(config, result)


def load_exhaustive_run(
    exhaustive_run_id: str,
    *,
    root: Path | None = None,
) -> tuple[ExhaustiveRunConfig, ExhaustiveRunResult]:
    """Load one Exhaustive Run by its immutable artifact id."""
    return _store(root).load(
        exhaustive_run_id,
        config_type=ExhaustiveRunConfig,
        result_type=ExhaustiveRunResult,
    )


def load_latest_for_walk_forward(
    walk_forward_id: str,
    *,
    root: Path | None = None,
) -> tuple[ExhaustiveRunConfig, ExhaustiveRunResult]:
    """Load the newest Exhaustive Run linked to a walk-forward receipt."""
    ids = _store(root).list_ids(parent_run_id=walk_forward_id, limit=1)
    if not ids:
        raise ExhaustiveRunNotFoundError(
            f"no Exhaustive Run found for walk-forward {walk_forward_id}"
        )
    return load_exhaustive_run(ids[0], root=root)
