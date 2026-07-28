"""Regression matrix for the broker-free AccountSafetySnapshot composer."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrConnectionHealth,
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.config import settings
from app.engine.live.account_artifacts import (
    AccountClerkLease,
    account_artifact_file_path,
    advance_account_clerk_generation,
    write_account_clerk_lease,
)
from app.engine.live.account_clerk_journal import (
    AccountClerkJournal,
    account_clerk_inbox_path,
    account_clerk_journal_path,
)
from app.engine.live.account_clerk_journal_models import AccountClerkInboxEntry
from app.engine.live.account_epoch import ACCOUNT_EPOCH_FILENAME, AccountEpochAuthority, AccountEpochState
from app.engine.live.account_observation_lease import AccountObservationLeaseRepo
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_safety import AccountSafetyAuthority, AccountSafetyVerdict, RetiredOwnerCustody
from app.schemas.account_safety_snapshot import AccountSafetySnapshotSource
from app.schemas.account_truth import (
    AccountTruthEvidenceTier,
    AccountTruthFactOwner,
    AccountTruthOrderCancelAction,
    AccountTruthOrderRow,
    AccountTruthOwnerBindingState,
    AccountTruthOwnerClass,
    AccountTruthPositionRow,
    AccountTruthResponse,
)
from app.schemas.artifact_io import atomic_write_pydantic_artifact, read_pydantic_artifact
from app.services.account_directory import AccountDirectoryService, CurrentBrokerAccount
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_safety_snapshot import AccountSafetySnapshotService
from app.services.account_truth_snapshot import get_account_truth_snapshot_provider
from app.services.journal_recovery import JournalRecoveryService

_ACCOUNT_ID = "DU1234567"


@pytest.fixture(autouse=True)
def _configured_presented_action_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)


def _service(tmp_path: Path, *, now_ms: int) -> AccountSafetySnapshotService:
    directory = AccountDirectoryService(
        artifacts_root=tmp_path,
        current_account=CurrentBrokerAccount(account_id=_ACCOUNT_ID, is_paper=True),
        now_ms=lambda: now_ms,
    )
    return AccountSafetySnapshotService(
        artifacts_root=tmp_path,
        directory=directory,
        now_ms=lambda: now_ms,
    )


def _truth(now_ms: int) -> AccountTruthResponse:
    return compose_account_truth(
        health=IbkrConnectionHealth(
            mode="paper", host="127.0.0.1", port=4002, client_id=7, connected=True,
            account_id=_ACCOUNT_ID, is_paper=True, fetched_at_ms=now_ms - 10,
            connection_state="connected", last_transition_ms=now_ms - 10,
        ),
        account_instance_bindings=[],
        account_recovery_state=AccountRecoveryState.clear(_ACCOUNT_ID),
        account=IbkrAccountSummary(
            account_id=_ACCOUNT_ID, is_paper=True, base_currency="USD",
            net_liquidation=100_000.0, buying_power=50_000.0, fetched_at_ms=now_ms - 10,
        ),
        positions_snapshot=IbkrPositionsSnapshot(
            account_id=_ACCOUNT_ID, is_paper=True, positions=[], fetched_at_ms=now_ms - 10,
        ),
        open_orders=[], completed_orders=[], executions=[], evidence_gaps=[], generated_at_ms=now_ms - 10,
    )


def _write_clean_authorities(tmp_path: Path, *, now_ms: int) -> None:
    generation = advance_account_clerk_generation(
        tmp_path, _ACCOUNT_ID, phase="accepting", recorded_at_ms=now_ms - 100, source="test"
    )
    write_account_clerk_lease(
        tmp_path,
        AccountClerkLease(
            account_id=_ACCOUNT_ID, generation=generation.generation, pid=123, ibkr_client_id=7,
            started_at_ms=now_ms - 100, renewed_at_ms=now_ms - 10, valid_until_ms=now_ms + 600_000,
        ),
    )
    AccountEpochAuthority(
        artifacts_root=tmp_path, account_id=_ACCOUNT_ID, clerk_generation=generation.generation,
        clerk_boot_id="test-boot", now_ms=lambda: now_ms - 100,
    ).initialize()
    AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        now_ms=lambda: now_ms - 10,
    ).observe_broker_retired_owner_custody((), observed_at_ms=now_ms - 10)
    AccountObservationLeaseRepo(tmp_path).renew(
        account_id=_ACCOUNT_ID, observed_at_ms=now_ms - 10, now_ms=now_ms,
        clerk_generation=generation.generation,
    )
    truth = _truth(now_ms)
    AccountReconciliationService(artifacts_root=tmp_path).write_receipt(
        requested_account_id=_ACCOUNT_ID, account_truth=truth, now_ms=now_ms,
    )
    get_account_truth_snapshot_provider().remember(truth, cached_at_ms=now_ms)


def test_snapshot_unavailable_critical_evidence_is_never_clean_and_has_stable_semantic_version(
    tmp_path: Path,
) -> None:
    provider = get_account_truth_snapshot_provider()
    provider.clear()
    first = _service(tmp_path, now_ms=1_780_000_000_000).snapshot(account_id=_ACCOUNT_ID)
    later = _service(tmp_path, now_ms=1_780_000_000_500).snapshot(account_id=_ACCOUNT_ID)

    assert first.verdict == "UNAVAILABLE"
    assert first.reason_code == "CRITICAL_SAFETY_EVIDENCE_UNAVAILABLE"
    assert first.generated_at_ms != later.generated_at_ms
    assert first.snapshot_version == later.snapshot_version
    assert first.snapshot_id == later.snapshot_id
    assert len(first.actions) == 1
    unavailable_action = first.actions[0]
    assert unavailable_action.action_id == "reconcile_now"
    assert unavailable_action.availability == "UNAVAILABLE"
    assert unavailable_action.target.account_id == _ACCOUNT_ID
    assert unavailable_action.issued_at_ms == first.generated_at_ms
    assert unavailable_action.expires_at_ms - unavailable_action.issued_at_ms == 60_000
    assert later.actions[0].issued_at_ms == later.generated_at_ms
    assert later.actions[0].expires_at_ms - later.actions[0].issued_at_ms == 60_000
    assert {
        "account_truth",
        "reconciliation",
        "account_safety",
        "account_epoch",
        "observation_lease",
        "clerk",
    }.issubset({source.source for source in first.sources})
    safety = next(source for source in first.sources if source.source == "account_safety")
    assert safety.state == "UNAVAILABLE"
    assert safety.as_of_ms is None


def test_first_snapshot_does_not_create_any_account_artifact(tmp_path: Path) -> None:
    get_account_truth_snapshot_provider().clear()

    _service(tmp_path, now_ms=1_780_000_000_000).snapshot(account_id=_ACCOUNT_ID)

    assert list(tmp_path.rglob("*")) == []


def test_reconciling_snapshot_does_not_present_an_executable_action_without_a_signing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "   ")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    action = AccountSafetySnapshotService._presented_actions(
        account_id=_ACCOUNT_ID,
        snapshot_id="a" * 64,
        generated_at_ms=1_780_000_000_000,
        verdict="RECONCILING",
        reason_code="ACCOUNT_RECONCILIATION_REQUIRED",
        evidence_refs=(),
    )[0]

    assert action.availability == "UNAVAILABLE"
    assert action.presentation_token is None


def test_snapshot_clean_then_epoch_invalid_and_truth_stale_advance_semantic_version(
    tmp_path: Path,
) -> None:
    now_ms = 1_780_000_000_000
    get_account_truth_snapshot_provider().clear()
    _write_clean_authorities(tmp_path, now_ms=now_ms)

    clean = _service(tmp_path, now_ms=now_ms).snapshot(account_id=_ACCOUNT_ID)
    stale = _service(tmp_path, now_ms=now_ms + 60_001).snapshot(account_id=_ACCOUNT_ID)
    authority = AccountEpochAuthority(
        artifacts_root=tmp_path, account_id=_ACCOUNT_ID, clerk_generation=1,
        clerk_boot_id="test-boot", now_ms=lambda: now_ms + 60_002,
    )
    authority.invalidate("SOCKET_LOSS")
    reconciling = _service(tmp_path, now_ms=now_ms + 60_002).snapshot(account_id=_ACCOUNT_ID)

    assert clean.verdict == "CLEAN"
    assert stale.verdict == "STALE"
    assert reconciling.verdict == "RECONCILING"
    assert reconciling.actions[0].availability == "AVAILABLE"
    assert reconciling.actions[0].snapshot_version == reconciling.snapshot_version
    assert reconciling.actions[0].idempotency_key == _service(
        tmp_path, now_ms=now_ms + 60_002
    ).snapshot(account_id=_ACCOUNT_ID).actions[0].idempotency_key
    clean_truth = next(source for source in clean.sources if source.source == "account_truth")
    stale_truth = next(source for source in stale.sources if source.source == "account_truth")
    clean_positions = next(source for source in clean.sources if source.source == "account_truth.positions")
    assert clean_truth.as_of_ms == now_ms
    assert clean_truth.age_ms == 0
    assert clean_positions.as_of_ms == now_ms - 10
    assert clean_positions.age_ms == 10
    assert stale_truth.as_of_ms == clean_truth.as_of_ms
    assert stale_truth.age_ms > clean_truth.age_ms
    assert clean.snapshot_version != reconciling.snapshot_version
    assert all(source.evidence_refs for source in clean.sources if source.critical)
    assert all(source.epoch_relation == "CURRENT" for source in clean.sources if source.critical)
    reconciliation = next(source for source in clean.sources if source.source == "reconciliation")
    assert reconciliation.as_of_ms == now_ms
    assert reconciliation.age_ms == 0


def test_snapshot_suspended_state_wins_over_missing_other_sources(tmp_path: Path) -> None:
    get_account_truth_snapshot_provider().clear()
    AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        now_ms=lambda: 1_780_000_000_000,
    ).suspend_retired_owner_custody(
        [
            RetiredOwnerCustody(
                account_id=_ACCOUNT_ID,
                strategy_instance_id="retired-bot",
                intent_id="retired-intent",
                order_ref="learn-ai/retired/v1:retired-intent",
            )
        ]
    )

    snapshot = _service(tmp_path, now_ms=1_780_000_000_001).snapshot(account_id=_ACCOUNT_ID)

    assert snapshot.verdict == "SUSPENDED"
    assert snapshot.custody.retired_owner_custody_count == 1


def test_snapshot_exposure_counts_only_current_bot_custody_as_managed(tmp_path: Path) -> None:
    now_ms = 1_780_000_000_000
    provider = get_account_truth_snapshot_provider()
    provider.clear()
    _write_clean_authorities(tmp_path, now_ms=now_ms)
    def owner(
        owner_class: AccountTruthOwnerClass,
        binding_state: AccountTruthOwnerBindingState,
    ) -> AccountTruthFactOwner:
        evidence_by_class: dict[AccountTruthOwnerClass, AccountTruthEvidenceTier] = {
            "bot": "bot_order_ref",
            "manual": "app_minted_manual",
            "mixed_known": "mixed_known",
            "foreign_or_unclaimed": "foreign_or_unclaimed",
        }
        return AccountTruthFactOwner(
            owner_class=owner_class,
            owner_key=f"{owner_class}:{binding_state}",
            owner_label=f"{owner_class} {binding_state}",
            evidence_tier=evidence_by_class[owner_class],
            evidence_label="Test ownership evidence",
            owner_binding_state=binding_state,
            severity="ok" if owner_class == "bot" and binding_state == "ACTIVE" else "warning",
        )

    def position(con_id: int, fact_owner: AccountTruthFactOwner) -> AccountTruthPositionRow:
        return AccountTruthPositionRow(
            account_id=_ACCOUNT_ID, con_id=con_id, symbol="AMD", sec_type="STK", quantity=1.0,
            avg_cost=100.0, owner=fact_owner, headline="Position", detail="Current position.", fetched_at_ms=now_ms,
        )

    def open_order(order_id: int, fact_owner: AccountTruthFactOwner) -> AccountTruthOrderRow:
        return AccountTruthOrderRow(
            fact_kind="open_order", lifecycle_id=f"order:{order_id}", lifecycle="submitted",
            account_id=_ACCOUNT_ID, order_id=order_id, client_id=7, con_id=order_id, symbol="AMD",
            sec_type="STK", action="BUY", quantity=1.0, order_type="LMT", limit_price=100.0,
            status="Submitted", cumulative_filled=0.0, remaining=1.0, owner=fact_owner,
            cancel_action=AccountTruthOrderCancelAction(
                visible=False, enabled=False, label="Cancel unavailable", detail="Not relevant to count.",
            ),
            headline="Open order", detail="Current order.", fetched_at_ms=now_ms,
        )

    truth = _truth(now_ms).model_copy(
        update={
            "orders": [
                open_order(1, owner("bot", "RETIRED")),
                open_order(2, owner("manual", "UNKNOWN")),
                open_order(3, owner("foreign_or_unclaimed", "UNKNOWN")),
            ],
            "positions": [
                position(101, owner("bot", "ACTIVE")),
                position(102, owner("bot", "UNKNOWN")),
                position(103, owner("mixed_known", "ACTIVE")),
            ],
        }
    )
    provider.remember(truth, cached_at_ms=now_ms)

    snapshot = _service(tmp_path, now_ms=now_ms).snapshot(account_id=_ACCOUNT_ID)

    assert snapshot.exposure.open_order_count == 3
    assert snapshot.exposure.position_count == 3
    assert snapshot.exposure.unmanaged_or_unknown_count == 5


@pytest.mark.parametrize(
    ("safety_verdict", "expected"),
    [(AccountSafetyVerdict.CONTAMINATED, "CONTAMINATED"), (AccountSafetyVerdict.RECONCILING, "UNAVAILABLE")],
)
def test_verdict_matrix_is_fail_closed_for_contamination_and_missing_critical_sources(
    safety_verdict: AccountSafetyVerdict,
    expected: str,
) -> None:
    verdict, _ = AccountSafetySnapshotService._verdict(
        safety_verdict,
        None,
        None,
        None,
        (AccountSafetySnapshotSource(source="account_truth", state="UNAVAILABLE", reason_code="MISSING"),),
    )

    assert verdict == expected


def test_snapshot_truth_that_is_fresh_but_not_proven_cannot_be_clean(tmp_path: Path) -> None:
    now_ms = 1_780_000_000_000
    provider = get_account_truth_snapshot_provider()
    provider.clear()
    _write_clean_authorities(tmp_path, now_ms=now_ms)
    provider.remember(
        _truth(now_ms).model_copy(update={"final_verdict": "not_proven"}),
        cached_at_ms=now_ms,
    )

    snapshot = _service(tmp_path, now_ms=now_ms).snapshot(account_id=_ACCOUNT_ID)

    assert snapshot.verdict == "RECONCILING"
    assert snapshot.reason_code == "ACCOUNT_TRUTH_NOT_PROVEN"


@pytest.mark.parametrize(("foreign_artifact", "expected_source"), [("receipt", "reconciliation"), ("epoch", "account_epoch")])
def test_snapshot_rejects_foreign_critical_artifacts(
    tmp_path: Path,
    foreign_artifact: str,
    expected_source: str,
) -> None:
    now_ms = 1_780_000_000_000
    get_account_truth_snapshot_provider().clear()
    _write_clean_authorities(tmp_path, now_ms=now_ms)
    reconciliation = AccountReconciliationService(artifacts_root=tmp_path)
    if foreign_artifact == "receipt":
        receipt = reconciliation.read_latest_receipt(_ACCOUNT_ID)
        assert receipt is not None
        atomic_write_pydantic_artifact(
            reconciliation.receipt_path(_ACCOUNT_ID),
            receipt.model_copy(update={"account_id": "DU7654321"}),
        )
    else:
        path = account_artifact_file_path(tmp_path, _ACCOUNT_ID, ACCOUNT_EPOCH_FILENAME)
        epoch = read_pydantic_artifact(path, AccountEpochState)
        assert epoch is not None
        atomic_write_pydantic_artifact(path, epoch.model_copy(update={"account_id": "DU7654321"}))

    snapshot = _service(tmp_path, now_ms=now_ms).snapshot(account_id=_ACCOUNT_ID)

    source = next(source for source in snapshot.sources if source.source == expected_source)
    assert source.epoch_relation == "MISMATCH"
    assert snapshot.verdict == "UNAVAILABLE"
    assert snapshot.reason_code == "CRITICAL_SAFETY_IDENTITY_MISMATCH"


@pytest.mark.parametrize(
    ("journal_contents", "expected_reason"),
    [
        ("not-json\n", "ACCOUNT_CLERK_JOURNAL_CORRUPT"),
        ('{"seq":1}\n', "ACCOUNT_CLERK_JOURNAL_CORRUPT"),
    ],
)
def test_snapshot_never_treats_a_corrupt_clerk_journal_as_fresh(
    tmp_path: Path,
    journal_contents: str,
    expected_reason: str,
) -> None:
    now_ms = 1_780_000_000_000
    get_account_truth_snapshot_provider().clear()
    _write_clean_authorities(tmp_path, now_ms=now_ms)
    journal = account_clerk_journal_path(tmp_path, _ACCOUNT_ID)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(journal_contents, encoding="utf-8")

    snapshot = _service(tmp_path, now_ms=now_ms).snapshot(account_id=_ACCOUNT_ID)

    clerk = next(source for source in snapshot.sources if source.source == "clerk")
    assert clerk.state == "UNAVAILABLE"
    assert clerk.reason_code == expected_reason
    assert snapshot.verdict == "UNAVAILABLE"


def test_snapshot_never_treats_broker_evidence_only_hold_as_fresh(tmp_path: Path) -> None:
    now_ms = 1_780_000_000_000
    get_account_truth_snapshot_provider().clear()
    _write_clean_authorities(tmp_path, now_ms=now_ms)
    journal = account_clerk_journal_path(tmp_path, _ACCOUNT_ID)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("not-json\n", encoding="utf-8")
    recovery = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: now_ms)
    recovery.quarantine(account_id=_ACCOUNT_ID, idempotency_key="quarantine-1")
    recovery.rebaseline(
        account_id=_ACCOUNT_ID,
        idempotency_key="rebaseline-1",
        snapshot=IbkrPositionsSnapshot(
            account_id=_ACCOUNT_ID,
            is_paper=True,
            positions=[
                IbkrPosition(
                    account_id=_ACCOUNT_ID,
                    con_id=1,
                    symbol="SPY",
                    sec_type="STK",
                    quantity=1.0,
                    avg_cost=100.0,
                    fetched_at_ms=now_ms,
                )
            ],
            fetched_at_ms=now_ms,
        ),
    )

    snapshot = _service(tmp_path, now_ms=now_ms).snapshot(account_id=_ACCOUNT_ID)

    clerk = next(source for source in snapshot.sources if source.source == "clerk")
    assert clerk.state == "UNAVAILABLE"
    assert clerk.reason_code == "ACCOUNT_CLERK_BROKER_EVIDENCE_ONLY_HOLD"
    assert snapshot.verdict == "UNAVAILABLE"


def test_snapshot_empty_journal_read_does_not_create_a_clerk_artifact(tmp_path: Path) -> None:
    now_ms = 1_780_000_000_000
    get_account_truth_snapshot_provider().clear()
    _write_clean_authorities(tmp_path, now_ms=now_ms)
    before = {
        path.relative_to(tmp_path)
        for path in tmp_path.rglob("*")
    }

    _service(tmp_path, now_ms=now_ms).snapshot(account_id=_ACCOUNT_ID)

    after = {
        path.relative_to(tmp_path)
        for path in tmp_path.rglob("*")
    }
    assert after == before


def test_snapshot_counts_asynchronous_clerk_custody_by_durable_stage(tmp_path: Path) -> None:
    now_ms = 1_780_000_000_000
    get_account_truth_snapshot_provider().clear()
    _write_clean_authorities(tmp_path, now_ms=now_ms)
    intent = AccountOwnerSubmitIntent(
        trace_id="trace-1",
        account_id=_ACCOUNT_ID,
        strategy_instance_id="strategy-1",
        run_id="run-1",
        bot_order_namespace="learn-ai/strategy-1/v1",
        intent_id="intent-1",
        order_ref="learn-ai/strategy-1/v1:intent-1",
        intent_kind="ORDER",
        order_spec={},
        owner_generation=1,
        created_at_ms=now_ms,
    )

    def accept_intent(_received: AccountOwnerSubmitIntent) -> None:
        return None

    AccountClerkJournal(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        now_ms=lambda: now_ms,
    ).record_intent(
        intent,
        validate_intent=accept_intent,
        async_custody_lane="entry",
    )

    snapshot = _service(tmp_path, now_ms=now_ms).snapshot(account_id=_ACCOUNT_ID)

    assert snapshot.custody.a0_custody_accepted_count == 1
    assert snapshot.custody.a1_broker_write_started_count == 0
    assert snapshot.custody.a2_broker_known_count == 0
    assert snapshot.custody.a3_economic_terminal_count == 0


def test_snapshot_observes_a_replayable_inbox_without_replaying_or_rewriting_it(
    tmp_path: Path,
) -> None:
    now_ms = 1_780_000_000_000
    get_account_truth_snapshot_provider().clear()
    _write_clean_authorities(tmp_path, now_ms=now_ms)
    intent = AccountOwnerSubmitIntent(
        trace_id="trace-inbox",
        account_id=_ACCOUNT_ID,
        strategy_instance_id="strategy-inbox",
        run_id="run-inbox",
        bot_order_namespace="learn-ai/strategy-inbox/v1",
        intent_id="intent-inbox",
        order_ref="learn-ai/strategy-inbox/v1:intent-inbox",
        intent_kind="ORDER",
        order_spec={},
        owner_generation=1,
        created_at_ms=now_ms,
    )
    inbox = account_clerk_inbox_path(tmp_path, _ACCOUNT_ID)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        AccountClerkInboxEntry(
            seq=1,
            received_at_ms=now_ms,
            async_custody_lane="entry",
            intent=intent,
        ).model_dump_json()
        + "\n",
        encoding="utf-8",
    )
    before = inbox.read_bytes()
    before_tree = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    journal = account_clerk_journal_path(tmp_path, _ACCOUNT_ID)

    snapshot = _service(tmp_path, now_ms=now_ms).snapshot(account_id=_ACCOUNT_ID)

    assert inbox.read_bytes() == before
    assert not journal.exists()
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before_tree
    assert snapshot.custody.a0_custody_accepted_count == 1
