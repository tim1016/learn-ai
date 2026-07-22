"""Behavioral tests for the account-wide live-session action gate."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.engine.live.account_session_policy import (
    assess_account_live_session,
    read_account_session_policy,
    write_account_live_feed_evidence,
    write_account_session_policy,
)

ACCOUNT_ID = "DU1234567"


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1_000)


def test_live_session_requires_calendar_open_and_recent_live_feed(tmp_path: Path) -> None:
    now_ms = _ms(2026, 6, 23, 15, 0)  # 11:00 ET, a normal NYSE session.
    write_account_live_feed_evidence(
        tmp_path,
        account_id=ACCOUNT_ID,
        observed_at_ms=now_ms - 30_000,
    )

    assessment = assess_account_live_session(tmp_path, account_id=ACCOUNT_ID, now_ms=now_ms)

    assert assessment.allowed is True
    assert assessment.reason_code == "LIVE_TRADABLE_SESSION_VERIFIED"
    assert assessment.to_gate_result().status == "pass"


def test_live_session_refuses_scheduled_closed_session_without_override(tmp_path: Path) -> None:
    now_ms = _ms(2026, 6, 23, 1, 0)  # 21:00 ET on the prior evening.

    assessment = assess_account_live_session(tmp_path, account_id=ACCOUNT_ID, now_ms=now_ms)

    assert assessment.allowed is False
    assert assessment.reason_code == "OUTSIDE_LIVE_TRADABLE_SESSION"
    assert assessment.disposition == "WAIT_FOR_LIVE_TRADABLE_SESSION"
    assert assessment.to_gate_result().status == "block"


def test_live_session_refuses_open_calendar_when_live_feed_is_unproven(tmp_path: Path) -> None:
    now_ms = _ms(2026, 6, 23, 15, 0)

    assessment = assess_account_live_session(tmp_path, account_id=ACCOUNT_ID, now_ms=now_ms)

    assert assessment.allowed is False
    assert assessment.reason_code == "LIVE_SESSION_LIVENESS_UNPROVEN"
    assert assessment.disposition == "RESTORE_LIVE_FEED_AND_WAIT_FOR_FRESH_EVIDENCE"


def test_explicit_account_override_is_the_only_outside_session_bypass(tmp_path: Path) -> None:
    now_ms = _ms(2026, 6, 23, 1, 0)
    policy = write_account_session_policy(
        tmp_path,
        account_id=ACCOUNT_ID,
        allow_outside_live_session=True,
        updated_at_ms=now_ms - 1,
    )

    assessment = assess_account_live_session(tmp_path, account_id=ACCOUNT_ID, now_ms=now_ms)

    assert policy.allow_outside_live_session is True
    assert read_account_session_policy(tmp_path, ACCOUNT_ID).allow_outside_live_session is True
    assert assessment.allowed is True
    assert assessment.reason_code == "OUTSIDE_LIVE_SESSION_OVERRIDE_ENABLED"
    assert assessment.outside_live_session_override is True
