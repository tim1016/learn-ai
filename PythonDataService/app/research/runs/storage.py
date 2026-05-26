"""File-backed persistence for ``RunLedger`` + ``BacktestRunResult``.

On-disk layout (v1):

    <root>/
        <run_id>/
            ledger.json   — RunLedger.model_dump(mode='json')
            result.json   — BacktestRunResult.model_dump(mode='json')

Write contract:

  * ``save_run`` writes both files atomically (tmp → rename) so a crash
    mid-write never leaves a half-populated run directory.
  * ``save_run`` refuses to overwrite an existing ``run_id`` directory
    by default — UUID collisions are vanishingly unlikely, but a
    deliberate replay should be a different run_id, not a silent
    overwrite. ``replace=True`` opts into clobbering.
  * ``load_run`` round-trips Pydantic models, so any schema drift in a
    persisted ledger surfaces as a Pydantic ``ValidationError`` rather
    than a silent type mismatch.

The default storage root is anchored relative to this file
(``<repo>/PythonDataService/artifacts/runs/``) and overridable via the
``LEARN_AI_ARTIFACTS_ROOT`` env var. For host persistence across
container rebuilds, mount that path in ``podman-compose.yml`` —
``artifacts/`` is gitignored. See ``docs/references/run-ledger.md`` for
the upgrade path to Postgres.

Listing semantics: ``list_runs`` enumerates every ``ledger.json`` under
the root, parses each, applies filters, and returns the survivors
sorted by ``created_at_ms`` descending (newest first). For workloads
where the directory grows beyond a few thousand runs, swap this to a
Postgres-backed index — same public function names, different backing
store.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from app.research.artifact.root import ARTIFACTS_ROOT_ENV, default_artifacts_root
from app.research.runs.ledger import RunLedger
from app.research.runs.result import BacktestRunResult

# Strict whitelist for ``run_id`` — exactly what ``uuid.uuid4().hex``
# emits: 32 lowercase hex chars, no separators. The runner is the only
# producer of run_ids, so widening the alphabet would only widen the
# attack surface without admitting any real caller. Tighter than the
# initial PR #107 fix (``[0-9a-fA-F-]{8,64}``) per PR-review nitpick:
# uppercase + hyphens admit nothing the runner generates, but accept
# IDs like ``--------`` that pass the regex. Path-resolution check at
# ``_run_dir`` line 115 stays as defense in depth.
_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

logger = logging.getLogger(__name__)

# ``ARTIFACTS_ROOT_ENV`` and ``default_artifacts_root`` are re-exported
# from ``app.research.artifact.root`` so existing callers
# (``walk_forward/storage.py``, ``baselines/storage.py``, the runs router,
# tests) keep working until their own PRs migrate to import from the
# artifact module directly.
__all__ = [
    "ARTIFACTS_ROOT_ENV",
    "RunAlreadyExistsError",
    "RunCorruptError",
    "RunNotFoundError",
    "default_artifacts_root",
    "list_runs",
    "load_run",
    "save_run",
]


class RunNotFoundError(LookupError):
    """Raised when ``load_run`` can't find a run_id under the given root."""


class RunAlreadyExistsError(FileExistsError):
    """Raised when ``save_run`` would overwrite a run_id without ``replace=True``."""


class RunCorruptError(RuntimeError):
    """Raised when a persisted ledger or result fails Pydantic validation."""


def _run_dir(run_id: str, root: Path | None) -> Path:
    """Resolve a run directory, refusing anything that escapes the root.

    Defense in depth against path traversal: the format check rejects
    ``../`` segments and absolute paths; the resolved-path check
    catches anything that slips past (e.g. symlinked roots, weird
    Windows separators). ``run_id`` is user-controlled via
    ``GET /api/research/strategy-runs/{run_id}`` so neither layer is
    optional.
    """
    if not run_id or not _RUN_ID_PATTERN.match(run_id):
        raise ValueError(
            f"run_id must match {_RUN_ID_PATTERN.pattern} "
            f"(got {run_id!r})"
        )
    base = root if root is not None else default_artifacts_root()
    candidate = (base / run_id).resolve()
    base_resolved = base.resolve()
    # Python 3.9+ has ``Path.is_relative_to``; use it as the second
    # gate so a regex bypass through normalization (``run_id="aa/.."``
    # passes the regex if the dot were allowed; it isn't here, but the
    # check is cheap insurance) still gets rejected.
    if not candidate.is_relative_to(base_resolved):
        raise ValueError(f"run_id resolves outside the artifacts root: {run_id!r}")
    return base / run_id


