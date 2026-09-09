"""The one resolver Start, Resume and the runner share for the arming fact (slice 7, R6)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_arming import LIVE_ARMING_LEDGER_INVALID, LIVE_ARMING_REQUIRED, LiveArmingRecord
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
    HoldState,
)
from app.schemas.account_authority import CustodyWorld
from app.schemas.run_admission import ArmingAdmissionFact
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.live_arming_admission import live_arming_admission_fact
from tests.broker.alpaca.clerk.live_arming_fixtures import ARMED_AT_MS, live_settings, record_sealed_binding
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES

SID = "ema-live-1"
NOW = ARMED_AT_MS + 60_000


def _binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=SID,
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        action_plan=alpaca_v1_action_plan("SPY"),
        sealed_account_id=LIVE_ACCT,
        run_id=f"{SID}-run-1",
        created_at_ms=NOW,
    )


def _custody(account_id: str = LIVE_ACCT, account_mode: str = "live") -> ClerkCustodySnapshot:
    def count() -> CustodyCountFact:
        return CustodyCountFact(state="zero", count=0)

    return ClerkCustodySnapshot(
        broker="alpaca",
        account_id=account_id,
        account_mode=account_mode,
        strategy_instance_id=SID,
        clerk_generation="clerk-1",
        journal_sequence=1,
        reconciliation_state="clean",
        reconciliation_fresh=True,
        reconciled_at_ms=NOW,
        exposure=CustodyExposureFact(state="zero", positions={}),
        working_orders=count(),
        pending_orders=count(),
        terminal_orders=count(),
        unresolved_effects=count(),
        hold=HoldState(active=False),
        freeze=AccountFreezeState(),
        reason_code="CLERK_CUSTODY_PROVEN",
        evidence_refs=(f"clerk:{account_id}:1",),
        observed_at_ms=NOW,
    )


def _fact(
    tmp_path: Path,
    live_state_root: Path,
    *,
    custody: ClerkCustodySnapshot | None = None,
    custody_world: CustodyWorld = "real_live",
) -> ArmingAdmissionFact | None:
    return live_arming_admission_fact(
        _binding(),
        custody or _custody(),
        NOW,
        custody_world=custody_world,
        settings=live_settings(),
        artifacts_root=tmp_path,
        live_state_root=live_state_root,
    )


@pytest.mark.parametrize(
    ("binding", "custody", "custody_world"),
    [
        pytest.param(_binding(), _custody("PA-TEST", "paper"), "real_paper", id="paper_custody_on_real_paper"),
        # The world-table leg on its own: the real-live world admits only a
        # `live` account mode, so a paper custody under it is not an arming
        # question at all (`world_admits_account_mode`, ADR 0059 D1 R8).
        pytest.param(_binding(), _custody("PA-TEST", "paper"), "real_live", id="paper_custody_on_real_live"),
        pytest.param(_binding(), _custody(), "shadow", id="live_custody_on_shadow"),
        pytest.param(
            _binding().model_copy(update={"mode": "dry_run", "sealed_account_id": f"sim:{SID}"}),
            _custody(),
            "real_live",
            id="dry_run_binding_on_real_live",
        ),
    ],
)
def test_paper_shadow_and_dry_run_launches_get_no_fact(
    tmp_path: Path,
    binding: BrokerBotBinding,
    custody: ClerkCustodySnapshot,
    custody_world: CustodyWorld,
) -> None:
    fact = live_arming_admission_fact(
        binding,
        custody,
        NOW,
        custody_world=custody_world,
        settings=live_settings(),
        artifacts_root=tmp_path,
        live_state_root=tmp_path / "runner",
    )
    assert fact is None


def test_a_never_armed_live_instance_is_not_armed_and_names_the_ceremony(tmp_path: Path) -> None:
    fact = _fact(tmp_path, tmp_path / "runner")
    assert fact is not None
    assert fact.state == "NOT_ARMED"
    assert fact.reason_code == LIVE_ARMING_REQUIRED
    assert fact.next_step is not None and "manage_alpaca_arming" in fact.next_step
    assert fact.observed_at_ms == NOW


def test_an_armed_live_instance_is_armed(tmp_path: Path) -> None:
    live_state_root = tmp_path / "runner"
    seal = record_sealed_binding(live_state_root, strategy_instance_id=SID, sealed_account_id=LIVE_ACCT)
    LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT).append(
        LiveArmingRecord.create(
            live_account_id=LIVE_ACCT,
            strategy_instance_id=SID,
            seal_hash=seal.bot_configuration_hash,
            configured_signal_hash=seal.configured_signal_hash,
            shadow_receipt_sha256="e" * 64,
            envelope=TEST_ENVELOPE_VALUES,
            armed_at_ms=ARMED_AT_MS,
            max_sessions=TEST_ENVELOPE_VALUES.arming_max_sessions,
        )
    )
    fact = _fact(tmp_path, live_state_root)
    assert fact is not None
    assert (fact.state, fact.reason_code) == ("ARMED", None)


def test_another_accounts_arming_record_does_not_arm_this_account(tmp_path: Path) -> None:
    """The resolver reads the ledger of the account being launched, not any ledger it finds."""
    live_state_root = tmp_path / "runner"
    seal = record_sealed_binding(live_state_root, strategy_instance_id=SID, sealed_account_id=LIVE_ACCT)
    other = "9LIVE0002"
    LiveArmingLedger(tmp_path, live_account_id=other).append(
        LiveArmingRecord.create(
            live_account_id=other,
            strategy_instance_id=SID,
            seal_hash=seal.bot_configuration_hash,
            configured_signal_hash=seal.configured_signal_hash,
            shadow_receipt_sha256=None,
            envelope=TEST_ENVELOPE_VALUES,
            armed_at_ms=ARMED_AT_MS,
            max_sessions=TEST_ENVELOPE_VALUES.arming_max_sessions,
        )
    )
    fact = _fact(tmp_path, live_state_root)
    assert fact is not None
    assert (fact.state, fact.reason_code) == ("NOT_ARMED", LIVE_ARMING_REQUIRED)


def test_an_unreadable_ledger_is_unreadable_not_unarmed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text('{"kind":"armed","schema_version":1}\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        fact = _fact(tmp_path, tmp_path / "runner")
    assert fact is not None
    assert (fact.state, fact.reason_code) == ("UNREADABLE", LIVE_ARMING_LEDGER_INVALID)
    assert any(record.action == "live_arming_admission_unreadable" for record in caplog.records)
