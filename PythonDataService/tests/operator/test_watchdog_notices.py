"""Tests for watchdog notice builders — five terminal outcomes."""

from __future__ import annotations

from app.operator.incidents.watchdog_notices import (
    broker_disconnected_before_flatten_notice,
    flatten_completed_notice,
    flatten_failed_notice,
    flatten_not_needed_notice,
    flatten_timed_out_notice,
    watchdog_incident,
)
from app.operator.notices.schema import OperatorNotice

# ---------------------------------------------------------------------------
# flatten_completed
# ---------------------------------------------------------------------------


def test_flatten_completed_notice_code_and_tier() -> None:
    n = flatten_completed_notice()
    assert n.code == "watchdog.flatten_completed"
    assert n.tier == "info"


def test_flatten_completed_notice_has_content() -> None:
    n = flatten_completed_notice(flatten_ms=3_400, occurred_at_ms=1_700_000_000_000)
    assert n.title
    assert n.message
    assert n.action.kind == "none"
    assert n.runbook_slug == "watchdog-halt"
    assert n.forensic_facts["flatten_ms"] == 3_400
    assert n.occurred_at_ms == 1_700_000_000_000


# ---------------------------------------------------------------------------
# flatten_not_needed
# ---------------------------------------------------------------------------


def test_flatten_not_needed_notice_code_and_tier() -> None:
    n = flatten_not_needed_notice()
    assert n.code == "watchdog.flatten_not_needed"
    assert n.tier == "info"


def test_flatten_not_needed_notice_has_content() -> None:
    n = flatten_not_needed_notice(occurred_at_ms=1_700_000_000_000)
    assert n.title
    assert n.message
    assert n.action.kind == "none"
    assert n.runbook_slug == "watchdog-halt"


# ---------------------------------------------------------------------------
# flatten_timed_out
# ---------------------------------------------------------------------------


def test_flatten_timed_out_notice_code_and_tier() -> None:
    n = flatten_timed_out_notice()
    assert n.code == "watchdog.flatten_timed_out"
    assert n.tier == "critical"


def test_flatten_timed_out_notice_has_content() -> None:
    n = flatten_timed_out_notice(flatten_timeout_ms=20_000, occurred_at_ms=1_700_000_000_000)
    assert n.title
    assert n.message
    assert n.action.kind == "external_manual_check"
    assert n.action.label
    assert n.runbook_slug == "watchdog-halt"
    assert n.forensic_facts["flatten_timeout_ms"] == 20_000


# ---------------------------------------------------------------------------
# flatten_failed
# ---------------------------------------------------------------------------


def test_flatten_failed_notice_code_and_tier() -> None:
    n = flatten_failed_notice()
    assert n.code == "watchdog.flatten_failed"
    assert n.tier == "critical"


def test_flatten_failed_notice_has_content() -> None:
    n = flatten_failed_notice(error_summary="TimeoutError()", occurred_at_ms=1_700_000_000_000)
    assert n.title
    assert n.message
    assert n.action.kind == "external_manual_check"
    assert n.action.label
    assert n.runbook_slug == "watchdog-halt"
    assert n.forensic_facts["error_summary"] == "TimeoutError()"


# ---------------------------------------------------------------------------
# broker_disconnected_before_flatten
# ---------------------------------------------------------------------------


def test_broker_disconnected_before_flatten_notice_code_and_tier() -> None:
    n = broker_disconnected_before_flatten_notice()
    assert n.code == "watchdog.broker_disconnected_before_flatten"
    assert n.tier == "critical"


def test_broker_disconnected_before_flatten_notice_has_content() -> None:
    n = broker_disconnected_before_flatten_notice(occurred_at_ms=1_700_000_000_000)
    assert n.title
    assert n.message
    assert n.action.kind == "external_manual_check"
    assert n.action.label
    assert n.runbook_slug == "watchdog-halt"


# ---------------------------------------------------------------------------
# watchdog_incident scaffold
# ---------------------------------------------------------------------------


def test_watchdog_incident_has_watchdog_category() -> None:
    incident = watchdog_incident(
        reason="LEASE_EXPIRED",
        started_at_ms=1_700_000_000_000,
    )
    assert incident.category == "watchdog"
    assert incident.started_at_ms == 1_700_000_000_000
    assert incident.resolved_at_ms is None


def test_watchdog_incident_id_is_deterministic_format() -> None:
    incident = watchdog_incident(
        reason="BOOT_ID_CHANGED",
        started_at_ms=1_700_000_000_000,
    )
    assert incident.incident_id.startswith("watchdog-1700000000000-")
    # UUID suffix: 8 hex chars
    suffix = incident.incident_id.split("-")[-1]
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)


def test_watchdog_incident_scaffold_notice_is_pessimistic_critical() -> None:
    incident = watchdog_incident(
        reason="LEASE_EXPIRED",
        started_at_ms=1_700_000_000_000,
    )
    # Scaffold uses flatten_failed (critical) so a child crash mid-halt leaves
    # a blocking incident for the post-halt gate (Finding 3 fix).
    assert isinstance(incident.notice, OperatorNotice)
    assert incident.notice.code == "watchdog.flatten_failed"
    assert incident.notice.tier == "critical"


def test_watchdog_incident_evidence_contains_reason() -> None:
    incident = watchdog_incident(
        reason="LEASE_EXPIRED",
        started_at_ms=1_700_000_000_000,
        evidence={"extra_key": "extra_value"},
    )
    assert incident.evidence["reason"] == "LEASE_EXPIRED"
    assert incident.evidence["extra_key"] == "extra_value"


def test_watchdog_incident_ids_are_unique() -> None:
    a = watchdog_incident(reason="LEASE_EXPIRED", started_at_ms=1_700_000_000_000)
    b = watchdog_incident(reason="LEASE_EXPIRED", started_at_ms=1_700_000_000_000)
    assert a.incident_id != b.incident_id
