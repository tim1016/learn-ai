"""Typed profile-resolution errors carrying the contract's ``reason`` codes.

Shapes follow ``docs/architecture/broker-configuration-profile-contract.md``
§6: a code-like snake_case ``reason``, operator-readable ``message`` prose and
a ``next_step``, which a router hands straight to ``HTTPException(detail=...)``
via :meth:`BrokerProfileError.as_detail`. ``reason`` is the stable code the
Frontend renders through the shared ``receiptLabel`` pipe; ``message`` and
``next_step`` are backend-authored prose the UI must not compose itself.

**Secret hygiene.** No message, ``next_step``, or attribute in this module
carries a credential value, a fragment, a length, or an environment-variable
name. The only identifiers that appear are slot labels (allowlisted constants),
endpoint modes, and broker account IDs — all non-secret audit facts the
contract already preserves verbatim in evidence.

**Echo policy**, stated once so the two cases do not read as inconsistent. Text
that arrived on the *request* is never reflected into prose the UI renders: a
rejected ``credential_slot`` is refused without naming what was sent, since the
caller already knows and reflecting untrusted text into rendered copy buys
nothing. Field names read out of the *profiles database* — an envelope key that
is not an envelope field — are named, because they were written by the
authenticated owner through Package B's validated write path and naming them is
the only way an operator can tell which stored row needs repairing. Neither case
ever echoes a *value*.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from app.broker.contract.errors import BrokerError


class BrokerProfileError(Exception):
    """Base class for every profile-resolution refusal.

    ``reason`` and ``http_status`` are the contract §6 row for the subclass;
    ``message`` is the *what* and ``next_step`` the operator's way forward.

    Both are declared without a default on purpose. A base-class fallback
    would emit a ``reason`` outside the taxonomy — a code the Frontend's
    ``receiptLabel`` pipe has no rendering for — so a subclass that forgets to
    declare its row fails loudly instead.
    """

    reason: ClassVar[str]
    http_status: ClassVar[int]

    def __init__(self, message: str, *, next_step: str) -> None:
        super().__init__(message)
        self.message = message
        self.next_step = next_step

    def as_detail(self) -> dict[str, str]:
        """The contract §6 ``detail`` dict for an ``HTTPException``."""
        return {
            "reason": self.reason,
            "message": self.message,
            "next_step": self.next_step,
        }


class CredentialSlotUnknown(BrokerProfileError):
    """A slot name outside the closed allowlist. Never used to build a lookup."""

    reason: ClassVar[str] = "credential_slot_unknown"
    http_status: ClassVar[int] = 422

    def __init__(self, *, known_slots: Sequence[str]) -> None:
        super().__init__(
            "That credential slot is not one this deployment recognises.",
            next_step="Choose one of the recognised slots: "
            + ", ".join(known_slots)
            + ".",
        )
        self.known_slots = tuple(known_slots)


class CredentialSlotUnavailable(BrokerProfileError):
    """An allowlisted slot whose injected credential pair is absent."""

    reason: ClassVar[str] = "credential_slot_unavailable"
    http_status: ClassVar[int] = 409

    def __init__(self, slot: str) -> None:
        super().__init__(
            f"No credential pair is injected for the {slot!r} slot.",
            next_step=(
                "Inject that slot's credential pair into the service environment, "
                "restart the service, then verify the account again."
            ),
        )
        self.slot = slot


class RevisionIncomplete(BrokerProfileError):
    """A revision cannot be resolved into a runtime binding as it stands.

    ``detail`` names the offending fields — never their values. When it comes
    from settings validation it arrives through
    ``config.alpaca_configuration_error_detail``, which keeps Pydantic's
    ``msg`` text and drops the echoed input.
    """

    reason: ClassVar[str] = "revision_incomplete"
    http_status: ClassVar[int] = 422

    def __init__(self, detail: str) -> None:
        # Pydantic prefixes a ``model_validator``'s message with "Value error, "
        # and its own messages end without a stop; normalise both so the prose
        # a surface renders is one sentence rather than a stitched fragment.
        normalised = detail.removeprefix("Value error, ").strip().rstrip(".")
        super().__init__(
            f"This configuration revision cannot be applied yet: {normalised}.",
            next_step="Complete the revision's values and save it again.",
        )
        self.detail = normalised


class AccountVerificationFailed(BrokerProfileError):
    """Read-only broker discovery did not produce a usable observed account."""

    reason: ClassVar[str] = "account_verification_failed"
    http_status: ClassVar[int] = 409

    def __init__(self, message: str, *, next_step: str, broker_detail: str | None = None) -> None:
        super().__init__(message, next_step=next_step)
        self.broker_detail = broker_detail

    @classmethod
    def from_broker_error(cls, exc: BrokerError) -> AccountVerificationFailed:
        """Wrap a broker-contract failure.

        ``BrokerError.message`` and ``.detail`` are both vendor- or
        repo-authored text that ``app/broker/alpaca/errors.py`` documents as
        carrying no secret ("Alpaca's error message is surfaced … never our
        keys"). Our credentials never appear in either.
        """
        return cls(
            f"The broker could not confirm this configuration's account: {exc.message}",
            next_step=(
                "Check the injected credential pair for this slot and the broker's "
                "availability, then verify the account again."
            ),
            broker_detail=exc.detail,
        )

    @classmethod
    def not_observed(cls, *, selected_account_id: str) -> AccountVerificationFailed:
        """The operator selected an account this verification did not observe."""
        return cls(
            f"Account {selected_account_id} was not among the accounts observed for "
            "this configuration.",
            next_step="Verify the account again and pin one of the observed accounts.",
        )

    @classmethod
    def stale(cls, *, age_ms: int, max_age_ms: int) -> AccountVerificationFailed:
        """The observation is too old to act on."""
        return cls(
            f"This account observation is {age_ms // 1000}s old; it is acted on "
            f"only while it is no older than {max_age_ms // 1000}s.",
            next_step="Verify the account again, then pin the account you selected.",
        )

    @classmethod
    def dated_after_the_clock(cls) -> AccountVerificationFailed:
        """The observation is stamped later than the clock reading it.

        A separate refusal from :meth:`stale` because it is a separate operator
        problem: nothing is old, the clocks disagree.
        """
        return cls(
            "This account observation is stamped later than the clock reading it.",
            next_step=(
                "Check the service clock, then verify the account again before "
                "pinning."
            ),
        )


class AccountModeDisagreement(BrokerProfileError):
    """The observed account contradicts the revision's ``endpoint_mode``.

    Reuses the existing live vocabulary: the mode that selected the endpoint is
    configuration truth, and the account shape is a refusal input only
    (ADR 0059 D1, ``adapter.from_alpaca_account``).
    """

    reason: ClassVar[str] = "account_mode_disagreement"
    http_status: ClassVar[int] = 409

    def __init__(self, *, endpoint_mode: str, credential_slot: str) -> None:
        super().__init__(
            f"The account reached with the {credential_slot!r} slot is not a "
            f"{endpoint_mode} account.",
            next_step=(
                "Point this configuration at the credential slot whose account matches "
                f"its {endpoint_mode} endpoint mode, or change the endpoint mode in a "
                "new revision."
            ),
        )
        self.endpoint_mode = endpoint_mode
        self.credential_slot = credential_slot


class AccountPinMismatch(BrokerProfileError):
    """The pin is contradicted. The previous pin is never replaced.

    Two situations reach this, and each authors its own prose: a re-observation
    that found a different account, and an operator selecting an account other
    than the one already pinned. Both leave the caller's pin as it was —
    neither constructor produces a pin value.
    """

    reason: ClassVar[str] = "account_pin_mismatch"
    http_status: ClassVar[int] = 409

    _NEXT_STEP: ClassVar[str] = (
        "Rotating a secret for the same account is a reconnection and needs no new "
        "pin. Pointing a configuration at a different account needs a new revision "
        "and fresh account approval."
    )

    def __init__(
        self,
        message: str,
        *,
        pinned_account_id: str,
        contradicting_account_ids: tuple[str, ...],
    ) -> None:
        super().__init__(message, next_step=self._NEXT_STEP)
        self.pinned_account_id = pinned_account_id
        self.contradicting_account_ids = contradicting_account_ids

    @classmethod
    def on_reobservation(
        cls, *, pinned_account_id: str, observed_account_ids: tuple[str, ...]
    ) -> AccountPinMismatch:
        """Re-verification observed accounts, none of them the pinned one.

        Every observed ID is named, not just the first: contract §2.5 keeps
        exact account IDs in audit evidence, and reporting one of several
        arbitrarily would make that evidence depend on iteration order.
        """
        return cls(
            f"This configuration is pinned to account {pinned_account_id}, but the "
            f"account now observed is {', '.join(observed_account_ids)}. The pin is "
            "unchanged.",
            pinned_account_id=pinned_account_id,
            contradicting_account_ids=observed_account_ids,
        )

    @classmethod
    def on_selection(
        cls, *, pinned_account_id: str, selected_account_id: str
    ) -> AccountPinMismatch:
        """The operator selected an account other than the one already pinned."""
        return cls(
            f"This configuration is pinned to account {pinned_account_id}; "
            f"{selected_account_id} cannot replace it on this revision. The pin is "
            "unchanged.",
            pinned_account_id=pinned_account_id,
            contradicting_account_ids=(selected_account_id,),
        )


__all__ = [
    "AccountModeDisagreement",
    "AccountPinMismatch",
    "AccountVerificationFailed",
    "BrokerProfileError",
    "CredentialSlotUnavailable",
    "CredentialSlotUnknown",
    "RevisionIncomplete",
]
