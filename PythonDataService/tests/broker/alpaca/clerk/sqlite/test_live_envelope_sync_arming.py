"""One tick, one ledger read: the seal, the arming snapshot, the transition warning (slice 7, R2, R5, R10)."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from app.broker.alpaca.clerk.live_arming import (
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


def _record() -> LiveArmingRecord:
    return LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=SID,
        seal_hash=SEAL,
        configured_signal_hash="b" * 64,
        shadow_receipt_sha256="e" * 64,
        envelope=TEST_ENVELOPE_VALUES,
        armed_at_ms=T0 - 60_000,
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
