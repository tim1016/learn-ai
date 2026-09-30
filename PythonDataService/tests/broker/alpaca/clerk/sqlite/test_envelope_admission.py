"""Envelope admission at the ENTER seam (ADR 0059 D4, plan R1).

``accept_enter`` gains a second gate beside ``require_admission``: the
envelope, which judges an ENTER by its deployment's budget and the account's
cash after every other claim, and admits none on an account not yet switched
to budgets (#2553). These tests drive it through the public entry point — no
direct call to ``require_envelope_admission`` — so what they pin is the
observable behaviour of accepting an ENTER: which legs are admitted, which
refusal each unobservable fact produces, and that a refusal writes nothing.

Every stamp comes from the fixture clock; the gate's observation is stamped
with the same ``T0``, so freshness is a property of the test's arithmetic
rather than of when the suite happens to run.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_CASH_EXCEEDED,
    LIVE_ENVELOPE_UNOBSERVED,
    OBSERVATION_MAX_AGE_MS,
    LiveEnvelopeGate,
)
from app.broker.alpaca.clerk.sqlite.budget_authority import BUDGETS_NOT_SWITCHED_ON
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.envelope_reservations import entry_cash_claims
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    RefusalClass,
    classify_admission_refusal,
)
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.conftest import (
    ENVELOPE_RUN_ID,
    ENVELOPE_SID,
    _start_legacy_run,
    _TestClock,
)
from tests.broker.alpaca.clerk.sqlite.conftest import (
    ENVELOPE_T0 as T0,
)
from tests.broker.alpaca.clerk.sqlite.conftest import (
    envelope_gate as _gate,
)


def _order_claims(repo: ClerkSqliteRepository) -> Decimal:
    """What the account's money read claims for working ENTERs at ``T0``."""
    return repo.account_budget(cash=100_000, seen_before_ms=T0).order_claims


def _leg(**overrides: Any) -> BrokerOrderLeg:
    base: dict[str, Any] = {"symbol": "SPY", "side": "buy", "quantity": 1}
    base.update(overrides)
    return BrokerOrderLeg(**base)


def _accept(
    envelope_repo: ClerkSqliteRepository,
    sid: str,
    run_id: str,
    *,
    decision_id: str,
    leg: BrokerOrderLeg,
    envelope: LiveEnvelopeGate | None,
    reference_price: float | None = 100.0,
) -> EnterSubmission:
    return accept_enter(
        envelope_repo,
        account_id=envelope_repo.account_id,
        strategy_instance_id=sid,
        decision_id=decision_id,
        lifecycle_run_id=run_id,
        leg=leg,
        envelope=envelope,
        reference_price=reference_price,
    )


def _refusal(exc_info: pytest.ExceptionInfo[AdmissionBlockedError]) -> str | None:
    return exc_info.value.decision.reason_code


