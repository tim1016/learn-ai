"""Every instant a custody fact keeps is inside the domain's admissible range (ADR 0022 (g)).

The ceiling is ``MAX_TIMESTAMP_MS``, the bound the facts' siblings (the synthetic
activation record, the live-arming record) already hold, not the int64 width. An
instant past it is a unit mix-up -- microseconds read as milliseconds -- and is
refused when the fact is read back, never carried.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite.activation import ActivationRecord, ActivationRecordInvalid
from app.broker.alpaca.clerk.sqlite.facts import (
    DecisionPricingFacts,
    EntryBuyingPowerComparedFacts,
    EntrySettledFacts,
    ExecutionSliceFilledFacts,
    ExitRecoveryEvaluatedFacts,
    ScheduledCloseFacts,
    validate_execution_slice_facts,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS

_PAST = MAX_TIMESTAMP_MS + 1
_NOON = 1_788_883_200_000

_RECOVERY: dict[str, Any] = {
    "uncertainty_id": "u-1", "outcome": "failure", "reason_code": "R", "last_checked_at_ms": _NOON,
    "first_failure_at_ms": _NOON - 1_000, "failure_elapsed_ms": 1_000, "lease_owner": "boot:x",
    "allowed_from_ms": _NOON + 1_000,
}
_SETTLED: dict[str, Any] = {
    "settlement": "entered", "standing": "filled", "units_asked": 1, "units_filled": 1, "until_ms": _NOON,
    "settled_at_ms": _NOON,
}
_COMPARED: dict[str, Any] = {
    "result": "consistent", "reason_code": None, "contracts_asked": 1, "contracts_filled": 1, "commitment_cents": 100,
    "set_aside_for_filled_cents": 100, "premium_paid_cents": 50, "cost_of_filled_cents": 100,
    "first_began_ms": _NOON, "first_ended_ms": _NOON, "first_buying_power": "1000.00", "sent_at_ms": _NOON,
    "second_began_ms": _NOON, "second_ended_ms": _NOON, "second_buying_power": "900.00", "fall_cents": 100,
    "compared_at_ms": _NOON, "holds": False,
}
_SLICE: dict[str, Any] = {
    "execution_id": "x-1", "symbol": "SPY", "side": "BUY", "slice_qty": 1.0, "slice_price": 100.0, "fee": None,
    "fee_fidelity": "not_reported", "evidence_source": "websocket", "source_event_at_ms": _NOON,
}


@pytest.mark.parametrize("name", ["last_checked_at_ms", "first_failure_at_ms", "failure_elapsed_ms", "allowed_from_ms"])
def test_an_exit_recovery_time_past_the_last_instant_is_refused_when_read_back(name: str) -> None:
    at_the_ceiling = ExitRecoveryEvaluatedFacts(**{**_RECOVERY, name: MAX_TIMESTAMP_MS}).to_facts_json()
    assert getattr(ExitRecoveryEvaluatedFacts.from_facts_json(at_the_ceiling), name) == MAX_TIMESTAMP_MS

    past = ExitRecoveryEvaluatedFacts(**{**_RECOVERY, name: _PAST}).to_facts_json()
    with pytest.raises(ValueError, match="Exit recovery times"):
        ExitRecoveryEvaluatedFacts.from_facts_json(past)


@pytest.mark.parametrize("name", ["until_ms", "settled_at_ms"])
def test_an_entry_settlement_time_past_the_last_instant_is_refused(name: str) -> None:
    assert getattr(EntrySettledFacts(**{**_SETTLED, name: MAX_TIMESTAMP_MS}), name) == MAX_TIMESTAMP_MS

    with pytest.raises(ValueError, match=name):
        EntrySettledFacts(**{**_SETTLED, name: _PAST})


@pytest.mark.parametrize(
    "name",
    ["compared_at_ms", "first_began_ms", "first_ended_ms", "sent_at_ms", "second_began_ms", "second_ended_ms"],
)
def test_a_buying_power_comparison_time_past_the_last_instant_is_refused(name: str) -> None:
    assert getattr(EntryBuyingPowerComparedFacts(**{**_COMPARED, name: MAX_TIMESTAMP_MS}), name) == MAX_TIMESTAMP_MS

    with pytest.raises(ValueError, match=name):
        EntryBuyingPowerComparedFacts(**{**_COMPARED, name: _PAST})


def test_a_decisions_pricing_close_past_the_last_instant_is_refused() -> None:
    at_the_ceiling = DecisionPricingFacts(bar_identity="bar-1", decided_at_ms=MAX_TIMESTAMP_MS)
    assert DecisionPricingFacts.from_mapping(at_the_ceiling.to_mapping()) == at_the_ceiling

    with pytest.raises(ValueError, match="decision's close"):
        DecisionPricingFacts.from_mapping({"bar_identity": "bar-1", "decided_at_ms": _PAST})


@pytest.mark.parametrize("name", ["due_ms", "session_open_ms"])
def test_a_scheduled_close_time_at_the_last_instant_reads_back(name: str) -> None:
    kept = {"kind": "expiry_day_close", "due_ms": _NOON, "session_open_ms": _NOON, name: MAX_TIMESTAMP_MS}

    assert getattr(ScheduledCloseFacts.from_mapping(kept), name) == MAX_TIMESTAMP_MS


def test_an_execution_slice_stamped_past_the_last_instant_is_refused() -> None:
    validate_execution_slice_facts(ExecutionSliceFilledFacts(**{**_SLICE, "source_event_at_ms": MAX_TIMESTAMP_MS}))

    with pytest.raises(ValueError, match="source_event_at_ms"):
        validate_execution_slice_facts(ExecutionSliceFilledFacts(**{**_SLICE, "source_event_at_ms": _PAST}))


def test_an_activation_stamped_past_the_last_instant_is_refused_as_its_siblings_are() -> None:
    def record(activated_at_ms: int) -> dict[str, Any]:
        return asdict(ActivationRecord.create(
            account_id="PA-TEST", authority_generation=1, db_identity_token="token", broker_proof_reference="proof",
            broker_proof_sha256="a" * 64, legacy_quarantine_manifest="manifest",
            legacy_quarantine_manifest_sha256="b" * 64, activated_at_ms=activated_at_ms,
        ))

    assert ActivationRecord.from_payload(record(MAX_TIMESTAMP_MS)).activated_at_ms == MAX_TIMESTAMP_MS
    with pytest.raises(ActivationRecordInvalid, match="timestamp"):
        ActivationRecord.from_payload(record(_PAST))
