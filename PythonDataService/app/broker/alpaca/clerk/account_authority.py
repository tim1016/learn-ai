"""Account identity and port binding for Clerk authorities.

An authority is selected by its account identity, never by whichever Clerk was
constructed last in this process.  ``sim:`` and ``shadow:`` are closed
namespaces (ADR 0059 D1): a real Alpaca port cannot be bound to either, a
synthetic port cannot be bound to an external account, and a shadow authority
reads one real-money account while never submitting to it.  ``real_live`` is
never inferred from an account id's shape — it is the caller's positively
learned mode (ADR 0054), so the kind derivation takes it as an input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.schemas.account_authority import AuthorityKind, CustodyWorld

# The clerk-side name for the one canonical account-world kind (ADR 0059 D1).
AccountAuthorityKind = AuthorityKind
SIM_ACCOUNT_PREFIX = "sim:"
SHADOW_ACCOUNT_PREFIX = "shadow:"
PAPER_EVIDENCE_ACCOUNT_PREFIX = "paper:"
"""Instance-scoped evidence namespace for real-paper retained source bars.

Not a Clerk custody account: custody stays on the real Alpaca account. This
namespace only scopes the ``SourceBarLedger`` file so each paper instance's
retained-replay warmup (FR-016) sees exactly its own observations, mirroring
Dry Run's ``sim:`` scoping.
"""


SHADOW_EVIDENCE_ACCOUNT_PREFIX = "shadow-evidence:"
"""Instance-scoped evidence namespace for shadow retained source bars.

Custody is the account-scoped ``shadow:<live_account_id>`` authority; every
instance that runs on it keeps its own retained-bar ledger here, exactly as a
real-paper instance keeps ``paper:<instance>``. The prefix is deliberately not
``shadow:`` — an evidence namespace is never a custody identity.
"""


# Every namespace a real Alpaca account id may not occupy: the two reserved
# custody worlds and the two instance-scoped evidence namespaces. All four are
# minted by this module, so a "real" account carrying any of them is a
# composition bug, not an operator typo.
_RESERVED_PREFIXES: tuple[str, ...] = (
    SIM_ACCOUNT_PREFIX,
    SHADOW_ACCOUNT_PREFIX,
    PAPER_EVIDENCE_ACCOUNT_PREFIX,
    SHADOW_EVIDENCE_ACCOUNT_PREFIX,
)


class AccountAuthorityIdentityError(ValueError):
    """A port or authority was given an account from the other namespace."""


def is_synthetic_account_id(account_id: str) -> bool:
    """Return whether ``account_id`` belongs to the reserved synthetic namespace."""
    return account_id.startswith(SIM_ACCOUNT_PREFIX)


def is_shadow_account_id(account_id: str) -> bool:
    """Return whether ``account_id`` belongs to the reserved shadow namespace."""
    return account_id.startswith(SHADOW_ACCOUNT_PREFIX)


def require_real_account_id(account_id: str) -> str:
    """Reject a reserved-namespace identity before it can bind an Alpaca port."""
    if not isinstance(account_id, str) or not account_id:
        raise AccountAuthorityIdentityError("real account identity must be non-empty")
    if account_id.startswith(_RESERVED_PREFIXES):
        raise AccountAuthorityIdentityError(
            "real Alpaca ports refuse the reserved sim:/shadow:/paper:/shadow-evidence: "
            "account identities"
        )
    return account_id


def require_synthetic_account_id(account_id: str) -> str:
    """Reject an external account before it can bind a simulated port."""
    if not isinstance(account_id, str) or not account_id.startswith(SIM_ACCOUNT_PREFIX):
        raise AccountAuthorityIdentityError("synthetic ports require a sim: account identity")
    if len(account_id) == len(SIM_ACCOUNT_PREFIX):
        raise AccountAuthorityIdentityError("synthetic account identity must name one account")
    return account_id


def require_shadow_account_id(account_id: str) -> str:
    """Reject anything but the reserved shadow namespace."""
    if not isinstance(account_id, str) or not account_id.startswith(SHADOW_ACCOUNT_PREFIX):
        raise AccountAuthorityIdentityError("shadow authorities require a shadow: account identity")
    if len(account_id) == len(SHADOW_ACCOUNT_PREFIX):
        raise AccountAuthorityIdentityError("shadow account identity must name one account")
    return account_id


def authority_kind_for_account(
    account_id: str,
    *,
    account_mode: Literal["paper", "live"] = "paper",
) -> AccountAuthorityKind:
    """Derive the closed read-contract kind from a namespace and a learned mode.

    ``account_mode`` is what the caller positively learned from the broker for
    a real account; it is never guessed from the id's shape.
    """
    if is_synthetic_account_id(account_id):
        return "synthetic"
    if is_shadow_account_id(account_id):
        return "shadow"
    return "real_live" if account_mode == "live" else "real_paper"


def synthetic_account_id_for_strategy(strategy_instance_id: str) -> str:
    """Return the one isolated Dry Run authority for an immutable instance."""
    from app.engine.live.identity import validate_strategy_instance_id

    return f"{SIM_ACCOUNT_PREFIX}{validate_strategy_instance_id(strategy_instance_id)}"


def shadow_account_id_for_live_account(live_account_id: str) -> str:
    """Return the one shadow authority that reads ``live_account_id``."""
    return f"{SHADOW_ACCOUNT_PREFIX}{require_real_account_id(live_account_id)}"


def custody_account_id_for(world: CustodyWorld, observed_account_id: str) -> str:
    """The custody id a ``world`` authority holds while observing ``observed_account_id``.

    A broker read answers the account it is pointed at; the projection
    answers the account the authority custodies. Those are the same id in
    the real-paper world and deliberately different under shadow, where
    custody is ``shadow:<live_account_id>``. Any identity comparison between
    the two must go through this function or it reads a correct shadow boot
    as a misconfiguration.
    """
    if world == "shadow":
        return shadow_account_id_for_live_account(observed_account_id)
    return observed_account_id


def live_account_id_for_shadow_account(account_id: str) -> str:
    """The live account a ``shadow:`` custody id observes; a real id is itself.

    The inverse of ``shadow_account_id_for_live_account``. The module that
    defines a namespace prefix is the only one that should add or strip it, so
    no feature module has to import ``SHADOW_ACCOUNT_PREFIX`` to do string
    surgery on an identity.
    """
    return account_id.removeprefix(SHADOW_ACCOUNT_PREFIX)


def custody_account_ids_for(live_account_id: str) -> frozenset[str]:
    """Every custody id an authority over this live account may hold.

    The set form of ``custody_account_id_for``: under the Shadow Account
    Authority custody is ``shadow:<live_account_id>``, so an instance
    rehearsing on this account seals the shadow id, while a ``real_live``
    custody seals the live id itself. Both are the same account to an operator.
    """
    return frozenset({live_account_id, custody_account_id_for("shadow", live_account_id)})


def paper_evidence_account_id_for_strategy(strategy_instance_id: str) -> str:
    """Return the isolated real-paper source-bar namespace for one instance."""
    from app.engine.live.identity import validate_strategy_instance_id

    return f"{PAPER_EVIDENCE_ACCOUNT_PREFIX}{validate_strategy_instance_id(strategy_instance_id)}"


def shadow_evidence_account_id_for_strategy(strategy_instance_id: str) -> str:
    """Return the isolated shadow source-bar namespace for one instance."""
    from app.engine.live.identity import validate_strategy_instance_id

    return f"{SHADOW_EVIDENCE_ACCOUNT_PREFIX}{validate_strategy_instance_id(strategy_instance_id)}"


def is_shadow_evidence_account_id(account_id: str) -> bool:
    """Return whether ``account_id`` is a shadow instance's evidence namespace."""
    return account_id.startswith(SHADOW_EVIDENCE_ACCOUNT_PREFIX)


