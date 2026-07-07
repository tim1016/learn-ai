from __future__ import annotations

import pytest

from app.operator.incidents.store import INCIDENTS_DIR, IncidentStore
from app.schemas.bot_events import (
    BotEventIdentity,
    BotEventRaw,
    BotEventRawType,
    SourceAuthority,
    TerminalError,
    TerminalErrorCode,
    TerminalErrorSource,
)
from app.services.bot_event_incidents import (
    append_terminal_incident,
    build_terminal_incident,
)


def _raw_terminal(
    *,
    code: TerminalErrorCode,
    seq: int = 1,
    event_type: BotEventRawType | None = None,
    identity: BotEventIdentity | None = None,
) -> BotEventRaw:
    raw_type = event_type or _raw_type_for(code)
    terminal_source = (
        TerminalErrorSource.IBKR
        if code is TerminalErrorCode.ORDER_REJECTED
        else TerminalErrorSource.ENGINE
    )
    return BotEventRaw(
        seq=seq,
        ts_ms=1_700_000_000_000 + seq,
        strategy_instance_id="sid-terminal-incidents",
        run_id="run-terminal-incidents",
        event_type=raw_type,
        source_authority=SourceAuthority.BROKER_SESSION
        if code is TerminalErrorCode.ORDER_REJECTED
        else SourceAuthority.ENGINE_LOOP,
        identity=identity or BotEventIdentity(evaluation_id="eval-1"),
        terminal_error=TerminalError(
            code=code,
            source=terminal_source,
            gate_id="broker.place_order"
            if code is TerminalErrorCode.ORDER_REJECTED
            else "submit.pipeline",
            message=f"{code.value} message",
            detail=f"{code.value} detail",
            external_code=201 if code is TerminalErrorCode.ORDER_REJECTED else None,
            external_message="Order rejected - insufficient buying power"
            if code is TerminalErrorCode.ORDER_REJECTED
            else None,
        ),
    )


def _raw_type_for(code: TerminalErrorCode) -> BotEventRawType:
    if code is TerminalErrorCode.ORDER_REJECTED:
        return BotEventRawType.ORDER_REJECTED
    if code is TerminalErrorCode.LAUNCH_FAILED:
        return BotEventRawType.LAUNCH_FAILED
    return BotEventRawType.HALTED


def test_order_rejected_terminal_incident_matches_rejection_contract() -> None:
    incident = build_terminal_incident(
        _raw_terminal(
            code=TerminalErrorCode.ORDER_REJECTED,
            identity=BotEventIdentity(order_ref="bot:sid:intent-1", req_id=42, order_id=42),
        )
    )

    assert incident.incident_id.startswith("order-rejected-")
    assert incident.category == "order"
    assert incident.notice.code == "order.rejected"
    assert incident.notice.title == "IBKR rejected the order"
    assert incident.notice.source_codes == ["201"]
    assert incident.notice.action.kind == "external_manual_check"
    assert incident.notice.action.target == "ibkr_order_rejection"
    assert incident.notice.forensic_facts["terminal_code"] == "order_rejected"
    assert incident.evidence["req_id"] == 42


def test_terminal_incident_contract_covers_every_terminal_code() -> None:
    covered_codes = {
        TerminalErrorCode.ORDER_REJECTED,
        TerminalErrorCode.SUBMIT_UNCERTAIN,
        TerminalErrorCode.HALTED,
        TerminalErrorCode.LAUNCH_FAILED,
        TerminalErrorCode.UNMAPPED_DIAGNOSTIC,
    }

    assert covered_codes == set(TerminalErrorCode)


@pytest.mark.parametrize(
    ("code", "notice_code", "action_kind", "incident_prefix"),
    [
        (
            TerminalErrorCode.SUBMIT_UNCERTAIN,
            "submit.uncertain",
            "external_manual_check",
            "submit-uncertain-",
        ),
        (TerminalErrorCode.HALTED, "submit.halted", "none", "submit-halted-"),
        (
            TerminalErrorCode.LAUNCH_FAILED,
            "submit.launch_failed",
            "redeploy",
            "submit-launch-failed-",
        ),
        (
            TerminalErrorCode.UNMAPPED_DIAGNOSTIC,
            "submit.unmapped_diagnostic",
            "external_manual_check",
            "submit-unmapped-diagnostic-",
        ),
    ],
)
def test_submit_terminal_codes_author_submit_incidents(
    code: TerminalErrorCode,
    notice_code: str,
    action_kind: str,
    incident_prefix: str,
) -> None:
    incident = build_terminal_incident(_raw_terminal(code=code))

    assert incident.incident_id.startswith(incident_prefix)
    assert incident.category == "submit"
    assert incident.notice.code == notice_code
    assert incident.notice.action.kind == action_kind
    assert incident.notice.forensic_facts["terminal_code"] == code.value
    assert incident.evidence["terminal_code"] == code.value


def test_terminal_incident_id_is_stable_for_repeated_observations() -> None:
    identity = BotEventIdentity(evaluation_id="eval-1", order_ref="bot:sid:intent-1")
    first = build_terminal_incident(
        _raw_terminal(code=TerminalErrorCode.SUBMIT_UNCERTAIN, seq=1, identity=identity)
    )
    second = build_terminal_incident(
        _raw_terminal(code=TerminalErrorCode.SUBMIT_UNCERTAIN, seq=2, identity=identity)
    )

    assert first.incident_id == second.incident_id


def test_append_terminal_incident_dedupes_to_one_unresolved_file(tmp_path) -> None:
    run_dir = tmp_path / "run-1"
    store = IncidentStore(run_dir)
    identity = BotEventIdentity(evaluation_id="eval-1", order_ref="bot:sid:intent-1")

    first = append_terminal_incident(
        store,
        _raw_terminal(code=TerminalErrorCode.SUBMIT_UNCERTAIN, seq=1, identity=identity),
    )
    second = append_terminal_incident(
        store,
        _raw_terminal(code=TerminalErrorCode.SUBMIT_UNCERTAIN, seq=2, identity=identity),
    )

    incident_files = sorted((run_dir / INCIDENTS_DIR).glob("*.json"))
    unresolved = store.list_unresolved()
    assert second.incident_id == first.incident_id
    assert len(incident_files) == 1
    assert len(unresolved) == 1
    assert unresolved[0].incident_id == first.incident_id
    assert unresolved[0].notice.forensic_facts["bot_event_seq"] == 2


def test_terminal_incident_requires_terminal_error() -> None:
    raw_event = BotEventRaw(
        seq=1,
        ts_ms=1_700_000_000_000,
        strategy_instance_id="sid-terminal-incidents",
        run_id="run-terminal-incidents",
        event_type=BotEventRawType.EVALUATION_IDLE,
        source_authority=SourceAuthority.ENGINE_LOOP,
        identity=BotEventIdentity(evaluation_id="eval-1"),
    )

    with pytest.raises(ValueError, match="terminal incident requires terminal_error"):
        build_terminal_incident(raw_event)
