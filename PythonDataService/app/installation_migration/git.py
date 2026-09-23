"""The git seam of installation migration (#2268).

The manifest records the source commit; import refuses unless the
destination's checked-out commit is that commit or a descendant of it —
decided by git ancestry, never by commit timestamps, which a rebase or a
clock can reorder.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from app.installation_migration.errors import MigrationRefused

_GIT_TIMEOUT_S = 60.0

Runner = Callable[..., subprocess.CompletedProcess[str]]


class GitPort(Protocol):
    """The git facts installation migration reads."""

    def head_commit(self) -> str: ...

    def tree_dirty(self) -> bool: ...

    def commit_exists(self, commit: str) -> bool: ...

    def is_ancestor(self, ancestor: str, descendant: str) -> bool: ...


class SubprocessGit:
    """:class:`GitPort` over the ``git`` executable in one checkout."""

    def __init__(self, repo_root: Path, *, run: Runner = subprocess.run) -> None:
        self._repo_root = repo_root
        self._run = run

    def _git(
        self, *arguments: str, allowed_exit_codes: frozenset[int] = frozenset({0})
    ) -> subprocess.CompletedProcess[str]:
        command = ["git", "-C", str(self._repo_root), *arguments]
        try:
            completed = self._run(
                command, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise MigrationRefused(
                "git_unavailable",
                f"`{' '.join(command)}` could not run: {exc}",
                details={"command": command},
            ) from exc
        if completed.returncode not in allowed_exit_codes:
            raise MigrationRefused(
                "git_command_failed",
                f"`{' '.join(command)}` exited {completed.returncode}: "
                f"{completed.stderr.strip()}",
                details={"command": command, "exit_code": completed.returncode},
            )
        return completed

    def head_commit(self) -> str:
        return self._git("rev-parse", "--verify", "HEAD^{commit}").stdout.strip()

    def tree_dirty(self) -> bool:
        status = self._git("status", "--porcelain", "--untracked-files=no")
        return bool(status.stdout.strip())

    def commit_exists(self, commit: str) -> bool:
        probe = self._git(
            "cat-file", "-e", f"{commit}^{{commit}}", allowed_exit_codes=frozenset({0, 1, 128})
        )
        return probe.returncode == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        probe = self._git(
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            allowed_exit_codes=frozenset({0, 1}),
        )
        return probe.returncode == 0


__all__ = ["GitPort", "SubprocessGit"]
