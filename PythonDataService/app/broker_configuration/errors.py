"""The refusal vocabulary of the broker-configuration surface.

Every refusal carries a code-like ``reason`` plus backend-authored prose, the
shape ``docs/architecture/broker-configuration-profile-contract.md`` §6 pins:
the Frontend renders ``reason`` through the shared ``receiptLabel`` pipe and
never composes ``message`` or ``next_step`` itself.

The router translates one of these into an ``HTTPException`` whose ``detail``
is :meth:`BrokerConfigurationError.detail`; nothing else in the module builds
an HTTP response.
"""

from __future__ import annotations

from typing import ClassVar


class BrokerConfigurationError(Exception):
    """A refusal the operator surface can render without inventing copy."""

    reason: ClassVar[str] = "broker_configuration_error"
    status_code: ClassVar[int] = 409

    def __init__(self, message: str, *, next_step: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.next_step = next_step

    def detail(self) -> dict[str, str]:
        body = {"reason": self.reason, "message": self.message}
        if self.next_step is not None:
            body["next_step"] = self.next_step
        return body


class ProfileNotFound(BrokerConfigurationError):
    reason: ClassVar[str] = "profile_not_found"
    status_code: ClassVar[int] = 404


class RevisionNotFound(BrokerConfigurationError):
    reason: ClassVar[str] = "revision_not_found"
    status_code: ClassVar[int] = 404


class RevisionConflict(BrokerConfigurationError):
    reason: ClassVar[str] = "revision_conflict"
    status_code: ClassVar[int] = 409


class SelectionGenerationConflict(BrokerConfigurationError):
    reason: ClassVar[str] = "selection_generation_conflict"
    status_code: ClassVar[int] = 409


class RevisionIncomplete(BrokerConfigurationError):
    reason: ClassVar[str] = "revision_incomplete"
    status_code: ClassVar[int] = 422


class ProfileArchived(BrokerConfigurationError):
    reason: ClassVar[str] = "profile_archived"
    status_code: ClassVar[int] = 409


class ProfileInUse(BrokerConfigurationError):
    reason: ClassVar[str] = "profile_in_use"
    status_code: ClassVar[int] = 409


class DisplayNameConflict(BrokerConfigurationError):
    """Two live profiles cannot share one display name (contract §2.2).

    Not in the §6 table, which enumerates the refusals the *flows* produce and
    omits the one the uniqueness rule in §2.2 makes unavoidable. Recorded here
    rather than folded into ``profile_in_use``, which means something else.
    """

    reason: ClassVar[str] = "display_name_conflict"
    status_code: ClassVar[int] = 409


class CredentialSlotUnknown(BrokerConfigurationError):
    """Declared here, raised by the slot resolver — see ``seams.py``.

    The allowlist's shape is ADR 0060 open question 2 and package C's fork, so
    this package stores a slot as an opaque string and performs no environment
    lookup. The refusal is shared vocabulary, not dead code: it is stated once
    here so C raises the contract's reason rather than inventing one.
    """

    reason: ClassVar[str] = "credential_slot_unknown"
    status_code: ClassVar[int] = 422


class CredentialSlotUnavailable(BrokerConfigurationError):
    """Also raised by the slot resolver, for the same reason as above."""

    reason: ClassVar[str] = "credential_slot_unavailable"
    status_code: ClassVar[int] = 409


class AccountVerificationFailed(BrokerConfigurationError):
    reason: ClassVar[str] = "account_verification_failed"
    status_code: ClassVar[int] = 409


class AccountModeDisagreement(BrokerConfigurationError):
    reason: ClassVar[str] = "account_mode_disagreement"
    status_code: ClassVar[int] = 409


class AccountPinMismatch(BrokerConfigurationError):
    reason: ClassVar[str] = "account_pin_mismatch"
    status_code: ClassVar[int] = 409


class ProfilesDatabaseUnavailable(BrokerConfigurationError):
    """The profiles database cannot be opened or read (ADR 0060 Decision 7).

    Fails closed with a reason instead of crashing the boot; no route grants
    broker authority off an unreadable database.
    """

    reason: ClassVar[str] = "profiles_database_unavailable"
    status_code: ClassVar[int] = 503


class InvalidLiveEnvelope(BrokerConfigurationError):
    """A live envelope value is outside the domain ``AlpacaSettings`` enforces.

    Not in the §6 table either. The contract states the envelope's domain in
    §2.4 without naming the refusal a violation produces, and this path has to
    refuse something an operator can act on rather than surface a ``TypeError``
    from the hash.
    """

    reason: ClassVar[str] = "live_envelope_invalid"
    status_code: ClassVar[int] = 422


__all__ = [
    "AccountModeDisagreement",
    "AccountPinMismatch",
    "AccountVerificationFailed",
    "BrokerConfigurationError",
    "CredentialSlotUnavailable",
    "CredentialSlotUnknown",
    "DisplayNameConflict",
    "InvalidLiveEnvelope",
    "ProfileArchived",
    "ProfileInUse",
    "ProfileNotFound",
    "ProfilesDatabaseUnavailable",
    "RevisionConflict",
    "RevisionIncomplete",
    "RevisionNotFound",
    "SelectionGenerationConflict",
]
