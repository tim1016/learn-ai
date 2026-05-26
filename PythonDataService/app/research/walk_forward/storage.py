"""Thin walk-forward persistence delegators over the shared artifact store.

The mechanics — id regex, path-traversal guard, atomic tmp+rename,
load-and-validate, scan+filter — live in
``app/research/artifact/store.py``. This module declares the walk-
forward specific surface: function signatures the runner and router
already call, plus the phase-specific ``spec_hash`` filter on
``list_walk_forwards`` that the artifact store's generic
``list_ids`` doesn't carry. See
``docs/architecture/research-artifact-seam.md`` for the design.

On-disk layout (unchanged from pre-seam):

    <root>/walk-forward/<wf_id>/
        config.json   — WalkForwardConfig.model_dump(mode='json')
        result.json   — WalkForwardResult.model_dump(mode='json')

Each fold's individual run is *not* persisted here — folds are normal
``RunLedger`` records under ``<root>/<fold_run_id>/`` (Phase A
storage). The ``parent_run_id`` field on each fold's ledger points
back at this WF so ``list_runs(parent_run_id=wf_id)`` enumerates them.

Public exception classes (``WalkForwardNotFoundError``,
``WalkForwardAlreadyExistsError``, ``WalkForwardCorruptError``) are
defined in ``app.research.walk_forward.errors`` and re-exported here
so existing callers that imported them from this module path keep
working.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.research.artifact.root import default_artifacts_root
from app.research.artifact.store import ArtifactStore
from app.research.walk_forward.descriptor import WALK_FORWARD_ARTIFACT
from app.research.walk_forward.errors import (
    WalkForwardAlreadyExistsError,
    WalkForwardCorruptError,
    WalkForwardNotFoundError,
)
from app.research.walk_forward.result import WalkForwardConfig, WalkForwardResult

logger = logging.getLogger(__name__)


# Re-exported for backwards compatibility with any caller that
# imported these classes from ``app.research.walk_forward.storage``
# directly. The canonical definitions live in
# ``app.research.walk_forward.errors``.
__all__ = [
    "WalkForwardAlreadyExistsError",
    "WalkForwardCorruptError",
    "WalkForwardNotFoundError",
    "default_artifacts_root",
    "list_walk_forwards",
    "load_walk_forward",
    "save_walk_forward",
]


def _store(root: Path | None) -> ArtifactStore:
    """Construct an ``ArtifactStore`` bound to the walk-forward descriptor."""
    return ArtifactStore(WALK_FORWARD_ARTIFACT, root=root)


def save_walk_forward(
    config: WalkForwardConfig,
    result: WalkForwardResult,
    *,
    root: Path | None = None,
    replace: bool = False,
) -> Path:
    """Persist ``(config, result)`` to disk and return the WF directory."""
    if config.walk_forward_id != result.walk_forward_id:
        raise ValueError(
            f"config.walk_forward_id {config.walk_forward_id!r} does not match "
            f"result.walk_forward_id {result.walk_forward_id!r}"
        )
    return _store(root).save(config, result, replace=replace)


def load_walk_forward(
    wf_id: str, *, root: Path | None = None
) -> tuple[WalkForwardConfig, WalkForwardResult]:
    """Load a previously-saved WF by ``wf_id``."""
    return _store(root).load(
        wf_id,
        config_type=WalkForwardConfig,
        result_type=WalkForwardResult,
    )


def list_walk_forwards(
    *,
    root: Path | None = None,
    parent_run_id: str | None = None,
    spec_hash: str | None = None,
    since_ms: int | None = None,
    limit: int | None = None,
) -> list[WalkForwardConfig]:
    """Enumerate persisted walk-forwards, optionally filtered, newest first.

    Returns ``WalkForwardConfig`` records (not full results) — the
    listing endpoint shape mirrors ``list_runs``: lightweight summary
    with full fetch on-demand via ``GET /walk-forward/{wf_id}``.

    Shared filters (``parent_run_id``, ``since_ms``) are applied
    inside the artifact store; the WF-specific ``spec_hash`` filter
    is applied here because the store doesn't (and shouldn't) know
    about phase-specific Pydantic fields. ``limit`` is enforced
    *after* the ``spec_hash`` filter — matching the pre-seam
    behaviour where every filter ran before the limit truncated.

    Corrupt configs and configs that fail Pydantic validation are
    skipped with a warning rather than raised; use
    ``load_walk_forward`` when you need the failure to be loud.
    """
    store = _store(root)
    # Pull the candidate ids (already filtered by ``parent_run_id``
    # and ``since_ms``, already newest-first sorted) from the
    # generic store. We don't pass ``limit`` here because the
    # WF-specific ``spec_hash`` filter has to run before the cap.
    ids = store.list_ids(
        parent_run_id=parent_run_id,
        since_ms=since_ms,
        limit=None,
    )

    # Reuse the descriptor's filename and the store's path
    # construction so we don't duplicate them here.
    base = store._base()  # thin delegator over our own store; private access is intentional

    out: list[WalkForwardConfig] = []
    for wf_id in ids:
        config_path = base / wf_id / WALK_FORWARD_ARTIFACT.config_filename
        try:
            config = WalkForwardConfig.model_validate_json(
                config_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "[WF] skipping corrupt walk-forward config at %s: %s",
                config_path,
                exc,
            )
            continue
        if spec_hash is not None and config.strategy_spec_hash != spec_hash:
            continue
        out.append(config)

    # The store sorts ids by ``created_at_ms`` desc; ``spec_hash``
    # filtering preserves that order. Apply ``limit`` last.
    if limit is not None:
        out = out[:limit]
    return out
