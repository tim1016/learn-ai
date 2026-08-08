"""Canonical test records derived from the bounded UI campaign contract."""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.qualification_ui_campaign_contract import (
    BOUNDED_UI_CAMPAIGN,
)


def ui_correlation_records() -> tuple[dict[str, object], ...]:
    """Build the exact browser/revision ledger shared by qualification tests."""

    return tuple(
        {
            "page_load_index": page_load_index,
            "event_index": event_index,
            "browser_epoch": f"browser-reload-{page_load_index}",
            "revision": revision,
            "historical_snapshot_state": BOUNDED_UI_CAMPAIGN.historical_snapshot_state,
            "presented_action_id": BOUNDED_UI_CAMPAIGN.presented_action_id,
            "click_target": BOUNDED_UI_CAMPAIGN.evidence_click_target,
            "evidence_request_method": "GET",
            "evidence_request_path": (
                "/api/brokers/alpaca/accounts/DUM284968/bots/sid-001/evidence"
                "?transaction_ref=tx-001&client_hint=operator-transaction-timeline"
            ),
            "operation_reference": f"receipt-{page_load_index}-{event_index}",
            "proof_reference": f"receipt-{page_load_index}-{event_index}",
            "dispatched_lifecycle_action_id": None,
            "durable_lifecycle_receipt_id": None,
        }
        for page_load_index in range(BOUNDED_UI_CAMPAIGN.page_load_count)
        for event_index, revision in enumerate(BOUNDED_UI_CAMPAIGN.revision_sequence)
    )
