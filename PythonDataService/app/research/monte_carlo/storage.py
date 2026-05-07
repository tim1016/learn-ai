"""File-backed persistence for Monte Carlo analyses.

On-disk layout:

    <root>/monte-carlo/<mc_id>/
        config.json   — MonteCarloConfig.model_dump(mode='json')
        result.json   — MonteCarloResult.model_dump(mode='json')

Sibling layout to ``<root>/walk-forward/<wf_id>/`` and parallel to the
top-level ``<root>/<run_id>/`` Phase A runs. Same correctness
contract as Phase A/C: strict regex on ``mc_id`` (matches
``uuid.uuid4().hex`` exactly), resolved-path containment as defence
in depth, atomic writes with result.json before config.json so an
interrupted save leaves an invisible orphan rather than a complete-
looking dir with a partial result.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from app.research.monte_carlo.result import MonteCarloConfig, MonteCarloResult
from app.research.runs.storage import default_artifacts_root

logger = logging.getLogger(__name__)

_MONTE_CARLO_DIRNAME = "monte-carlo"
_MC_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class MonteCarloNotFoundError(LookupError):
    """Raised when an MC cannot be found under the given root."""


class MonteCarloAlreadyExistsError(FileExistsError):
    """Raised when ``save_monte_carlo`` would overwrite without ``replace=True``."""


class MonteCarloCorruptError(RuntimeError):
    """Raised when a persisted MC fails Pydantic validation."""


def _mc_dir(mc_id: str, root: Path | None) -> Path:
    """Resolve an MC directory, refusing anything that escapes the root."""
    if not mc_id or not _MC_ID_PATTERN.match(mc_id):
        raise ValueError(
            f"monte_carlo_id must match {_MC_ID_PATTERN.pattern} (got {mc_id!r})"
        )
    base = (root if root is not None else default_artifacts_root()) / _MONTE_CARLO_DIRNAME
    candidate = (base / mc_id).resolve()
    base_resolved = base.resolve()
    if not candidate.is_relative_to(base_resolved):
        raise ValueError(
            f"monte_carlo_id resolves outside the artifacts root: {mc_id!r}"
        )
    return base / mc_id


def _atomic_write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


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

    mc_dir = _mc_dir(config.monte_carlo_id, root)
    if mc_dir.exists() and not replace:
        raise MonteCarloAlreadyExistsError(
            f"monte-carlo directory already exists: {mc_dir} "
            f"(pass replace=True to clobber)"
        )

    # Result first, config last — see Phase A/C for the rationale.
    _atomic_write_json(mc_dir / "result.json", result.model_dump(mode="json"))
    _atomic_write_json(mc_dir / "config.json", config.model_dump(mode="json"))
    return mc_dir


def load_monte_carlo(
    mc_id: str, *, root: Path | None = None
) -> tuple[MonteCarloConfig, MonteCarloResult]:
    """Load a previously-saved MC by ``mc_id``."""
    mc_dir = _mc_dir(mc_id, root)
    config_path = mc_dir / "config.json"
    result_path = mc_dir / "result.json"
    if not config_path.is_file() or not result_path.is_file():
        raise MonteCarloNotFoundError(
            f"monte-carlo not found: {mc_id} (looked in {mc_dir})"
        )
    try:
        config = MonteCarloConfig.model_validate_json(
            config_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise MonteCarloCorruptError(f"failed to parse {config_path}: {exc}") from exc
    try:
        result = MonteCarloResult.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise MonteCarloCorruptError(f"failed to parse {result_path}: {exc}") from exc
    return config, result


def list_monte_carlos(
    *,
    root: Path | None = None,
    parent_run_id: str | None = None,
    method: str | None = None,
    since_ms: int | None = None,
    limit: int | None = None,
) -> list[MonteCarloConfig]:
    """Enumerate persisted Monte Carlos, optionally filtered, newest first."""
    base = (root if root is not None else default_artifacts_root()) / _MONTE_CARLO_DIRNAME
    if not base.is_dir():
        return []

    out: list[MonteCarloConfig] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        config_path = child / "config.json"
        if not config_path.is_file():
            continue
        try:
            config = MonteCarloConfig.model_validate_json(
                config_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "[MC] skipping corrupt monte-carlo config at %s: %s", config_path, exc
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
