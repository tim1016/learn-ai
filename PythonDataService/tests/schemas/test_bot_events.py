"""Schema contract tests for ADR-0024 Bot event stream models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.bot_events import (
    BotEventIdentity,
    BotEventRaw,
    BotEventRawType,
    BotEventRow,
    BotEventSeverity,
    BotEventType,
    GateStep,
    GateStepResult,
    IncidentDedupeKey,
    SourceAuthority,
    TerminalError,
    TerminalErrorCode,
    TerminalErrorSource,
)


def _identity() -> BotEventIdentity:
    return BotEventIdentity(evaluation_id="eval-1")


def _terminal_error() -> TerminalError:
    return TerminalError(
        code=TerminalErrorCode.ORDER_REJECTED,
        source=TerminalErrorSource.IBKR,
        gate_id="broker.place_order",
        message="Order rejected",
        external_code=201,
        external_message="insufficient buying power",
    )


def _gate_step(gate_result: GateStepResult = GateStepResult.BLOCK) -> GateStep:
    return GateStep(
        evaluation_id="eval-1",
        gate_id="daily_order_cap",
        gate_result=gate_result,
        source_authority=SourceAuthority.ENGINE_LOOP,
        facts={"orders_used": 3, "orders_cap": 3},
    )


def test_event_type_vocabulary_matches_slice_minus_one_contract() -> None:
    assert {value.value for value in BotEventType} == {
        "evaluation_idle",
        "signal_fired",
        "order_submitted",
        "order_filled",
        "order_cancelled",
        "order_rejected",
        "blocked",
        "halted",
        "launch_failed",
    }


def test_raw_type_vocabulary_tracks_row_types_plus_gate_step() -> None:
    assert {value.value for value in BotEventRawType} == {"gate_step"} | {value.value for value in BotEventType}


def test_identity_requires_at_least_one_identity_field() -> None:
    with pytest.raises(ValidationError):
        BotEventIdentity()


def test_identity_rejects_empty_string_tokens() -> None:
    with pytest.raises(ValidationError):
        BotEventIdentity(order_ref="")


def test_raw_gate_step_requires_gate_step_child() -> None:
    with pytest.raises(ValidationError):
        BotEventRaw(
            seq=1,
            ts_ms=1_700_000_000_000,
            strategy_instance_id="bot-a",
            run_id="run-a",
            event_type=BotEventRawType.GATE_STEP,
            source_authority=SourceAuthority.ENGINE_LOOP,
            identity=_identity(),
        )


def test_raw_events_reject_mismatched_child_payloads() -> None:
    with pytest.raises(ValidationError):
        BotEventRaw(
            seq=1,
            ts_ms=1_700_000_000_000,
            strategy_instance_id="bot-a",
            run_id="run-a",
            event_type=BotEventRawType.ORDER_FILLED,
            source_authority=SourceAuthority.BROKER_SESSION,
            identity=_identity(),
            gate_step=_gate_step(),
        )

    with pytest.raises(ValidationError):
        BotEventRaw(
            seq=1,
            ts_ms=1_700_000_000_000,
            strategy_instance_id="bot-a",
            run_id="run-a",
            event_type=BotEventRawType.ORDER_FILLED,
            source_authority=SourceAuthority.BROKER_SESSION,
            identity=_identity(),
            terminal_error=_terminal_error(),
        )


def test_raw_gate_step_round_trips_with_enforcement_identity() -> None:
    raw = BotEventRaw(
        seq=1,
        ts_ms=1_700_000_000_000,
        strategy_instance_id="bot-a",
        run_id="run-a",
        event_type=BotEventRawType.GATE_STEP,
        source_authority=SourceAuthority.ENGINE_LOOP,
        identity=_identity(),
        gate_step=_gate_step(),
    )

    assert BotEventRaw.model_validate_json(raw.model_dump_json()) == raw


@pytest.mark.parametrize(
    "event_type",
    (BotEventType.ORDER_REJECTED, BotEventType.HALTED, BotEventType.LAUNCH_FAILED),
)
def test_terminal_rows_require_terminal_error(event_type: BotEventType) -> None:
    with pytest.raises(ValidationError):
        BotEventRow(
            seq=1,
            ts_ms=1_700_000_000_000,
            event_type=event_type,
            source_authority=SourceAuthority.BROKER_SESSION,
            identity=BotEventIdentity(order_ref="learn-ai/bot-a/v1:intent-1"),
            severity=BotEventSeverity.CRITICAL,
            headline="Order rejected",
            narrative="IBKR rejected the order.",
        )


def test_rows_reject_terminal_error_on_non_terminal_event() -> None:
    with pytest.raises(ValidationError):
        BotEventRow(
            seq=1,
            ts_ms=1_700_000_000_000,
            event_type=BotEventType.ORDER_FILLED,
            source_authority=SourceAuthority.BROKER_SESSION,
            identity=BotEventIdentity(exec_id="exec-1"),
            severity=BotEventSeverity.INFO,
            headline="Order filled",
            narrative="The order filled.",
            terminal_error=_terminal_error(),
        )


def test_blocked_rows_require_blocking_gate_step() -> None:
    with pytest.raises(ValidationError):
        BotEventRow(
            seq=1,
            ts_ms=1_700_000_000_000,
            event_type=BotEventType.BLOCKED,
            source_authority=SourceAuthority.ENGINE_LOOP,
            identity=_identity(),
            severity=BotEventSeverity.WARNING,
            headline="Evaluation blocked",
            narrative="The order cap blocked the evaluation.",
            gate_steps=(_gate_step(GateStepResult.PASS),),
        )

    row = BotEventRow(
        seq=1,
        ts_ms=1_700_000_000_000,
        event_type=BotEventType.BLOCKED,
        source_authority=SourceAuthority.ENGINE_LOOP,
        identity=_identity(),
        severity=BotEventSeverity.WARNING,
        headline="Evaluation blocked",
        narrative="The order cap blocked the evaluation.",
        gate_steps=(_gate_step(GateStepResult.BLOCK),),
    )

    assert row.gate_steps[0].gate_result is GateStepResult.BLOCK


def test_terminal_row_round_trips_with_error_and_identity_ladder() -> None:
    row = BotEventRow(
        seq=1,
        ts_ms=1_700_000_000_000,
        event_type=BotEventType.ORDER_REJECTED,
        source_authority=SourceAuthority.BROKER_SESSION,
        identity=BotEventIdentity(
            evaluation_id="eval-1",
            intent_id="intent-1",
            order_ref="learn-ai/bot-a/v1:intent-1",
            req_id=42,
            order_id=100,
            perm_id=200,
            exec_id="exec-1",
        ),
        severity=BotEventSeverity.CRITICAL,
        headline="Order rejected",
        narrative="IBKR rejected the order.",
        terminal_error=_terminal_error(),
    )

    assert BotEventRow.model_validate_json(row.model_dump_json()) == row


def test_incident_dedupe_key_requires_order_or_evaluation_identity() -> None:
    with pytest.raises(ValidationError):
        IncidentDedupeKey(
            strategy_instance_id="bot-a",
            terminal_code=TerminalErrorCode.ORDER_REJECTED,
        )


def test_incident_dedupe_key_accepts_broker_identity() -> None:
    key = IncidentDedupeKey(
        strategy_instance_id="bot-a",
        terminal_code=TerminalErrorCode.ORDER_REJECTED,
        req_id=42,
    )

    assert key.req_id == 42


def test_incident_dedupe_key_round_trips() -> None:
    key = IncidentDedupeKey(
        strategy_instance_id="bot-a",
        terminal_code=TerminalErrorCode.ORDER_REJECTED,
        order_ref="learn-ai/bot-a/v1:intent-1",
    )

    assert IncidentDedupeKey.model_validate_json(key.model_dump_json()) == key
