"""File-backed persistence for null-baseline analyses.

On-disk layout:

    <root>/baselines/<baseline_id>/
        config.json   — BaselineConfig.model_dump(mode='json')
        result.json   — BaselineResult.model_dump(mode='json')

Sibling layout to ``<root>/walk-forward/<wf_id>/`` (Phase C) and
``<root>/monte-carlo/<mc_id>/`` (Phase D), parallel to Phase A's
``<root>/<run_id>/``. Same correctness contract: strict regex on
``baseline_id`` (matches ``uuid.uuid4().hex`` exactly), resolved-path
containment as defence in depth, atomic writes with ``result.json``
before ``config.json``.

The per-baseline child runs are *not* persisted here — they're
normal Phase A ``RunLedger``s under ``<root>/<baseline_run_id>/``,
discoverable via ``list_runs(parent_run_id=baseline_id)``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from app.research.baselines.result import BaselineConfig, BaselineResult
from app.research.runs.storage import default_artifacts_root

logger = logging.getLogger(__name__)

_BASELINES_DIRNAME = "baselines"
_BASELINE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class BaselineNotFoundError(LookupError):
    """Raised when a baseline cannot be found under the given root."""


class BaselineAlreadyExistsError(FileExistsError):
    """Raised when ``save_baseline`` would overwrite without ``replace=True``."""


class BaselineCorruptError(RuntimeError):
    """Raised when a persisted baseline fails Pydantic validation."""


def _baseline_dir(baseline_id: str, root: Path | None) -> Path:
    """Resolve a baseline directory, refusing escapes."""
    if not baseline_id or not _BASELINE_ID_PATTERN.match(baseline_id):
        raise ValueError(
            f"baseline_id must match {_BASELINE_ID_PATTERN.pattern} (got {baseline_id!r})"
        )
    base = (root if root is not None else default_artifacts_root()) / _BASELINES_DIRNAME
    candidate = (base / baseline_id).resolve()
    base_resolved = base.resolve()
    if not candidate.is_relative_to(base_resolved):
        raise ValueError(
            f"baseline_id resolves outside the artifacts root: {baseline_id!r}"
        )
    return base / baseline_id


def _atomic_write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


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

    bdir = _baseline_dir(config.baseline_id, root)
    if bdir.exists() and not replace:
        raise BaselineAlreadyExistsError(
            f"baseline directory already exists: {bdir} "
            f"(pass replace=True to clobber)"
        )
    _atomic_write_json(bdir / "result.json", result.model_dump(mode="json"))
    _atomic_write_json(bdir / "config.json", config.model_dump(mode="json"))
    return bdir


def load_baseline(
    baseline_id: str, *, root: Path | None = None
) -> tuple[BaselineConfig, BaselineResult]:
    """Load a previously-saved baseline by ``baseline_id``."""
    bdir = _baseline_dir(baseline_id, root)
    config_path = bdir / "config.json"
    result_path = bdir / "result.json"
    if not config_path.is_file() or not result_path.is_file():
        raise BaselineNotFoundError(
            f"baseline not found: {baseline_id} (looked in {bdir})"
        )
    try:
        config = BaselineConfig.model_validate_json(
            config_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise BaselineCorruptError(f"failed to parse {config_path}: {exc}") from exc
    try:
        result = BaselineResult.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise BaselineCorruptError(f"failed to parse {result_path}: {exc}") from exc
    return config, result


def list_baselines(
    *,
    root: Path | None = None,
    parent_run_id: str | None = None,
    method: str | None = None,
    since_ms: int | None = None,
    limit: int | None = None,
) -> list[BaselineConfig]:
    """Enumerate persisted baselines, optionally filtered, newest first."""
    base = (root if root is not None else default_artifacts_root()) / _BASELINES_DIRNAME
    if not base.is_dir():
        return []

    out: list[BaselineConfig] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        config_path = child / "config.json"
        if not config_path.is_file():
            continue
        try:
            config = BaselineConfig.model_validate_json(
                config_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "[BASELINES] skipping corrupt baseline config at %s: %s",
                config_path, exc,
            )
            continue

        if parent_run_id is not None and config.parent_run_id != parent_run_id:
            continue
        if method is not None and config.method != method:
            continue
        if since_ms is not None and config.created_at_ms < since_ms:
            continue

        out.append(config)

    out.sort(key=lambda c: c.created_at_ms, reverse=True)
    if limit is not None:
        out = out[:limit]
    return out
