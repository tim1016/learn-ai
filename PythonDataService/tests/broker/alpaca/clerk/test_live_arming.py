"""The sealed arming record, its revocation, and the pure status rule (ADR 0059 D3)."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date

import pytest

from app.broker.alpaca.clerk.live_arming import (
    ARMING_REASON_CODES,
    LIVE_ARMING_FUTURE_DATED,
    LIVE_ARMING_LAPSED,
    LIVE_ARMING_REVOKED,
    LIVE_ARMING_SEAL_CHANGED,
    LIVE_ENVELOPE_DISAGREEMENT,
    LIVE_ENVELOPE_MISSING,
    ArmingStatus,
    LedgerRecord,
    LiveArmingInvalid,
    LiveArmingRecord,
    LiveArmingRefused,
    LiveDisarmRecord,
    arming_status,
    sessions_used,
)
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.lean_sidecar.trading_calendar import is_trading_day, trading_session_count
from app.services.session_authority import et_minute_of_day_ms
from app.utils.session_anchors import MAX_TIMESTAMP_MS

ACCOUNT = "9LIVE0001"
SID = "ema-shadow-1"
SEAL = "a" * 64
SIGNAL = "b" * 64
RECEIPT = "c" * 64
ENVELOPE = LiveEnvelopeValues(
    loss_fraction=0.05,
    loss_usd=5_000.0,
    shadow_sessions=1,
    arming_max_sessions=20,
    xh_entry_bps=10.0,
    xh_exit_bps=10.0,
)
# Friday 2026-09-11, 10:00 ET — a full NYSE session.
FRIDAY_MS = et_minute_of_day_ms(date(2026, 9, 11), 10 * 60)
MONDAY_MS = et_minute_of_day_ms(date(2026, 9, 14), 10 * 60)
TUESDAY_MS = et_minute_of_day_ms(date(2026, 9, 15), 10 * 60)


def _armed(*, max_sessions: int = 20, armed_at_ms: int = FRIDAY_MS, seal: str = SEAL, instance: str = SID,
           account: str = ACCOUNT, envelope: LiveEnvelopeValues = ENVELOPE) -> LiveArmingRecord:
    return LiveArmingRecord.create(
        live_account_id=account,
        strategy_instance_id=instance,
        seal_hash=seal,
        configured_signal_hash=SIGNAL,
        shadow_receipt_sha256=RECEIPT,
        envelope=envelope,
        armed_at_ms=armed_at_ms,
        max_sessions=max_sessions,
    )


def _disarmed(record: LiveArmingRecord, *, at_ms: int = MONDAY_MS) -> LiveDisarmRecord:
    return LiveDisarmRecord.create(
        live_account_id=record.live_account_id,
        strategy_instance_id=record.strategy_instance_id,
        revokes_record_sha256=record.record_sha256,
        disarmed_at_ms=at_ms,
    )


def _status(records: list[LedgerRecord], **overrides: object) -> ArmingStatus:
    kwargs: dict = {
        "live_account_id": ACCOUNT,
        "strategy_instance_id": SID,
        "seal_hash": SEAL,
        "configured_envelope": ENVELOPE,
        "now_ms": FRIDAY_MS,
    }
    kwargs.update(overrides)
    return arming_status(records, **kwargs)


def test_the_record_seals_every_field_and_round_trips() -> None:
    record = _armed()
    assert record.kind == "armed" and record.schema_version == 1
    assert len(record.record_sha256) == 64
    assert record.envelope_sha256 == ENVELOPE.sha
    assert record.envelope == ENVELOPE
    assert LiveArmingRecord.from_payload(asdict(record)) == record


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("live_account_id", "9LIVE0002"),
        ("strategy_instance_id", "other"),
        ("seal_hash", "d" * 64),
        ("configured_signal_hash", "d" * 64),
        ("shadow_receipt_sha256", "d" * 64),
        ("armed_at_ms", FRIDAY_MS + 1),
        ("max_sessions", 19),
    ],
)
def test_tampering_with_any_sealed_field_breaks_the_digest(field: str, value: object) -> None:
    payload = {**asdict(_armed()), field: value}
    with pytest.raises(LiveArmingInvalid, match="digest does not verify"):
        LiveArmingRecord.from_payload(payload)


def test_a_rewritten_envelope_is_caught_by_the_envelope_sha_before_the_digest() -> None:
    """R1's second check: the sealed values must still hash to the sealed sha."""
    record = _armed()
    payload = {**asdict(record), "envelope_values": {**record.envelope_values, "loss_usd": 4_000.0}}
    with pytest.raises(LiveArmingInvalid, match="envelope sha does not match"):
        LiveArmingRecord.from_payload(payload)


