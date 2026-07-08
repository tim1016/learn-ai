"""Tests for the post-halt reconciliation gate."""

from __future__ import annotations

from pathlib import Path

from app.engine.live.post_halt_gate import check_post_halt_gate
from app.operator.incidents.store import IncidentStore
from app.operator.notices.schema import (
    NOTICE_CODE_CONTRACTS,
    OperatorIncident,
    OperatorNotice,
    OperatorNoticeAction,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_notice(code: str, tier: str = "critical") -> OperatorNotice:
    contract = NOTICE_CODE_CONTRACTS[code]
    if contract.actionability == "actuatable":
        action = OperatorNoticeAction(
            kind="focus_cockpit_action",
            label="Run reconciliation",
            target="reconcile_now",
        )
    elif contract.actionability == "routed":
        action = OperatorNoticeAction(
            kind="external_manual_check",
            label="Check positions at IBKR",
            target="ibkr_positions",
        )
    else:
        action = OperatorNoticeAction(kind="none")
    return OperatorNotice(
        code=code,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        title="Test notice",
        message="Test message",
        actionability=contract.actionability,
        resolution="Clears when the fixture's contract condition is satisfied.",
        remedy_status=contract.remedy_status,
        action=action,
    )


def _make_incident(
    incident_id: str,
    *,
    notice_code: str,
    notice_tier: str = "critical",
    resolved_at_ms: int | None = None,
    category: str = "watchdog",
) -> OperatorIncident:
    return OperatorIncident(
        incident_id=incident_id,
        category=category,  # type: ignore[arg-type]
        notice=_make_notice(notice_code, tier=notice_tier),
        started_at_ms=1_700_000_000_000,
        resolved_at_ms=resolved_at_ms,
    )


def _write_incident(tmp_path: Path, incident: OperatorIncident) -> None:
    IncidentStore(tmp_path).append(incident)


# ---------------------------------------------------------------------------
# No incidents → None
# ---------------------------------------------------------------------------


def test_no_incidents_returns_none(tmp_path: Path) -> None:
    assert check_post_halt_gate(tmp_path, now_ms=1_700_000_001_000) is None


def test_missing_run_dir_returns_none(tmp_path: Path) -> None:
    result = check_post_halt_gate(tmp_path / "no-such-run", now_ms=1_700_000_001_000)
    assert result is None


# ---------------------------------------------------------------------------
# Resolved incident → None
# ---------------------------------------------------------------------------


def test_resolved_incident_does_not_block(tmp_path: Path) -> None:
    _write_incident(
        tmp_path,
        _make_incident(
            "inc-1",
            notice_code="watchdog.flatten_timed_out",
            resolved_at_ms=1_700_000_100_000,
        ),
    )
    result = check_post_halt_gate(tmp_path, now_ms=1_700_000_001_000)
    assert result is None


# ---------------------------------------------------------------------------
# Safe outcome codes → None even if unresolved
# ---------------------------------------------------------------------------


def test_unresolved_completed_incident_does_not_block(tmp_path: Path) -> None:
    """An unresolved flatten_completed incident MUST NOT block restart."""
    _write_incident(
        tmp_path,
        _make_incident(
            "inc-1",
            notice_code="watchdog.flatten_completed",
            notice_tier="info",
            resolved_at_ms=None,
        ),
    )
    result = check_post_halt_gate(tmp_path, now_ms=1_700_000_001_000)
    assert result is None


def test_unresolved_not_needed_incident_does_not_block(tmp_path: Path) -> None:
    """An unresolved flatten_not_needed incident MUST NOT block restart."""
    _write_incident(
        tmp_path,
        _make_incident(
            "inc-1",
            notice_code="watchdog.flatten_not_needed",
            notice_tier="info",
            resolved_at_ms=None,
        ),
    )
    result = check_post_halt_gate(tmp_path, now_ms=1_700_000_001_000)
    assert result is None


# ---------------------------------------------------------------------------
# Uncertain outcome codes → blocking incident
# ---------------------------------------------------------------------------


def test_unresolved_timed_out_incident_blocks(tmp_path: Path) -> None:
    _write_incident(
        tmp_path,
        _make_incident("inc-timed-out", notice_code="watchdog.flatten_timed_out"),
    )
    result = check_post_halt_gate(tmp_path, now_ms=1_700_000_001_000)
    assert result is not None
    assert result.notice.code == "reconciliation.required_after_uncertain_flatten"
    assert result.notice.tier == "critical"


def test_unresolved_failed_incident_blocks(tmp_path: Path) -> None:
    _write_incident(
        tmp_path,
        _make_incident("inc-failed", notice_code="watchdog.flatten_failed"),
    )
    result = check_post_halt_gate(tmp_path, now_ms=1_700_000_001_000)
    assert result is not None
    assert result.notice.code == "reconciliation.required_after_uncertain_flatten"


def test_unresolved_broker_disconnected_blocks(tmp_path: Path) -> None:
    _write_incident(
        tmp_path,
        _make_incident(
            "inc-disco",
            notice_code="watchdog.broker_disconnected_before_flatten",
        ),
    )
    result = check_post_halt_gate(tmp_path, now_ms=1_700_000_001_000)
    assert result is not None
    assert result.notice.code == "reconciliation.required_after_uncertain_flatten"


# ---------------------------------------------------------------------------
# Blocking incident includes prior evidence
# ---------------------------------------------------------------------------


def test_blocked_incident_includes_prior_evidence(tmp_path: Path) -> None:
    prior_incident = _make_incident("inc-prior", notice_code="watchdog.flatten_timed_out")
    _write_incident(tmp_path, prior_incident)

    result = check_post_halt_gate(tmp_path, now_ms=1_700_000_001_000)

    assert result is not None
    ev = result.evidence
    assert ev["prior_incident_id"] == "inc-prior"
    assert ev["prior_notice_code"] == "watchdog.flatten_timed_out"
    assert ev["prior_started_at_ms"] == 1_700_000_000_000


# ---------------------------------------------------------------------------
# Non-watchdog category incident → None
# ---------------------------------------------------------------------------


def test_non_watchdog_category_does_not_block(tmp_path: Path) -> None:
    _write_incident(
        tmp_path,
        _make_incident(
            "inc-1",
            notice_code="reconciliation.required_after_uncertain_flatten",
            category="reconciliation",
        ),
    )
    result = check_post_halt_gate(tmp_path, now_ms=1_700_000_001_000)
    assert result is None


# ---------------------------------------------------------------------------
# Mixed incidents: one safe, one uncertain → blocks
# ---------------------------------------------------------------------------


def test_mixed_incidents_blocks_when_any_uncertain(tmp_path: Path) -> None:
    store = IncidentStore(tmp_path)
    store.append(_make_incident("inc-safe", notice_code="watchdog.flatten_completed", notice_tier="info"))
    store.append(_make_incident("inc-bad", notice_code="watchdog.flatten_timed_out"))

    result = check_post_halt_gate(tmp_path, now_ms=1_700_000_001_000)
    assert result is not None
    assert result.notice.code == "reconciliation.required_after_uncertain_flatten"


# ---------------------------------------------------------------------------
# Finding 3: scaffold incident (watchdog.flatten_failed used as pessimistic
# placeholder) blocks restart without a terminal write.
# ---------------------------------------------------------------------------


def test_scaffold_only_incident_blocks_restart(tmp_path: Path) -> None:
    """Finding 3: the initial scaffold written by watchdog_incident() uses
    watchdog.flatten_failed as its notice code.  If the child crashes before
    the terminal write, this scaffold stays unresolved and the gate must block.
    """
    from app.operator.incidents.watchdog_notices import watchdog_incident

    scaffold = watchdog_incident(reason="LEASE_EXPIRED", started_at_ms=1_700_000_000_000)

    # Verify the scaffold has the pessimistic notice code.
    assert scaffold.notice.code == "watchdog.flatten_failed", (
        "scaffold must use flatten_failed so the post-halt gate blocks restart on crash"
    )
    assert scaffold.resolved_at_ms is None

    IncidentStore(tmp_path).append(scaffold)

    result = check_post_halt_gate(tmp_path, now_ms=1_700_000_001_000)
    assert result is not None, (
        "gate must block when only the scaffold is present (child died mid-halt)"
    )
    assert result.notice.code == "reconciliation.required_after_uncertain_flatten"
