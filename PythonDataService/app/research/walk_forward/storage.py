"""File-backed persistence for walk-forward analyses.

On-disk layout:

    <root>/walk-forward/<wf_id>/
        config.json   — WalkForwardConfig.model_dump(mode='json')
        result.json   — WalkForwardResult.model_dump(mode='json')

Each fold's individual run is *not* persisted here — folds are normal
``RunLedger`` records under ``<root>/<fold_run_id>/`` (Phase A
storage). The ``parent_run_id`` field on each fold's ledger points
back at this WF so ``list_runs(parent_run_id=wf_id)`` enumerates them.

Same correctness contract as ``app/research/runs/storage.py``:
  * Strict regex on ``wf_id`` to reject path-traversal attempts.
  * Resolved-path containment check as defense in depth.
  * Atomic writes (tmp → rename); ``result.json`` written before
    ``config.json`` so an interrupted save leaves a discoverable-as-
    incomplete directory rather than a complete-looking one with a
    partial result.

Because this layout is parallel to ``<root>/<run_id>/``, the
artifacts root contains a mix of run dirs (32-char hex UUID names)
and a single ``walk-forward/`` subdirectory. ``list_runs`` ignores
the latter (its iteration treats ``walk-forward`` as not matching the
run-id regex, so the corresponding ``ledger.json`` lookup misses
silently).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from app.research.runs.storage import default_artifacts_root
from app.research.walk_forward.result import WalkForwardConfig, WalkForwardResult

logger = logging.getLogger(__name__)

_WALK_FORWARD_DIRNAME = "walk-forward"
# Same alphabet as run_id — UUID4 hex, 32 lowercase hex chars.
_WF_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class WalkForwardNotFoundError(LookupError):
    """Raised when a WF cannot be found under the given root."""


class WalkForwardAlreadyExistsError(FileExistsError):
    """Raised when ``save_walk_forward`` would clobber an existing dir
    without ``replace=True``."""


class WalkForwardCorruptError(RuntimeError):
    """Raised when a persisted WF fails Pydantic validation."""


def _wf_dir(wf_id: str, root: Path | None) -> Path:
    """Resolve a WF directory, refusing anything that escapes the root."""
    if not wf_id or not _WF_ID_PATTERN.match(wf_id):
        raise ValueError(
            f"walk_forward_id must match {_WF_ID_PATTERN.pattern} (got {wf_id!r})"
        )
    base = (root if root is not None else default_artifacts_root()) / _WALK_FORWARD_DIRNAME
    candidate = (base / wf_id).resolve()
    base_resolved = base.resolve()
    if not candidate.is_relative_to(base_resolved):
        raise ValueError(
            f"walk_forward_id resolves outside the artifacts root: {wf_id!r}"
        )
    return base / wf_id


def _atomic_write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


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

    wf_dir = _wf_dir(config.walk_forward_id, root)
    if wf_dir.exists() and not replace:
        raise WalkForwardAlreadyExistsError(
            f"walk-forward directory already exists: {wf_dir} "
            f"(pass replace=True to clobber)"
        )

    # Same write order as Phase A: result first, config (the discovery
    # key) last. An interrupted save leaves an orphan result.json but
    # ``load_walk_forward`` checks for both files, so it surfaces as
    # ``WalkForwardNotFoundError`` rather than corrupt-looking state.
    _atomic_write_json(wf_dir / "result.json", result.model_dump(mode="json"))
    _atomic_write_json(wf_dir / "config.json", config.model_dump(mode="json"))
    return wf_dir


def load_walk_forward(
    wf_id: str, *, root: Path | None = None
) -> tuple[WalkForwardConfig, WalkForwardResult]:
    """Load a previously-saved WF by ``wf_id``."""
    wf_dir = _wf_dir(wf_id, root)
    config_path = wf_dir / "config.json"
    result_path = wf_dir / "result.json"
    if not config_path.is_file() or not result_path.is_file():
        raise WalkForwardNotFoundError(
            f"walk-forward not found: {wf_id} (looked in {wf_dir})"
        )
    try:
        config = WalkForwardConfig.model_validate_json(
            config_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise WalkForwardCorruptError(f"failed to parse {config_path}: {exc}") from exc
    try:
        result = WalkForwardResult.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise WalkForwardCorruptError(f"failed to parse {result_path}: {exc}") from exc
    return config, result


def list_walk_forwards(
    *,
    root: Path | None = None,
    parent_run_id: str | None = None,
    spec_hash: str | None = None,
    since_ms: int | None = None,
    limit: int | None = None,
) -> list[WalkForwardConfig]:
    """Enumerate persisted walk-forwards, optionally filtered.

    Returns ``WalkForwardConfig`` records (not full results) — the
    listing endpoint shape mirrors ``list_runs``: lightweight summary
    with full fetch on-demand via ``GET /walk-forward/{wf_id}``.
    """
    base = (root if root is not None else default_artifacts_root()) / _WALK_FORWARD_DIRNAME
    if not base.is_dir():
        return []

    out: list[WalkForwardConfig] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        config_path = child / "config.json"
        if not config_path.is_file():
            continue
        try:
            config = WalkForwardConfig.model_validate_json(
                config_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "[WF] skipping corrupt walk-forward config at %s: %s", config_path, exc
            )
            continue

        if parent_run_id is not None and config.parent_run_id != parent_run_id:
            continue
        if spec_hash is not None and config.strategy_spec_hash != spec_hash:
            continue
        if since_ms is not None and config.created_at_ms < since_ms:
            continue

        out.append(config)

    out.sort(key=lambda c: c.created_at_ms, reverse=True)
    if limit is not None:
        out = out[:limit]
    return out