def evidence_account_id_for(
    *,
    mode: str,
    strategy_instance_id: str,
    custody_kind: AccountAuthorityKind,
) -> str:
    """The evidence namespace whose ledger retains a binding's bars.

    Dry Run's custody and evidence share ``sim:<instance>``. Every other
    binding's evidence is instance-scoped under the world the primary
    authority custodies in — ``paper:`` on the real-paper authority,
    ``shadow-evidence:`` on the shadow authority — so two instances on one
    symbol never share a ledger and a replay proof reads exactly what its run
    retained. Whether a mode is replayable is the replay proof's judgement,
    not this function's.
    """
    if mode == "dry_run":
        return synthetic_account_id_for_strategy(strategy_instance_id)
    if custody_kind == "shadow":
        return shadow_evidence_account_id_for_strategy(strategy_instance_id)
    return paper_evidence_account_id_for_strategy(strategy_instance_id)


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
    """Create a real-account composition only after rejecting reserved namespaces."""
    return AccountBoundBrokerPorts(
        account_id=require_real_account_id(account_id),
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


def bind_shadow_ports(
    *,
    account_id: str,
    read: BrokerReadPort,
    trade: BrokerTradePort,
) -> AccountBoundBrokerPorts:
    """Create a shadow composition only for the reserved namespace (ADR 0059 D2).

    The trade port is checked, not trusted: a shadow authority binds only
    the no-submit port, so the live port its selector still holds in scope
    cannot be wired here by a later edit that type-checks.
    """
    # Local import: ``shadow_broker`` imports this module for the namespace
    # helpers, so a module-level import would cycle.
    from app.broker.alpaca.clerk.shadow_broker import NoSubmitAlpacaTradePort

    if not isinstance(trade, NoSubmitAlpacaTradePort):
        raise AccountAuthorityIdentityError(
            "shadow authorities bind only the no-submit trade port"
        )
    return AccountBoundBrokerPorts(
        account_id=require_shadow_account_id(account_id),
        authority_kind="shadow",
        read=read,
        trade=trade,
    )


__all__ = [
    "PAPER_EVIDENCE_ACCOUNT_PREFIX",
    "SHADOW_ACCOUNT_PREFIX",
    "SHADOW_EVIDENCE_ACCOUNT_PREFIX",
    "SIM_ACCOUNT_PREFIX",
    "AccountAuthorityIdentityError",
    "AccountAuthorityKind",
    "AccountBoundBrokerPorts",
    "authority_kind_for_account",
    "bind_real_alpaca_ports",
    "bind_shadow_ports",
    "bind_synthetic_ports",
    "custody_account_id_for",
    "custody_account_ids_for",
    "evidence_account_id_for",
    "is_shadow_account_id",
    "is_shadow_evidence_account_id",
    "is_synthetic_account_id",
    "live_account_id_for_shadow_account",
    "paper_evidence_account_id_for_strategy",
    "require_real_account_id",
    "require_shadow_account_id",
    "require_synthetic_account_id",
    "shadow_account_id_for_live_account",
    "shadow_evidence_account_id_for_strategy",
    "synthetic_account_id_for_strategy",
]
