"""Module D (submit_state_machine) unit tests — ADR-0008 §4 / PRD #446 plan D.

Every transition incl. the three uncertain-resolution outcomes; provably-absent
retry reuses the same ref; retry cap is 1; NOT_PROVABLE halts; blind retry is
unrepresentable.
"""

from __future__ import annotations

import pytest

from app.engine.live.intent_events import IntentEventType
from app.engine.live.submit_state_machine import (
    RETRY_CAP,
    AckOutcome,
    BrokerProbe,
    IllegalSubmitTransition,
    SubmitVerdict,
    next_action,
    verdict_to_event_type,
)


def test_clean_ack_records_submitted() -> None:
    assert (
        next_action(
            current_status=IntentEventType.PENDING_INTENT,
            ack_outcome=AckOutcome.CLEAN_ACK,
        )
        is SubmitVerdict.RECORD_SUBMITTED
    )


def test_raise_or_timeout_records_uncertain() -> None:
    assert (
        next_action(
            current_status=IntentEventType.PENDING_INTENT,
            ack_outcome=AckOutcome.RAISED_OR_TIMEOUT,
        )
        is SubmitVerdict.RECORD_ACK_FAILED_UNCERTAIN
    )


def test_uncertain_present_recovers_adopt() -> None:
    assert (
        next_action(
            current_status=IntentEventType.ACK_FAILED_UNCERTAIN,
            probe=BrokerProbe.PRESENT,
        )
        is SubmitVerdict.RECOVER_ADOPT
    )


def test_uncertain_provably_absent_retries_once() -> None:
    assert (
        next_action(
            current_status=IntentEventType.ACK_FAILED_UNCERTAIN,
            probe=BrokerProbe.PROVABLY_ABSENT,
            retry_count=0,
        )
        is SubmitVerdict.RETRY_ONCE
    )


def test_second_uncertain_on_retried_intent_halts() -> None:
    assert RETRY_CAP == 1
    assert (
        next_action(
            current_status=IntentEventType.ACK_FAILED_UNCERTAIN,
            probe=BrokerProbe.PROVABLY_ABSENT,
            retry_count=1,
        )
        is SubmitVerdict.HALT
    )


def test_uncertain_not_provable_halts() -> None:
    assert (
        next_action(
            current_status=IntentEventType.ACK_FAILED_UNCERTAIN,
            probe=BrokerProbe.NOT_PROVABLE,
        )
        is SubmitVerdict.HALT
    )


def test_blind_retry_is_unrepresentable() -> None:
    # RETRY_ONCE is reachable ONLY via PROVABLY_ABSENT with retry_count < cap.
    for probe in (BrokerProbe.PRESENT, BrokerProbe.NOT_PROVABLE):
        assert (
            next_action(
                current_status=IntentEventType.ACK_FAILED_UNCERTAIN, probe=probe
            )
            is not SubmitVerdict.RETRY_ONCE
        )
    # And never on an already-retried intent, regardless of probe.
    assert (
        next_action(
            current_status=IntentEventType.ACK_FAILED_UNCERTAIN,
            probe=BrokerProbe.PROVABLY_ABSENT,
            retry_count=5,
        )
        is SubmitVerdict.HALT
    )


def test_negative_retry_count_is_rejected() -> None:
    # A negative count must never satisfy `< RETRY_CAP` and slip a 2nd retry
    # through — that would be a double-submit path.
    for rc in (-1, -100):
        with pytest.raises(IllegalSubmitTransition):
            next_action(
                current_status=IntentEventType.ACK_FAILED_UNCERTAIN,
                probe=BrokerProbe.PROVABLY_ABSENT,
                retry_count=rc,
            )


@pytest.mark.parametrize(
    "kwargs",
    [
        # ack phase given a probe
        {"current_status": IntentEventType.PENDING_INTENT, "probe": BrokerProbe.PRESENT},
        # ack phase missing the outcome
        {"current_status": IntentEventType.PENDING_INTENT},
        # resolution phase missing the probe
        {"current_status": IntentEventType.ACK_FAILED_UNCERTAIN},
        # resolution phase given an ack outcome
        {
            "current_status": IntentEventType.ACK_FAILED_UNCERTAIN,
            "ack_outcome": AckOutcome.CLEAN_ACK,
        },
        # a status with no defined transition
        {
            "current_status": IntentEventType.SUBMITTED,
            "ack_outcome": AckOutcome.CLEAN_ACK,
        },
    ],
)
def test_illegal_transitions_raise(kwargs: dict[str, object]) -> None:
    with pytest.raises(IllegalSubmitTransition):
        next_action(**kwargs)  # type: ignore[arg-type]


def test_verdict_to_event_type_mapping() -> None:
    assert verdict_to_event_type(SubmitVerdict.RECORD_SUBMITTED) is IntentEventType.SUBMITTED
    assert (
        verdict_to_event_type(SubmitVerdict.RECORD_ACK_FAILED_UNCERTAIN)
        is IntentEventType.ACK_FAILED_UNCERTAIN
    )
    assert (
        verdict_to_event_type(SubmitVerdict.RECOVER_ADOPT)
        is IntentEventType.SUBMITTED_RECOVERED
    )
    assert (
        verdict_to_event_type(SubmitVerdict.RETRY_ONCE)
        is IntentEventType.INTENT_NOT_ACCEPTED
    )
    assert (
        verdict_to_event_type(SubmitVerdict.HALT)
        is IntentEventType.SUBMIT_UNCERTAIN_HALTED
    )
