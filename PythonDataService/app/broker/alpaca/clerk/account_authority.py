"""Account identity and port binding for Clerk authorities.

An authority is selected by its account identity, never by whichever Clerk was
constructed last in this process.  ``sim:`` is a closed namespace: a real
Alpaca port cannot be bound to it and a synthetic port cannot be bound to an
external account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.broker.contract.ports import BrokerReadPort, BrokerTradePort

AccountAuthorityKind = Literal["real_paper", "synthetic"]
SIM_ACCOUNT_PREFIX = "sim:"


class AccountAuthorityIdentityError(ValueError):
    """A port or authority was given an account from the other namespace."""


def is_synthetic_account_id(account_id: str) -> bool:
    """Return whether ``account_id`` belongs to the reserved synthetic namespace."""
    return account_id.startswith(SIM_ACCOUNT_PREFIX)


def require_real_paper_account_id(account_id: str) -> str:
    """Reject a synthetic identity before it can bind an Alpaca port."""
    if not isinstance(account_id, str) or not account_id:
        raise AccountAuthorityIdentityError("real-paper account identity must be non-empty")
    if is_synthetic_account_id(account_id):
        raise AccountAuthorityIdentityError("real Alpaca ports refuse sim: account identities")
    return account_id


def require_synthetic_account_id(account_id: str) -> str:
    """Reject an external account before it can bind a simulated port."""
    if not isinstance(account_id, str) or not account_id.startswith(SIM_ACCOUNT_PREFIX):
        raise AccountAuthorityIdentityError("synthetic ports require a sim: account identity")
    if len(account_id) == len(SIM_ACCOUNT_PREFIX):
        raise AccountAuthorityIdentityError("synthetic account identity must name one account")
    return account_id


def authority_kind_for_account(account_id: str) -> AccountAuthorityKind:
    """Derive the closed read-contract kind from an account namespace."""
    return "synthetic" if is_synthetic_account_id(account_id) else "real_paper"


@dataclass(frozen=True)
class AccountBoundBrokerPorts:
    """Ports bound to one verified account identity at composition time."""

    account_id: str
    authority_kind: AccountAuthorityKind
    read: BrokerReadPort
    trade: BrokerTradePort


def bind_real_alpaca_ports(
    *,
    account_id: str,
    read: BrokerReadPort,
    trade: BrokerTradePort,
) -> AccountBoundBrokerPorts:
    """Create a real-paper composition only after rejecting ``sim:``."""
    return AccountBoundBrokerPorts(
        account_id=require_real_paper_account_id(account_id),
        authority_kind="real_paper",
        read=read,
        trade=trade,
    )


def bind_synthetic_ports(
    *,
    account_id: str,
    read: BrokerReadPort,
    trade: BrokerTradePort,
) -> AccountBoundBrokerPorts:
    """Create a synthetic composition only for the reserved namespace."""
    return AccountBoundBrokerPorts(
        account_id=require_synthetic_account_id(account_id),
        authority_kind="synthetic",
        read=read,
        trade=trade,
    )


__all__ = [
    "SIM_ACCOUNT_PREFIX",
    "AccountAuthorityIdentityError",
    "AccountAuthorityKind",
    "AccountBoundBrokerPorts",
    "authority_kind_for_account",
    "bind_real_alpaca_ports",
    "bind_synthetic_ports",
    "is_synthetic_account_id",
    "require_real_paper_account_id",
    "require_synthetic_account_id",
]
