from __future__ import annotations

from pathlib import Path

import pytest

from app.operator.notices.schema import (
    NOTICE_CODE_CONTRACTS,
    OperatorIncident,
    OperatorNotice,
    OperatorNoticeAction,
    OperatorNoticeActionability,
    OperatorNoticeCode,
    OperatorNoticeRemedyStatus,
    OperatorNoticeTier,
    RuntimeFreshnessReasonCode,
)
from tests.operator._helpers import get_literal_args


def _baseline_action() -> OperatorNoticeAction:
    return OperatorNoticeAction(kind="none")


def _baseline_notice() -> OperatorNotice:
    return OperatorNotice(
        code="runtime.market_closed",
        tier="info",
        title="Market closed",
        message="The bot is idle until the regular trading session opens.",
        actionability="self_resolving",
        resolution="Clears automatically when the regular trading session opens.",
        action=_baseline_action(),
    )


def test_tier_literal_is_three_values():
    assert set(get_literal_args(OperatorNoticeTier)) == {"info", "warning", "critical"}


def test_actionability_literal_is_four_values():
    assert set(get_literal_args(OperatorNoticeActionability)) == {
        "actuatable",
        "routed",
        "self_resolving",
        "no_remedy",
    }


def test_remedy_status_literal_is_two_values():
    assert set(get_literal_args(OperatorNoticeRemedyStatus)) == {"inherent", "unbuilt"}


def test_action_kind_is_six_values():
    assert set(get_literal_args(OperatorNoticeAction.model_fields["kind"].annotation)) == {
        "none",
        "open_runbook",
        "focus_cockpit_action",
        "renew_control_plane_lease",
        "external_manual_check",
        "redeploy",
    }


def test_code_literal_declares_pr1_runtime_slots():
    codes = set(get_literal_args(OperatorNoticeCode))
    runtime_slots = {
        "runtime.market_closed",
        "runtime.market_session_halted",
        "runtime.market_data_stale",
        "runtime.market_data_first_bar_timeout",
        "runtime.market_data_feed_stalled",
        "runtime.broker_probe_stale",
        "runtime.broker_probe_missing",
        "runtime.command_loop_unresponsive",
        "runtime.engine_runtime_incompatible",
        "runtime.control_plane_lease_stale",
        "runtime.control_plane_boot_id_mismatch",
    }
    assert runtime_slots <= codes


def test_code_literal_declares_pr2_watchdog_slots():
    codes = set(get_literal_args(OperatorNoticeCode))
    watchdog_slots = {
        "watchdog.flatten_completed",
        "watchdog.flatten_not_needed",
        "watchdog.flatten_timed_out",
        "watchdog.flatten_failed",
        "watchdog.broker_disconnected_before_flatten",
    }
    assert watchdog_slots <= codes


def test_code_literal_declares_pr5_activity_slots():
    codes = set(get_literal_args(OperatorNoticeCode))
    activity_slots = {
        "activity.publisher_starting",
        "activity.publisher_not_running",
        "activity.publisher_degraded",
        "activity.source_blind_to_bot_orders",
        "activity.dropped_paused_intent",
    }
    assert activity_slots <= codes


def test_code_literal_declares_pr6_reconciliation_slots():
    codes = set(get_literal_args(OperatorNoticeCode))
    reconciliation_slots = {
        "reconciliation.required_after_uncertain_flatten",
        "reconciliation.discovered_execution_not_in_engine_state",
        "reconciliation.divergence_while_submitting",
    }
    assert reconciliation_slots <= codes


def test_code_literal_declares_fleet_slots():
    codes = set(get_literal_args(OperatorNoticeCode))
    assert "fleet.sibling_liveness_unproven" in codes


def test_code_literal_declares_safety_halt_slot():
    assert "safety_halt.poisoned" in set(get_literal_args(OperatorNoticeCode))


def test_code_literal_declares_broker_session_slots():
    codes = set(get_literal_args(OperatorNoticeCode))
    assert "broker_session.orphaned_socket" in codes


def test_code_literal_declares_order_submit_slots():
    codes = set(get_literal_args(OperatorNoticeCode))
    order_submit_slots = {
        "order.rejected",
        "submit.uncertain",
        "submit.halted",
        "submit.launch_failed",
        "submit.unmapped_diagnostic",
    }
    assert order_submit_slots <= codes


def test_runtime_freshness_reason_code_literal_has_thirteen_members():
    codes = set(get_literal_args(RuntimeFreshnessReasonCode))
    assert codes == {
        "ENGINE_RUNTIME_MISSING",
        "ENGINE_RUNTIME_INVALID_OR_INCOMPATIBLE",
        "COMMAND_LOOP_STALE",
        "BROKER_PROBE_STALE",
        "BROKER_PROBE_MISSING",
        "BAR_LOOP_HEARTBEAT_STALE",
        "BAR_LOOP_FIRST_BAR_TIMEOUT",
        "BAR_LOOP_SOURCE_MISSING",
        "BAR_LOOP_LATEST_BAR_STALE",
        "BAR_LOOP_SESSION_CLOSED",
        "BAR_LOOP_SESSION_HALTED",
        "CONTROL_PLANE_LEASE_STALE",
        "CONTROL_PLANE_BOOT_ID_MISMATCH",
    }