def test_a_reserved_namespace_account_is_never_a_live_account() -> None:
    with pytest.raises(LiveArmingInvalid, match="reserved account identity"):
        _armed(account=f"shadow:{ACCOUNT}")


def test_invalid_integer_and_hash_facts_are_refused() -> None:
    with pytest.raises(LiveArmingInvalid, match="invalid integer or identity facts"):
        _armed(max_sessions=0)
    with pytest.raises(LiveArmingInvalid, match="invalid integer or identity facts"):
        _armed(armed_at_ms=MAX_TIMESTAMP_MS + 1)
    assert _armed(armed_at_ms=MAX_TIMESTAMP_MS).armed_at_ms == MAX_TIMESTAMP_MS
    with pytest.raises(LiveArmingInvalid, match="invalid hash facts"):
        _armed(seal="not-a-sha")


def test_a_type_confused_row_leaves_by_this_modules_own_error() -> None:
    payload = {**asdict(_armed()), "armed_at_ms": str(FRIDAY_MS)}
    with pytest.raises(LiveArmingInvalid, match="invalid shape"):
        LiveArmingRecord.from_payload(payload)
    with pytest.raises(LiveArmingInvalid, match="invalid shape"):
        LiveArmingRecord.from_payload({"kind": "armed"})


def test_a_disarm_row_is_sealed_and_names_what_it_revokes() -> None:
    record = _armed()
    disarm = _disarmed(record)
    assert disarm.kind == "disarmed" and disarm.revokes_record_sha256 == record.record_sha256
    assert LiveDisarmRecord.from_payload(asdict(disarm)) == disarm
    with pytest.raises(LiveArmingInvalid, match="digest does not verify"):
        LiveDisarmRecord.from_payload({**asdict(disarm), "disarmed_at_ms": MONDAY_MS + 1})


def test_a_refusal_carries_its_reason_code() -> None:
    refusal = LiveArmingRefused(LIVE_ARMING_LAPSED, "the arming lapsed")
    assert refusal.reason_code == LIVE_ARMING_LAPSED
    assert str(refusal) == "the arming lapsed"
    assert isinstance(refusal, ValueError)


def test_every_code_is_its_own_name_and_the_set_is_closed() -> None:
    assert frozenset(
        {
            "LIVE_ARMING_LAPSED",
            "LIVE_ARMING_REVOKED",
            "LIVE_ARMING_SEAL_CHANGED",
            "LIVE_ARMING_INSTANCE_UNSEALED",
            "LIVE_ARMING_TOKEN_INVALID",
            "LIVE_ARMING_PLAN_EXPIRED",
            "LIVE_ARMING_INPUTS_CHANGED",
            "LIVE_ARMING_NOT_ARMED",
            "LIVE_ARMING_TTL_INVALID",
            "LIVE_ARMING_FUTURE_DATED",
            "LIVE_SHADOW_INCOMPLETE",
            "LIVE_ENVELOPE_DISAGREEMENT",
            "LIVE_ENVELOPE_MISSING",
        }
    ) == ARMING_REASON_CODES
    assert LIVE_ENVELOPE_MISSING == "LIVE_ENVELOPE_MISSING"
    assert LIVE_ARMING_FUTURE_DATED == "LIVE_ARMING_FUTURE_DATED"
    from app.broker.alpaca.clerk import live_arming as live_arming_module

    for name in ARMING_REASON_CODES:
        assert getattr(live_arming_module, name) == name


def test_the_arming_session_counts_as_one_and_a_weekend_spends_nothing() -> None:
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=FRIDAY_MS) == 1
    # Saturday and Sunday are not sessions: Monday is only the second.
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=MONDAY_MS) == 2
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=TUESDAY_MS) == 3
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=MONDAY_MS) == trading_session_count(
        date(2026, 9, 11), date(2026, 9, 14)
    )


def test_a_market_holiday_spends_nothing_either() -> None:
    """Thanksgiving 2026-11-26 is a Thursday and not a session; 11-27 is a half day."""
    assert not is_trading_day(date(2026, 11, 26))
    armed = et_minute_of_day_ms(date(2026, 11, 25), 10 * 60)
    checked = et_minute_of_day_ms(date(2026, 11, 30), 10 * 60)
    expected = trading_session_count(date(2026, 11, 25), date(2026, 11, 30))
    assert expected == 3  # Wed 25, Fri 27 (half day), Mon 30
    assert sessions_used(armed_at_ms=armed, now_ms=checked) == expected


