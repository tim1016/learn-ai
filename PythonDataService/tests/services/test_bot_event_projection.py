"""Tests for deterministic Bot event raw-to-row projection."""

from __future__ import annotations

import pytest

from app.schemas.bot_events import (
    BotEventIdentity,
    BotEventRaw,
    BotEventRawType,
    BotEventSeverity,
    BotEventType,
    GateStep,
    GateStepResult,
    SourceAuthority,
    TerminalError,
    TerminalErrorCode,
    TerminalErrorSource,
)
from app.services.bot_event_projection import project_bot_event_rows

RUN_ID = "run-1"
SID = "bot-a"


def _raw(
    *,
    seq: int,
    event_type: BotEventRawType,
    identity: BotEventIdentity,
    ts_ms: int | None = None,
    source_authority: SourceAuthority = SourceAuthority.ENGINE_LOOP,
    gate_step: GateStep | None = None,
    terminal_error: TerminalError | None = None,
) -> BotEventRaw:
    return BotEventRaw(
        seq=seq,
        ts_ms=ts_ms or 1_700_000_000_000 + seq,
        strategy_instance_id=SID,
        run_id=RUN_ID,
        event_type=event_type,
        source_authority=source_authority,
        identity=identity,
        gate_step=gate_step,
        terminal_error=terminal_error,
    )


def _gate(seq: int, *, result: GateStepResult, gate_id: str = "broker.safety") -> BotEventRaw:
    step = GateStep(
        evaluation_id="eval-1",
        gate_id=gate_id,
        gate_result=result,
        source_authority=SourceAuthority.ENGINE_LOOP,
        facts={"gate_id": gate_id},
    )
    return _raw(
        seq=seq,
        event_type=BotEventRawType.GATE_STEP,
        identity=BotEventIdentity(evaluation_id="eval-1"),
        gate_step=step,
    )


