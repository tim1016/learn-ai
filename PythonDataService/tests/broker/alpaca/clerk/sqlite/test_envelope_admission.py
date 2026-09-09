"""Envelope admission at the ENTER seam (ADR 0059 D4, plan R1).

``accept_enter`` gains a second gate beside ``require_admission``: the
envelope's cash bound. These tests drive it through the public entry point —
no direct call to ``require_envelope_admission`` — so what they pin is the
observable behaviour of accepting an ENTER: which legs are admitted, which
refusal each unobservable fact produces, and that a refusal writes nothing.

Every stamp comes from the fixture clock; the gate's observation is stamped
with the same ``T0``, so freshness is a property of the test's arithmetic
rather than of when the suite happens to run.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_CASH_EXCEEDED,
    LIVE_ENVELOPE_DISAGREEMENT,
    LIVE_ENVELOPE_UNOBSERVED,
    OBSERVATION_MAX_AGE_MS,
    AccountObservation,
    LiveEnvelopeGate,
    LiveEnvelopeValues,
)
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.conftest import (
    ENVELOPE_T0 as T0,
)


def _gate(
    *,
    cash: float = 100_000.0,
    observed_at_ms: int = T0,
    sealed: LiveEnvelopeValues | None = None,
) -> LiveEnvelopeGate:
    gate = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, sealed=sealed, custody_is_simulated=True)
    gate.publish(
        AccountObservation(
            observed_at_ms=observed_at_ms,
            broker_cash_usd=cash,
            cash_available_usd=cash,
            last_equity_usd=cash,
            unrealized_pl_usd=0.0,
            position_count=0,
        )
    )
    return gate


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
    assert envelope_repo.reserved_cash_usd(observed_at_ms=T0) == pytest.approx(10_000.0)


def test_a_market_enter_beyond_cash_is_refused_and_nothing_is_written(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    before = envelope_repo.control_meta_snapshot().control_revision
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, decision_id="d1", leg=_leg(quantity=1_001), envelope=_gate())
    assert _refusal(exc_info) == LIVE_ENVELOPE_CASH_EXCEEDED
    assert "100100.00 USD" in (exc_info.value.decision.why or "")
    assert envelope_repo.control_meta_snapshot().control_revision == before
    assert envelope_repo.reserved_cash_usd(observed_at_ms=T0) == 0.0


def test_two_instances_cannot_spend_the_same_cash(
    envelope_repo: ClerkSqliteRepository, two_active_instances: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    (a, run_a), (b, run_b) = two_active_instances
    gate = _gate()
    _accept(envelope_repo, a, run_a, decision_id="d1", leg=_leg(quantity=600), envelope=gate)
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, b, run_b, decision_id="d2", leg=_leg(quantity=600), envelope=gate)
    assert _refusal(exc_info) == LIVE_ENVELOPE_CASH_EXCEEDED
    _accept(envelope_repo, b, run_b, decision_id="d3", leg=_leg(quantity=400), envelope=gate)


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


def test_a_sealed_envelope_that_disagrees_with_the_environment_refuses(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    other = LiveEnvelopeValues(**{**TEST_ENVELOPE_VALUES.to_mapping(), "loss_usd": 4_999.0})
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(
            envelope_repo, sid, run_id, decision_id="d1", leg=_leg(quantity=1), envelope=_gate(sealed=other)
        )
    assert _refusal(exc_info) == LIVE_ENVELOPE_DISAGREEMENT


def test_a_disagreement_is_named_even_when_nothing_has_been_observed(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    """The ordering is load-bearing: a disagreed envelope is not 'unobserved'.

    Both facts hold at once here, and only one of them tells the operator what
    to do — re-arm, rather than wait for the sync to catch up.
    """
    sid, run_id = active_instance
    other = LiveEnvelopeValues(**{**TEST_ENVELOPE_VALUES.to_mapping(), "loss_usd": 4_999.0})
    never_observed = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, sealed=other, custody_is_simulated=True)
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(
            envelope_repo, sid, run_id, decision_id="d1", leg=_leg(quantity=1), envelope=never_observed
        )
    assert _refusal(exc_info) == LIVE_ENVELOPE_DISAGREEMENT


def test_no_envelope_means_no_envelope_check(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    accepted = _accept(
        envelope_repo,
        sid,
        run_id,
        decision_id="d1",
        leg=_leg(quantity=1_000_000),
        envelope=None,
        reference_price=None,
    )
    assert accepted.created
    assert envelope_repo.reserved_cash_usd(observed_at_ms=T0) == 0.0


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