def test_sessions_used_refuses_a_reversed_range() -> None:
    """A now_ms behind the arming fails closed, to the millisecond -- the calendar never sees a reversed range."""
    with pytest.raises(ValueError, match="precedes armed_at_ms"):
        sessions_used(armed_at_ms=MONDAY_MS, now_ms=FRIDAY_MS)
    with pytest.raises(ValueError):
        sessions_used(armed_at_ms=FRIDAY_MS, now_ms=FRIDAY_MS - 1)
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=FRIDAY_MS) == 1


def test_no_record_is_unarmed() -> None:
    assert _status([]) == ArmingStatus(
        state="unarmed", reason_code=None, record=None, sessions_used=0, sessions_remaining=0
    )


def test_the_latest_record_decides_and_a_re_arm_supersedes() -> None:
    first = _armed(max_sessions=2)
    second = _armed(max_sessions=5, armed_at_ms=MONDAY_MS)
    status = _status([first, second], now_ms=MONDAY_MS)
    assert (status.state, status.record) == ("armed", second)
    assert (status.sessions_used, status.sessions_remaining) == (1, 4)


def test_a_disarm_revokes_until_the_instance_is_armed_again() -> None:
    record = _armed()
    status = _status([record, _disarmed(record)], now_ms=MONDAY_MS)
    assert (status.state, status.reason_code, status.record) == ("disarmed", LIVE_ARMING_REVOKED, None)
    assert (status.sessions_used, status.sessions_remaining) == (0, 0)

    rearmed = _armed(armed_at_ms=MONDAY_MS)
    assert _status([record, _disarmed(record), rearmed], now_ms=MONDAY_MS).state == "armed"


def test_a_changed_seal_disarms_and_an_absent_binding_counts_as_changed() -> None:
    records = [_armed()]
    changed = _status(records, seal_hash="d" * 64)
    assert (changed.state, changed.reason_code) == ("disarmed", LIVE_ARMING_SEAL_CHANGED)
    absent = _status(records, seal_hash=None)
    assert (absent.state, absent.reason_code) == ("disarmed", LIVE_ARMING_SEAL_CHANGED)
    # The record is still reported so an operator can see what was armed.
    assert changed.record == records[0] and changed.sessions_used == 1


def test_a_changed_environment_disagrees_with_the_sealed_envelope() -> None:
    status = _status([_armed()], configured_envelope=replace(ENVELOPE, loss_usd=4_000.0))
    assert (status.state, status.reason_code) == ("disarmed", LIVE_ENVELOPE_DISAGREEMENT)


def test_the_arming_lapses_only_once_the_count_is_exceeded() -> None:
    records = [_armed(max_sessions=2)]
    on_the_last_session = _status(records, now_ms=MONDAY_MS)
    assert on_the_last_session.state == "armed"
    assert (on_the_last_session.sessions_used, on_the_last_session.sessions_remaining) == (2, 0)

    lapsed = _status(records, now_ms=TUESDAY_MS)
    assert (lapsed.state, lapsed.reason_code) == ("lapsed", LIVE_ARMING_LAPSED)
    assert (lapsed.sessions_used, lapsed.sessions_remaining) == (3, 0)


def test_a_record_dated_after_the_clock_is_disarmed() -> None:
    """A record armed after now_ms fails closed under a thirteenth code (controller ruling)."""
    record = _armed(armed_at_ms=MONDAY_MS)
    status = _status([record], now_ms=FRIDAY_MS)
    assert (status.state, status.reason_code, status.record) == ("disarmed", LIVE_ARMING_FUTURE_DATED, record)
    assert (status.sessions_used, status.sessions_remaining) == (0, record.max_sessions)


def test_the_checks_run_in_the_order_r5_fixes() -> None:
    """A record that fails three ways at once is reported by the first failure."""
    records = [_armed(max_sessions=1)]
    status = _status(
        records, seal_hash="d" * 64, configured_envelope=replace(ENVELOPE, loss_usd=4_000.0), now_ms=TUESDAY_MS
    )
    assert status.reason_code == LIVE_ARMING_SEAL_CHANGED
    envelope_first = _status(records, configured_envelope=replace(ENVELOPE, loss_usd=4_000.0), now_ms=TUESDAY_MS)
    assert envelope_first.reason_code == LIVE_ENVELOPE_DISAGREEMENT


def test_another_accounts_or_another_instances_record_never_answers_here() -> None:
    foreign_account = _armed(account="9LIVE0002")
    foreign_instance = _armed(instance="other")
    assert _status([foreign_account, foreign_instance]).state == "unarmed"
