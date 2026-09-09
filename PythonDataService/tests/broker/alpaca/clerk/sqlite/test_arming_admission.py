"""Per-instance arming at the ENTER seam (ADR 0059 slice 7, R5).

``accept_enter`` gains a third gate between ``require_admission`` and the
envelope. Driven through the public entry point, like the envelope tests:
what is pinned is which ENTER is admitted, which refusal each fact produces,
in which order, and that a refusal writes nothing.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_LAPSED,
    LIVE_ARMING_LEDGER_INVALID,
    LIVE_ARMING_REQUIRED,
    LIVE_ARMING_UNOBSERVED,
    LiveArmingRecord,
)
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate, ArmingSnapshot
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_UNOBSERVED,
    OBSERVATION_MAX_AGE_MS,
    AccountObservation,
    LiveEnvelopeGate,
)
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    TRANSIENT_ADMISSION_REASON_CODES,
    AdmissionBlockedError,
)
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.conftest import ENVELOPE_T0 as T0

SEAL = "a" * 64
ONE_WEEK_MS = 7 * 86_400_000


def _record(sid: str, *, envelope=TEST_ENVELOPE_VALUES, armed_at_ms: int = T0 - 60_000) -> LiveArmingRecord:
    return LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=sid,
        seal_hash=SEAL,
        configured_signal_hash="b" * 64,
        shadow_receipt_sha256="e" * 64,
        envelope=envelope,
        armed_at_ms=armed_at_ms,
        max_sessions=envelope.arming_max_sessions,
    )


def _arming(sid: str, *records: LiveArmingRecord, observed_at_ms: int = T0, envelope=TEST_ENVELOPE_VALUES) -> ArmingGate:
    gate = ArmingGate()
    gate.publish(
        ArmingSnapshot(
            observed_at_ms=observed_at_ms,
            live_account_id=LIVE_ACCT,
            records=tuple(records),
            seals={sid: SEAL},
            configured_envelope=envelope,
        )
    )
    return gate


def _envelope(observed: bool = True) -> LiveEnvelopeGate:
    gate = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=False)
    if observed:
        gate.publish(
            AccountObservation(
                observed_at_ms=T0,
                broker_cash_usd=100_000.0,
                cash_available_usd=100_000.0,
                last_equity_usd=100_000.0,
                unrealized_pl_usd=0.0,
                position_count=0,
            )
        )
    return gate


def _leg(**overrides: Any) -> BrokerOrderLeg:
    base: dict[str, Any] = {"symbol": "SPY", "side": "buy", "quantity": 1}
    base.update(overrides)
    return BrokerOrderLeg(**base)


def _accept(repo: ClerkSqliteRepository, sid: str, run_id: str, *, arming: ArmingGate | None, envelope: LiveEnvelopeGate | None, decision_id: str = "d1"):
    return accept_enter(
        repo,
        account_id=repo.account_id,
        strategy_instance_id=sid,
        decision_id=decision_id,
        lifecycle_run_id=run_id,
        leg=_leg(),
        arming=arming,
        envelope=envelope,
        reference_price=100.0,
    )


def _refusal(exc_info: pytest.ExceptionInfo[AdmissionBlockedError]) -> str | None:
    return exc_info.value.decision.reason_code


def test_an_armed_instance_is_admitted_through_all_three_gates(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    accepted = _accept(envelope_repo, sid, run_id, arming=_arming(sid, _record(sid)), envelope=_envelope())
    assert accepted.created
    assert envelope_repo.reserved_cash_usd(observed_at_ms=T0) == pytest.approx(100.0)


def test_no_gate_means_no_arming_check_paper_and_shadow_unchanged(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    assert _accept(envelope_repo, sid, run_id, arming=None, envelope=_envelope()).created


@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        pytest.param(ArmingGate(), LIVE_ARMING_UNOBSERVED, id="never-refreshed"),
        pytest.param(_arming("x", observed_at_ms=T0 - OBSERVATION_MAX_AGE_MS - 1), LIVE_ARMING_UNOBSERVED, id="stale-snapshot"),
    ],
)
def test_an_unobserved_gate_refuses_closed(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str], gate: ArmingGate, expected: str
) -> None:
    sid, run_id = active_instance
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, arming=gate, envelope=_envelope())
    assert _refusal(exc_info) == expected


def test_an_invalid_ledger_refuses_before_freshness_is_even_asked(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    gate = _arming(sid, _record(sid))
    gate.invalidate("row 3: digest does not verify")
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, arming=gate, envelope=_envelope())
    assert _refusal(exc_info) == LIVE_ARMING_LEDGER_INVALID
    assert "digest does not verify" in (exc_info.value.decision.why or "")


def test_a_mid_session_mode_disagreement_refuses_under_its_own_code(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    from app.broker.alpaca.clerk.live_arming import LIVE_MODE_DISAGREEMENT

    sid, run_id = active_instance
    gate = _arming(sid, _record(sid))
    gate.invalidate("the broker answered paper", reason_code=LIVE_MODE_DISAGREEMENT)
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, arming=gate, envelope=_envelope())
    assert _refusal(exc_info) == LIVE_MODE_DISAGREEMENT


def test_a_never_armed_instance_is_required_and_nothing_is_written(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    before = envelope_repo.control_meta_snapshot().control_revision
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, arming=_arming(sid), envelope=_envelope())
    assert _refusal(exc_info) == LIVE_ARMING_REQUIRED
    assert envelope_repo.control_meta_snapshot().control_revision == before
    assert envelope_repo.reserved_cash_usd(observed_at_ms=T0) == 0.0


def test_a_lapsed_instance_refuses_with_its_own_code(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    sid, run_id = active_instance
    one_session = replace(TEST_ENVELOPE_VALUES, arming_max_sessions=1)
    gate = _arming(sid, _record(sid, envelope=one_session, armed_at_ms=T0 - ONE_WEEK_MS), envelope=one_session)
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, arming=gate, envelope=_envelope())
    assert _refusal(exc_info) == LIVE_ARMING_LAPSED


def test_arming_runs_before_the_envelope_so_an_unarmed_instance_never_reserves_cash(
    envelope_repo: ClerkSqliteRepository, active_instance: tuple[str, str]
) -> None:
    """Both gates would refuse; the arming refusal is the one that names the ENTER."""
    sid, run_id = active_instance
    with pytest.raises(AdmissionBlockedError) as exc_info:
        _accept(envelope_repo, sid, run_id, arming=_arming(sid), envelope=_envelope(observed=False))
    assert _refusal(exc_info) == LIVE_ARMING_REQUIRED
    assert _refusal(exc_info) != LIVE_ENVELOPE_UNOBSERVED


def test_every_arming_refusal_retries_on_the_next_clock() -> None:
    from app.broker.alpaca.clerk.live_arming import ARMING_ADMISSION_REASON_CODES

    assert ARMING_ADMISSION_REASON_CODES <= TRANSIENT_ADMISSION_REASON_CODES