def _atomic_write_json(path: Path, payload: dict | list) -> None:
    """Write ``payload`` to ``path`` atomically.

    Writes to ``<path>.tmp`` then renames into place. ``os.replace`` is
    atomic on POSIX and Windows so a reader either sees the previous
    contents or the new contents — never a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def save_run(
    ledger: RunLedger,
    result: BacktestRunResult,
    *,
    root: Path | None = None,
    replace: bool = False,
) -> Path:
    """Persist ``(ledger, result)`` to disk and return the run directory.

    ``ledger.run_id`` and ``result.run_id`` must agree — they're the same
    run by construction, and a mismatch is a bug worth surfacing here
    rather than discovering at load time.
    """
    if ledger.run_id != result.run_id:
        raise ValueError(
            f"ledger.run_id {ledger.run_id!r} does not match "
            f"result.run_id {result.run_id!r}"
        )

    run_dir = _run_dir(ledger.run_id, root)
    if run_dir.exists() and not replace:
        raise RunAlreadyExistsError(
            f"run directory already exists: {run_dir} "
            f"(pass replace=True to clobber)"
        )

    # Write order matters: ``ledger.json`` is the discovery key for
    # ``list_runs`` and the existence check in ``load_run``. Writing
    # ``result.json`` first means a crash between the two writes leaves
    # a directory with only ``result.json`` — invisible to the listing
    # and treated as missing by ``load_run``, instead of an orphan
    # ledger pointing at a non-existent result. On retry, the orphan
    # ``result.json`` is overwritten by the next attempt.
    _atomic_write_json(run_dir / "result.json", result.model_dump(mode="json"))
    _atomic_write_json(run_dir / "ledger.json", ledger.model_dump(mode="json"))
    return run_dir


def load_run(run_id: str, *, root: Path | None = None) -> tuple[RunLedger, BacktestRunResult]:
    """Load a previously-saved run by ``run_id``.

    Raises:
      * ``RunNotFoundError`` when the run directory or its ledger/result
        files are missing.
      * ``RunCorruptError`` when the JSON parses but Pydantic validation
        fails — typically a schema mismatch we owe a migration for.
    """
    run_dir = _run_dir(run_id, root)
    ledger_path = run_dir / "ledger.json"
    result_path = run_dir / "result.json"

    if not ledger_path.is_file() or not result_path.is_file():
        raise RunNotFoundError(f"run not found: {run_id} (looked in {run_dir})")

    # Parse each file separately so the error message names which one
    # is actually corrupt — important when a partial-write recovery
    # has left only one file readable.
    try:
        ledger = RunLedger.model_validate_json(ledger_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RunCorruptError(f"failed to parse {ledger_path}: {exc}") from exc
    try:
        result = BacktestRunResult.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise RunCorruptError(f"failed to parse {result_path}: {exc}") from exc

    return ledger, result


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
    """Enumerate persisted runs, optionally filtered.

    Filters are AND-combined and compare for equality (or, for
    ``since_ms``, ``ledger.created_at_ms >= since_ms``). Results are
    sorted by ``created_at_ms`` descending so the newest runs appear
    first; ``limit`` truncates after sorting.

    Corrupt ledgers are *skipped* with a warning rather than raising —
    a single broken ledger should not blind the rest of the listing.
    Use ``load_run`` directly when you need the failure to be loud.
    """
    base = root if root is not None else default_artifacts_root()
    if not base.is_dir():
        return []

    out: list[RunLedger] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        ledger_path = child / "ledger.json"
        if not ledger_path.is_file():
            continue
        try:
            ledger = RunLedger.model_validate_json(ledger_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[RUNS] skipping corrupt ledger at %s: %s", ledger_path, exc)
            continue

        if spec_hash is not None and ledger.strategy_spec_hash != spec_hash:
            continue
        if symbol is not None and ledger.symbol != symbol:
            continue
        if status is not None and ledger.status != status:
            continue
        if parent_run_id is not None and ledger.parent_run_id != parent_run_id:
            continue
        if parent_spec_hash is not None and ledger.parent_spec_hash != parent_spec_hash:
            continue
        if since_ms is not None and ledger.created_at_ms < since_ms:
            continue

        out.append(ledger)

    out.sort(key=lambda lg: lg.created_at_ms, reverse=True)
    if limit is not None:
        out = out[:limit]
    return out
