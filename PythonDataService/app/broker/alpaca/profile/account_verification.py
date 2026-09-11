"""Read-only account discovery and the pin decisions around it (contract §3).

Verification observes which broker account a configuration's resolved
credentials actually reach, under that configuration's endpoint mode. It is
**read-only, period**: no order submission, no cancellation, no mutation of any
kind, under any code path here. That is not a convention to be reviewed for —
:class:`AccountDiscoveryPort` declares exactly one method, so the seam this
module talks to cannot express a submit or a cancel at all.

The pin rules, which exist to keep two different operations from being confused
for one another:

- **A pin is only ever set by explicit selection of an observed account.**
  :func:`pin_observed_account` refuses an account the verification did not
  observe, so nobody types an account ID and no discovery result auto-pins.
- **Rotating a secret for the same account is a reconnection**, not a re-pin.
  :func:`reverify_pinned_account` confirms the pin and returns the observation;
  it has no way to return a *different* pin, because on its success path the
  observed ID equals the pin, and on any other path it raises.
- **Pointing at a different account needs a new revision.** Selecting an
  account other than an existing pin is refused as ``account_pin_mismatch``,
  and the previous pin is returned to the caller untouched — a failed
  re-verification never blanks or changes one.
- **A missing credential or a mode disagreement refuses the same way**, and
  likewise leaves the pin as it was: those refusals are raised before any pin
  value is produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.profile.errors import (
    AccountModeDisagreement,
    AccountPinMismatch,
    AccountVerificationFailed,
)
from app.broker.alpaca.profile.runtime_context import AlpacaRuntimeContext, EndpointMode
from app.broker.contract.errors import BrokerAccountModeDisagreement, BrokerError
from app.broker.contract.models import BrokerAccountSnapshot
from app.utils.timestamps import now_ms_utc

# How old an observation may be and still justify a pin. Long enough for an
# operator to read the observed accounts and click one; short enough that the
# broker's answer is still the one they are looking at. This bounds a UI
# ceremony, not risk — no envelope value is derived from it.
ACCOUNT_VERIFICATION_MAX_AGE_MS: Final[int] = 5 * 60 * 1000


class AccountDiscoveryPort(Protocol):
    """The whole broker surface verification is allowed to reach.

    One read method. Every object this module talks to is one of these,
    including the default — :class:`_BrokerAccountDiscovery` below narrows
    ``AlpacaBroker`` down to it rather than handing the full trade port to a
    read-only ceremony. There is no reference to a mutating method anywhere in
    this module for a future edit to reach for by accident.
    """

    async def get_account(self) -> BrokerAccountSnapshot: ...


class _BrokerAccountDiscovery:
    """Narrows a full ``AlpacaBroker`` to the one read verification may make.

    ``AlpacaBroker`` implements the trade port too. Binding the default through
    this adapter means the object verification holds genuinely exposes nothing
    but ``get_account``, instead of relying on an annotation the repo has no
    type checker to enforce. The wrapped broker is private so the adapter does
    not hand back the surface it exists to withhold.
    """

    def __init__(self, broker: AlpacaBroker) -> None:
        self._broker = broker

    async def get_account(self) -> BrokerAccountSnapshot:
        return await self._broker.get_account()


@dataclass(frozen=True)
class ObservedAccount:
    """One broker account this configuration's credentials actually reached."""

    account_id: str
    account_mode: EndpointMode
    account_status: str
    trading_blocked: bool
    account_blocked: bool
    observed_at_ms: int


@dataclass(frozen=True)
class AccountVerification:
    """The result the operator picks from — observed candidates, never typed.

    A verification always carries at least one candidate. Discovery that
    resolved no account is ``account_verification_failed``, not an empty
    success, and holding that as an invariant here means every consumer can
    read ``candidates[0]`` without a "no account observed" branch that would
    otherwise have to invent a placeholder ID.
    """

    credential_slot: str
    endpoint_mode: EndpointMode
    candidates: tuple[ObservedAccount, ...]
    verified_at_ms: int

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError("an account verification carries at least one candidate")


@dataclass(frozen=True)
class AccountPin:
    """An explicitly selected observed account, bound to the mode it was seen under."""

    account_id: str
    endpoint_mode: EndpointMode
    pinned_at_ms: int


def _observed_from_snapshot(snapshot: BrokerAccountSnapshot) -> ObservedAccount:
    return ObservedAccount(
        account_id=snapshot.account_id,
        account_mode=snapshot.account_mode,
        account_status=snapshot.account_status,
        trading_blocked=snapshot.trading_blocked,
        account_blocked=snapshot.account_blocked,
        observed_at_ms=snapshot.observed_at_ms,
    )


