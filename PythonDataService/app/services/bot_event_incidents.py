"""Operator incidents authored from terminal Bot event raw captures."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from app.operator.incidents.store import IncidentStore
from app.operator.notices.schema import (
    OperatorIncident,
    OperatorNotice,
    OperatorNoticeAction,
    OperatorNoticeCode,
)
from app.schemas.bot_events import (
    BotEventRaw,
    IncidentDedupeKey,
    TerminalError,
    TerminalErrorCode,
)

IncidentCategory = Literal["order", "submit"]


@dataclass(frozen=True)
class _IncidentTemplate:
    category: IncidentCategory
    notice_code: OperatorNoticeCode
    title: str
    action_kind: Literal["none", "external_manual_check", "redeploy"]
    runbook_slug: str
    action_label: str | None = None
    action_target: str | None = None

    def build_action(self) -> OperatorNoticeAction:
        return OperatorNoticeAction(
            kind=self.action_kind,
            label=self.action_label,
            target=self.action_target,
        )


def append_terminal_incident(
    incident_store: IncidentStore,
    raw_event: BotEventRaw,
) -> OperatorIncident:
    """Persist the terminal incident for ``raw_event`` and return it.

    The incident id is a deterministic hash of the ADR-0024 terminal
    dedupe key, so repeated live/backfill observations replace the same
    file instead of producing duplicate visible stories.
    """

    incident = build_terminal_incident(raw_event)
    incident_store.append(incident)
    return incident


def build_terminal_incident(raw_event: BotEventRaw) -> OperatorIncident:
    terminal_error = raw_event.terminal_error
    if terminal_error is None:
        raise ValueError("terminal incident requires terminal_error")
    template = _template_for(terminal_error)
    identity = raw_event.identity
    return OperatorIncident(
        incident_id=terminal_incident_id(terminal_incident_dedupe_key(raw_event)),
        category=template.category,
        notice=OperatorNotice(
            code=template.notice_code,
            tier="critical",
            title=template.title,
            message=_message_for(terminal_error),
            source_codes=[str(terminal_error.external_code)]
            if terminal_error.external_code is not None
            else [],
            forensic_facts={
                "bot_event_seq": raw_event.seq,
                "evaluation_id": identity.evaluation_id,
                "intent_id": identity.intent_id,
                "order_ref": identity.order_ref,
                "req_id": identity.req_id,
                "order_id": identity.order_id,
                "perm_id": identity.perm_id,
                "exec_id": identity.exec_id,
                "terminal_code": terminal_error.code.value,
                "terminal_source": terminal_error.source.value,
                "gate_id": terminal_error.gate_id,
                "external_code": terminal_error.external_code,
                "external_message": terminal_error.external_message,
            },
            action=template.build_action(),
            runbook_slug=template.runbook_slug,
            occurred_at_ms=raw_event.ts_ms,
        ),
        started_at_ms=raw_event.ts_ms,
        evidence={
            "bot_event_seq": raw_event.seq,
            "strategy_instance_id": raw_event.strategy_instance_id,
            "run_id": raw_event.run_id,
            "evaluation_id": identity.evaluation_id,
            "order_ref": identity.order_ref,
            "req_id": identity.req_id,
            "order_id": identity.order_id,
            "perm_id": identity.perm_id,
            "terminal_code": terminal_error.code.value,
        },
    )


def terminal_incident_dedupe_key(raw_event: BotEventRaw) -> IncidentDedupeKey:
    if raw_event.terminal_error is None:
        raise ValueError("terminal dedupe key requires terminal_error")
    identity = raw_event.identity
    return IncidentDedupeKey(
        strategy_instance_id=raw_event.strategy_instance_id,
        terminal_code=raw_event.terminal_error.code,
        evaluation_id=identity.evaluation_id,
        order_ref=identity.order_ref,
        req_id=identity.req_id,
        order_id=identity.order_id,
        perm_id=identity.perm_id,
    )


def terminal_incident_id(key: IncidentDedupeKey) -> str:
    digest = hashlib.sha256(key.model_dump_json().encode("utf-8")).hexdigest()[:16]
    prefix = _INCIDENT_ID_PREFIX.get(key.terminal_code)
    if prefix is None:
        raise ValueError(f"unmapped terminal incident id prefix for {key.terminal_code.value}")
    return f"{prefix}-{digest}"


def _template_for(error: TerminalError) -> _IncidentTemplate:
    return _TEMPLATES[error.code]


def _message_for(error: TerminalError) -> str:
    detail = error.external_message or error.detail or error.message
    if error.code is TerminalErrorCode.ORDER_REJECTED:
        return (
            "IBKR rejected an order from this bot. Review the broker "
            f"message before retrying: {detail}"
        )
    if error.code is TerminalErrorCode.SUBMIT_UNCERTAIN:
        return f"The bot could not prove whether the submitted order exists: {detail}"
    if error.code is TerminalErrorCode.LAUNCH_FAILED:
        return f"The bot failed before it could start trading: {detail}"
    if error.code is TerminalErrorCode.UNMAPPED_DIAGNOSTIC:
        return f"A terminal failure used an unmapped diagnostic shape: {detail}"
    if error.code is TerminalErrorCode.HALTED:
        return f"The bot halted before it could continue trading: {detail}"
    raise ValueError(f"unmapped terminal incident message for {error.code.value}")


_INCIDENT_ID_PREFIX: dict[TerminalErrorCode, str] = {
    TerminalErrorCode.ORDER_REJECTED: "order-rejected",
    TerminalErrorCode.SUBMIT_UNCERTAIN: "submit-uncertain",
    TerminalErrorCode.HALTED: "submit-halted",
    TerminalErrorCode.LAUNCH_FAILED: "submit-launch-failed",
    TerminalErrorCode.UNMAPPED_DIAGNOSTIC: "submit-unmapped-diagnostic",
}

_TEMPLATES: dict[TerminalErrorCode, _IncidentTemplate] = {
    TerminalErrorCode.ORDER_REJECTED: _IncidentTemplate(
        category="order",
        notice_code="order.rejected",
        title="IBKR rejected the order",
        action_kind="external_manual_check",
        action_label="Review in IBKR",
        action_target="ibkr_order_rejection",
        runbook_slug="ibkr-order-rejection",
    ),
    TerminalErrorCode.SUBMIT_UNCERTAIN: _IncidentTemplate(
        category="submit",
        notice_code="submit.uncertain",
        title="Submit outcome is uncertain",
        action_kind="external_manual_check",
        action_label="Verify in IBKR",
        action_target="ibkr_order_status",
        runbook_slug="submit-outcome-uncertain",
    ),
    TerminalErrorCode.HALTED: _IncidentTemplate(
        category="submit",
        notice_code="submit.halted",
        title="Bot halted before submit",
        action_kind="none",
        runbook_slug="bot-halted",
    ),
    TerminalErrorCode.LAUNCH_FAILED: _IncidentTemplate(
        category="submit",
        notice_code="submit.launch_failed",
        title="Bot launch failed",
        action_kind="redeploy",
        action_label="Redeploy bot",
        action_target="redeploy",
        runbook_slug="bot-launch-failed",
    ),
    TerminalErrorCode.UNMAPPED_DIAGNOSTIC: _IncidentTemplate(
        category="submit",
        notice_code="submit.unmapped_diagnostic",
        title="Unmapped terminal diagnostic",
        action_kind="external_manual_check",
        action_label="Review run logs",
        action_target="run_logs",
        runbook_slug="unmapped-terminal-diagnostic",
    ),
}


__all__ = [
    "append_terminal_incident",
    "build_terminal_incident",
    "terminal_incident_dedupe_key",
    "terminal_incident_id",
]
