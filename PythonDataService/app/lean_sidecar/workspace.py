"""Workspace layout, path resolution, and run-id validation.

Every LEAN Lab run owns a fresh directory under the configured artifacts
root. The launcher mounts **only** that directory into the LEAN container.
This module is the single source of truth for what lives where.

Authority: docs/architecture/lean-sidecar-lab.md §"Workspace contract".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ``run_id`` is a strict label, not arbitrary text. The launcher resolves
# ``run_id`` to a host-absolute path; allowing path separators or "."
# components would defeat the path-under-root check.
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")

# Equity ticker symbols flow into LEAN data-folder paths
# (``equity/usa/minute/<symbol>/...``). Without a strict regex, a
# request with ``symbol="../../etc/passwd"`` would traverse outside
# the workspace via the staging writers. Permit upper/lower alpha,
# digits, dot for share-class tickers (``BRK.B``), and dash; reject
# everything else. ``^[A-Z0-9.-]{1,16}$`` is comfortably above any
# real US equity symbol but rejects path traversal characters
# (``/``, ``\``, ``..``-only strings, whitespace, NULs).
TICKER_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.\-]{1,16}$")


class SymbolValidationError(ValueError):
    """Raised when a caller-supplied symbol is not a valid ticker."""


def validate_symbol(symbol: str) -> str:
    """Return the upper-cased symbol or raise on path-unsafe input.

    Validates *before* any join with a filesystem path. The pattern
    rejects ``..``-only strings (which would pass through ``.lower()``
    intact), path separators, and any character that is not in the
    permitted ticker alphabet.
    """
    if not isinstance(symbol, str):
        raise SymbolValidationError(f"symbol must be str, got {type(symbol).__name__}")
    if not TICKER_SYMBOL_PATTERN.fullmatch(symbol):
        raise SymbolValidationError(f"symbol must match {TICKER_SYMBOL_PATTERN.pattern} (got {symbol!r})")
    # A dot-only string would pass the regex but resolve to ``.`` /
    # ``..`` in path joins. Reject up-front.
    if set(symbol) <= {"."}:
        raise SymbolValidationError(f"symbol cannot be only dots (got {symbol!r})")
    return symbol.upper()


class WorkspaceError(ValueError):
    """Raised when a workspace request violates the layout contract."""


@dataclass(frozen=True, slots=True)
class Workspace:
    """Resolved on-disk layout for one run.

    All paths are absolute. Construction does not create directories;
    call :meth:`ensure_layout` to materialize the empty tree.
    """

    run_id: str
    artifacts_root: Path
    root: Path

    @property
    def workspace_dir(self) -> Path:
        """The single directory mounted into the LEAN container."""
        return self.root / "workspace"

    @property
    def project_dir(self) -> Path:
        return self.workspace_dir / "project"

    @property
    def data_dir(self) -> Path:
        return self.workspace_dir / "data"

    @property
    def output_dir(self) -> Path:
        return self.workspace_dir / "output"

    @property
    def object_store_dir(self) -> Path:
        """LEAN ObjectStore root inside the workspace.

        LEAN's default ObjectStore is rooted at
        ``/Lean/Launcher/bin/Debug/storage`` (image overlay) which is
        invisible to the manifest and unwritable under ``--read-only``.
        Pointing ``object-store-root`` here keeps everything LEAN
        writes inside the workspace where the manifest can hash it and
        the operator can inspect it.
        """
        return self.output_dir / "storage"

    @property
    def lean_log_path(self) -> Path:
        """LEAN's own runtime log; the launcher reads it to classify errors."""
        return self.output_dir / "log.txt"

    @property
    def launcher_dir(self) -> Path:
        return self.workspace_dir / "launcher"

    @property
    def normalized_dir(self) -> Path:
        return self.root / "normalized"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def launcher_log_path(self) -> Path:
        return self.launcher_dir / "launcher.log"

    def ensure_layout(self) -> None:
        """Create the directory tree if it does not exist.

        Idempotent so callers can pre-create the workspace and stage data
        before the launcher request; the launcher re-asserts existence.
        ``object_store_dir`` is created up-front so LEAN — even when
        launched with ``--read-only`` once the Phase 1c relaxation
        lands — does not need to ``mkdir`` it itself.
        """
        for d in (
            self.workspace_dir,
            self.project_dir,
            self.data_dir,
            self.output_dir,
            self.object_store_dir,
            self.launcher_dir,
            self.normalized_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def validate_run_id(run_id: str) -> str:
    """Return the validated slug or raise.

    Returns the validated string (not ``None``) so callers can bind
    ``safe_run_id = validate_run_id(user_input)`` and have CodeQL's
    taint analysis see the regex as the sanitizer between user input
    and any path operation. The previous signature returned ``None``
    and relied on the side-effect of raising, which is correct at
    runtime but invisible to dataflow analysis.

    Path separators, "." components, leading hyphens, uppercase, or
    runs over 64 chars are all rejected — anything that could be used
    to escape the artifacts-root boundary via the resolved path.
    """
    if not isinstance(run_id, str):
        raise WorkspaceError(f"run_id must be str, got {type(run_id).__name__}")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise WorkspaceError(f"run_id must match ^[a-z0-9][a-z0-9_-]{{2,63}}$ (got {run_id!r})")
    return run_id


def resolve_workspace(run_id: str, artifacts_root: Path) -> Workspace:
    """Resolve ``run_id`` to a workspace strictly under ``artifacts_root``.

    Three-stage defense against path traversal:

    1. :func:`validate_run_id` rejects any slug that contains a path
       separator, ``.`` component, or whitespace — the boundary check
       the router/launcher use directly.
    2. **Reconstruct from the regex match group** so the value flowing
       into the path operation is provably derived from the regex
       capture rather than from the raw user input. CodeQL's
       path-injection rule recognises ``re.Match.group()`` output as
       sanitized; the cross-function ``validate_run_id`` return value
       it does not follow.
    3. After the join, the candidate is resolved and verified to live
       under ``artifacts_root.resolve()`` via :func:`os.path.commonpath`.
       This catches symlink escapes the regex cannot see (a symlink
       under ``artifacts_root`` pointing outside it).

    Three together close the path-traversal class even though each
    alone would be insufficient: the regex blocks textual escapes,
    the match-group reconstruction makes the sanitizer visible to
    dataflow analysis, and the commonpath check blocks filesystem-
    level escapes.
    """
    import os

    # Boundary check (raises on bad input).
    validate_run_id(run_id)
    # Reconstruct from the regex match group so the value used below
    # is provably from the regex capture, not the raw user input.
    # CodeQL's path-injection rule treats ``re.Match.group(0)`` as
    # sanitized; an indirect-via-helper-function value, it does not.
    match = RUN_ID_PATTERN.fullmatch(run_id)
    if match is None:
        # Defensive: validate_run_id already raised on this case.
        raise WorkspaceError(f"run_id rejected on second check: {run_id!r}")
    safe_run_id = match.group(0)

    root_resolved = artifacts_root.resolve()
    # ``resolve(strict=False)`` returns the canonical path even when
    # the target does not yet exist — that's the Phase 1 case where
    # the workspace dir is materialized after this call.
    candidate = (root_resolved / safe_run_id).resolve(strict=False)
    # ``commonpath`` is the documented CodeQL sanitizer for path
    # traversal: if the common prefix is not the root, the candidate
    # escapes via symlinks or normalization.
    try:
        common = os.path.commonpath([str(candidate), str(root_resolved)])
    except ValueError as e:
        # commonpath raises on drive-letter mismatches on Windows.
        raise WorkspaceError(f"workspace path {candidate} cannot share a root with {root_resolved}") from e
    if common != str(root_resolved):
        raise WorkspaceError(f"workspace path {candidate} escapes artifacts root {root_resolved}")
    return Workspace(
        run_id=safe_run_id,
        artifacts_root=root_resolved,
        root=candidate,
    )
