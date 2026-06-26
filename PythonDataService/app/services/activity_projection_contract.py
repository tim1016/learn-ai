"""Backend-authored Activity projection display contract."""

from __future__ import annotations

from typing import Any

from app.schemas.live_runs import ActivityBrokerEventRow, ActivityEvidenceRef

_EVIDENCE_NARRATIVES: dict[str, tuple[str, str, str]] = {
    "accountSummaryAsync": (
        "Account summary refreshed",
        "Account summary",
        "The bot refreshed account balances, margin, and available-for-trading evidence from IBKR.",
    ),
    "reqPositionsAsync": (
        "Broker positions refreshed",
        "Positions refreshed",
        "The bot refreshed broker-held positions for the connected account.",
    ),
    "reqExecutionsAsync": (
        "Broker executions refreshed",
        "Executions refreshed",
        "The bot refreshed broker executions and fills for this session.",
    ),
    "reqAllOpenOrders": (
        "Open orders refreshed",
        "Open orders refreshed",
        "The bot refreshed working broker orders for the connected account.",
    ),
    "reqPnL": (
        "Account P&L refreshed",
        "P&L refreshed",
        "The bot refreshed account-level profit and loss evidence from IBKR.",
    ),
    "reqPnLSingle": (
        "Position P&L refreshed",
        "P&L refreshed",
        "The bot refreshed position-level profit and loss evidence from IBKR.",
    ),
    "reqMktData": (
        "Market quote refreshed",
        "Market quote",
        "The bot refreshed market quote evidence for an instrument.",
    ),
    "reqRealTimeBars": (
        "Live bars refreshed",
        "Live bars",
        "The bot refreshed live bar evidence used by the cockpit chart.",
    ),
    "qualifyContractsAsync": (
        "Contract details refreshed",
        "Contract details",
        "The bot refreshed broker contract details for an instrument.",
    ),
    "reqSecDefOptParamsAsync": (
        "Option chain metadata refreshed",
        "Option metadata",
        "The bot refreshed option-chain metadata from IBKR.",
    ),
    "reqMatchingSymbolsAsync": (
        "Symbol search refreshed",
        "Symbol search",
        "The bot refreshed broker symbol-search evidence.",
    ),
    "placeOrder": (
        "Order submitted",
        "Order submitted",
        "The bot submitted an order request to IBKR.",
    ),
    "cancelOrder": (
        "Cancel request sent",
        "Cancel request",
        "The bot sent a broker order-cancel request.",
    ),
}


def activity_evidence_narrative(ref: ActivityEvidenceRef) -> dict[str, str]:
    mapped = _EVIDENCE_NARRATIVES.get(ref.request_call)
    if mapped is None:
        return {
            "display_type": "Broker diagnostic",
            "source_label": "IBKR API evidence",
            "status": "Unmapped diagnostic",
            "summary": (
                "The bot captured an IBKR API observation that is not yet mapped "
                "to a trader-facing meaning. Raw request details remain in evidence."
            ),
            "fold_key": f"evidence:unmapped:{ref.request_call}:{ref.response_callback or ''}",
        }
    display_type, status, summary = mapped
    return {
        "display_type": display_type,
        "source_label": "IBKR API evidence",
        "status": status,
        "summary": summary,
        "fold_key": f"evidence:{ref.request_call}:{ref.response_callback or ''}",
    }


def activity_cluster_label(row: Any) -> str | None:
    if row.perm_id is not None:
        return f"Order #{row.perm_id}"
    if row.order_ref:
        return "Engine order"
    return None


def fold_activity_event_rows(rows: list[ActivityBrokerEventRow]) -> list[ActivityBrokerEventRow]:
    folded: list[ActivityBrokerEventRow] = []
    group_key: str | None = None
    group: list[ActivityBrokerEventRow] = []

    def flush_group() -> None:
        nonlocal group, group_key
        if not group:
            return
        child_ids = [
            child_id
            for row in group
            for child_id in (row.child_evidence_ids or [row.visible_row_id or row.id])
        ]
        evidence = [ref for row in group for ref in row.evidence]
        anchor = child_ids[-1]
        folded.append(
            group[0].model_copy(
                update={
                    "visible_row_id": f"fold:{group_key}:{anchor}",
                    "fold_count": sum(row.fold_count for row in group),
                    "child_evidence_ids": child_ids,
                    "evidence": evidence,
                }
            )
        )
        group = []
        group_key = None

    for row in sorted(rows, key=lambda item: item.ts_ms, reverse=True):
        if not row.fold_key:
            flush_group()
            folded.append(row)
            continue
        if row.fold_key != group_key:
            flush_group()
            group_key = row.fold_key
        group.append(row)
    flush_group()
    return folded


__all__ = [
    "activity_cluster_label",
    "activity_evidence_narrative",
    "fold_activity_event_rows",
]
