"""Resolve a broker-configuration profile revision into a runtime binding.

Package C of the user-owned broker configurations plan (ADR 0060,
``docs/architecture/broker-configuration-profile-contract.md`` §3 and §7). This
package owns three things and nothing else:

1. **The credential slot allowlist and resolver** (:mod:`.credentials`) — a
   closed, code-owned set of opaque slot names, each mapping to exactly one
   injected environment pair. A slot name off the allowlist is refused before
   any environment lookup happens; a profile can therefore never name a
   variable, enumerate the environment, or reach a secret it was not given.
2. **The immutable resolved runtime context** (:mod:`.runtime_context`) — the
   one object a worker binds once at construction, carrying the revision's
   endpoint mode, its validated live-envelope values and the credentials its
   slot resolved to. The API base URL stays *derived* from the mode and is
   never a field.
3. **Read-only account verification and pinning**
   (:mod:`.account_verification`) — broker account discovery over a seam that
   can express nothing but ``get_account``, plus the pin decisions that keep
   "rotate a secret for the same account" and "point at a different account"
   structurally different operations.

Nothing here reads a *user* value from the environment: only secrets (via the
slot allowlist) and deployment bootstrap (``ALPACA_CLERK_DIR``, resolved by
``config.AlpacaSettings`` itself) come from there. ``config.py`` next door
remains the environment-backed settings type this package builds *from*
validated profile values rather than replacing.

This package wires nothing into ``app/main.py`` and changes no boot path;
Package D composes it into the worker.
"""

from __future__ import annotations

from app.broker.alpaca.profile.account_verification import (
    ACCOUNT_VERIFICATION_MAX_AGE_MS,
    AccountDiscoveryPort,
    AccountPin,
    AccountVerification,
    ObservedAccount,
    pin_observed_account,
    reverify_pinned_account,
    verify_account,
)
from app.broker.alpaca.profile.credentials import (
    CREDENTIAL_SLOT_DEFAULT,
    CREDENTIAL_SLOT_LIVE,
    CREDENTIAL_SLOTS,
    AlpacaCredentialEnvironment,
    CredentialSlotAvailability,
    ResolvedCredentials,
    credential_slot_available,
    describe_credential_slots,
    is_known_credential_slot,
    require_known_credential_slot,
    resolve_credentials,
)
from app.broker.alpaca.profile.errors import (
    AccountModeDisagreement,
    AccountPinMismatch,
    AccountVerificationFailed,
    BrokerProfileError,
    CredentialSlotUnavailable,
    CredentialSlotUnknown,
    RevisionIncomplete,
)
from app.broker.alpaca.profile.runtime_context import (
    LIVE_ENVELOPE_FIELDS,
    AlpacaRuntimeContext,
    resolve_runtime_context,
)

__all__ = [
    "ACCOUNT_VERIFICATION_MAX_AGE_MS",
    "CREDENTIAL_SLOTS",
    "CREDENTIAL_SLOT_DEFAULT",
    "CREDENTIAL_SLOT_LIVE",
    "LIVE_ENVELOPE_FIELDS",
    "AccountDiscoveryPort",
    "AccountModeDisagreement",
    "AccountPin",
    "AccountPinMismatch",
    "AccountVerification",
    "AccountVerificationFailed",
    "AlpacaCredentialEnvironment",
    "AlpacaRuntimeContext",
    "BrokerProfileError",
    "CredentialSlotAvailability",
    "CredentialSlotUnavailable",
    "CredentialSlotUnknown",
    "ObservedAccount",
    "ResolvedCredentials",
    "RevisionIncomplete",
    "credential_slot_available",
    "describe_credential_slots",
    "is_known_credential_slot",
    "pin_observed_account",
    "require_known_credential_slot",
    "resolve_credentials",
    "resolve_runtime_context",
    "reverify_pinned_account",
    "verify_account",
]
