"""Runtime broker-write fence for AccountOwner-controlled writes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class AccountOwnerWriteGrant:
    account_id: str
    owner_generation: int
    boundary: str


class AccountOwnerWriteFenceError(RuntimeError):
    def __init__(
        self,
        *,
        reason: str,
        boundary: str,
        account_id: str | None = None,
        current_owner_generation: int | None = None,
        grant_owner_generation: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.boundary = boundary
        self.account_id = account_id
        self.current_owner_generation = current_owner_generation
        self.grant_owner_generation = grant_owner_generation


_current_account_owner_write_grant: ContextVar[AccountOwnerWriteGrant | None] = ContextVar(
    "current_account_owner_write_grant",
    default=None,
)


@contextmanager
def account_owner_write_grant(
    *,
    account_id: str,
    owner_generation: int,
    boundary: str,
) -> Iterator[AccountOwnerWriteGrant]:
    grant = AccountOwnerWriteGrant(
        account_id=account_id,
        owner_generation=owner_generation,
        boundary=boundary,
    )
    token = _current_account_owner_write_grant.set(grant)
    try:
        yield grant
    finally:
        _current_account_owner_write_grant.reset(token)


def current_account_owner_write_grant() -> AccountOwnerWriteGrant | None:
    return _current_account_owner_write_grant.get()


def require_account_owner_write_grant(
    *,
    account_id: str | None,
    boundary: str,
) -> AccountOwnerWriteGrant:
    grant = current_account_owner_write_grant()
    if grant is None:
        raise AccountOwnerWriteFenceError(
            reason="ACCOUNT_OWNER_WRITE_GRANT_MISSING",
            boundary=boundary,
            account_id=account_id,
        )
    if account_id is not None and grant.account_id != account_id:
        raise AccountOwnerWriteFenceError(
            reason="ACCOUNT_OWNER_WRITE_ACCOUNT_MISMATCH",
            boundary=boundary,
            account_id=account_id,
            grant_owner_generation=grant.owner_generation,
        )
    return grant


__all__ = [
    "AccountOwnerWriteFenceError",
    "AccountOwnerWriteGrant",
    "account_owner_write_grant",
    "current_account_owner_write_grant",
    "require_account_owner_write_grant",
]