def test_blocking_gate_steps_project_to_blocked_row() -> None:
    rows = project_bot_event_rows(
        [
            _gate(1, result=GateStepResult.PASS, gate_id="session"),
            _gate(2, result=GateStepResult.BLOCK, gate_id="daily_order_cap"),
        ]
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.event_type is BotEventType.BLOCKED
    assert row.severity is BotEventSeverity.WARNING
    assert row.identity.evaluation_id == "eval-1"
    assert [step.gate_id for step in row.gate_steps] == ["session", "daily_order_cap"]
    assert row.facts["gate_ids"] == ["session", "daily_order_cap"]


def test_nonblocking_gate_only_cluster_stays_quiet() -> None:
    rows = project_bot_event_rows([_gate(1, result=GateStepResult.PASS)])

    assert rows == []


def test_terminal_row_carries_error_and_gate_walk() -> None:
    error = TerminalError(
        code=TerminalErrorCode.ORDER_REJECTED,
        source=TerminalErrorSource.IBKR,
        gate_id="broker.place_order",
        message="order rejected",
        external_code=201,
        external_message="insufficient buying power",
    )
    rejected = _raw(
        seq=3,
        event_type=BotEventRawType.ORDER_REJECTED,
        identity=BotEventIdentity(
            evaluation_id="eval-1",
            intent_id="intent-1",
            order_ref="learn-ai/bot-a/v1:intent-1",
            req_id=42,
        ),
        source_authority=SourceAuthority.BROKER_SESSION,
        terminal_error=error,
    )

    rows = project_bot_event_rows(
        [
            _gate(1, result=GateStepResult.PASS, gate_id="session"),
            rejected,
        ]
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.event_type is BotEventType.ORDER_REJECTED
    assert row.severity is BotEventSeverity.CRITICAL
    assert row.identity.evaluation_id == "eval-1"
    assert row.identity.order_ref == "learn-ai/bot-a/v1:intent-1"
    assert row.identity.req_id == 42
    assert row.gate_steps[0].gate_id == "session"
    assert row.terminal_error == error
    assert row.headline == "IBKR rejected the order"
    assert row.narrative == "insufficient buying power"


def test_order_event_promotes_evaluation_cluster_to_order_identity() -> None:
    submitted = _raw(
        seq=2,
        event_type=BotEventRawType.ORDER_SUBMITTED,
        identity=BotEventIdentity(
            evaluation_id="eval-1",
            intent_id="intent-1",
            order_ref="learn-ai/bot-a/v1:intent-1",
        ),
    )

    rows = project_bot_event_rows(
        [
            _gate(1, result=GateStepResult.PASS, gate_id="sizing"),
            submitted,
        ]
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.event_type is BotEventType.ORDER_SUBMITTED
    assert row.identity.evaluation_id == "eval-1"
    assert row.identity.intent_id == "intent-1"
    assert row.identity.order_ref == "learn-ai/bot-a/v1:intent-1"
    assert row.gate_steps[0].gate_id == "sizing"


def test_order_ref_terminal_event_promotes_existing_evaluation_cluster() -> None:
    order_ref = "learn-ai/bot-a/v1:intent-1"
    submitted = _raw(
        seq=2,
        event_type=BotEventRawType.ORDER_SUBMITTED,
        identity=BotEventIdentity(
            evaluation_id="eval-1",
            intent_id="intent-1",
            order_ref=order_ref,
            order_id=42,
        ),
    )
    rejected = _raw(
        seq=3,
        event_type=BotEventRawType.ORDER_REJECTED,
        identity=BotEventIdentity(
            order_ref=order_ref,
            req_id=42,
            order_id=42,
        ),
        source_authority=SourceAuthority.BROKER_SESSION,
        terminal_error=TerminalError(
            code=TerminalErrorCode.ORDER_REJECTED,
            source=TerminalErrorSource.IBKR,
            message="order rejected",
            external_code=201,
            external_message="insufficient buying power",
        ),
    )

    rows = project_bot_event_rows(
        [
            _gate(1, result=GateStepResult.PASS, gate_id="sizing"),
            submitted,
            rejected,
        ]
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.event_type is BotEventType.ORDER_REJECTED
    assert row.identity.evaluation_id == "eval-1"
    assert row.identity.intent_id == "intent-1"
    assert row.identity.order_ref == order_ref
    assert row.identity.req_id == 42
    assert row.gate_steps[0].gate_id == "sizing"
    assert row.facts["raw_event_types"] == ["order_submitted", "order_rejected"]


def test_idle_rows_fold_until_next_visible_event() -> None:
    idle_1 = _raw(
        seq=1,
        event_type=BotEventRawType.EVALUATION_IDLE,
        identity=BotEventIdentity(evaluation_id="bar:1"),
    )
    idle_2 = _raw(
        seq=2,
        event_type=BotEventRawType.EVALUATION_IDLE,
        identity=BotEventIdentity(evaluation_id="bar:2"),
    )

    rows = project_bot_event_rows(
        [
            idle_1,
            idle_2,
            _gate(3, result=GateStepResult.BLOCK, gate_id="session"),
        ]
    )

    assert [row.event_type for row in rows] == [
        BotEventType.EVALUATION_IDLE,
        BotEventType.BLOCKED,
    ]
    idle = rows[0]
    assert idle.facts["folded_count"] == 2
    assert idle.facts["raw_event_seqs"] == [1, 2]
    assert idle.narrative == "The bot evaluated 2 bars without a trade signal."


def test_invisible_pass_gate_does_not_split_idle_fold() -> None:
    idle_1 = _raw(
        seq=1,
        event_type=BotEventRawType.EVALUATION_IDLE,
        identity=BotEventIdentity(evaluation_id="bar:1"),
    )
    pass_gate = _gate(2, result=GateStepResult.PASS, gate_id="session")
    idle_2 = _raw(
        seq=3,
        event_type=BotEventRawType.EVALUATION_IDLE,
        identity=BotEventIdentity(evaluation_id="bar:2"),
    )

    rows = project_bot_event_rows([idle_1, pass_gate, idle_2])

    assert len(rows) == 1
    assert rows[0].event_type is BotEventType.EVALUATION_IDLE
    assert rows[0].facts["folded_count"] == 2
    assert rows[0].facts["raw_event_seqs"] == [1, 3]


def test_blocked_spine_event_without_blocking_gate_step_fails_loudly() -> None:
    blocked = _raw(
        seq=1,
        event_type=BotEventRawType.BLOCKED,
        identity=BotEventIdentity(evaluation_id="eval-1"),
    )

    with pytest.raises(ValueError, match="captured blocking gate-step"):
        project_bot_event_rows([blocked])