def test_an_affordable_market_enter_is_admitted_and_reserved(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    accepted = _accept(envelope_repo, sid, run_id, decision_id="d1", leg=_leg(quantity=100), envelope=_gate())
    assert accepted.created
    # The notional plus the recorded fee provision (100 shares of CAT, rounded up to the cent).
    assert _order_claims(envelope_repo) == Decimal("10000.01")


def test_a_market_enter_beyond_cash_is_refused_and_nothing_is_written(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    before = envelope_repo.control_meta_snapshot().control_revision
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, decision_id="d1", leg=_leg(quantity=1_001), envelope=_gate())
    assert _refusal(exc_info) == LIVE_ENVELOPE_CASH_EXCEEDED
    assert "100100.01 USD" in (exc_info.value.decision.why or "")
    assert envelope_repo.control_meta_snapshot().control_revision == before
    assert _order_claims(envelope_repo) == 0


def test_fractional_cent_shortage_is_refused_without_float_tolerance(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(
            envelope_repo, sid, run_id, decision_id="exact-cash",
            leg=_leg(quantity=1), envelope=_gate(cash=100), reference_price=100.0000000001,
        )
    assert _refusal(exc_info) == LIVE_ENVELOPE_CASH_EXCEEDED


def test_the_entry_requirement_includes_the_fee_provision(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    """Cash that covers only the notional cannot admit the ENTER.

    The one entry requirement is the notional plus its fee provision, which
    the reservation then records and claims (#2553).
    """
    sid, run_id = active_instance
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, decision_id="d1", leg=_leg(quantity=100), envelope=_gate(cash=10_000.0))
    assert _refusal(exc_info) == LIVE_ENVELOPE_CASH_EXCEEDED
    assert "10000.01 USD" in (exc_info.value.decision.why or "")
    assert _accept(
        envelope_repo, sid, run_id, decision_id="d2", leg=_leg(quantity=100), envelope=_gate(cash=10_000.01)
    ).created


def test_two_instances_cannot_spend_the_same_cash(
    envelope_repo: ClerkSqliteRepository, two_active_instances: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    """Each bot owns a $1,000.01 budget; the account's cash after the other's claims still bounds it."""
    (a, run_a), (b, run_b) = two_active_instances
    _accept(envelope_repo, a, run_a, decision_id="d1", leg=_leg(quantity=6), envelope=_gate(cash=2_000.02))
    # Cash falls to $1,500: A's 600.01 order and its 400.00 still free are
    # claimed, so B's own budget covers 6 shares but the cash left does not.
    after_fall = _gate(cash=1_500.0)
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, b, run_b, decision_id="d2", leg=_leg(quantity=6), envelope=after_fall)
    assert _refusal(exc_info) == LIVE_ENVELOPE_CASH_EXCEEDED
    assert "account cash after other claims is 499.99 USD" in (exc_info.value.decision.why or "")
    # 4 shares need 400.01 of the 499.99 left.
    _accept(envelope_repo, b, run_b, decision_id="d3", leg=_leg(quantity=4), envelope=after_fall)


def test_a_limit_leg_is_priced_at_its_limit_not_the_reference(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    leg = _leg(
        quantity=100, order_type=OrderType.LIMIT, limit_price=1_001.0, extended_hours=True
    )
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(
            envelope_repo, sid, run_id, decision_id="d1", leg=leg, envelope=_gate(), reference_price=1.0
        )
    assert _refusal(exc_info) == LIVE_ENVELOPE_CASH_EXCEEDED


@pytest.mark.parametrize(
    ("gate", "reference_price"),
    [
        pytest.param(
            LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=True),
            100.0,
            id="never-observed",
        ),
        pytest.param(
            _gate(observed_at_ms=T0 - OBSERVATION_MAX_AGE_MS - 1), 100.0, id="stale-observation"
        ),
        pytest.param(_gate(), None, id="market-leg-without-a-decision-bar"),
    ],
)
def test_unobservable_facts_refuse_closed(
    envelope_repo: ClerkSqliteRepository,
    active_instance: tuple[str, str],
    gate: LiveEnvelopeGate,
    reference_price: float | None,
) -> None:
    sid, run_id = active_instance
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(
            envelope_repo,
            sid,
            run_id,
            decision_id="d1",
            leg=_leg(quantity=1),
            envelope=gate,
            reference_price=reference_price,
        )
    assert _refusal(exc_info) == LIVE_ENVELOPE_UNOBSERVED


def test_no_envelope_means_no_envelope_check(
    envelope_repo: ClerkSqliteRepository, envelope_clock: _TestClock
) -> None:
    """A store no account authority composed (a rehearsal) has no envelope, so nothing is checked or reserved."""
    _start_legacy_run(envelope_repo, envelope_clock, strategy_instance_id=ENVELOPE_SID, symbol="SPY", run_id=ENVELOPE_RUN_ID)
    accepted = _accept(
        envelope_repo,
        ENVELOPE_SID,
        ENVELOPE_RUN_ID,
        decision_id="d1",
        leg=_leg(quantity=1_000_000),
        envelope=None,
        reference_price=None,
    )
    assert accepted.created
    assert entry_cash_claims(envelope_repo._conn, seen_before_ms=T0) == ()


def test_an_account_not_switched_to_budgets_refuses_every_enter(
    envelope_repo: ClerkSqliteRepository, envelope_clock: _TestClock
) -> None:
    """Owner decision 2026-09-29 (#2553): a version-1 account opens no position, and is never switched for you.

    Its bot keeps running -- the refusal is transient, retried on the next
    decision clock -- and the reason tells the owner where the switch is.
    """
    _start_legacy_run(envelope_repo, envelope_clock, strategy_instance_id=ENVELOPE_SID, symbol="SPY", run_id=ENVELOPE_RUN_ID)
    before = envelope_repo.control_meta_snapshot().control_revision

    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, ENVELOPE_SID, ENVELOPE_RUN_ID, decision_id="d1", leg=_leg(quantity=1), envelope=_gate())

    assert _refusal(exc_info) == BUDGETS_NOT_SWITCHED_ON
    assert exc_info.value.decision.why == (
        "This account has not switched to budgets, so no bot on it can open a new position. "
        "Switch this account to budgets in Settings, then deploy each bot again with its own dollar budget."
    )
    assert classify_admission_refusal(BUDGETS_NOT_SWITCHED_ON) is RefusalClass.TRANSIENT
    assert envelope_repo.control_meta_snapshot().control_revision == before
    assert envelope_repo.budget_authority_version() == 1


def test_a_sell_leg_cannot_be_an_envelope_enter(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    with pytest.raises(ValueError, match="BUY"):
        _accept(
            envelope_repo,
            sid,
            run_id,
            decision_id="d1",
            leg=_leg(quantity=1, side=OrderSide.SELL),
            envelope=_gate(),
        )
