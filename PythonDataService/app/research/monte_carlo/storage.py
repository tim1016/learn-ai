"""Thin Monte Carlo persistence delegators over the shared artifact store.

The mechanics — id regex, path-traversal guard, atomic tmp+rename,
load-and-validate, scan+filter — live in
``app/research/artifact/store.py``. This module declares the Monte
Carlo-specific surface: function signatures the runner and router
already call, plus the phase-specific ``method`` filter on
``list_monte_carlos`` that the artifact store's generic
``list_ids`` doesn't carry. See
``docs/architecture/research-artifact-seam.md`` for the design.

On-disk layout (unchanged from pre-seam):

    <root>/monte-carlo/<mc_id>/
        config.json   — MonteCarloConfig.model_dump(mode='json')
        result.json   — MonteCarloResult.model_dump(mode='json')

Public exception classes (``MonteCarloNotFoundError``,
``MonteCarloAlreadyExistsError``, ``MonteCarloCorruptError``) are
defined in ``app.research.monte_carlo.errors`` and re-exported here
so existing callers that imported them from this module path keep
working.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.research.artifact.store import ArtifactStore
from app.research.monte_carlo.descriptor import MONTE_CARLO_ARTIFACT
from app.research.monte_carlo.errors import (
    MonteCarloAlreadyExistsError,
    MonteCarloCorruptError,
    MonteCarloNotFoundError,
)
from app.research.monte_carlo.result import MonteCarloConfig, MonteCarloResult

logger = logging.getLogger(__name__)


# Re-exported for backwards compatibility with any caller that
# imported these classes from ``app.research.monte_carlo.storage``
# directly. The canonical definitions live in
# ``app.research.monte_carlo.errors``.
__all__ = [
    "MonteCarloAlreadyExistsError",
    "MonteCarloCorruptError",
    "MonteCarloNotFoundError",
    "list_monte_carlos",
    "load_monte_carlo",
    "save_monte_carlo",
]


def _store(root: Path | None) -> ArtifactStore:
    """Construct an ``ArtifactStore`` bound to the Monte Carlo descriptor."""
    return ArtifactStore(MONTE_CARLO_ARTIFACT, root=root)


def save_monte_carlo(
    config: MonteCarloConfig,
    result: MonteCarloResult,
    *,
    root: Path | None = None,
    replace: bool = False,
) -> Path:
    """Persist ``(config, result)`` and return the MC directory."""
    if config.monte_carlo_id != result.monte_carlo_id:
        raise ValueError(
            f"config.monte_carlo_id {config.monte_carlo_id!r} does not match "
            f"result.monte_carlo_id {result.monte_carlo_id!r}"
        )
    return _store(root).save(config, result, replace=replace)


def load_monte_carlo(
    mc_id: str, *, root: Path | None = None
) -> tuple[MonteCarloConfig, MonteCarloResult]:
    """Load a previously-saved MC by ``mc_id``."""
    return _store(root).load(
        mc_id,
        config_type=MonteCarloConfig,
        result_type=MonteCarloResult,
    )


def list_monte_carlos(
    *,
    root: Path | None = None,
    parent_run_id: str | None = None,
    method: str | None = None,
    since_ms: int | None = None,
    limit: int | None = None,
) -> list[MonteCarloConfig]:
    """Enumerate persisted Monte Carlos, optionally filtered, newest first.

    Shared filters (``parent_run_id``, ``since_ms``) are applied
    inside the artifact store; the MC-specific ``method`` filter is
    applied here because the store doesn't (and shouldn't) know
    about phase-specific Pydantic fields. ``limit`` is enforced
    *after* the ``method`` filter — matching the pre-seam behaviour
    where every filter ran before the limit truncated.

    Corrupt configs and configs that fail Pydantic validation are
    skipped with a warning rather than raised; use ``load_monte_carlo``
    when you need the failure to be loud. Skip messages preserve the
    legacy ``skipping corrupt monte-carlo`` substring so existing
    log assertions keep matching.
    """
    store = _store(root)
    # Pull the candidate ids (already filtered by ``parent_run_id``
    # and ``since_ms``, already newest-first sorted) from the
    # generic store. We don't pass ``limit`` here because the
    # MC-specific ``method`` filter has to run before the cap.
    ids = store.list_ids(
        parent_run_id=parent_run_id,
        since_ms=since_ms,
        limit=None,
    )

    # Reuse the descriptor's filename and the store's path
    # construction so we don't duplicate them here.
    base = store._base()  # thin delegator over our own store; private access is intentional

    out: list[MonteCarloConfig] = []
    for mc_id in ids:
        config_path = base / mc_id / MONTE_CARLO_ARTIFACT.config_filename
        try:
            config = MonteCarloConfig.model_validate_json(
                config_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "[MC] skipping corrupt monte-carlo config at %s: %s",
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
