"""Established-accounts registry — pinned contracts doc §1a.

Lives one directory above any single account (``accounts/alpaca/``, not
``accounts/alpaca/<account_id>/``) so deleting or corrupting one account's
directory can never erase its own establishment evidence. This is what lets
startup check 2 distinguish "this account's database was deleted" from
"this account has never been initialized" — the PRD's own adversarial test
15.4 ("remove clerk.db after authority was established and prove it is not
recreated") has no other mechanism to appeal to.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.broker.alpaca.paths import fsync_directory

REGISTRY_FILENAME = "_established_generations.jsonl"


@dataclass(frozen=True)
class EstablishedGeneration:
    account_id: str
    authority_generation: int
    db_identity_token: str
    established_at_ms: int


class EstablishedAccountsRegistry:
    """Append-only, fsync'd ledger of every successful ``clerk.db`` init."""

    def __init__(self, alpaca_accounts_root: Path) -> None:
        self._path = alpaca_accounts_root / REGISTRY_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def record(self, generation: EstablishedGeneration) -> None:
        """Append one line, fsync'd, for a newly-initialized authority generation."""
        existed = self._path.is_file()
        line = (
            json.dumps(
                {
                    "account_id": generation.account_id,
                    "authority_generation": generation.authority_generation,
                    "db_identity_token": generation.db_identity_token,
                    "established_at_ms": generation.established_at_ms,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            # Same durability gap as the mirror's first write, closed for
            # the same reason (open-pr-review-2026-08-05.md P2 "First
            # registry creation lacks parent-directory fsync").
            fsync_directory(self._path.parent)

    def is_established(self, account_id: str) -> bool:
        """True if this account has ever had a ``clerk.db`` authority initialized."""
        return self.latest(account_id) is not None

    def latest(self, account_id: str) -> EstablishedGeneration | None:
        """The highest-generation entry recorded for this account, if any."""
        if not self._path.is_file():
            return None
        latest: EstablishedGeneration | None = None
        with self._path.open(encoding="utf-8") as handle:
            for raw in handle:
                stripped = raw.strip()
                if not stripped:
                    continue
                entry: dict[str, Any] = json.loads(stripped)
                if entry["account_id"] != account_id:
                    continue
                if latest is None or entry["authority_generation"] > latest.authority_generation:
                    latest = EstablishedGeneration(**entry)
        return latest
