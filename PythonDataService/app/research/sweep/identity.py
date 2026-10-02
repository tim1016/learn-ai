"""Executable identity for a sweep's receipt.

A git commit label is not the identity of the code that computed a number:
uncommitted changes and a restart at the same HEAD look identical to it
(review F16). The receipt therefore records what actually loaded —

Formula:
  * ``source_digest`` — sha256 over ``(relative path, bytes)`` of every
    ``.py`` file under the paths that decide a backtest's figures, in sorted
    path order. Test directories are not part of it: a test cannot move a
    figure, so editing one must not strand an in-flight study (#2588).
  * ``environment_digest`` — sha256 over the interpreter (``sys.version``,
    ``sys.platform``, ``platform.machine()``) and the sorted set of
    ``(PEP 503 name, version)`` of every distribution installed on
    ``sys.path``. It reads what pip installed, not what a requirement file
    asked for: an unpinned or transitive upgrade on a rebuild moves it, a
    comment edit does not (#2588). A distribution whose metadata cannot be
    read, or an environment with none, refuses rather than digesting around
    the gap.
  * ``digest_scheme`` — which formula produced the two digests. Digests of
    different schemes are not comparable, so a study launched under another
    scheme cannot be finished under this one.
  * ``git_revision`` — the HEAD label, for humans.
  * ``tree_state`` — ``clean`` / ``dirty`` when git can answer for the
    identity paths (test directories excluded, as in the source digest),
    ``unknown`` when it cannot (the service container ships no git binary).
    A dirty tree labels the study "uncommitted changes" and makes it
    non-resumable; ``unknown`` claims nothing and Finish falls back on the
    digests, which are the check that actually protects it.

"Same environment" is scoped to the data-plane process, which both launches
and finishes every study: the same interpreter build on the same OS and CPU,
with the same installed ``(name, version)`` set. Install paths are not part
of it, so a rebuilt image with identical contents matches, while the host
venv (macOS, developer tooling installed) never matches the container
(Linux, runtime stage). The set is order-free: neither ``sys.path`` order
nor directory listing order moves the digest, and a project installed twice
contributes both copies, so an upgrade of whichever one imports moves it. An
editable install contributes its metadata version only; its source is not
hashed here (none is installed in the image).

Reference: PRD https://github.com/tim1016/learn-ai/issues/1926 review
  amendment F16 and its revision-4 decision; issue #2588.
Canonical implementation: this file.
Validated against: tests/research/sweep/test_identity.py.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
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
    "app/research/walk_forward_study",
    # Golden Search's procedures, evaluator and rules decide which points a stage scores, how a run is
    # recorded and what its evidence concludes (#2696). Its copy, read models, persistence and approval
    # do not, so a wording fix never strands a resumable study.
    *(
        f"app/research/golden_search/{module}.py"
        for module in (
            "budget",
            "declarations",
            "evaluator",
            "evidence",
            "exam_rules",
            "exposure_rules",
            "grid_procedure",
            "planning",
            "procedure_history",
            "protocol",
            "selection",
            "stages",
            "zoom",
        )
    ),
    "app/routers/engine.py",
    "app/schemas/engine_backtest.py",
    "app/services/engine_backtest_service.py",
    "app/lean_sidecar/trading_calendar.py",
    "app/lean_sidecar/closing_bar.py",
    "app/utils/timestamps.py",
    "app/utils/session_anchors.py",
)
# A directory of this name, at any depth under an identity path, holds tests.
TEST_DIRECTORY = "tests"

# The current formula. Scheme 1 hashed the requirement files' bytes and the
# test sources; its receipts predate the field and carry none (#2588).
DIGEST_SCHEME = 2
LEGACY_DIGEST_SCHEME = 1

TreeState = Literal["clean", "dirty", "unknown"]

_NAME_SEPARATORS = re.compile(r"[-_.]+")


class EnvironmentIdentityError(RuntimeError):
    """The installed environment cannot be read well enough to identify it."""


@dataclass(frozen=True)
class CodeIdentity:
    git_revision: str
    tree_state: TreeState
    source_digest: str
    environment_digest: str
    digest_scheme: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, recorded: Mapping[str, Any]) -> CodeIdentity:
        """A receipted identity; one written before ``digest_scheme`` existed is the legacy scheme."""
        return cls(**{"digest_scheme": LEGACY_DIGEST_SCHEME, **recorded})


def _python_files(root: Path, paths: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for relative in paths:
        target = root / relative
        if target.is_dir():
            files.extend(
                candidate
                for candidate in target.rglob("*.py")
                if not {"__pycache__", TEST_DIRECTORY} & set(candidate.relative_to(root).parts)
            )
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


def installed_distributions(path: Sequence[str] | None = None) -> tuple[tuple[str, str], ...]:
    """The sorted set of ``(PEP 503 name, version)`` installed on ``path`` (default ``sys.path``)."""
    search = list(sys.path if path is None else path)
    installed: set[tuple[str, str]] = set()
    for distribution in importlib.metadata.distributions(path=search):
        name, version = distribution.metadata.get("Name"), distribution.metadata.get("Version")
        if not name or not version:
            missing = "name" if not name else "version"
            raise EnvironmentIdentityError(
                f"the installed distribution {name or '<unnamed>'!r} in {distribution.locate_file('')} has no {missing} in its metadata, "
                "so the environment cannot be identified"
            )
        installed.add((_NAME_SEPARATORS.sub("-", name).lower(), version))
    if not installed:
        raise EnvironmentIdentityError(f"no installed distribution was found on {search}, so the environment cannot be identified")
    return tuple(sorted(installed))


def environment_digest(path: Sequence[str] | None = None) -> str:
    environment = {
        "python": sys.version,
        "platform": sys.platform,
        "machine": platform.machine(),
        "distributions": installed_distributions(path),
    }
    return hashlib.sha256(json.dumps(environment, sort_keys=True).encode("utf-8")).hexdigest()


def tree_state(root: Path = SERVICE_ROOT, paths: Iterable[str] = IDENTITY_SOURCE_PATHS) -> TreeState:
    """Ask git whether the identity paths differ from HEAD; ``unknown`` if it cannot say."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths, f":(exclude,glob)**/{TEST_DIRECTORY}/**"],
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
        environment_digest=environment_digest(),
        digest_scheme=DIGEST_SCHEME,
    )
