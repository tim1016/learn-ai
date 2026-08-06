"""Process-local cache of open ``ClerkSqliteRepository`` instances.

The execution lease (pinned §2) and the nine startup checks (§9) are
per-process, meaningful-once concerns — not something to redo on every HTTP
request. This module is the one place that opens and reuses a repository
for the life of the process, matching the pinned trusted-local,
single-worker topology (decision 1).
"""

from __future__ import annotations

import threading
from pathlib import Path

from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

_lock = threading.Lock()
_repositories: dict[str, ClerkSqliteRepository] = {}


def get_or_open_repository(*, account_id: str, artifacts_root: Path) -> ClerkSqliteRepository:
    """Return this process's repository for ``account_id``, opening it once."""
    with _lock:
        repo = _repositories.get(account_id)
        if repo is not None:
            return repo
        repo = ClerkSqliteRepository.open(account_id=account_id, artifacts_root=artifacts_root)
        _repositories[account_id] = repo
        return repo


def close_all_repositories() -> None:
    """Close and drop every cached repository during process shutdown."""
    with _lock:
        for repo in _repositories.values():
            repo.close()
        _repositories.clear()


def reset_for_testing() -> None:
    """Test-only alias for production shutdown cleanup."""
    close_all_repositories()
