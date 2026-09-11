"""The two seams this package declares and does not implement.

Both belong to Package C of the user-owned broker configurations plan: the
credential-slot allowlist and resolver, and read-only broker account
verification (contract §7). This package owns the *storage* of what they
produce — an opaque slot string on a revision, and an account pin — so it
declares the protocols its routes call and ships fail-closed defaults, rather
than reaching into ``app/broker/alpaca/config.py`` or guessing an allowlist
shape that ADR 0060 open question 2 leaves to the owner.

The defaults refuse. An installation running only Package B can list profiles
and stage a selection; it cannot claim a slot exists or that an account was
observed, because in that build neither fact has a source.

**This package does not validate a credential slot against the allowlist, on
purpose.** Contract §3 says a slot outside the allowlist is refused with
``credential_slot_unknown`` and never used to build a variable name — but the
*shape* of that allowlist is ADR 0060 open question 2, unanswered, and package
C's fork to make. So a slot arrives here as an opaque string and is stored as
one; the refusal it will produce is declared in ``errors.py``
(``CredentialSlotUnknown``, ``CredentialSlotUnavailable``) as shared vocabulary
for C to raise, not left for C to invent. What this package guarantees is
narrower and checkable: **it performs no environment lookup at all**, so a slot
name it stores cannot reach one.

``observe_accounts`` also carries the revision's ``live_envelope``. That was
added by package D, the first package to *implement* the protocol, and the
reason is a hard constraint rather than a convenience: an Alpaca runtime
binding for a ``live`` endpoint mode cannot be constructed without all six
envelope values — ``AlpacaSettings._enforce_mode_agreement`` refuses it — so a
verifier handed only a slot and a mode could never observe the account of a
live revision at all. The alternative was fabricating placeholder envelope
values to get past the validator, which would put a number nobody chose on the
live path (ADR 0059). The three parameters are now exactly the inputs a
resolvable binding needs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from app.broker_configuration.errors import AccountVerificationFailed
from app.broker_configuration.records import CredentialSlotStatus, EndpointMode, ObservedAccount


@runtime_checkable
class CredentialSlotDirectory(Protocol):
    """Slot labels and availability. Never a value, a fragment, or a var name."""

    def list_slots(self) -> tuple[CredentialSlotStatus, ...]: ...


@runtime_checkable
class AccountVerifier(Protocol):
    """Read-only broker account discovery under one revision's mode.

    No submit, no cancel, no mutation of any kind (contract §3).

    ``live_envelope`` carries the revision's six values under
    ``LiveEnvelopeValues``' own field names, or ``None`` on a revision that
    declares none. A ``live`` mode needs it to build a binding at all; a
    ``paper`` mode ignores it.
    """

    async def observe_accounts(
        self,
        *,
        credential_slot: str,
        endpoint_mode: EndpointMode,
        live_envelope: Mapping[str, object] | None = None,
    ) -> tuple[ObservedAccount, ...]: ...


class UnconfiguredCredentialSlotDirectory:
    """No slot directory is installed, so no slot is claimed to exist."""

    def list_slots(self) -> tuple[CredentialSlotStatus, ...]:
        return ()


class UnconfiguredAccountVerifier:
    """No broker verifier is installed, so no account can be observed."""

    async def observe_accounts(
        self,
        *,
        credential_slot: str,
        endpoint_mode: EndpointMode,
        live_envelope: Mapping[str, object] | None = None,
    ) -> tuple[ObservedAccount, ...]:
        raise AccountVerificationFailed(
            "This build cannot verify a broker account: no account verifier is installed.",
            next_step="Verify the account once credential resolution is wired into this service.",
        )


__all__ = [
    "AccountVerifier",
    "CredentialSlotDirectory",
    "UnconfiguredAccountVerifier",
    "UnconfiguredCredentialSlotDirectory",
]
