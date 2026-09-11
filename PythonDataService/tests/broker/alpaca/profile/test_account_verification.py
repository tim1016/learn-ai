"""Read-only account verification and the pin rules around it (contract §3).

Two claims carry the most weight and are asserted rather than reviewed for:
verification cannot mutate anything, and a failed re-verification never
replaces the pin it contradicts.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.profile.account_verification import (
    ACCOUNT_VERIFICATION_MAX_AGE_MS,
    AccountPin,
    AccountVerification,
    ObservedAccount,
    _BrokerAccountDiscovery,
    pin_observed_account,
    reverify_pinned_account,
    verify_account,
)
from app.broker.alpaca.profile.credentials import AlpacaCredentialEnvironment
from app.broker.alpaca.profile.errors import (
    AccountModeDisagreement,
    AccountPinMismatch,
    AccountVerificationFailed,
)
from app.broker.alpaca.profile.runtime_context import (
    AlpacaRuntimeContext,
    resolve_runtime_context,
)
from app.broker.contract.errors import (
    BrokerAccountModeDisagreement,
    BrokerUnavailable,
)
from app.broker.contract.models import BrokerAccountSnapshot
from tests.broker.alpaca.profile.conftest import (
    COMPLETE_ENVELOPE,
    DEFAULT_SLOT_KEY,
    DEFAULT_SLOT_SECRET,
    LIVE_SLOT_KEY,
    ROTATED_LIVE_SLOT_SECRET,
    make_environment,
)

_OBSERVED_AT_MS = 1_757_500_000_000
_PAPER_ACCOUNT_ID = "PA3TESTACCOUNT"
_OTHER_PAPER_ACCOUNT_ID = "PA9OTHERACCOUNT"


def _snapshot(
    *,
    account_id: str = _PAPER_ACCOUNT_ID,
    account_mode: str = "paper",
    observed_at_ms: int = _OBSERVED_AT_MS,
) -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id=account_id,
        account_mode=account_mode,
        account_status="ACTIVE",
        currency="USD",
        cash=1000.0,
        equity=1000.0,
        buying_power=1000.0,
        portfolio_value=1000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=None,
        observed_at_ms=observed_at_ms,
    )


class _RecordingDiscovery:
    """A discovery port that records every method anyone reached for."""

    def __init__(self, result: BrokerAccountSnapshot | Exception) -> None:
        self._result = result
        self.calls: list[str] = []

    async def get_account(self) -> BrokerAccountSnapshot:
        self.calls.append("get_account")
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def __getattr__(self, name: str) -> object:  # pragma: no cover - must not run
        raise AssertionError(f"verification reached for {name!r} on the broker")


def _paper_context(
    environment: AlpacaCredentialEnvironment | None = None,
) -> AlpacaRuntimeContext:
    return resolve_runtime_context(
        endpoint_mode="paper",
        credential_slot="default",
        environment=environment
        or make_environment(
            api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET
        ),
    )


def _verification(
    *,
    account_id: str = _PAPER_ACCOUNT_ID,
    verified_at_ms: int = _OBSERVED_AT_MS,
) -> AccountVerification:
    return AccountVerification(
        credential_slot="default",
        endpoint_mode="paper",
        candidates=(
            ObservedAccount(
                account_id=account_id,
                account_mode="paper",
                account_status="ACTIVE",
                trading_blocked=False,
                account_blocked=False,
                observed_at_ms=verified_at_ms,
            ),
        ),
        verified_at_ms=verified_at_ms,
    )


# ── Discovery is read-only ──────────────────────────────────────────────────


class _ReadOnlyDouble:
    """Exposes one read method and nothing else — no submit, no cancel."""

    def __init__(self, snapshot: BrokerAccountSnapshot) -> None:
        self._snapshot = snapshot

    async def get_account(self) -> BrokerAccountSnapshot:
        return self._snapshot


async def test_verification_needs_nothing_but_a_read_method() -> None:
    double = _ReadOnlyDouble(_snapshot())

    assert not hasattr(double, "submit")
    assert not hasattr(double, "cancel")

    verification = await verify_account(_paper_context(), discovery=double)

    assert verification.candidates[0].account_id == _PAPER_ACCOUNT_ID


def test_the_default_discovery_port_exposes_no_mutating_method() -> None:
    """The default is a narrowing adapter, not the full trade port.

    ``AlpacaBroker`` implements ``submit`` and ``cancel``; handing one straight
    to a read-only ceremony would make "verification performs no mutation" a
    claim about discipline. The adapter makes it a claim about the object.
    """
    port = _BrokerAccountDiscovery(AlpacaBroker())

    assert not hasattr(port, "submit")
    assert not hasattr(port, "cancel")
    assert callable(port.get_account)


async def test_verify_account_observes_one_candidate_and_calls_nothing_else() -> None:
    discovery = _RecordingDiscovery(_snapshot())

    verification = await verify_account(_paper_context(), discovery=discovery)

    assert discovery.calls == ["get_account"]
    assert verification.credential_slot == "default"
    assert verification.endpoint_mode == "paper"
    assert verification.verified_at_ms == _OBSERVED_AT_MS
    assert [candidate.account_id for candidate in verification.candidates] == [
        _PAPER_ACCOUNT_ID
    ]


def test_a_verification_with_no_observed_account_cannot_be_constructed() -> None:
    # Discovery that resolved no account is a failure, not an empty success —
    # so no consumer needs a branch that invents a placeholder account ID.
    with pytest.raises(ValueError, match="at least one candidate"):
        AccountVerification(
            credential_slot="default",
            endpoint_mode="paper",
            candidates=(),
            verified_at_ms=_OBSERVED_AT_MS,
        )


async def test_verify_account_translates_a_mode_disagreement() -> None:
    discovery = _RecordingDiscovery(
        BrokerAccountModeDisagreement("mode disagreement", broker="alpaca")
    )

    with pytest.raises(AccountModeDisagreement) as info:
        await verify_account(_paper_context(), discovery=discovery)

    assert info.value.reason == "account_mode_disagreement"
    assert info.value.http_status == 409
    assert info.value.endpoint_mode == "paper"


async def test_verify_account_translates_any_other_broker_failure() -> None:
    discovery = _RecordingDiscovery(
        BrokerUnavailable("Could not reach Alpaca while fetching account.", broker="alpaca")
    )

    with pytest.raises(AccountVerificationFailed) as info:
        await verify_account(_paper_context(), discovery=discovery)

    assert info.value.reason == "account_verification_failed"
    assert info.value.http_status == 409


async def test_a_live_revision_verifies_against_its_own_slot_credentials() -> None:
    context = resolve_runtime_context(
        endpoint_mode="live",
        credential_slot="live",
        live_envelope=COMPLETE_ENVELOPE,
        environment=make_environment(
            credential_live_key_id=LIVE_SLOT_KEY,
            credential_live_secret_key=ROTATED_LIVE_SLOT_SECRET,
        ),
    )
    discovery = _RecordingDiscovery(
        _snapshot(account_id="123456789", account_mode="live")
    )

    verification = await verify_account(context, discovery=discovery)

    assert verification.credential_slot == "live"
    assert verification.endpoint_mode == "live"
    assert context.settings.api_key_id.get_secret_value() == LIVE_SLOT_KEY


# ── Pinning ─────────────────────────────────────────────────────────────────


def test_pinning_an_observed_account_records_it_against_the_observed_mode() -> None:
    pin = pin_observed_account(
        _verification(),
        selected_account_id=_PAPER_ACCOUNT_ID,
        now_ms=_OBSERVED_AT_MS + 1_000,
    )

    assert pin == AccountPin(
        account_id=_PAPER_ACCOUNT_ID,
        endpoint_mode="paper",
        # When the pin was recorded, not when the account was seen.
        pinned_at_ms=_OBSERVED_AT_MS + 1_000,
    )


def test_an_account_that_was_not_observed_cannot_be_pinned() -> None:
    with pytest.raises(AccountVerificationFailed) as info:
        pin_observed_account(
            _verification(),
            selected_account_id=_OTHER_PAPER_ACCOUNT_ID,
            now_ms=_OBSERVED_AT_MS + 1_000,
        )

    assert info.value.reason == "account_verification_failed"
    assert _OTHER_PAPER_ACCOUNT_ID in info.value.message


def test_selecting_a_different_account_than_the_pin_refuses_and_keeps_the_pin() -> None:
    verification = _verification(account_id=_OTHER_PAPER_ACCOUNT_ID)

    with pytest.raises(AccountPinMismatch) as info:
        pin_observed_account(
            verification,
            selected_account_id=_OTHER_PAPER_ACCOUNT_ID,
            existing_pin=_PAPER_ACCOUNT_ID,
            now_ms=_OBSERVED_AT_MS + 1_000,
        )

    assert info.value.pinned_account_id == _PAPER_ACCOUNT_ID
    assert info.value.contradicting_account_ids == (_OTHER_PAPER_ACCOUNT_ID,)
    assert "unchanged" in info.value.message


def test_reselecting_the_pinned_account_is_idempotent() -> None:
    pin = pin_observed_account(
        _verification(),
        selected_account_id=_PAPER_ACCOUNT_ID,
        existing_pin=_PAPER_ACCOUNT_ID,
        now_ms=_OBSERVED_AT_MS + 1_000,
    )

    assert pin.account_id == _PAPER_ACCOUNT_ID


@pytest.mark.parametrize(
    "now_ms",
    [
        _OBSERVED_AT_MS + ACCOUNT_VERIFICATION_MAX_AGE_MS + 1,
        _OBSERVED_AT_MS - 1,
    ],
    ids=["too-old", "dated-after-the-clock"],
)
def test_a_stale_verification_cannot_justify_a_pin(now_ms: int) -> None:
    with pytest.raises(AccountVerificationFailed, match="observation"):
        pin_observed_account(
            _verification(),
            selected_account_id=_PAPER_ACCOUNT_ID,
            now_ms=now_ms,
        )


# ── Re-verification: rotation versus an account change ──────────────────────


def test_reverification_confirms_a_pin_and_returns_only_an_observation() -> None:
    observed = reverify_pinned_account(
        _verification(), pinned_account_id=_PAPER_ACCOUNT_ID, now_ms=_OBSERVED_AT_MS + 1_000
    )

    assert isinstance(observed, ObservedAccount)
    assert not isinstance(observed, AccountPin)
    assert observed.account_id == _PAPER_ACCOUNT_ID


async def test_rotating_a_secret_for_the_same_account_leaves_the_pin_alone() -> None:
    rotated = resolve_runtime_context(
        endpoint_mode="live",
        credential_slot="live",
        live_envelope=COMPLETE_ENVELOPE,
        account_pin="123456789",
        environment=make_environment(
            credential_live_key_id=LIVE_SLOT_KEY,
            credential_live_secret_key=ROTATED_LIVE_SLOT_SECRET,
        ),
    )
    discovery = _RecordingDiscovery(
        _snapshot(account_id="123456789", account_mode="live")
    )

    verification = await verify_account(rotated, discovery=discovery)
    observed = reverify_pinned_account(
        verification, pinned_account_id="123456789", now_ms=_OBSERVED_AT_MS + 1_000
    )

    assert rotated.account_pin == "123456789"
    assert observed.account_id == rotated.account_pin


def test_reverification_against_a_different_account_refuses_without_replacing_the_pin() -> None:
    verification = _verification(account_id=_OTHER_PAPER_ACCOUNT_ID)

    with pytest.raises(AccountPinMismatch) as info:
        reverify_pinned_account(
            verification, pinned_account_id=_PAPER_ACCOUNT_ID, now_ms=_OBSERVED_AT_MS + 1_000
        )

    assert info.value.reason == "account_pin_mismatch"
    assert info.value.pinned_account_id == _PAPER_ACCOUNT_ID
    assert info.value.contradicting_account_ids == (_OTHER_PAPER_ACCOUNT_ID,)
    assert "new revision" in info.value.next_step


@pytest.mark.parametrize(
    "now_ms",
    [
        _OBSERVED_AT_MS + ACCOUNT_VERIFICATION_MAX_AGE_MS + 1,
        _OBSERVED_AT_MS - 1,
    ],
    ids=["too-old", "dated-after-the-clock"],
)
def test_reverification_refuses_a_stale_observation(now_ms: int) -> None:
    # This is the gate that authorises binding a real account at apply and at
    # startup, so replaying a cached verification must not satisfy it. It holds
    # the same bound the pinning ceremony does.
    with pytest.raises(AccountVerificationFailed):
        reverify_pinned_account(
            _verification(), pinned_account_id=_PAPER_ACCOUNT_ID, now_ms=now_ms
        )
