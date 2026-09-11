"""The Alpaca-backed implementations of the two protocols ``seams.py`` declares.

Package B declared :class:`~app.broker_configuration.seams.CredentialSlotDirectory`
and :class:`~app.broker_configuration.seams.AccountVerifier` and shipped
fail-closed defaults that refuse everything, because the allowlist's shape and
the verification ceremony were package C's to build. C built them. This module
is the wiring, and nothing more: it holds no policy of its own, resolves no
credential itself, and reaches no environment variable — every one of those is
C's, behind ``app.broker.alpaca.profile``.

It lives here rather than in ``app/broker/alpaca/profile/`` on purpose. C is
deliberately unaware of the profiles database (it takes a revision's *values*,
never a revision), and B is deliberately unaware of the broker (its ``seams``
docstring says so). Something has to know both, and that something is a
composition module in the configuration package, imported lazily by
``runtime.build_service`` so neither package gains a module-level dependency on
the other.

**No secret crosses this boundary in either direction.** A slot's label and a
boolean is the whole credential fact that leaves :class:`AlpacaCredentialSlotDirectory`
(contract §3), and the verifier returns observed account identity — never a
value, a fragment, a length, or a variable name.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from app.broker.alpaca.profile import (
    CREDENTIAL_SLOT_DEFAULT,
    CREDENTIAL_SLOT_LIVE,
    CREDENTIAL_SLOTS,
    AccountDiscoveryPort,
    AlpacaCredentialEnvironment,
    describe_credential_slots,
    resolve_runtime_context,
    verify_account,
)
from app.broker.alpaca.profile import ObservedAccount as VerifiedAccount
from app.broker_configuration.records import CredentialSlotStatus, EndpointMode, ObservedAccount

# Operator-facing names for the code-owned closed allowlist. Presentation for a
# fixed set of two, so it is a literal rather than configuration — a slot the
# operator can pick but cannot name is not a settings surface.
# ``test_alpaca_seams.py`` asserts this covers every slot in ``CREDENTIAL_SLOTS``,
# so the fallback in :func:`_label` is a safety net, not a routine path.
CREDENTIAL_SLOT_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        CREDENTIAL_SLOT_DEFAULT: "Default credentials",
        CREDENTIAL_SLOT_LIVE: "Live credentials",
    }
)


def _label(slot: str) -> str:
    """The operator-facing label, falling back to the slot's own name.

    A slot added to C's allowlist without a label here shows its raw name
    rather than disappearing from the picker or raising on a listing route.
    """
    return CREDENTIAL_SLOT_LABELS.get(slot, slot)


def _observed(account: VerifiedAccount) -> ObservedAccount:
    """C's observation as B's stored shape.

    B's record deliberately carries less: the blocked flags and the observation
    clock are evidence for the operator's *selection* ceremony, which is C's
    verification result, not something the profiles database stores.
    """
    return ObservedAccount(
        account_id=account.account_id,
        account_mode=account.account_mode,
        account_status=account.account_status,
    )


class AlpacaCredentialSlotDirectory:
    """Slot labels and availability, over C's allowlist.

    Reports availability only. It deliberately does **not** populate
    ``verified_account_id`` / ``verified_at_ms``: answering those would mean a
    broker call per slot from a listing route, and contract §3 is explicit that
    credential resolution happens "at context construction — never per tick,
    never in a router". Which account a slot reaches is what the verify-account
    ceremony answers, on demand, for one revision at a time.
    """

    def __init__(self, *, environment: AlpacaCredentialEnvironment | None = None) -> None:
        # ``None`` reads the process environment; a supplied environment is the
        # injection seam C built for tests, passed straight through.
        self._environment = environment

    def list_slots(self) -> tuple[CredentialSlotStatus, ...]:
        return tuple(
            CredentialSlotStatus(
                slot=availability.slot,
                label=_label(availability.slot),
                available=availability.available,
            )
            for availability in describe_credential_slots(environment=self._environment)
        )


class AlpacaAccountVerifier:
    """Read-only broker account discovery, over C's verification.

    Every call resolves a **fresh** runtime context and lets ``verify_account``
    build a broker bound to it. Nothing is cached between calls: a verifier that
    reused a broker would answer for whichever revision happened to be verified
    first, which is the cross-configuration leak the resolved context exists to
    prevent.

    Refusals propagate as C's ``BrokerProfileError`` subclasses — the slot is
    unknown or unavailable, the revision will not resolve, the observed account
    contradicts the mode. They carry the contract §6 shape and are translated
    once, by the handler registered in ``app/main.py``.
    """

    def __init__(
        self,
        *,
        environment: AlpacaCredentialEnvironment | None = None,
        discovery: AccountDiscoveryPort | None = None,
    ) -> None:
        self._environment = environment
        self._discovery = discovery

    async def observe_accounts(
        self,
        *,
        credential_slot: str,
        endpoint_mode: EndpointMode,
        live_envelope: Mapping[str, object] | None = None,
    ) -> tuple[ObservedAccount, ...]:
        context = resolve_runtime_context(
            endpoint_mode=endpoint_mode,
            credential_slot=credential_slot,
            live_envelope=live_envelope,
            environment=self._environment,
        )
        verification = await verify_account(context, discovery=self._discovery)
        return tuple(_observed(candidate) for candidate in verification.candidates)


__all__ = [
    "CREDENTIAL_SLOTS",
    "CREDENTIAL_SLOT_LABELS",
    "AlpacaAccountVerifier",
    "AlpacaCredentialSlotDirectory",
]
