"""Regression tests for retired-owner account suspension (Custody S6)."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest

from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.engine.live.account_epoch import (
    AccountEpochAuthority,
    AccountEpochOutageChanges,
    AccountEpochOutageDiff,
    AccountEpochReconciliationProof,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    bot_order_namespace_for_instance,
)
from app.engine.live.account_safety import (
    AccountSafetyAuthority,
    AccountSafetyEntryBlockedError,
    AccountSafetyState,
    AccountSafetySuspension,
    AccountSafetyVerdict,
    RetiredOwnerCustody,
    account_safety_admission_lock,
    account_safety_blocks_current_bot,
    account_safety_path,
    account_safety_state_exists,
    retired_owner_nonterminal_custody,
)

ACCOUNT_ID = "DU1234567"
NOW_MS = 1_780_000_000_000


def _custody(*, strategy_instance_id: str = "retired-amd") -> RetiredOwnerCustody:
    return RetiredOwnerCustody(
        account_id=ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        intent_id="intent-retired-1",
        order_ref=f"learn-ai/{strategy_instance_id}/v1:intent-retired-1",
    )


def _epoch_authority(root: Path) -> AccountEpochAuthority:
    return AccountEpochAuthority(
        artifacts_root=root,
        account_id=ACCOUNT_ID,
        clerk_generation=1,
        clerk_boot_id="s6-test-boot",
        now_ms=lambda: NOW_MS,
    )


def _clean_proof(state) -> AccountEpochReconciliationProof:
    assert state.reconciliation_id is not None
    assert state.required_reconciliation_depth is not None
    return AccountEpochReconciliationProof(
        account_id=ACCOUNT_ID,
        reconciliation_id=state.reconciliation_id,
        candidate_epoch=state.current_epoch,
        required_reconciliation_depth=state.required_reconciliation_depth,
        journal_tail_seq=0,
        evidence_status="COMPLETE",
        outage_diff=AccountEpochOutageDiff(
            required_reconciliation_depth=state.required_reconciliation_depth,
            intents=AccountEpochOutageChanges(),
            orders=AccountEpochOutageChanges(),
            executions=AccountEpochOutageChanges(),
            positions=AccountEpochOutageChanges(),
        ),
    )


def test_retired_owner_suspension_lifts_only_from_bound_clean_epoch_proof(tmp_path: Path) -> None:
    epoch = _epoch_authority(tmp_path)
    epoch.initialize()
    invalid = epoch.invalidate("RETIRED_OWNER_EXPOSURE")
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    )
    safety.suspend_retired_owner_custody((_custody(),))
    safety.bind_reconciliation(epoch.read())

    assert safety.read().verdict is AccountSafetyVerdict.SUSPENDED
    assert safety.lift_if_proven(epoch=epoch.read(), retained_custody=()).verdict is AccountSafetyVerdict.SUSPENDED

    clean = epoch.complete_reconciliation(_clean_proof(epoch.read()))
    lifted = safety.lift_if_proven(epoch=clean, retained_custody=())

    assert invalid.reconciliation_id == clean.last_reconciliation_id
    assert lifted.verdict is AccountSafetyVerdict.CLEAN
    assert lifted.last_reconciliation_id == clean.last_reconciliation_id


def test_retired_owner_suspension_never_lifts_while_nonterminal_custody_remains(tmp_path: Path) -> None:
    epoch = _epoch_authority(tmp_path)
    epoch.initialize()
    epoch.invalidate("RETIRED_OWNER_EXPOSURE")
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    )
    custody = _custody()
    safety.suspend_retired_owner_custody((custody,))
    safety.bind_reconciliation(epoch.read())
    clean = epoch.complete_reconciliation(_clean_proof(epoch.read()))

    still_suspended = safety.lift_if_proven(epoch=clean, retained_custody=(custody,))

    assert still_suspended.verdict is AccountSafetyVerdict.SUSPENDED
    with pytest.raises(AccountSafetyEntryBlockedError):
        safety.require_entry_admission()


def test_admission_lock_serializes_concurrent_writers(tmp_path: Path) -> None:
    """The fenced single-writer claim (ADR 0048 4f) excludes concurrent
    writers. Readers are no longer part of the protocol -- this replaces the
    retired reader/writer-turnstile coverage (``account_safety_entry_admission_lock``
    and the writer reservation it drained, both deleted under 4f)."""

    first_holds = Event()
    release_first = Event()
    second_entered = Event()

    def hold_first() -> None:
        with account_safety_admission_lock(tmp_path, ACCOUNT_ID):
            first_holds.set()
            assert release_first.wait(timeout=1)

    def acquire_second() -> None:
        assert first_holds.wait(timeout=1)
        with account_safety_admission_lock(tmp_path, ACCOUNT_ID):
            second_entered.set()

    first = Thread(target=hold_first)
    second = Thread(target=acquire_second)
    first.start()
    assert first_holds.wait(timeout=1)
    second.start()
    assert not second_entered.wait(timeout=0.05)

    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)
    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()


def test_read_falls_back_to_legacy_json_state_recorded_before_the_sqlite_migration(tmp_path: Path) -> None:
    """ADR 0048 4f moved ``AccountSafetyState``'s durable storage into the
    fenced SQLite claim store. State recorded before that move, in the
    legacy ``account_safety.json`` projection, must still be seen -- a
    ``SUSPENDED`` account must not silently become admitting-``CLEAN``
    just because its record predates the migration, and the
    broker-observed watermark ``mark_broker_clearance_required`` guards
    against moving backwards must not reset to zero.
    """

    custody = RetiredOwnerCustody(
        account_id=ACCOUNT_ID,
        strategy_instance_id="retired-legacy",
        intent_id="intent-legacy-1",
        order_ref="learn-ai/retired-legacy/v1:intent-legacy-1",
    )
    legacy_state = AccountSafetyState(
        account_id=ACCOUNT_ID,
        verdict=AccountSafetyVerdict.SUSPENDED,
        suspension=AccountSafetySuspension(
            suspension_id="account-suspension:legacy",
            custody=(custody,),
            detected_at_ms=NOW_MS,
        ),
        last_broker_observed_at_ms=NOW_MS,
        updated_at_ms=NOW_MS,
    )
    legacy_path = account_safety_path(tmp_path, ACCOUNT_ID)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(legacy_state.model_dump_json(), encoding="utf-8")

    # No admission claim and no SQLite write has ever happened for this
    # account -- exactly the state of every account at the moment this
    # migration ships.
    authority = AccountSafetyAuthority(artifacts_root=tmp_path, account_id=ACCOUNT_ID, now_ms=lambda: NOW_MS + 1)

    read_state = authority.read()
    assert read_state.verdict is AccountSafetyVerdict.SUSPENDED
    assert read_state.last_broker_observed_at_ms == NOW_MS

    with pytest.raises(AccountSafetyEntryBlockedError):
        authority.require_entry_admission()

    assert account_safety_state_exists(tmp_path, ACCOUNT_ID) is True


def test_account_safety_state_exists_true_for_legacy_json_only(tmp_path: Path) -> None:
    """``account_safety_state_exists`` is an operator-visible honesty signal
    -- it distinguishes "never observed" from "an explicit CLEAN was
    recorded" (``ACCOUNT_SAFETY_NOT_AVAILABLE`` on the account-truth
    snapshot). An account whose only recorded state is still the
    pre-migration JSON file has recorded state, not none.
    """

    legacy_state = AccountSafetyState(account_id=ACCOUNT_ID, updated_at_ms=NOW_MS)
    legacy_path = account_safety_path(tmp_path, ACCOUNT_ID)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(legacy_state.model_dump_json(), encoding="utf-8")

    assert account_safety_state_exists(tmp_path, ACCOUNT_ID) is True




# ---------------------------------------------------------------------------
# account_safety_blocks_current_bot — sibling-exemption regression (BUG-11)
# ---------------------------------------------------------------------------


def test_account_safety_blocks_current_bot_blocks_own_sid(tmp_path: Path) -> None:
    """A bot whose own SID is in custody must be blocked."""
    authority = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    )
    authority.suspend_retired_owner_custody(
        (
            RetiredOwnerCustody(
                account_id=ACCOUNT_ID,
                strategy_instance_id="dv-20260727-amd",
                intent_id="intent-amd-001",
                order_ref="learn-ai/dv-20260727-amd/v1:intent-amd-001",
            ),
        ),
    )
    safety = authority.read()

    assert account_safety_blocks_current_bot(safety, "dv-20260727-amd") is True


def test_account_safety_blocks_current_bot_exempts_sibling_sid(tmp_path: Path) -> None:
    """A bot not named in custody must NOT be blocked by a sibling's suspension.

    This is the root cause of the Jul 27 2026 cascade: AMD crashed with an
    in-flight intent, its namespace was RETIRED, Clerk detected nonterminal
    custody, suspended the account, and all 7 sibling bots were halted at
    their next bar evaluation.  After this fix each sibling's gate provider
    calls account_safety_blocks_current_bot() and sees its own SID is absent
    from custody, so it returns None (no block) instead of a block GateResult.
    """
    authority = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    )
    authority.suspend_retired_owner_custody(
        (
            RetiredOwnerCustody(
                account_id=ACCOUNT_ID,
                strategy_instance_id="dv-20260727-amd",
                intent_id="intent-amd-001",
                order_ref="learn-ai/dv-20260727-amd/v1:intent-amd-001",
            ),
        ),
    )
    safety = authority.read()

    # Siblings (SPY, QQQ, AAPL, …) must not be blocked
    for sibling_sid in ("dv-20260727-spy", "dv-20260727-qqq", "dv-20260727-aapl"):
        assert account_safety_blocks_current_bot(safety, sibling_sid) is False, sibling_sid


def test_cancel_confirmed_entry_resolves_custody() -> None:
    """cancel_confirmed must terminate retired-owner custody.

    Root cause of the 2026-07-29 SUSPENDED→infinite-epoch-invalidation loop:
    smoke-20260729-spy-2 crashed with SubmitUncertainHaltError; one of its
    intents had cancel_submitting + cancel_confirmed (order never reached the
    broker), but _custody_is_terminal did not check cancel_confirmed, so the
    intent was retained as non-terminal custody forever.  The Clerk's
    reconciliation loop kept re-invalidating the epoch (seq 819→835) without
    ever lifting the safety suspension.
    """
    strategy_instance_id = "crashed-spy-2"
    intent = AccountOwnerSubmitIntent(
        trace_id="trace-cancel-terminal",
        account_id=ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        run_id="run-crashed-spy-2",
        bot_order_namespace=bot_order_namespace_for_instance(strategy_instance_id),
        intent_id="intent-cancel-only",
        order_ref=f"learn-ai/{strategy_instance_id}/v1:intent-cancel-only",
        intent_kind="STRATEGY",
        order_spec={"symbol": "SPY"},
        owner_generation=1,
        created_at_ms=NOW_MS,
    )
    entries = [
        AccountClerkJournalEntry(seq=1, entry_kind="recorded", recorded_at_ms=NOW_MS, intent=intent),
        AccountClerkJournalEntry(seq=2, entry_kind="cancel_submitting", recorded_at_ms=NOW_MS, intent=intent),
        AccountClerkJournalEntry(seq=3, entry_kind="cancel_confirmed", recorded_at_ms=NOW_MS, intent=intent),
    ]

    retained = retired_owner_nonterminal_custody(entries, strategy_instance_id=strategy_instance_id)

    assert retained == (), f"cancel_confirmed must terminate custody, got: {retained}"
