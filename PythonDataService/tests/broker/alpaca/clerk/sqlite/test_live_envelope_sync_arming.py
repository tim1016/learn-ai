"""One tick, one ledger read: the seal, the arming snapshot, the transition warning (slice 7, R2, R5, R10)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest

from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_LAPSED,
    LIVE_ARMING_REVOKED,
    LIVE_MODE_DISAGREEMENT,
    LIVE_VERDICT_TRANSITION_HALT,
    LiveArmingRecord,
)
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerAccountModeDisagreement
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES, _LiveBroker
from tests.broker.alpaca.clerk.sqlite.conftest import ENVELOPE_T0 as T0

SID = "ema-live-1"
SEAL = "a" * 64


class _DisagreeingBroker(_LiveBroker):
    """The live account whose broker-reported mode can stop agreeing mid-session."""

    disagree = False

    async def get_account(self):
        if self.disagree:
            raise BrokerAccountModeDisagreement(
                "The configured Alpaca mode and the observed account disagree.",
                broker="alpaca",
                detail="ALPACA_MODE='live' but the account number begins with 'PA'",
            )
        return await super().get_account()


def _halts(caplog: pytest.LogCaptureFixture) -> list:
    return [r for r in caplog.records if getattr(r, "action", None) == "live_verdict_transition_halt"]


def _record(*, armed_at_ms: int = T0 - 60_000) -> LiveArmingRecord:
    return LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=SID,
        seal_hash=SEAL,
        configured_signal_hash="b" * 64,
        shadow_receipt_sha256="e" * 64,
        envelope=TEST_ENVELOPE_VALUES,
        armed_at_ms=armed_at_ms,
        max_sessions=TEST_ENVELOPE_VALUES.arming_max_sessions,
    )


def _sync(
    repo: ClerkSqliteRepository,
    ledger: LiveArmingLedger,
    gate: ArmingGate,
    seals: Mapping[str, str],
    broker: _LiveBroker | None = None,
) -> LiveEnvelopeSync:
    return LiveEnvelopeSync(
        repo=repo,
        read=broker or _LiveBroker(now_ms=T0),
        envelope=LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=False),
        arming_ledger=ledger,
        arming_gate=gate,
        instance_seals=lambda: seals,
    )


async def test_a_tick_publishes_the_snapshot_and_seals_the_envelope_from_one_read(
    envelope_repo: ClerkSqliteRepository, tmp_path
) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    gate = ArmingGate()
    sync = _sync(envelope_repo, ledger, gate, {SID: SEAL})

    await sync.tick()

    snapshot = gate.fresh_snapshot(T0)
    assert snapshot is not None
    assert snapshot.live_account_id == LIVE_ACCT
    assert snapshot.status_for(SID, now_ms=T0).state == "armed"
    assert sync.envelope.sealed == TEST_ENVELOPE_VALUES
    assert sync.envelope.agreement == "agreed"


async def test_an_empty_ledger_publishes_an_unarmed_snapshot(
    envelope_repo: ClerkSqliteRepository, tmp_path
) -> None:
    gate = ArmingGate()
    sync = _sync(envelope_repo, LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT), gate, {})

    await sync.tick()

    snapshot = gate.fresh_snapshot(T0)
    assert snapshot is not None
    assert snapshot.status_for(SID, now_ms=T0).state == "unarmed"
    assert sync.envelope.sealed is None


async def test_an_unreadable_ledger_invalidates_the_gate_and_logs_once(
    envelope_repo: ClerkSqliteRepository, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    ledger.path.write_text(ledger.path.read_text(encoding="utf-8").replace('"kind":"armed"', '"kind":"armed ', 1), encoding="utf-8")
    gate = ArmingGate()
    sync = _sync(envelope_repo, ledger, gate, {SID: SEAL})

    with caplog.at_level("ERROR"):
        await sync.tick()
        await sync.tick()

    assert gate.invalid_reason_code == "LIVE_ARMING_LEDGER_INVALID"
    assert gate.fresh_snapshot(T0) is None
    assert sync.envelope.sealed is None
    assert sum(1 for r in caplog.records if getattr(r, "action", None) == "live_arming_ledger_invalid") == 1


async def test_an_instance_leaving_armed_is_warned_about_once_with_its_code(
    envelope_repo: ClerkSqliteRepository, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR 0059 D8 as R10 builds it: the loud part of the halt is one warning per transition."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    gate = ArmingGate()
    sync = _sync(envelope_repo, ledger, gate, {SID: SEAL})

    with caplog.at_level("WARNING"):
        await sync.tick()
        assert _halts(caplog) == []
        ledger.revoke_latest(SID, disarmed_at_ms=T0)
        await sync.tick()
        await sync.tick()

    halts = _halts(caplog)
    assert len(halts) == 1
    assert halts[0].reason_code == LIVE_ARMING_REVOKED
    assert halts[0].strategy_instance_id == SID
    assert halts[0].live_account_id == LIVE_ACCT
    assert LIVE_VERDICT_TRANSITION_HALT in halts[0].getMessage()


async def test_a_ledger_that_verifies_again_restores_the_gate(
    envelope_repo: ClerkSqliteRepository, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """The invalidation is a fault, not a latch: the next readable tick publishes again."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    sound = ledger.path.read_text(encoding="utf-8")
    ledger.path.write_text(sound.replace('"kind":"armed"', '"kind":"armed ', 1), encoding="utf-8")
    gate = ArmingGate()
    sync = _sync(envelope_repo, ledger, gate, {SID: SEAL})

    with caplog.at_level("ERROR"):
        await sync.tick()
        assert gate.invalid_reason_code == "LIVE_ARMING_LEDGER_INVALID"
        ledger.path.write_text(sound, encoding="utf-8")
        await sync.tick()

    assert gate.invalid_reason_code is None
    snapshot = gate.fresh_snapshot(T0)
    assert snapshot is not None
    assert snapshot.status_for(SID, now_ms=T0).state == "armed"
    assert sync.envelope.sealed == TEST_ENVELOPE_VALUES


