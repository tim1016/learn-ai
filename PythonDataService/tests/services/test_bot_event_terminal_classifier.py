"""Tests for the ADR-0024 terminal exception classifier."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.ibkr.client import BrokerError, IbkrClientIdInUseError
from app.broker.ibkr.orders import OrderRefusedError
from app.engine.live.halt import FatalHaltError, PoisonedHaltReason, PoisonedHaltTrigger
from app.engine.live.live_engine import (
    BrokerSafetyVerdictTransitionHaltError,
    CancelConfirmTimeoutHaltError,
    ReconnectAccountMismatchHaltError,
)
from app.engine.live.live_portfolio import (
    AccountFreezeBlockError,
    ControlledLiveHaltError,
    SubmitUncertainHaltError,
)
from app.schemas.bot_events import TerminalErrorCode, TerminalErrorSource
from app.services.bot_event_terminal_classifier import (
    classify_terminal_exception,
    known_terminal_exception_type_names,
)


def test_known_terminal_exception_snapshot() -> None:
    assert known_terminal_exception_type_names() == (
        "AccountFreezeBlockError",
        "AccountRegistryBlockError",
        "AccountTruthBlockError",
        "BrokerError",
        "BrokerRecoveryReconcileBlockedError",
        "BrokerSafetyVerdictBlockError",
        "BrokerSafetyVerdictTransitionHaltError",
        "CancelConfirmTimeoutHaltError",
        "ConnectionRefusedDueToSentinelError",
        "ControlledLiveHaltError",
        "FatalHaltError",
        "IbkrClientIdInUseError",
        "LiveBrokerEventStreamError",
        "MaxOrdersPerDayExceeded",
        "NotConnectedError",
        "OrderRefusedDuringReconnectRecoveryError",
        "OrderRefusedError",
        "ReconnectAccountMismatchHaltError",
        "SubmitGateBlockError",
        "SubmitUncertainHaltError",
    )


def test_submit_uncertain_halt_maps_to_uncertain_terminal_notice() -> None:
    result = classify_terminal_exception(
        SubmitUncertainHaltError(
            intent_id="intent-1",
            order_ref="learn-ai/sid/v1:intent-1",
            probe_result="not_provable",
            retry_count=1,
            reason="probe could not prove absence",
        )
    )

    assert result.notice_code == "submit.uncertain"
    assert result.action_kind == "external_manual_check"
    assert result.terminal_error.code is TerminalErrorCode.SUBMIT_UNCERTAIN
    assert result.terminal_error.gate_id == "submit.ack_classify"
    assert result.terminal_error.forensic_facts["order_ref"] == "learn-ai/sid/v1:intent-1"


@pytest.mark.parametrize(
    ("exc", "gate_id"),
    (
        (AccountFreezeBlockError(evidence=SimpleNamespace(reason="manual_freeze")), "account.freeze"),
        (BrokerSafetyVerdictTransitionHaltError(verdict="unsafe"), "broker.safety_verdict"),
        (CancelConfirmTimeoutHaltError(timeout_s=5.0), "cancel.confirm_timeout"),
        (
            ReconnectAccountMismatchHaltError(
                ledger_account_id="DU123",
                connected_account="DU999",
                connection_epoch=2,
            ),
            "broker.account_identity",
        ),
        (
            FatalHaltError(
                PoisonedHaltReason(
                    trigger=PoisonedHaltTrigger.LOST_FILL,
                    halted_at_ms=1_700_000_000_000,
                    last_clean_bar_close_ms=1_699_999_940_000,
                    details={"order_id": 42},
                )
            ),
            "fatal_halt.lost_fill",
        ),
    ),
)
def test_known_halts_map_to_halted_terminal_notice(exc: BaseException, gate_id: str) -> None:
    result = classify_terminal_exception(exc)

    assert result.notice_code == "submit.halted"
    assert result.terminal_error.code is TerminalErrorCode.HALTED
    assert result.terminal_error.gate_id == gate_id


def test_launch_broker_session_error_maps_to_launch_failed_notice() -> None:
    result = classify_terminal_exception(IbkrClientIdInUseError("client id already in use"))

    assert result.notice_code == "submit.launch_failed"
    assert result.action_kind == "redeploy"
    assert result.terminal_error.code is TerminalErrorCode.LAUNCH_FAILED
    assert result.terminal_error.source is TerminalErrorSource.BROKER_SESSION
    assert result.terminal_error.gate_id == "broker.connect.client_id"


def test_classifier_walks_to_innermost_cause() -> None:
    try:
        try:
            raise OrderRefusedError("IBKR_READONLY must be false")
        except OrderRefusedError as exc:
            raise RuntimeError("broker.place_order raised") from exc
    except RuntimeError as wrapped:
        result = classify_terminal_exception(wrapped)

    assert result.notice_code == "submit.halted"
    assert result.terminal_error.gate_id == "broker.place_order.preflight"
    assert result.terminal_error.forensic_facts["wrapper_exception_type"] == "RuntimeError"
    assert [entry.split(":", 1)[0] for entry in result.terminal_error.cause_chain] == [
        "RuntimeError",
        "OrderRefusedError",
    ]


def test_unmapped_broker_error_is_visible_diagnostic() -> None:
    result = classify_terminal_exception(BrokerError("new broker code shape"))

    assert result.notice_code == "submit.unmapped_diagnostic"
    assert result.action_kind == "external_manual_check"
    assert result.terminal_error.code is TerminalErrorCode.UNMAPPED_DIAGNOSTIC
    assert result.terminal_error.source is TerminalErrorSource.IBKR
    assert result.terminal_error.detail == "new broker code shape"


def test_unmapped_controlled_halt_is_visible_diagnostic() -> None:
    result = classify_terminal_exception(ControlledLiveHaltError("new controlled halt"))

    assert result.notice_code == "submit.unmapped_diagnostic"
    assert result.terminal_error.code is TerminalErrorCode.UNMAPPED_DIAGNOSTIC
    assert result.terminal_error.gate_id == "submit.controlled_halt"
