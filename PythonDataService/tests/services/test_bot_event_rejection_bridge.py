from __future__ import annotations

import pytest

from app.schemas.bot_events import (
    BotEventIdentity,
    BotEventRaw,
    BotEventRawType,
    IncidentDedupeKey,
    SourceAuthority,
    TerminalError,
    TerminalErrorCode,
    TerminalErrorSource,
)
from app.services.bot_event_rejection_bridge import (
    build_order_rejected_incident,
    order_rejected_incident_id,
)


def _raw_rejection(*, seq: int = 1) -> BotEventRaw:
    return BotEventRaw(
        seq=seq,
        ts_ms=1_700_000_000_000,
        strategy_instance_id="sid-bridge-test",
        run_id="run-bridge-test",
        event_type=BotEventRawType.ORDER_REJECTED,
        source_authority=SourceAuthority.BROKER_SESSION,
        identity=BotEventIdentity(intent_id="intent-1", req_id=42, order_id=42),
        terminal_error=TerminalError(
            code=TerminalErrorCode.ORDER_REJECTED,
            source=TerminalErrorSource.IBKR,
            gate_id="broker.place_order",
            message="IBKR order rejected",
            external_code=201,
            external_message="Order rejected - insufficient buying power",
        ),
    )


def test_order_rejected_incident_id_is_stable_for_req_id_identity() -> None:
    first = build_order_rejected_incident(_raw_rejection(seq=1))
    second = build_order_rejected_incident(_raw_rejection(seq=2))

    assert first.incident_id == second.incident_id
    assert first.category == "order"
    assert first.notice.code == "order.rejected"
    assert first.evidence["req_id"] == 42


def test_order_rejected_incident_rejects_other_terminal_codes() -> None:
    raw_event = BotEventRaw(
        seq=1,
        ts_ms=1_700_000_000_000,
        strategy_instance_id="sid-bridge-test",
        run_id="run-bridge-test",
        event_type=BotEventRawType.HALTED,
        source_authority=SourceAuthority.ENGINE_LOOP,
        identity=BotEventIdentity(evaluation_id="eval-1"),
        terminal_error=TerminalError(
            code=TerminalErrorCode.HALTED,
            source=TerminalErrorSource.ENGINE,
            gate_id="submit.pipeline",
            message="Bot halted",
        ),
    )

    with pytest.raises(ValueError, match="requires order_rejected terminal_error"):
        build_order_rejected_incident(raw_event)


def test_order_rejected_incident_id_rejects_other_terminal_codes() -> None:
    key = IncidentDedupeKey(
        strategy_instance_id="sid-bridge-test",
        terminal_code=TerminalErrorCode.HALTED,
        evaluation_id="eval-1",
    )

    with pytest.raises(ValueError, match="requires order_rejected terminal code"):
        order_rejected_incident_id(key)
