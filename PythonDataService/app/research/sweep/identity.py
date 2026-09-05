"""Executable identity for a sweep's receipt.

A git commit label is not the identity of the code that computed a number:
uncommitted changes and a restart at the same HEAD look identical to it
(review F16). The receipt therefore records what actually loaded —

Formula:
  * ``source_digest`` — sha256 over ``(relative path, bytes)`` of every
    ``.py`` file under the paths that decide a backtest's figures, in sorted
    path order.
  * ``environment_digest`` — sha256 over the interpreter version and the
    pinned requirement files.
  * ``git_revision`` — the HEAD label, for humans.
  * ``tree_state`` — ``clean`` / ``dirty`` when git can answer for the
    identity paths, ``unknown`` when it cannot (the service container ships
    no git binary). A dirty tree labels the study "uncommitted changes" and
    makes it non-resumable; ``unknown`` claims nothing and Finish falls back
    on the digests, which are the check that actually protects it.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1926 review
  amendment F16 and its revision-4 decision.
Canonical implementation: this file.
Validated against: tests/research/sweep/test_identity.py.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

from app.services.data_plane_health import resolved_code_revision

SERVICE_ROOT = Path(__file__).resolve().parents[3]

# Relative to the service root. Anything that can change a backtest's numbers.
IDENTITY_SOURCE_PATHS: tuple[str, ...] = (
    "app/engine",
    "app/research/sweep",
    "app/research/grid_search",
    "app/routers/engine.py",
    "app/lean_sidecar/trading_calendar.py",
    "app/utils/timestamps.py",
)
ENVIRONMENT_FILES: tuple[str, ...] = ("requirements-heavy.txt", "requirements-light.txt")

TreeState = Literal["clean", "dirty", "unknown"]


@dataclass(frozen=True)
class CodeIdentity:
    git_revision: str
    tree_state: TreeState
    source_digest: str
    environment_digest: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def matches(self, other: CodeIdentity) -> bool:
        """Same loaded source and environment — the resumability test."""
        return self.source_digest == other.source_digest and self.environment_digest == other.environment_digest


def _python_files(root: Path, paths: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for relative in paths:
        target = root / relative
        if target.is_dir():
            files.extend(candidate for candidate in target.rglob("*.py") if "__pycache__" not in candidate.parts)
        elif target.is_file():
            files.append(target)
    return sorted(set(files))


def source_digest(root: Path = SERVICE_ROOT, paths: Iterable[str] = IDENTITY_SOURCE_PATHS) -> str:
    digest = hashlib.sha256()
    for file in _python_files(root, paths):
        digest.update(file.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def environment_digest(root: Path = SERVICE_ROOT, files: Iterable[str] = ENVIRONMENT_FILES) -> str:
    digest = hashlib.sha256(sys.version.encode("utf-8"))
    for name in files:
        path = root / name
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes() if path.is_file() else b"<absent>")
    return digest.hexdigest()


def tree_state(root: Path = SERVICE_ROOT, paths: Iterable[str] = IDENTITY_SOURCE_PATHS) -> TreeState:
    """Ask git whether the identity paths differ from HEAD; ``unknown`` if it cannot say."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return "dirty" if result.stdout.strip() else "clean"


@cache
def resolve_code_identity(root: Path = SERVICE_ROOT) -> CodeIdentity:
    """The identity of THIS process's loaded code — constant for its lifetime, so computed once."""
    return CodeIdentity(
        git_revision=resolved_code_revision(),
        tree_state=tree_state(root),
        source_digest=source_digest(root),
        environment_digest=environment_digest(root),
    )
