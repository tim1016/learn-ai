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
from pathlib import Path

from app.research.runs.ledger import RunLedger
from app.research.runs.result import BacktestRunResult

logger = logging.getLogger(__name__)

ARTIFACTS_ROOT_ENV = "LEARN_AI_ARTIFACTS_ROOT"
"""Env var that overrides the default artifacts root."""


class RunNotFoundError(LookupError):
    """Raised when ``load_run`` can't find a run_id under the given root."""


class RunAlreadyExistsError(FileExistsError):
    """Raised when ``save_run`` would overwrite a run_id without ``replace=True``."""


class RunCorruptError(RuntimeError):
    """Raised when a persisted ledger or result fails Pydantic validation."""


def default_artifacts_root() -> Path:
    """Return the default ``artifacts/runs/`` directory.

    Resolution order:
      1. ``$LEARN_AI_ARTIFACTS_ROOT`` if set (caller is responsible for
         existence; ``save_run`` creates it on first write).
      2. ``<package_root>/artifacts/runs`` — anchored relative to this
         file so the path is correct regardless of CWD.

    The "package root" is the parent of ``app/`` — i.e.
    ``Path(__file__).resolve().parents[3]``. From
    ``app/research/runs/storage.py`` that climbs:
    ``runs → research → app → <package_root>``.
    """
    explicit = os.environ.get(ARTIFACTS_ROOT_ENV)
    if explicit:
        return Path(explicit)
    return Path(__file__).resolve().parents[3] / "artifacts" / "runs"


def _run_dir(run_id: str, root: Path | None) -> Path:
    if not run_id:
        raise ValueError("run_id must be a non-empty string")
    base = root if root is not None else default_artifacts_root()
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

    _atomic_write_json(run_dir / "ledger.json", ledger.model_dump(mode="json"))
    _atomic_write_json(run_dir / "result.json", result.model_dump(mode="json"))
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

    try:
        ledger = RunLedger.model_validate_json(ledger_path.read_text(encoding="utf-8"))
        result = BacktestRunResult.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise RunCorruptError(f"failed to parse persisted run {run_id}: {exc}") from exc

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
