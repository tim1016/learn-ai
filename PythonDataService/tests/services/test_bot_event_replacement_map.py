"""Tests for the Slice -1 broker-activity replacement map."""

from __future__ import annotations

from app.schemas.bot_events import BotEventSeverity, BotEventType, SourceAuthority, TerminalErrorCode
from app.schemas.broker_activity import (
    BrokerActivityRow,
    EngineOverlay,
    ReasonCode,
    Verdict,
)
from app.services.bot_event_replacement_map import broker_activity_row_to_bot_event_row

ORDER_REF = "learn-ai/bot-a/v1:intent-1"


def _row(**overrides) -> BrokerActivityRow:
    base = {
        "seq": 1,
        "ts_ms": 1_700_000_000_000,
        "exec_id": "exec-1",
        "perm_id": 123,
        "order_ref": ORDER_REF,
        "symbol": "SPY",
        "side": "BUY",
        "quantity": 100.0,
        "price": 450.0,
        "commission": 1.0,
        "net_amount": -45_001.0,
        "order_type": "MKT",
        "exec_ts_ms": 1_700_000_000_100,
        "verdict": Verdict.EXPECTED,
        "template_key": "normal_fill",
        "template_version": 1,
        "headline": "Filled 100 SPY",
        "narrative": "Market order filled.",
        "reason_codes": (ReasonCode.NORMAL_FILL,),
        "engine_overlay": EngineOverlay(intent_id="intent-1"),
    }
    base.update(overrides)
    return BrokerActivityRow(**base)


def test_normal_fill_maps_to_order_filled_tail_row() -> None:
    mapped = broker_activity_row_to_bot_event_row(_row())

    assert mapped.event_type is BotEventType.ORDER_FILLED
    assert mapped.source_authority is SourceAuthority.BROKER_SESSION
    assert mapped.severity is BotEventSeverity.INFO
    assert mapped.headline == "Filled 100 SPY"
    assert mapped.narrative == "Market order filled."
    assert mapped.identity.intent_id == "intent-1"
    assert mapped.identity.order_ref == ORDER_REF
    assert mapped.identity.perm_id == 123
    assert mapped.identity.exec_id == "exec-1"
    assert mapped.facts["legacy_schema"] == "BrokerActivityRow"
    assert mapped.facts["legacy_reason_codes"] == ["normal_fill"]
    assert mapped.facts["broker"]["symbol"] == "SPY"
    assert mapped.facts["broker"]["quantity"] == 100.0
    assert mapped.facts["engine_overlay"]["intent_id"] == "intent-1"


def test_pending_acknowledgement_maps_to_order_submitted() -> None:
    row = _row(
        exec_id=None,
        perm_id=None,
        price=None,
        commission=None,
        net_amount=None,
        exec_ts_ms=None,
        verdict=Verdict.ENGINE_ONLY_PENDING,
        template_key="pending_acknowledgement",
        headline="Order submitted",
        narrative="Awaiting broker acknowledgement.",
        reason_codes=(ReasonCode.PENDING_ACKNOWLEDGEMENT,),
    )

    mapped = broker_activity_row_to_bot_event_row(row)

    assert mapped.event_type is BotEventType.ORDER_SUBMITTED
    assert mapped.source_authority is SourceAuthority.ENGINE_LOOP
    assert mapped.severity is BotEventSeverity.INFO
    assert mapped.terminal_error is None


def test_expected_with_caveat_maps_to_warning_severity() -> None:
    row = _row(
        verdict=Verdict.EXPECTED_WITH_CAVEAT,
        reason_codes=(ReasonCode.PARTIAL_FILL,),
        template_key="partial_fill",
        headline="Partial fill",
        narrative="The order partially filled.",
    )

    mapped = broker_activity_row_to_bot_event_row(row)

    assert mapped.severity is BotEventSeverity.WARNING


def test_rejection_maps_to_terminal_order_rejected_not_expected_row() -> None:
    row = _row(
        exec_id=None,
        price=None,
        commission=None,
        net_amount=None,
        exec_ts_ms=None,
        verdict=Verdict.EXPECTED,
        template_key="rejection",
        headline="Order rejected",
        narrative="IBKR rejected the order.",
        reason_codes=(ReasonCode.REJECTION,),
    )

    mapped = broker_activity_row_to_bot_event_row(row)

    assert mapped.event_type is BotEventType.ORDER_REJECTED
    assert mapped.severity is BotEventSeverity.CRITICAL
    assert mapped.terminal_error is not None
    assert mapped.terminal_error.code is TerminalErrorCode.ORDER_REJECTED
    assert mapped.terminal_error.message == "Order rejected"


def test_cancellation_maps_to_order_cancelled_without_terminal_error() -> None:
    row = _row(
        exec_id=None,
        price=None,
        commission=None,
        net_amount=None,
        exec_ts_ms=None,
        template_key="cancellation",
        headline="Order cancelled",
        narrative="Broker cancelled the order.",
        reason_codes=(ReasonCode.CANCELLATION,),
    )

    mapped = broker_activity_row_to_bot_event_row(row)

    assert mapped.event_type is BotEventType.ORDER_CANCELLED
    assert mapped.severity is BotEventSeverity.INFO
    assert mapped.terminal_error is None


def test_legacy_row_without_durable_identity_uses_explicit_fallback() -> None:
    row = _row(order_ref=None, perm_id=None, exec_id=None, engine_overlay=None)

    mapped = broker_activity_row_to_bot_event_row(row)

    assert mapped.identity.evaluation_id == "legacy-broker-activity-seq:1"
    assert mapped.facts["legacy_identity_fallback"] is True