async def verify_account(
    context: AlpacaRuntimeContext,
    *,
    discovery: AccountDiscoveryPort | None = None,
) -> AccountVerification:
    """Observe the account this context's credentials and mode reach.

    Alpaca issues one account per credential pair, so a successful discovery
    yields exactly one candidate. The result is still a tuple: the contract's
    verify route returns *candidates* the operator selects from, and a vendor
    that ever exposes more than one must not need a second shape.

    Raises :class:`AccountModeDisagreement` (409) when the observed account
    contradicts the context's endpoint mode — the adapter's existing
    ADR 0059 D1 refusal, re-stated in profile vocabulary — and
    :class:`AccountVerificationFailed` (409) for any other broker failure.
    """
    port: AccountDiscoveryPort = (
        discovery
        if discovery is not None
        else _BrokerAccountDiscovery(AlpacaBroker(settings=context.settings))
    )
    try:
        snapshot = await port.get_account()
    except BrokerAccountModeDisagreement as exc:
        raise AccountModeDisagreement(
            endpoint_mode=context.settings.mode,
            credential_slot=context.credential_slot,
        ) from exc
    except BrokerError as exc:
        raise AccountVerificationFailed.from_broker_error(exc) from exc

    observed = _observed_from_snapshot(snapshot)
    return AccountVerification(
        credential_slot=context.credential_slot,
        endpoint_mode=context.settings.mode,
        candidates=(observed,),
        verified_at_ms=observed.observed_at_ms,
    )


def _require_fresh(verification: AccountVerification, *, now_ms: int) -> None:
    """Refuse an observation dated too long ago — or after the reading clock.

    A backward clock step would otherwise make a negative age look fresh
    indefinitely, the same reason ``LiveEnvelopeGate.fresh_observation`` bounds
    its age below as well as above. The two are separate refusals because they
    are separate operator problems: one is a stale ceremony, the other is a
    disagreeing clock.
    """
    age_ms = now_ms - verification.verified_at_ms
    if age_ms < 0:
        raise AccountVerificationFailed.dated_after_the_clock()
    if age_ms > ACCOUNT_VERIFICATION_MAX_AGE_MS:
        raise AccountVerificationFailed.stale(
            age_ms=age_ms, max_age_ms=ACCOUNT_VERIFICATION_MAX_AGE_MS
        )


def pin_observed_account(
    verification: AccountVerification,
    *,
    selected_account_id: str,
    existing_pin: str | None = None,
    now_ms: int | None = None,
) -> AccountPin:
    """Pin one explicitly selected observed account.

    ``existing_pin`` is the revision's current pin, if it has one. Selecting a
    *different* account is an account change, which the contract routes through
    a new revision and fresh account approval — so it is refused here as
    ``account_pin_mismatch`` and the caller's pin stays as it was. Re-selecting
    the account already pinned is idempotent and re-stamps the pin.

    Refuses a stale verification: a pin is recorded against an observation the
    operator is actually looking at, not one from an hour ago.
    """
    pinned_at_ms = now_ms if now_ms is not None else now_ms_utc()
    _require_fresh(verification, now_ms=pinned_at_ms)

    observed_ids = {candidate.account_id for candidate in verification.candidates}
    if selected_account_id not in observed_ids:
        raise AccountVerificationFailed.not_observed(selected_account_id=selected_account_id)

    if existing_pin is not None and existing_pin != selected_account_id:
        raise AccountPinMismatch.on_selection(
            pinned_account_id=existing_pin,
            selected_account_id=selected_account_id,
        )

    return AccountPin(
        account_id=selected_account_id,
        endpoint_mode=verification.endpoint_mode,
        # When the pin was recorded, not when the account was seen. Two pins
        # minted from one observation are two events (contract §2.3's
        # ``account_pinned_at_ms``).
        pinned_at_ms=pinned_at_ms,
    )


def reverify_pinned_account(
    verification: AccountVerification,
    *,
    pinned_account_id: str,
    now_ms: int | None = None,
) -> ObservedAccount:
    """Confirm a pin against a fresh observation, at apply and at startup.

    Freshness is required here too, and for a stronger reason than at pinning
    time: the contract says the pin is *re-observed* at apply and at startup,
    and a caller replaying a cached verification would otherwise satisfy that
    with a fact of any age. This is the gate that authorises binding a real
    account, so it holds the same bound the ceremony does.

    Returns the observation that matched, and can return nothing else — there
    is no path through this function that produces a pin, so a re-verification
    can never silently move one. A contradiction raises
    :class:`AccountPinMismatch` and leaves the caller's pin untouched.
    """
    _require_fresh(verification, now_ms=now_ms if now_ms is not None else now_ms_utc())
    for candidate in verification.candidates:
        if candidate.account_id == pinned_account_id:
            return candidate
    raise AccountPinMismatch.on_reobservation(
        pinned_account_id=pinned_account_id,
        observed_account_ids=tuple(
            candidate.account_id for candidate in verification.candidates
        ),
    )


__all__ = [
    "ACCOUNT_VERIFICATION_MAX_AGE_MS",
    "AccountDiscoveryPort",
    "AccountPin",
    "AccountVerification",
    "ObservedAccount",
    "pin_observed_account",
    "reverify_pinned_account",
    "verify_account",
]
