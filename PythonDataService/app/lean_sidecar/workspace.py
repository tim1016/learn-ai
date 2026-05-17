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


def validate_run_id(run_id: str) -> None:
    """Reject any run_id that is not a strict slug.

    Path separators, "." components, leading hyphens, uppercase, or
    runs over 64 chars are all rejected — anything that could be used
    to escape the artifacts-root boundary via the resolved path.
    """
    if not isinstance(run_id, str):
        raise WorkspaceError(f"run_id must be str, got {type(run_id).__name__}")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise WorkspaceError(f"run_id must match ^[a-z0-9][a-z0-9_-]{{2,63}}$ (got {run_id!r})")


def resolve_workspace(run_id: str, artifacts_root: Path) -> Workspace:
    """Resolve ``run_id`` to a workspace strictly under ``artifacts_root``.

    Path-under-root is enforced after symlink resolution: the resolved
    workspace root must equal ``artifacts_root.resolve() / run_id``.
    """
    validate_run_id(run_id)
    root_resolved = artifacts_root.resolve()
    workspace_root = root_resolved / run_id
    # Re-resolve the candidate; if it resolves outside root (symlink
    # escape attempts, etc.) refuse.
    resolved = workspace_root.resolve() if workspace_root.exists() else workspace_root
    try:
        resolved.relative_to(root_resolved)
    except ValueError as e:
        raise WorkspaceError(f"workspace path {resolved} escapes artifacts root {root_resolved}") from e
    return Workspace(
        run_id=run_id,
        artifacts_root=root_resolved,
        root=workspace_root,
    )
