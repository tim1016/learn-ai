"""Thin runs/ persistence delegators over the shared artifact store.

The mechanics — id regex, path-traversal guard, atomic tmp+rename,
load-and-validate, scan+filter — live in
``app/research/artifact/store.py``. This module declares the runs/
specific surface: function signatures the runner and router
already call, plus the phase-specific filters
(``spec_hash``, ``symbol``, ``status``, ``parent_spec_hash``) on
``list_runs`` that the artifact store's generic ``list_ids``
doesn't carry. See ``docs/architecture/research-artifact-seam.md``
for the design.

On-disk layout (unchanged from pre-seam — runs/ is the flat
``subdir=""`` shape):

    <root>/<run_id>/
        ledger.json   — RunLedger.model_dump(mode='json')
        result.json   — BacktestRunResult.model_dump(mode='json')

Public exception classes (``RunNotFoundError``,
``RunAlreadyExistsError``, ``RunCorruptError``) are defined in
``app.research.runs.errors`` and re-exported here so existing
callers that imported them from this module path
(``monte_carlo/runner.py``, ``baselines/runner.py``, the runs
router, tests) keep working.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.research.artifact.store import ArtifactStore
from app.research.runs.descriptor import RUNS_ARTIFACT
from app.research.runs.errors import (
    RunAlreadyExistsError,
    RunCorruptError,
    RunNotFoundError,
)
from app.research.runs.ledger import RunLedger
from app.research.runs.result import BacktestRunResult

logger = logging.getLogger(__name__)


# Re-exported for backwards compatibility with any caller that
# imported these classes from ``app.research.runs.storage``
# directly (``monte_carlo/runner.py`` and ``baselines/runner.py``
# both do). The canonical definitions live in
# ``app.research.runs.errors``.
__all__ = [
    "RunAlreadyExistsError",
    "RunCorruptError",
    "RunNotFoundError",
    "list_runs",
    "load_run",
    "save_run",
]


def _store(root: Path | None) -> ArtifactStore:
    """Construct an ``ArtifactStore`` bound to the runs/ descriptor."""
    return ArtifactStore(RUNS_ARTIFACT, root=root)


def save_run(
    ledger: RunLedger,
    result: BacktestRunResult,
    *,
    root: Path | None = None,
    replace: bool = False,
) -> Path:
    """Persist ``(ledger, result)`` to disk and return the run directory.

    ``ledger.run_id`` and ``result.run_id`` must agree — they're the
    same run by construction, and a mismatch is a bug worth
    surfacing here rather than discovering at load time.
    """
    if ledger.run_id != result.run_id:
        raise ValueError(
            f"ledger.run_id {ledger.run_id!r} does not match "
            f"result.run_id {result.run_id!r}"
        )
    return _store(root).save(ledger, result, replace=replace)


def load_run(
    run_id: str, *, root: Path | None = None
) -> tuple[RunLedger, BacktestRunResult]:
    """Load a previously-saved run by ``run_id``.

    Raises:
      * ``RunNotFoundError`` when the run directory or its
        ledger/result files are missing.
      * ``RunCorruptError`` when the JSON parses but Pydantic
        validation fails — typically a schema mismatch we owe a
        migration for.
    """
    return _store(root).load(
        run_id,
        config_type=RunLedger,
        result_type=BacktestRunResult,
    )


def list_runs(
    *,
    root: Path | None = None,
    spec_hash: str | None = None,
    symbol: str | None = None,
    status: str | None = None,
    parent_run_id: str | None = None,
    parent_spec_hash: str | None = None,
    since_ms: int | None = None,
    limit: int | None = None,
) -> list[RunLedger]:
    """Enumerate persisted runs, optionally filtered, newest first.

    Returns ``RunLedger`` records — the pre-seam listing shape. Shared
    filters (``parent_run_id``, ``since_ms``) are applied inside the
    artifact store; the runs/-specific filters (``spec_hash``,
    ``symbol``, ``status``, ``parent_spec_hash``) are applied here
    because the store doesn't (and shouldn't) know about phase-
    specific Pydantic fields. ``limit`` is enforced *after* the
    phase-specific filters — matching the pre-seam behaviour where
    every filter ran before the limit truncated.

    Corrupt ledgers and ledgers that fail Pydantic validation are
    skipped with a warning rather than raised; use ``load_run`` when
    you need the failure to be loud.
    """
    store = _store(root)
    # Pull the candidate ids (already filtered by ``parent_run_id``
    # and ``since_ms``, already newest-first sorted) from the
    # generic store. We don't pass ``limit`` here because the
    # runs/-specific filters have to run before the cap.
    ids = store.list_ids(
        parent_run_id=parent_run_id,
        since_ms=since_ms,
        limit=None,
    )

    # Reuse the descriptor's filename and the store's path
    # construction so we don't duplicate them here.
    base = store._base()  # thin delegator over our own store; private access is intentional

    out: list[RunLedger] = []
    for run_id in ids:
        ledger_path = base / run_id / RUNS_ARTIFACT.config_filename
        try:
            ledger = RunLedger.model_validate_json(
                ledger_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "[RUNS] skipping corrupt ledger at %s: %s",
                ledger_path,
                exc,
            )
            continue
        if spec_hash is not None and ledger.strategy_spec_hash != spec_hash:
            continue
        if symbol is not None and ledger.symbol != symbol:
            continue
        if status is not None and ledger.status != status:
            continue
        if parent_spec_hash is not None and ledger.parent_spec_hash != parent_spec_hash:
            continue
        out.append(ledger)

    # The store sorts ids by ``created_at_ms`` desc; phase-specific
    # filtering preserves that order. Apply ``limit`` last.
    if limit is not None:
        out = out[:limit]
    return out