def test_notice_round_trips_through_pydantic():
    notice = _baseline_notice()
    parsed = OperatorNotice.model_validate(notice.model_dump())
    assert parsed == notice


def test_notice_rejects_unknown_code():
    with pytest.raises(ValueError):
        OperatorNotice.model_validate(
            {
                **_baseline_notice().model_dump(),
                "code": "runtime.unknown_code_not_in_literal",
            }
        )


def test_notice_forensic_facts_accept_scalar_values_only():
    notice = OperatorNotice(
        code="runtime.market_data_stale",
        tier="warning",
        title="Market data is stale",
        message="No fresh bar has arrived for 92 seconds.",
        forensic_facts={"age_ms": 92_000, "expected_window_ms": 30_000, "feed": "polygon"},
        actionability="routed",
        resolution="Clears when a fresh IBKR bar arrives inside the freshness window.",
        action=OperatorNoticeAction(
            kind="external_manual_check",
            label="Check IBKR market data",
            target="ibkr_connection",
        ),
    )
    assert notice.forensic_facts["age_ms"] == 92_000


def test_notice_rejects_missing_resolution():
    payload = _baseline_notice().model_dump()
    payload.pop("resolution")
    with pytest.raises(ValueError):
        OperatorNotice.model_validate(payload)


def test_notice_rejects_illegal_actionability_pairing():
    with pytest.raises(ValueError, match="self_resolving notices cannot carry"):
        OperatorNotice(
            code="runtime.market_closed",
            tier="info",
            title="Market closed",
            message="The bot is idle.",
            actionability="self_resolving",
            resolution="Clears when the session opens.",
            action=OperatorNoticeAction(kind="open_runbook", label="Open", target="runtime-freshness"),
        )


def test_notice_rejects_routed_without_target():
    with pytest.raises(ValueError, match="routed notices require a named target"):
        OperatorNotice(
            code="runtime.command_loop_unresponsive",
            tier="critical",
            title="Bot is not responding",
            message="Commands may not take effect.",
            actionability="routed",
            resolution="Clears when command loop evidence is fresh again.",
            action=OperatorNoticeAction(kind="external_manual_check", label="Check positions"),
        )


def test_notice_rejects_no_remedy_without_remedy_status():
    with pytest.raises(ValueError, match="remedy_status='unbuilt'"):
        OperatorNotice(
            code="fleet.sibling_liveness_unproven",
            tier="critical",
            title="Sibling liveness is unproven",
            message="The cockpit cannot prove whether a sibling bot is still alive.",
            actionability="no_remedy",
            resolution="Resolution unknown — requires manual reconciliation.",
            action=OperatorNoticeAction(kind="none"),
        )


def test_notice_code_contract_exhaustive_and_pinned():
    codes = list(get_literal_args(OperatorNoticeCode))
    assert list(NOTICE_CODE_CONTRACTS) == codes
    for code, contract in NOTICE_CODE_CONTRACTS.items():
        if contract.actionability == "no_remedy":
            assert contract.remedy_status in {"inherent", "unbuilt"}
        else:
            assert contract.remedy_status is None


def test_unbuilt_remedies_are_cross_referenced_in_known_gaps():
    known_gaps = (
        Path(__file__).resolve().parents[3] / "docs" / "known-gaps.md"
    ).read_text(encoding="utf-8")
    unbuilt_codes = [
        code
        for code, contract in NOTICE_CODE_CONTRACTS.items()
        if contract.remedy_status == "unbuilt"
    ]
    assert unbuilt_codes
    for code in unbuilt_codes:
        assert code in known_gaps


def test_incident_records_started_and_unresolved_by_default():
    incident = OperatorIncident(
        incident_id="incident-test-1",
        category="watchdog",
        notice=_baseline_notice(),
        started_at_ms=1_750_000_000_000,
    )
    assert incident.resolved_at_ms is None
    assert incident.schema_version == 1


def test_incident_category_is_closed_enum():
    assert set(get_literal_args(OperatorIncident.model_fields["category"].annotation)) == {
        "watchdog",
        "activity",
        "reconciliation",
        "order",
        "submit",
        "safety-halt",
    }
    with pytest.raises(ValueError):
        OperatorIncident.model_validate(
            {
                "incident_id": "incident-test-2",
                "category": "unknown_category",
                "notice": _baseline_notice().model_dump(),
                "started_at_ms": 1_750_000_000_000,
            }
        )
