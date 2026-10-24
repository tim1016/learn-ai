"""Thin runs/ persistence delegators over the shared artifact store.

The mechanics — id regex, path-traversal guard, atomic tmp+rename,
load-and-validate, scan+filter — live in
``app/research/artifact/store.py``. This module declares the runs/
specific surface: function signatures the runner and router
already call.

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

# Re-exported for backwards compatibility with any caller that
# imported these classes from ``app.research.runs.storage``
# directly (``monte_carlo/runner.py`` and ``baselines/runner.py``
# both do). The canonical definitions live in
# ``app.research.runs.errors``.
__all__ = [
    "RunAlreadyExistsError",
    "RunCorruptError",
    "RunNotFoundError",
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
