from __future__ import annotations

import pytest

from app.operator.notices.schema import (
    OperatorIncident,
    OperatorNotice,
    OperatorNoticeAction,
    OperatorNoticeCode,
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
        action=_baseline_action(),
    )


def test_tier_literal_is_three_values():
    assert set(get_literal_args(OperatorNoticeTier)) == {"info", "warning", "critical"}


def test_action_kind_is_six_values():
    assert set(get_literal_args(OperatorNoticeAction.model_fields["kind"].annotation)) == {
        "none",
        "wait",
        "open_runbook",
        "focus_cockpit_action",
        "external_manual_check",
        "redeploy",
    }


def test_code_literal_declares_pr1_runtime_slots():
    codes = set(get_literal_args(OperatorNoticeCode))
    runtime_slots = {
        "runtime.market_closed",
        "runtime.market_session_halted",
        "runtime.market_data_stale",
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
    }
    assert reconciliation_slots <= codes


def test_runtime_freshness_reason_code_literal_has_eleven_members():
    codes = set(get_literal_args(RuntimeFreshnessReasonCode))
    assert codes == {
        "ENGINE_RUNTIME_MISSING",
        "ENGINE_RUNTIME_INVALID_OR_INCOMPATIBLE",
        "COMMAND_LOOP_STALE",
        "BROKER_PROBE_STALE",
        "BROKER_PROBE_MISSING",
        "BAR_LOOP_HEARTBEAT_STALE",
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


def test_notice_facts_accept_scalar_values_only():
    notice = OperatorNotice(
        code="runtime.market_data_stale",
        tier="warning",
        title="Market data is stale",
        message="No fresh bar has arrived for 92 seconds.",
        facts={"age_ms": 92_000, "expected_window_ms": 30_000, "feed": "polygon"},
        action=_baseline_action(),
    )
    assert notice.facts["age_ms"] == 92_000


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
    with pytest.raises(ValueError):
        OperatorIncident.model_validate(
            {
                "incident_id": "incident-test-2",
                "category": "unknown_category",
                "notice": _baseline_notice().model_dump(),
                "started_at_ms": 1_750_000_000_000,
            }
        )
