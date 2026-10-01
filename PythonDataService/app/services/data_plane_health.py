"""The data-plane process's resolved code revision (env override or git SHA)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _read_git_revision() -> str:
    repo_root = Path(os.getenv("DATA_PLANE_GIT_ROOT", Path(__file__).resolve().parents[2]))
    revision = _read_git_head(repo_root)
    if revision != "unknown":
        return revision
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    revision = result.stdout.strip()
    return revision if revision else "unknown"


def _read_git_head(repo_root: Path) -> str:
    git_dir = repo_root / ".git"
    if git_dir.is_file():
        try:
            marker = git_dir.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return "unknown"
        prefix = "gitdir:"
        if not marker.lower().startswith(prefix):
            return "unknown"
        git_dir = (git_dir.parent / marker[len(prefix):].strip()).resolve()
    head_path = git_dir / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    ref_prefix = "ref:"
    if head.startswith(ref_prefix):
        ref_path = git_dir / head[len(ref_prefix):].strip()
        try:
            revision = ref_path.read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown"
        return revision if revision else "unknown"
    return head if head else "unknown"


def _env_revision(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    revision = value.strip()
    if not revision or revision.lower() == "unknown":
        return None
    return revision


_CODE_REVISION = (
    _env_revision("DATA_PLANE_CODE_REVISION")
    or _env_revision("GIT_SHA")
    or _env_revision("CODE_SHA")
    or _read_git_revision()
)


def resolved_code_revision() -> str:
    """Return the process's resolved code revision (env override or git SHA).

    The provenance value callers stamp on their evidence — e.g. the Recency
    Chart's evidence fingerprint (ADR 0072 decision 5), which must distinguish
    trades produced by different strategy-code revisions.
    """
    return _CODE_REVISION
