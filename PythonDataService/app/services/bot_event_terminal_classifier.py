"""Pure terminal-error classifier for ADR-0024 Bot events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.broker.ibkr.client import (
    BrokerError,
    ConnectionRefusedDueToSentinelError,
    IbkrClientIdInUseError,
    NotConnectedError,
)
from app.broker.ibkr.orders import OrderRefusedDuringReconnectRecoveryError, OrderRefusedError
from app.schemas.bot_events import FactValue, TerminalError, TerminalErrorCode, TerminalErrorSource

TerminalNoticeCode = Literal[
    "submit.uncertain",
    "submit.halted",
    "submit.launch_failed",
    "submit.unmapped_diagnostic",
]


@dataclass(frozen=True)
class TerminalExceptionClassification:
    terminal_error: TerminalError
    notice_code: TerminalNoticeCode
    title: str
    message: str
    action_kind: Literal["none", "external_manual_check", "redeploy"]


def classify_terminal_exception(exc: BaseException) -> TerminalExceptionClassification:
    """Classify the most granular exception in a terminal failure chain."""

    root, cause_chain = _innermost_cause(exc)
    terminal_error = _terminal_error_for(root, cause_chain=cause_chain, wrapper=exc if root is not exc else None)
    return _classification_for(terminal_error)


def known_terminal_exception_type_names() -> tuple[str, ...]:
    """Closed exception names the classifier intentionally handles."""

    classes = _live_engine_exception_classes() + _live_portfolio_exception_classes()
    classes += (
        BrokerError,
        ConnectionRefusedDueToSentinelError,
        IbkrClientIdInUseError,
        NotConnectedError,
        OrderRefusedDuringReconnectRecoveryError,
        OrderRefusedError,
    )
    return tuple(sorted({cls.__name__ for cls in classes}))


def _innermost_cause(exc: BaseException) -> tuple[BaseException, tuple[str, ...]]:
    chain: list[BaseException] = [exc]
    current = exc
    while current.__cause__ is not None:
        current = current.__cause__
        chain.append(current)
    return current, tuple(f"{type(item).__name__}: {item}" for item in chain)


def _terminal_error_for(
    exc: BaseException,
    *,
    cause_chain: tuple[str, ...],
    wrapper: BaseException | None,
) -> TerminalError:
    from app.engine.live.halt import FatalHaltError
    from app.engine.live.live_engine import (
        BrokerRecoveryReconcileBlockedError,
        BrokerSafetyVerdictTransitionHaltError,
        CancelConfirmTimeoutHaltError,
        MaxOrdersPerDayExceeded,
        ReconnectAccountMismatchHaltError,
    )
    from app.engine.live.live_portfolio import (
        AccountFreezeBlockError,
        AccountRegistryBlockError,
        AccountTruthBlockError,
        BrokerSafetyVerdictBlockError,
        ControlledLiveHaltError,
        LiveBrokerEventStreamError,
        SubmitUncertainHaltError,
    )

    facts = _base_facts(exc, wrapper)
    if isinstance(exc, SubmitUncertainHaltError):
        facts.update(
            {
                "intent_id": exc.intent_id,
                "order_ref": exc.order_ref,
                "probe_result": exc.probe_result,
                "retry_count": exc.retry_count,
                "reason": exc.reason,
            }
        )
        return TerminalError(
            code=TerminalErrorCode.SUBMIT_UNCERTAIN,
            source=TerminalErrorSource.ENGINE,
            gate_id="submit.ack_classify",
            message=f"Submit outcome is uncertain: {exc.reason}",
            cause_chain=cause_chain,
            forensic_facts=facts,
        )

    if isinstance(exc, FatalHaltError):
        facts.update(
            {
                "trigger": exc.reason.trigger.value,
                "halted_at_ms": exc.reason.halted_at_ms,
                "last_clean_bar_close_ms": exc.reason.last_clean_bar_close_ms,
                "details": dict(exc.reason.details),
            }
        )
        return TerminalError(
            code=TerminalErrorCode.HALTED,
            source=TerminalErrorSource.ENGINE,
            gate_id=f"fatal_halt.{exc.reason.trigger.value}",
            message=f"Live run halted: {exc.reason.trigger.value}",
            cause_chain=cause_chain,
            forensic_facts=facts,
        )

    if isinstance(exc, AccountFreezeBlockError):
        return _halted_error(
            exc,
            gate_id="account.freeze",
            message="Account freeze blocked submission",
            facts=facts,
            cause_chain=cause_chain,
        )
    if isinstance(exc, AccountRegistryBlockError):
        return _halted_error(
            exc,
            gate_id="account.instance_registry",
            message="Account instance registry blocked submission",
            facts=facts,
            cause_chain=cause_chain,
        )
    if isinstance(exc, AccountTruthBlockError):
        return _halted_error(
            exc,
            gate_id="account.truth",
            message="Account Truth blocked submission",
            facts=facts,
            cause_chain=cause_chain,
        )
    if isinstance(exc, (BrokerSafetyVerdictBlockError, BrokerSafetyVerdictTransitionHaltError)):
        facts["verdict"] = getattr(exc, "verdict", "")
        return _halted_error(
            exc,
            gate_id="broker.safety_verdict",
            message="Broker safety verdict halted submission",
            facts=facts,
            cause_chain=cause_chain,
        )
    if isinstance(exc, CancelConfirmTimeoutHaltError):
        facts["timeout_s"] = exc.timeout_s
        return _halted_error(
            exc,
            gate_id="cancel.confirm_timeout",
            message="Cancel confirmation timed out",
            facts=facts,
            cause_chain=cause_chain,
        )
    if isinstance(exc, ReconnectAccountMismatchHaltError):
        facts.update(
            {
                "ledger_account_id": exc.ledger_account_id,
                "connected_account": exc.connected_account,
                "connection_epoch": exc.connection_epoch,
            }
        )
        return _halted_error(
            exc,
            gate_id="broker.account_identity",
            message="Broker reconnected to a different account",
            facts=facts,
            cause_chain=cause_chain,
        )
    if isinstance(exc, BrokerRecoveryReconcileBlockedError):
        facts["outcome"] = exc.outcome
        return _halted_error(
            exc,
            gate_id="broker.recovery_reconcile",
            message="Broker recovery reconciliation blocked resume",
            facts=facts,
            cause_chain=cause_chain,
        )
    if isinstance(exc, LiveBrokerEventStreamError):
        return _halted_error(
            exc,
            gate_id="broker.event_stream",
            message="Broker event stream stopped unexpectedly",
            facts=facts,
            cause_chain=cause_chain,
        )
    if isinstance(exc, MaxOrdersPerDayExceeded):
        return _halted_error(
            exc,
            gate_id="orders_cap",
            message="Daily order cap halted submission",
            facts=facts,
            cause_chain=cause_chain,
        )
    if isinstance(exc, OrderRefusedDuringReconnectRecoveryError):
        return _halted_error(
            exc,
            gate_id="broker.reconnect_recovery",
            message="Order refused during reconnect recovery",
            facts=facts,
            cause_chain=cause_chain,
        )
    if isinstance(exc, OrderRefusedError):
        return _halted_error(
            exc,
            gate_id="broker.place_order.preflight",
            message="Order was refused before reaching IBKR",
            facts=facts,
            cause_chain=cause_chain,
        )
    if isinstance(exc, IbkrClientIdInUseError):
        return TerminalError(
            code=TerminalErrorCode.LAUNCH_FAILED,
            source=TerminalErrorSource.BROKER_SESSION,
            gate_id="broker.connect.client_id",
            message="IBKR client id is already in use",
            cause_chain=cause_chain,
            forensic_facts=facts,
        )
    if isinstance(exc, ConnectionRefusedDueToSentinelError):
        return TerminalError(
            code=TerminalErrorCode.LAUNCH_FAILED,
            source=TerminalErrorSource.BROKER_SESSION,
            gate_id="broker.connect.account_sentinel",
            message="Broker account sentinel refused the connection",
            cause_chain=cause_chain,
            forensic_facts=facts,
        )
    if isinstance(exc, NotConnectedError):
        return TerminalError(
            code=TerminalErrorCode.HALTED,
            source=TerminalErrorSource.BROKER_SESSION,
            gate_id="broker.connection",
            message="Broker connection was unavailable",
            cause_chain=cause_chain,
            forensic_facts=facts,
        )
    if isinstance(exc, BrokerError):
        return TerminalError(
            code=TerminalErrorCode.UNMAPPED_DIAGNOSTIC,
            source=TerminalErrorSource.IBKR,
            gate_id="broker",
            message="Unmapped broker diagnostic",
            detail=str(exc),
            cause_chain=cause_chain,
            forensic_facts=facts,
        )
    if isinstance(exc, ControlledLiveHaltError):
        return TerminalError(
            code=TerminalErrorCode.UNMAPPED_DIAGNOSTIC,
            source=TerminalErrorSource.ENGINE,
            gate_id="submit.controlled_halt",
            message="Unmapped controlled halt diagnostic",
            detail=str(exc),
            cause_chain=cause_chain,
            forensic_facts=facts,
        )
    return TerminalError(
        code=TerminalErrorCode.UNMAPPED_DIAGNOSTIC,
        source=TerminalErrorSource.UNKNOWN,
        message="Unmapped terminal diagnostic",
        detail=str(exc),
        cause_chain=cause_chain,
        forensic_facts=facts,
    )


def _classification_for(error: TerminalError) -> TerminalExceptionClassification:
    if error.code is TerminalErrorCode.SUBMIT_UNCERTAIN:
        return TerminalExceptionClassification(
            terminal_error=error,
            notice_code="submit.uncertain",
            title="Submit outcome is uncertain",
            message=error.message,
            action_kind="external_manual_check",
        )
    if error.code is TerminalErrorCode.LAUNCH_FAILED:
        return TerminalExceptionClassification(
            terminal_error=error,
            notice_code="submit.launch_failed",
            title="Bot launch failed",
            message=error.message,
            action_kind="redeploy",
        )
    if error.code is TerminalErrorCode.UNMAPPED_DIAGNOSTIC:
        return TerminalExceptionClassification(
            terminal_error=error,
            notice_code="submit.unmapped_diagnostic",
            title="Unmapped terminal diagnostic",
            message=error.detail or error.message,
            action_kind="external_manual_check",
        )
    return TerminalExceptionClassification(
        terminal_error=error,
        notice_code="submit.halted",
        title="Bot halted before submit",
        message=error.message,
        action_kind="none",
    )


def _halted_error(
    exc: BaseException,
    *,
    gate_id: str,
    message: str,
    facts: dict[str, FactValue],
    cause_chain: tuple[str, ...],
) -> TerminalError:
    return TerminalError(
        code=TerminalErrorCode.HALTED,
        source=TerminalErrorSource.ENGINE,
        gate_id=gate_id,
        message=message,
        detail=str(exc),
        cause_chain=cause_chain,
        forensic_facts=facts,
    )


def _base_facts(exc: BaseException, wrapper: BaseException | None) -> dict[str, FactValue]:
    facts: dict[str, FactValue] = {
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
    }
    if wrapper is not None:
        facts["wrapper_exception_type"] = type(wrapper).__name__
        facts["wrapper_exception_message"] = str(wrapper)
    return facts


def _live_engine_exception_classes() -> tuple[type[BaseException], ...]:
    from app.engine.live.live_engine import (
        BrokerRecoveryReconcileBlockedError,
        BrokerSafetyVerdictTransitionHaltError,
        CancelConfirmTimeoutHaltError,
        MaxOrdersPerDayExceeded,
        ReconnectAccountMismatchHaltError,
    )

    return (
        BrokerRecoveryReconcileBlockedError,
        BrokerSafetyVerdictTransitionHaltError,
        CancelConfirmTimeoutHaltError,
        MaxOrdersPerDayExceeded,
        ReconnectAccountMismatchHaltError,
    )


def _live_portfolio_exception_classes() -> tuple[type[BaseException], ...]:
    from app.engine.live.halt import FatalHaltError
    from app.engine.live.live_portfolio import (
        AccountFreezeBlockError,
        AccountRegistryBlockError,
        AccountTruthBlockError,
        BrokerSafetyVerdictBlockError,
        ControlledLiveHaltError,
        LiveBrokerEventStreamError,
        SubmitGateBlockError,
        SubmitUncertainHaltError,
    )

    return (
        AccountFreezeBlockError,
        AccountRegistryBlockError,
        AccountTruthBlockError,
        BrokerSafetyVerdictBlockError,
        ControlledLiveHaltError,
        FatalHaltError,
        LiveBrokerEventStreamError,
        SubmitGateBlockError,
        SubmitUncertainHaltError,
    )
