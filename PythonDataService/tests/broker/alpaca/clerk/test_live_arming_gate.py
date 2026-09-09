"""The per-instance arming gate is a pure cache of one ledger read (ADR 0059 slice 7, R5)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.broker.alpaca.clerk.live_arming import (
    ARMING_ADMISSION_REASON_CODES,
    ARMING_REASON_CODES,
    LIVE_ARMING_LAPSED,
    LIVE_ARMING_LEDGER_INVALID,
    LIVE_ARMING_REQUIRED,
    LIVE_ARMING_UNOBSERVED,
    LIVE_MODE_DISAGREEMENT,
    LIVE_VERDICT_TRANSITION_HALT,
    LiveArmingRecord,
)
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate, ArmingSnapshot
from app.broker.alpaca.clerk.live_envelope import OBSERVATION_MAX_AGE_MS
from tests.broker.alpaca.clerk.live_arming_fixtures import ARMED_AT_MS, ARMING_SID
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES

SEAL = "a" * 64
NOW = ARMED_AT_MS + 60_000
ONE_WEEK_MS = 7 * 86_400_000


def _record(*, envelope=TEST_ENVELOPE_VALUES, armed_at_ms: int = ARMED_AT_MS) -> LiveArmingRecord:
    return LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=ARMING_SID,
        seal_hash=SEAL,
        configured_signal_hash="b" * 64,
        shadow_receipt_sha256="e" * 64,
        envelope=envelope,
        armed_at_ms=armed_at_ms,
        max_sessions=envelope.arming_max_sessions,
    )


def _snapshot(records, *, observed_at_ms: int = NOW, seals=None) -> ArmingSnapshot:
    return ArmingSnapshot(
        observed_at_ms=observed_at_ms,
        live_account_id=LIVE_ACCT,
        records=tuple(records),
        seals={ARMING_SID: SEAL} if seals is None else seals,
        configured_envelope=TEST_ENVELOPE_VALUES,
    )


def test_the_new_codes_are_in_the_closed_set_and_the_admission_subset_is_closed() -> None:
    for code in (
        LIVE_ARMING_REQUIRED,
        LIVE_ARMING_UNOBSERVED,
        LIVE_ARMING_LEDGER_INVALID,
        LIVE_VERDICT_TRANSITION_HALT,
    ):
        assert code in ARMING_REASON_CODES
        assert code == code.upper()
    # LIVE_MODE_DISAGREEMENT is the one admission code deliberately outside
    # ARMING_REASON_CODES (design R11: "reused from where it lives" — the
    # adapter's own code, restated here rather than re-minted); every other
    # admission code is one of the closed set's own names.
    assert ARMING_ADMISSION_REASON_CODES - {LIVE_MODE_DISAGREEMENT} <= ARMING_REASON_CODES
    assert LIVE_VERDICT_TRANSITION_HALT not in ARMING_ADMISSION_REASON_CODES


def test_a_gate_starts_unobserved_and_publishing_makes_a_fresh_snapshot() -> None:
    gate = ArmingGate()
    assert gate.fresh_snapshot(NOW) is None
    gate.publish(_snapshot([_record()]))
    fresh = gate.fresh_snapshot(NOW)
    assert fresh is not None
    assert fresh.status_for(ARMING_SID, now_ms=NOW).state == "armed"


@pytest.mark.parametrize("age_ms", [OBSERVATION_MAX_AGE_MS + 1, -1])
def test_a_stale_or_future_snapshot_is_not_fresh(age_ms: int) -> None:
    gate = ArmingGate()
    gate.publish(_snapshot([_record()], observed_at_ms=NOW - age_ms))
    assert gate.fresh_snapshot(NOW) is None
    assert gate.latest_snapshot() is not None


def test_invalidate_drops_the_snapshot_and_publish_clears_the_fault() -> None:
    gate = ArmingGate()
    gate.publish(_snapshot([_record()]))
    gate.invalidate("digest does not verify")
    assert gate.invalid_why == "digest does not verify"
    assert gate.invalid_reason_code == LIVE_ARMING_LEDGER_INVALID
    assert gate.latest_snapshot() is None
    gate.publish(_snapshot([_record()]))
    assert gate.invalid_why is None
    assert gate.invalid_reason_code is None


def test_a_standing_hold_survives_a_publish_and_release_admits_it() -> None:
    """A ``hold`` is a sticky fault: ``publish`` refreshes the cache but the fault stands until ``release``."""
    gate = ArmingGate()
    gate.hold(LIVE_MODE_DISAGREEMENT, "the broker answered paper")
    snapshot = _snapshot([_record()])
    gate.publish(snapshot)
    assert gate.fresh_snapshot(NOW) is None
    assert gate.invalid_reason_code == LIVE_MODE_DISAGREEMENT
    gate.release()
    assert gate.fresh_snapshot(NOW) is snapshot
    assert gate.invalid_reason_code is None


def test_a_gate_can_be_invalidated_under_the_mode_disagreement_code() -> None:
    from app.broker.alpaca.clerk.live_arming import LIVE_MODE_DISAGREEMENT
    from app.broker.contract.errors import BrokerAccountModeDisagreement

    gate = ArmingGate()
    gate.invalidate("the broker answered paper", reason_code=LIVE_MODE_DISAGREEMENT)
    assert gate.invalid_reason_code == LIVE_MODE_DISAGREEMENT
    # One string, restated: the adapter's exception and the gate name the same code.
    assert BrokerAccountModeDisagreement("m", broker="alpaca", detail="d").reason_code == LIVE_MODE_DISAGREEMENT


def test_armed_instance_ids_lists_exactly_the_armed_instances() -> None:
    snapshot = _snapshot([_record()])
    assert snapshot.armed_instance_ids(NOW) == frozenset({ARMING_SID})
    assert _snapshot([_record()], seals={}).armed_instance_ids(NOW) == frozenset()


def test_status_is_derived_at_the_callers_instant_not_the_snapshots() -> None:
    """A lapse at the ET-date boundary is enforced when asked, not at the next tick."""
    one_session = replace(TEST_ENVELOPE_VALUES, arming_max_sessions=1)
    snapshot = ArmingSnapshot(
        observed_at_ms=NOW,
        live_account_id=LIVE_ACCT,
        records=(_record(envelope=one_session, armed_at_ms=ARMED_AT_MS),),
        seals={ARMING_SID: SEAL},
        configured_envelope=one_session,
    )
    assert snapshot.status_for(ARMING_SID, now_ms=NOW).state == "armed"
    lapsed = snapshot.status_for(ARMING_SID, now_ms=NOW + ONE_WEEK_MS)
    assert (lapsed.state, lapsed.reason_code) == ("lapsed", LIVE_ARMING_LAPSED)


def test_an_instance_with_no_seal_on_disk_is_seal_changed_not_armed() -> None:
    snapshot = _snapshot([_record()], seals={})
    assert snapshot.status_for(ARMING_SID, now_ms=NOW).reason_code == "LIVE_ARMING_SEAL_CHANGED"
