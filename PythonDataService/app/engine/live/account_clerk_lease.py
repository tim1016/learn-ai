"""Renewable lifecycle lease for one supervised Account Clerk process."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.engine.live.account_artifacts import AccountClerkLease, write_account_clerk_lease
from app.engine.live.account_clerk_journal import _now_ms

_CLERK_LEASE_TTL_MS = 5_000


class AccountClerkLeaseWriter:
    """Renew one supervised Clerk lease until the daemon reaps its process."""

    def __init__(
        self,
        *,
        artifacts_root: Path,
        account_id: str,
        generation: int,
        pid: int,
        ibkr_client_id: int | None = None,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._generation = generation
        self._pid = pid
        self._ibkr_client_id = ibkr_client_id
        self._now_ms = now_ms
        self._started_at_ms = now_ms()

    def renew(self, *, draining: bool = False) -> AccountClerkLease:
        now_ms = self._now_ms()
        lease = AccountClerkLease(
            account_id=self._account_id,
            generation=self._generation,
            pid=self._pid,
            ibkr_client_id=self._ibkr_client_id,
            status="DRAINING" if draining else "RUNNING",
            started_at_ms=self._started_at_ms,
            renewed_at_ms=now_ms,
            valid_until_ms=now_ms if draining else now_ms + _CLERK_LEASE_TTL_MS,
        )
        write_account_clerk_lease(self._artifacts_root, lease)
        return lease
