from __future__ import annotations

from pathlib import Path

from app.services.clerk_transaction_projection import read_appended_alpaca_journal


def test_read_appended_alpaca_journal_projects_shared_rows_without_ibkr_callback_semantics() -> None:
    fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / "alpaca"
        / "transaction_history"
        / "order_journal.jsonl"
    )

    batch = read_appended_alpaca_journal(
        account_id="PA-SLICE4", journal_path=fixture, cursor=None
    )

    assert batch is not None
    assert len(batch.transactions) == 1
    transaction = batch.transactions[0]
    assert transaction.broker == "alpaca"
    assert transaction.native_order_id == "00000000-0000-0000-0000-000000000001"
    assert transaction.order_id is None
    assert [event.event_kind for event in batch.events] == [
        "submitted",
        "fill",
        "activity_fill",
    ]
    assert batch.events[1].callback_identity.startswith("exec:")
    assert batch.events[1].native_execution_id == "00000000-0000-0000-0000-000000000004"
    assert batch.events[2].native_order_id == "00000000-0000-0000-0000-000000000001"
    assert batch.events[1].receipt["recovery_source"] == "closed_orders_window"
    assert batch.events[1].receipt["recovery_window_limit"] == 500
    assert batch.events[2].receipt["recovery_source"] == "account_activities_cursor_window"
    assert batch.events[2].receipt["recovery_window_limit"] == 100