async def test_a_second_loss_of_arming_is_warned_about_again(
    envelope_repo: ClerkSqliteRepository, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Once *per transition*, not once per process: re-arming rearms the warning too."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    gate = ArmingGate()
    sync = _sync(envelope_repo, ledger, gate, {SID: SEAL})

    with caplog.at_level("WARNING"):
        await sync.tick()
        ledger.revoke_latest(SID, disarmed_at_ms=T0)
        await sync.tick()
        assert len(_halts(caplog)) == 1
        ledger.append(_record(armed_at_ms=T0 - 30_000))
        await sync.tick()
        assert len(_halts(caplog)) == 1
        ledger.revoke_latest(SID, disarmed_at_ms=T0)
        await sync.tick()

    halts = _halts(caplog)
    assert len(halts) == 2
    assert [record.reason_code for record in halts] == [LIVE_ARMING_REVOKED, LIVE_ARMING_REVOKED]


async def test_the_first_tick_after_boot_warns_about_nothing_already_lapsed(
    envelope_repo: ClerkSqliteRepository, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """A transition is a change this process observed, never the state it booted into.

    An instance whose sessions were spent before the sync existed never left
    the armed set, so warning about it would make every restart of a
    long-lapsed account look like a fresh halt.
    """
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    # Armed 400 calendar days ago: far past the 20-session grant, so the very
    # first snapshot this process publishes already reads ``lapsed``.
    ledger.append(_record(armed_at_ms=T0 - 400 * 86_400_000))
    gate = ArmingGate()
    sync = _sync(envelope_repo, ledger, gate, {SID: SEAL})

    with caplog.at_level("WARNING"):
        await sync.tick()

    snapshot = gate.fresh_snapshot(T0)
    assert snapshot is not None
    assert snapshot.status_for(SID, now_ms=T0).reason_code == LIVE_ARMING_LAPSED
    assert _halts(caplog) == []


async def test_a_mid_session_mode_disagreement_invalidates_the_gate_until_a_read_agrees_again(
    envelope_repo: ClerkSqliteRepository, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """R2: the one verdict input that can move mid-session refuses under its own code."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    gate = ArmingGate()
    broker = _DisagreeingBroker(now_ms=T0)
    sync = _sync(envelope_repo, ledger, gate, {SID: SEAL}, broker=broker)
    await sync.tick()
    assert gate.fresh_snapshot(T0) is not None

    broker.disagree = True
    with caplog.at_level("WARNING"):
        assert await sync.tick() == "mode_disagreed"
        await sync.tick()

    assert gate.invalid_reason_code == LIVE_MODE_DISAGREEMENT
    assert sync.envelope.fresh_observation(T0) is None
    assert sum(1 for r in caplog.records if getattr(r, "action", None) == "live_envelope_mode_disagreed") == 1

    broker.disagree = False
    await sync.tick()
    assert gate.invalid_reason_code is None
    assert gate.fresh_snapshot(T0) is not None


async def test_the_hold_is_raised_against_the_sealed_limit_not_a_loosened_configured_one(
    envelope_repo: ClerkSqliteRepository, tmp_path
) -> None:
    """Raising the hold and clearing it are one judgement, so both read the seal.

    ADR 0059 D3: an operator who loosens ``ALPACA_LIVE_LOSS_*`` without
    re-arming has not widened anything. The account is judged by the armed
    envelope until the next ceremony -- which is also what makes the guarded
    clear safe, since it re-observes through this same reading.
    """
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    # limit = min(0.05 × 100,000, 5,000) = 5,000, and the day is -5,000: a
    # breach of the sealed limit and not of the loosened 9,000 below.
    broker = _LiveBroker(now_ms=T0, unrealized=-5_000.0)
    sync = _sync(envelope_repo, ledger, ArmingGate(), {SID: SEAL}, broker=broker)
    sync.envelope.values = replace(TEST_ENVELOPE_VALUES, loss_usd=9_000.0, loss_fraction=0.09)

    assert await sync.tick() == "hold_raised"

    assert sync.envelope.in_force == TEST_ENVELOPE_VALUES
    assert sync.envelope.agreement == "disagreed"


async def test_an_unreadable_seal_makes_the_account_unjudgeable_rather_than_relaxing_the_limit(
    envelope_repo: ClerkSqliteRepository, tmp_path
) -> None:
    """The fallback in ``in_force`` must not become a way to widen a limit.

    ``sealed`` returns to ``None`` when the arming inputs cannot be read, so a
    plain fallback would let an operator loosen ``ALPACA_LIVE_LOSS_*``, corrupt
    the ledger, and have the account judged by the looser number. An unreadable
    seal is unjudgeable: the observation is withdrawn and every ENTER refuses.
    """
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8").replace('"kind":"armed"', '"kind":"armed ', 1),
        encoding="utf-8",
    )
    broker = _LiveBroker(now_ms=T0, unrealized=-5_000.0)
    sync = _sync(envelope_repo, ledger, ArmingGate(), {SID: SEAL}, broker=broker)
    sync.envelope.values = replace(TEST_ENVELOPE_VALUES, loss_usd=9_000.0, loss_fraction=0.09)

    assert await sync.tick() == "unknown"

    assert sync.envelope.fresh_observation(T0) is None
    assert (await sync.observe()).loss_limit_usd is None


async def test_bindings_that_cannot_be_read_are_the_same_fault_not_an_escaping_error(
    envelope_repo: ClerkSqliteRepository, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """The seals callable reaches disk, so it can fail the way the ledger can.

    It must fail *closed* under this module's own fault: the guarded loss-hold
    clear re-observes through ``observe``, and an escaping ``OSError`` would
    answer HTTP 500 where the contract is "refused, the hold stands".
    """

    def _unreadable() -> Mapping[str, str]:
        raise OSError("the binding store is unreadable")

    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    gate = ArmingGate()
    sync = LiveEnvelopeSync(
        repo=envelope_repo,
        read=_LiveBroker(now_ms=T0),
        envelope=LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=False),
        arming_ledger=ledger,
        arming_gate=gate,
        instance_seals=_unreadable,
    )

    with caplog.at_level("ERROR"):
        assert await sync.tick() == "unknown"

    assert gate.invalid_reason_code == "LIVE_ARMING_LEDGER_INVALID"
    assert sync.envelope.sealed is None
    invalid = [r for r in caplog.records if getattr(r, "action", None) == "live_arming_ledger_invalid"]
    assert len(invalid) == 1
    assert invalid[0].exc_info is not None, "the traceback names which input would not read"


async def test_an_account_no_ceremony_has_armed_is_judged_by_the_configured_limit(
    envelope_repo: ClerkSqliteRepository, tmp_path
) -> None:
    """The fallback, stated: an empty ledger seals nothing, so nothing overrides."""
    broker = _LiveBroker(now_ms=T0, unrealized=-5_000.0)
    sync = _sync(
        envelope_repo,
        LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT),
        ArmingGate(),
        {},
        broker=broker,
    )
    sync.envelope.values = replace(TEST_ENVELOPE_VALUES, loss_usd=9_000.0, loss_fraction=0.09)

    assert await sync.tick() == "observed"

    assert sync.envelope.sealed is None
    assert sync.envelope.in_force == sync.envelope.values


async def test_without_a_gate_the_sync_only_seals_as_in_slice_6(
    envelope_repo: ClerkSqliteRepository, tmp_path
) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.append(_record())
    sync = LiveEnvelopeSync(
        repo=envelope_repo,
        read=_LiveBroker(now_ms=T0),
        envelope=LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=True),
        arming_ledger=ledger,
    )
    await sync.tick()
    assert sync.envelope.sealed == TEST_ENVELOPE_VALUES
