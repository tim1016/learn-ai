"""Regression coverage for Clerk custody timing evidence."""

from __future__ import annotations

from app.broker.ibkr.models import IbkrOrderAck
from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.services.clerk_custody_timeline import (
    custody_timeline_from_journal_entries,
    custody_timeline_from_transaction,
)
from app.services.clerk_transaction_projection_ibkr import event_from_journal, row_from_event


def _intent() -> AccountOwnerSubmitIntent:
    return AccountOwnerSubmitIntent(
        trace_id="trace-custody",
        account_id="DU123",
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace="learn-ai/bot-a/v1",
        intent_id="custody-intent",
        order_ref="learn-ai/bot-a/v1:custody-intent",
        intent_kind="STRATEGY",
        order_spec={},
        owner_generation=3,
        created_at_ms=80,
    )


def _ack() -> IbkrOrderAck:
    return IbkrOrderAck(
        account_id="DU123",
        is_paper=True,
        order_id=42,
        client_id=7,
        con_id=1,
        symbol="SPY",
        action="BUY",
        quantity=1,
        order_type="MKT",
        status="Submitted",
        order_ref="learn-ai/bot-a/v1:custody-intent",
        placed_at_ms=180,
    )


def _entries(*, include_late_reordered_fill: bool = False) -> list[AccountClerkJournalEntry]:
    intent = _intent()
    entries = [
        AccountClerkJournalEntry(
            seq=1,
            entry_kind="recorded",
            recorded_at_ms=130,
            clerk_request_received_at_ms=100,
            clerk_intake_admitted_at_ms=110,
            inbox_fsynced_at_ms=120,
            intent=intent,
        ),
        AccountClerkJournalEntry(
            seq=2,
            entry_kind="broker_submitting",
            recorded_at_ms=140,
            intent=intent,
        ),
        AccountClerkJournalEntry(
            seq=3,
            entry_kind="broker_acked",
            recorded_at_ms=190,
            intent=intent,
            order_id=42,
            broker_ack=_ack(),
        ),
        AccountClerkJournalEntry(
            seq=4,
            entry_kind="broker_event",
            recorded_at_ms=205,
            intent=intent,
            broker_callback_idempotency_key="status:submitted",
            broker_event={
                "account_id": "DU123",
                "order_id": 42,
                "event_type": "status",
                "status": "Submitted",
                "order_ref": intent.order_ref,
                "ts_ms": 200,
            },
        ),
        AccountClerkJournalEntry(
            seq=5,
            entry_kind="broker_event",
            recorded_at_ms=220,
            intent=intent,
            broker_callback_idempotency_key="exec:exec-first",
            broker_event={
                "account_id": "DU123",
                "order_id": 42,
                "event_type": "fill",
                "status": "Filled",
                "order_ref": intent.order_ref,
                "exec_id": "exec-first",
                "exec_time_ms": 170,
                "ts_ms": 210,
            },
        ),
    ]
    if include_late_reordered_fill:
        entries.append(
            AccountClerkJournalEntry(
                seq=6,
                entry_kind="broker_event",
                recorded_at_ms=310,
                intent=intent,
                broker_callback_idempotency_key="exec:exec-late-reordered",
                broker_event={
                    "account_id": "DU123",
                    "order_id": 42,
                    "event_type": "fill",
                    "status": "Filled",
                    "order_ref": intent.order_ref,
                    "exec_id": "exec-late-reordered",
                    "exec_time_ms": 160,
                    "ts_ms": 300,
                },
            )
        )
    return entries


def test_custody_timeline_reports_each_durable_phase_and_duration() -> None:
    timeline = custody_timeline_from_journal_entries(_entries(), now_ms=270)

    assert timeline.model_dump() == {
        "intent_created_at_ms": 80,
        "clerk_request_received_at_ms": 100,
        "clerk_intake_admitted_at_ms": 110,
        "inbox_fsynced_at_ms": 120,
        "a0_custody_accepted_at_ms": 130,
        "broker_write_started_at_ms": 140,
        "broker_call_returned_at_ms": 180,
        "broker_ack_recorded_at_ms": 190,
        "earliest_broker_source_at_ms": 170,
        "first_callback_arrived_at_ms": 200,
        "first_callback_recorded_at_ms": 205,
        "economic_terminal_recorded_at_ms": 220,
        "durations": {
            "request_to_intake_ms": 10,
            "intake_to_a0_ms": 20,
            "a0_to_broker_write_ms": 10,
            "broker_write_to_return_ms": 40,
            "broker_return_to_first_callback_ms": 20,
            "terminal_age_ms": 50,
        },
    }


def test_late_callback_preserves_earlier_arrival_and_durable_phase_facts() -> None:
    timeline = custody_timeline_from_journal_entries(
        _entries(include_late_reordered_fill=True),
        now_ms=320,
    )

    # The source clock can disclose an older execution arriving late. It may
    # not rewrite the already-observed callback or durable terminal clocks.
    assert timeline.earliest_broker_source_at_ms == 160
    assert timeline.first_callback_arrived_at_ms == 200
    assert timeline.first_callback_recorded_at_ms == 205
    assert timeline.economic_terminal_recorded_at_ms == 220
    assert timeline.a0_custody_accepted_at_ms == 130
    assert timeline.broker_write_started_at_ms == 140


def test_custody_timeline_leaves_unobserved_phase_facts_unknown() -> None:
    intent = _intent()
    timeline = custody_timeline_from_journal_entries(
        [
            AccountClerkJournalEntry(
                seq=1,
                entry_kind="recorded",
                recorded_at_ms=130,
                intent=intent,
            ),
            AccountClerkJournalEntry(
                seq=2,
                entry_kind="broker_submitting",
                recorded_at_ms=140,
                intent=intent,
            ),
        ],
        now_ms=270,
    )

    assert timeline.clerk_request_received_at_ms is None
    assert timeline.clerk_intake_admitted_at_ms is None
    assert timeline.inbox_fsynced_at_ms is None
    assert timeline.broker_call_returned_at_ms is None
    assert timeline.first_callback_arrived_at_ms is None
    assert timeline.economic_terminal_recorded_at_ms is None
    assert timeline.durations.model_dump() == {
        "request_to_intake_ms": None,
        "intake_to_a0_ms": None,
        "a0_to_broker_write_ms": 10,
        "broker_write_to_return_ms": None,
        "broker_return_to_first_callback_ms": None,
        "terminal_age_ms": None,
    }


def test_transaction_detail_rebuilds_its_timeline_from_projected_receipts() -> None:
    entries = _entries()
    first_event = event_from_journal(entries[0])
    assert first_event is not None
    row = row_from_event("DU123", entries[0], first_event).model_copy(
        update={
            "events": [
                event
                for entry in entries
                if (event := event_from_journal(entry)) is not None
            ]
        }
    )

    timeline = custody_timeline_from_transaction(row, now_ms=270)

    assert timeline is not None
    assert timeline.a0_custody_accepted_at_ms == 130
    assert timeline.economic_terminal_recorded_at_ms == 220
