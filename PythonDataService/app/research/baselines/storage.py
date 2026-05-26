"""Thin baselines persistence delegators over the shared artifact store.

The mechanics — id regex, path-traversal guard, atomic tmp+rename,
load-and-validate, scan+filter — live in
``app/research/artifact/store.py``. This module declares the baselines-
specific surface: function signatures the runner and router already
call, plus the phase-specific ``method`` filter on
``list_baselines`` that the artifact store's generic ``list_ids``
doesn't carry. See
``docs/architecture/research-artifact-seam.md`` for the design.

On-disk layout (unchanged from pre-seam):

    <root>/baselines/<baseline_id>/
        config.json   — BaselineConfig.model_dump(mode='json')
        result.json   — BaselineResult.model_dump(mode='json')

Sibling layout to ``<root>/walk-forward/<wf_id>/`` (Phase C) and
``<root>/monte-carlo/<mc_id>/`` (Phase D), parallel to Phase A's
``<root>/<run_id>/``. The per-baseline child runs are *not* persisted
here — they're normal Phase A ``RunLedger``s under
``<root>/<baseline_run_id>/``, discoverable via
``list_runs(parent_run_id=baseline_id)``.

Public exception classes (``BaselineNotFoundError``,
``BaselineAlreadyExistsError``, ``BaselineCorruptError``) are defined
in ``app.research.baselines.errors`` and re-exported here so existing
callers that imported them from this module path keep working.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.research.artifact.store import ArtifactStore
from app.research.baselines.descriptor import BASELINES_ARTIFACT
from app.research.baselines.errors import (
    BaselineAlreadyExistsError,
    BaselineCorruptError,
    BaselineNotFoundError,
)
from app.research.baselines.result import BaselineConfig, BaselineResult

logger = logging.getLogger(__name__)


# Re-exported for backwards compatibility with any caller that
# imported these classes from ``app.research.baselines.storage``
# directly. The canonical definitions live in
# ``app.research.baselines.errors``.
__all__ = [
    "BaselineAlreadyExistsError",
    "BaselineCorruptError",
    "BaselineNotFoundError",
    "list_baselines",
    "load_baseline",
    "save_baseline",
]


def _store(root: Path | None) -> ArtifactStore:
    """Construct an ``ArtifactStore`` bound to the baselines descriptor."""
    return ArtifactStore(BASELINES_ARTIFACT, root=root)


def save_baseline(
    config: BaselineConfig,
    result: BaselineResult,
    *,
    root: Path | None = None,
    replace: bool = False,
) -> Path:
    """Persist ``(config, result)`` and return the baseline directory."""
    if config.baseline_id != result.baseline_id:
        raise ValueError(
            f"config.baseline_id {config.baseline_id!r} does not match "
            f"result.baseline_id {result.baseline_id!r}"
        )
    return _store(root).save(config, result, replace=replace)


def load_baseline(
    baseline_id: str, *, root: Path | None = None
) -> tuple[BaselineConfig, BaselineResult]:
    """Load a previously-saved baseline by ``baseline_id``."""
    return _store(root).load(
        baseline_id,
        config_type=BaselineConfig,
        result_type=BaselineResult,
    )


def list_baselines(
    *,
    root: Path | None = None,
    parent_run_id: str | None = None,
    method: str | None = None,
    since_ms: int | None = None,
    limit: int | None = None,
) -> list[BaselineConfig]:
    """Enumerate persisted baselines, optionally filtered, newest first.

    Shared filters (``parent_run_id``, ``since_ms``) are applied
    inside the artifact store; the baselines-specific ``method``
    filter is applied here because the store doesn't (and shouldn't)
    know about phase-specific Pydantic fields. ``limit`` is enforced
    *after* the ``method`` filter — matching the pre-seam behaviour
    where every filter ran before the limit truncated.

    Corrupt configs and configs that fail Pydantic validation are
    skipped with a warning rather than raised; use ``load_baseline``
    when you need the failure to be loud. Skip messages preserve the
    legacy ``skipping corrupt baseline`` substring so existing log
    assertions keep matching.
    """
    store = _store(root)
    # Pull the candidate ids (already filtered by ``parent_run_id``
    # and ``since_ms``, already newest-first sorted) from the
    # generic store. We don't pass ``limit`` here because the
    # baselines-specific ``method`` filter has to run before the cap.
    ids = store.list_ids(
        parent_run_id=parent_run_id,
        since_ms=since_ms,
        limit=None,
    )

    # Reuse the descriptor's filename and the store's path
    # construction so we don't duplicate them here.
    base = store._base()  # thin delegator over our own store; private access is intentional

    out: list[BaselineConfig] = []
    for baseline_id in ids:
        config_path = base / baseline_id / BASELINES_ARTIFACT.config_filename
        try:
            config = BaselineConfig.model_validate_json(
                config_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "[BASELINES] skipping corrupt baseline config at %s: %s",
                config_path,
                exc,
            )
            continue
        if method is not None and config.method != method:
            continue
        out.append(config)

    # The store sorts ids by ``created_at_ms`` desc; ``method``
    # filtering preserves that order. Apply ``limit`` last.
    if limit is not None:
        out = out[:limit]
    return out
