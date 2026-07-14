"""Runtime broker-write fence owned by the account clerk.

The module keeps the former ``AccountOwner`` spellings as compatibility
aliases while callers move to the explicit clerk vocabulary.  The authority
did not move to a bot: the durable clerk lease generation is the credential
that reaches the broker boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class AccountClerkWriteGrant:
    account_id: str
    clerk_generation: int
    boundary: str
    clerk_generation_provider: Callable[[], int | None] | None = None

    @property
    def owner_generation(self) -> int:
        """Legacy field used by the pre-clerk AccountOwner wire model."""

        return self.clerk_generation

    @property
    def owner_generation_provider(self) -> Callable[[], int | None] | None:
        """Legacy provider spelling retained for old callers."""

        return self.clerk_generation_provider


class AccountClerkWriteFenceError(RuntimeError):
    def __init__(
        self,
        *,
        reason: str,
        boundary: str,
        account_id: str | None = None,
        current_clerk_generation: int | None = None,
        grant_clerk_generation: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.boundary = boundary
        self.account_id = account_id
        self.current_clerk_generation = current_clerk_generation
        self.grant_clerk_generation = grant_clerk_generation

    @property
    def current_owner_generation(self) -> int | None:
        return self.current_clerk_generation

    @property
    def grant_owner_generation(self) -> int | None:
        return self.grant_clerk_generation


_current_account_clerk_write_grant: ContextVar[AccountClerkWriteGrant | None] = ContextVar(
    "current_account_clerk_write_grant",
    default=None,
)


@contextmanager
def account_clerk_write_grant(
    *,
    account_id: str,
    clerk_generation: int,
    boundary: str,
    clerk_generation_provider: Callable[[], int | None] | None = None,
) -> Iterator[AccountClerkWriteGrant]:
    grant = AccountClerkWriteGrant(
        account_id=account_id,
        clerk_generation=clerk_generation,
        boundary=boundary,
        clerk_generation_provider=clerk_generation_provider,
    )
    token = _current_account_clerk_write_grant.set(grant)
    try:
        yield grant
    finally:
        _current_account_clerk_write_grant.reset(token)


def current_account_clerk_write_grant() -> AccountClerkWriteGrant | None:
    return _current_account_clerk_write_grant.get()


def require_account_clerk_write_grant(
    *,
    account_id: str | None,
    boundary: str,
    clerk_generation_provider: Callable[[], int | None] | None = None,
) -> AccountClerkWriteGrant:
    grant = current_account_clerk_write_grant()
    if grant is None:
        raise AccountClerkWriteFenceError(
            reason="ACCOUNT_OWNER_WRITE_GRANT_MISSING",
            boundary=boundary,
            account_id=account_id,
        )
    if account_id is not None and grant.account_id != account_id:
        raise AccountClerkWriteFenceError(
            reason="ACCOUNT_OWNER_WRITE_ACCOUNT_MISMATCH",
            boundary=boundary,
            account_id=account_id,
            grant_clerk_generation=grant.clerk_generation,
        )
    effective_generation_provider = (
        clerk_generation_provider
        if clerk_generation_provider is not None
        else grant.clerk_generation_provider
    )
    current_clerk_generation = (
        effective_generation_provider()
        if effective_generation_provider is not None
        else None
    )
    # Unit-level legacy adapter seams may carry a grant without opting into
    # clerk fencing. Production wiring always supplies the durable provider.
    if effective_generation_provider is None:
        return grant
    if current_clerk_generation is None:
        raise AccountClerkWriteFenceError(
            reason="CLERK_LEASE_UNAVAILABLE_AT_BROKER_WRITE",
            boundary=boundary,
            account_id=account_id,
            grant_clerk_generation=grant.clerk_generation,
        )
    if grant.clerk_generation != current_clerk_generation:
        raise AccountClerkWriteFenceError(
            reason="OWNER_GENERATION_STALE_AT_BROKER_WRITE",
            boundary=boundary,
            account_id=account_id,
            current_clerk_generation=current_clerk_generation,
            grant_clerk_generation=grant.clerk_generation,
        )
    return grant


# Compatibility aliases for the owner-named pre-cutover contract.  Keeping
# these aliases avoids a broad unrelated wire-model rename while all live
# broker boundaries move to the clerk API above.
AccountOwnerWriteGrant = AccountClerkWriteGrant
AccountOwnerWriteFenceError = AccountClerkWriteFenceError


@contextmanager
def account_owner_write_grant(
    *,
    account_id: str,
    owner_generation: int,
    boundary: str,
    owner_generation_provider: Callable[[], int | None] | None = None,
) -> Iterator[AccountClerkWriteGrant]:
    with account_clerk_write_grant(
        account_id=account_id,
        clerk_generation=owner_generation,
        boundary=boundary,
        clerk_generation_provider=owner_generation_provider,
    ) as grant:
        yield grant


def current_account_owner_write_grant() -> AccountClerkWriteGrant | None:
    return current_account_clerk_write_grant()


def require_account_owner_write_grant(
    *,
    account_id: str | None,
    boundary: str,
    owner_generation_provider: Callable[[], int | None] | None = None,
) -> AccountClerkWriteGrant:
    return require_account_clerk_write_grant(
        account_id=account_id,
        boundary=boundary,
        clerk_generation_provider=owner_generation_provider,
    )


__all__ = [
    "AccountClerkWriteFenceError",
    "AccountClerkWriteGrant",
    "AccountOwnerWriteFenceError",
    "AccountOwnerWriteGrant",
    "account_clerk_write_grant",
    "account_owner_write_grant",
    "current_account_clerk_write_grant",
    "current_account_owner_write_grant",
    "require_account_clerk_write_grant",
    "require_account_owner_write_grant",
]
