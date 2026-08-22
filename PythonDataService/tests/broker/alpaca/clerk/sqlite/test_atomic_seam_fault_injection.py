"""Fault-injection coverage at the Slice-2 atomic decision-to-custody seam.

Issue #1728 (PRD section 16 "Decision and custody protocol", FR-018 through
FR-024, FR-028), acceptance criterion #11: "Fault injection around every
durable write and port-contact boundary proves no orphaned effect, duplicate
contact, silent divergence, or real-account write."

Slice 2 moved decision-receipt capture off the old provisional/final
two-step pattern onto one atomic write --
:func:`~app.broker.alpaca.clerk.sqlite.decision_receipts.append_atomic_decision_receipt_row`,
committed by :meth:`ClerkSqliteRepository._commit_transition_row` in the SAME
SQLite transaction as the ENTER/EXIT custody transition, before any broker
contact. ``tests/broker/alpaca/clerk/sqlite/test_enter.py`` already proves
general crash/lost-response recovery at the ENTER function level; these
tests target the specific new seam this slice introduced and the four
invariants the acceptance criterion requires there:

1. a crash between the atomic accept and broker contact leaves no orphaned
   effect and causes no duplicate broker contact on retry;
2. an unknown broker acknowledgement recovers the SAME ``effect_operation_id``
   without a second port contact (FR-024);
3. a repeated identical evaluation returns the original result, and
   conflicting evidence under the same identity quarantines rather than
   silently overwriting (FR-019, the ``DecisionReceiptConflictError`` path);
4. a Dry Run / synthetic-authority failure can never produce a write against
   a real account (FR-028).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.account_authority import AccountAuthorityIdentityError
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    AtomicDecisionReceipt,
    DecisionReceiptConflictError,
    DecisionReceiptResource,
    append_atomic_decision_receipt_row,
)
from app.broker.alpaca.clerk.sqlite.enter import (
    accept_enter,
    resolve_enter_submission,
    submit_enter,
)
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"
RUN_ID = "run-1"


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    clock = _clock_at(1_700_000_000_000)
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield r
    r.close()


def _leg(**overrides: Any) -> BrokerOrderLeg:
    base: dict[str, Any] = {"symbol": "SPY", "side": "buy", "quantity": 1}
    base.update(overrides)
    return BrokerOrderLeg(**base)


def _broker_order(client_order_id: str, *, order_id: str = "broker-order-1") -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id=order_id,
        client_order_id=client_order_id,
        symbol="SPY",
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=1.0,
        filled_quantity=0.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=None,
        status="accepted",
        submitted_at_ms=1_700_000_000_100,
        created_at_ms=1_700_000_000_100,
        updated_at_ms=1_700_000_000_500,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=1_700_000_000_500,
    )


class _FakeTrade:
    """Minimal ``BrokerTradePort`` double: submit/lookup, both call-counted."""

    def __init__(
        self,
        *,
        submit_error: Exception | None = None,
        lookup_result: BrokerOrder | None = None,
        lookup_absent: bool = False,
    ) -> None:
        self._submit_error = submit_error
        self._lookup_result = lookup_result
        self._lookup_absent = lookup_absent
        self.submit_calls: list[tuple[BrokerOrderLeg, str]] = []
        self.lookup_calls: list[str] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append((leg, client_order_id))
        if self._submit_error is not None:
            raise self._submit_error
        return _broker_order(client_order_id)

    async def cancel(self, order_id: str) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        if self._lookup_absent:
            return None
        return self._lookup_result or _broker_order(client_order_id)


def _atomic_receipt(
    *,
    decision_id: str,
    outcome: str = "enter_intent",
    bar_ref: str = "decision-bar:test:SPY:1700000000000",
    observed_at_ms: int = 1_700_000_000_000,
) -> AtomicDecisionReceipt:
    return AtomicDecisionReceipt(
        outcome=outcome,  # type: ignore[arg-type]
        symbol="SPY",
        decision_id=decision_id,
        observed_at_ms=observed_at_ms,
        facts_json=json.dumps(
            {
                "bar_ref": bar_ref,
                "decision_id": decision_id,
                "evaluation_id": decision_id,
                "reason_code": "STRATEGY_ENTER",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


# ── 1. Crash between the atomic accept and broker contact ──────────────────


async def test_crash_after_atomic_accept_leaves_no_orphan_and_no_duplicate_broker_contact_on_retry(
    repo: ClerkSqliteRepository,
) -> None:
    """The ENTER_ACCEPTED transition, its fold, and the atomic decision
    receipt are one SQLite transaction, durable before any broker call. A
    crash right after that commit (simulated here by simply never calling
    ``submit_accepted_enter``) must still leave a durable, recoverable
    effect -- not an orphan -- and recovery must never contact the broker
    twice for it."""
    receipt = _atomic_receipt(decision_id="dec-1")
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        decision_receipt=receipt,
    )
    assert accepted.created
    assert accepted.order_ref is not None

    before = repo.decision_receipt_tail(strategy_instance_id=SID, limit=10)
    assert [d.outcome for d in before] == ["enter_intent"]
    assert before[0].intent_id == "dec-1"

    # "Restart": a fresh trade double that never had submit() called before
    # this process came back up.
    trade = _FakeTrade()
    resolved = await resolve_enter_submission(repo, order_ref=accepted.order_ref, trade=trade)

    assert trade.submit_calls == []  # recovery resolves by identity, never resubmits
    assert len(trade.lookup_calls) == 1
    effect = repo.effect_operation(resolved.effect_operation_id)
    assert effect is not None and effect.state == "in_progress"  # recovered, not stranded

    # The atomic receipt from the original accept is untouched -- recovery
    # does not duplicate or rewrite it.
    after = repo.decision_receipt_tail(strategy_instance_id=SID, limit=10)
    assert len(after) == 1
    assert after[0].seq == before[0].seq

    # A genuine retry of the identical decision post-recovery is the same
    # content-addressed command: no new effect, no new broker contact.
    retry = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        decision_receipt=receipt,
    )
    assert not retry.created
    assert retry.effect_operation_id == accepted.effect_operation_id
    assert trade.submit_calls == []
    assert repo.decision_receipt_tail(strategy_instance_id=SID, limit=10) == after


# ── 2. Unknown acknowledgement recovers the same operation (FR-024) ────────


async def test_unknown_acknowledgement_recovers_same_effect_operation_id_without_second_broker_contact(
    repo: ClerkSqliteRepository,
) -> None:
    """FR-024: when the broker's submit acknowledgement is lost
    (``BrokerUnavailable``), the effect folds ``unknown``. Once later broker
    truth proves the order actually landed, recovery must resolve the SAME
    ``effect_operation_id`` by identity lookup -- never by submitting a
    second order."""
    receipt = _atomic_receipt(decision_id="dec-1")
    lost = _FakeTrade(submit_error=BrokerUnavailable("timeout", broker="alpaca"), lookup_absent=True)
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=lost,
        decision_receipt=receipt,
    )
    assert len(lost.submit_calls) == 1  # the one real write attempt
    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "unknown"
    original_effect_operation_id = submission.effect_operation_id

    # The broker actually received the order; the ack alone was lost.
    found = _FakeTrade(lookup_result=_broker_order(submission.order_ref))
    resolved = await resolve_enter_submission(repo, order_ref=submission.order_ref, trade=found)

    assert resolved.effect_operation_id == original_effect_operation_id
    assert found.submit_calls == []  # never a second port contact
    assert len(found.lookup_calls) == 1
    recovered = repo.effect_operation(resolved.effect_operation_id)
    assert recovered is not None and recovered.state == "in_progress"

    decisions = repo.decision_receipt_tail(strategy_instance_id=SID, limit=10)
    assert len(decisions) == 1
    assert decisions[0].intent_id == "dec-1"


# ── 3. Replay/conflict at the atomic receipt primitive (FR-019) ────────────


def test_atomic_decision_receipt_replay_is_idempotent_and_conflicting_replay_quarantines(
    repo: ClerkSqliteRepository,
) -> None:
    """PRD section 16: "replay of identical evidence returns the original
    result; conflicting reuse is quarantined." Exercises
    ``append_atomic_decision_receipt_row`` directly -- the exact primitive
    ``_commit_transition_row`` calls inside its own open transaction -- so
    this proves the invariant at the real atomic-write function, not at a
    higher command-idempotency shortcut that would never reach it (a
    same-payload command retry short-circuits before rebuilding the
    transition at all)."""

    def write(receipt: AtomicDecisionReceipt) -> DecisionReceiptResource:
        repo._conn.execute("BEGIN IMMEDIATE")
        try:
            result = append_atomic_decision_receipt_row(
                repo._conn,
                strategy_instance_id=SID,
                run_id=RUN_ID,
                effect_operation_id="effect:spy-bot:dec-1",
                order_ref=None,
                receipt=receipt,
            )
        except Exception:
            repo._conn.rollback()
            raise
        else:
            repo._conn.commit()
        return result

    original = write(_atomic_receipt(decision_id="dec-1"))
    assert original.seq == 1

    # Identical replay (same decision identity, same facts) returns the
    # original row -- no new sequence number, no duplicate row.
    replay = write(_atomic_receipt(decision_id="dec-1"))
    assert replay == original
    assert repo.decision_receipt_tail(strategy_instance_id=SID, limit=10) == [original]

    # Conflicting reuse under the SAME decision identity (a different bar)
    # must quarantine, never silently overwrite the original evidence.
    with pytest.raises(DecisionReceiptConflictError):
        write(_atomic_receipt(decision_id="dec-1", bar_ref="decision-bar:test:SPY:999"))

    assert repo.decision_receipt_tail(strategy_instance_id=SID, limit=10) == [original]


# ── 3b. The commit_first_transition shortcut cannot bypass that check ──────


def test_commit_first_transition_shortcut_surfaces_diverging_decision_receipt_as_a_conflict(
    repo: ClerkSqliteRepository,
) -> None:
    """Open-PR-review finding: ``payload_hash`` covers only
    ``(account_id, strategy_instance_id, decision_id, action, leg)`` --
    never ``decision_receipt``. A replay under the identical idempotency
    key and payload but a *different* decision receipt (a different
    ``bar_ref``, i.e. different evaluation evidence) used to hit
    ``commit_first_transition``'s ``CommandExistingSame`` shortcut, which
    returns before ever calling ``append_transition`` again -- so the new
    receipt was silently discarded and never even compared against what
    was stored. This proves the shortcut itself (not
    ``append_atomic_decision_receipt_row`` in isolation, which
    the test above already covers) now durably surfaces that divergence as
    a conflict instead."""
    original = _atomic_receipt(decision_id="dec-1", bar_ref="decision-bar:test:SPY:1")
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        decision_receipt=original,
    )
    assert accepted.created

    before = repo.decision_receipt_tail(strategy_instance_id=SID, limit=10)
    assert len(before) == 1
    assert json.loads(before[0].facts_json)["bar_ref"] == "decision-bar:test:SPY:1"

    diverging = _atomic_receipt(decision_id="dec-1", bar_ref="decision-bar:test:SPY:999")
    with pytest.raises(DurableConflictError):
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="dec-1",
            lifecycle_run_id=RUN_ID,
            leg=_leg(),
            decision_receipt=diverging,
        )

    # Append-only: the durable conflict never rewrites or appends anything.
    after = repo.decision_receipt_tail(strategy_instance_id=SID, limit=10)
    assert after == before
    assert json.loads(after[0].facts_json)["bar_ref"] == "decision-bar:test:SPY:1"


def test_commit_first_transition_shortcut_is_idempotent_for_a_byte_identical_replay(
    repo: ClerkSqliteRepository,
) -> None:
    """The flip side of the conflict test above: a genuine transport retry
    -- identical decision receipt, not merely identical command payload --
    must still take the fast, no-append ``CommandExistingSame`` path."""
    receipt = _atomic_receipt(decision_id="dec-1")
    first = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        decision_receipt=receipt,
    )
    assert first.created
    before = repo.decision_receipt_tail(strategy_instance_id=SID, limit=10)

    retry = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        decision_receipt=_atomic_receipt(decision_id="dec-1"),
    )

    assert not retry.created
    assert retry.effect_operation_id == first.effect_operation_id
    assert repo.decision_receipt_tail(strategy_instance_id=SID, limit=10) == before


# ── 4. Synthetic authority can never write a real account (FR-028) ─────────


def test_synthetic_authority_construction_refuses_a_real_account_repo(tmp_path: Path) -> None:
    """FR-028 / FR-026: a Dry Run (synthetic-authority) facade must refuse to
    bind a real-paper account's repository at construction, and a real-paper
    facade must refuse a synthetic (``sim:``) repository. ``self._repo`` is
    fixed for a facade's entire lifetime and no runtime call can substitute a
    different one, so a construction-time refusal is the strongest possible
    proof that a synthetic-authority failure can never reach a real
    account."""
    real_repo = ClerkSqliteRepository.initialize(account_id="PA-REAL", artifacts_root=tmp_path / "real")
    dummy_broker = SyntheticBroker(account_id="sim:whatever")
    try:
        with pytest.raises(AccountAuthorityIdentityError):
            SqliteAlpacaClerkFacade(
                repo=real_repo,
                read=dummy_broker,
                trade=dummy_broker,
                authority_kind="synthetic",
            )
        # Refused before any write landed against the real account.
        assert real_repo.custody_transitions() == []
    finally:
        real_repo.close()

    synthetic_repo = ClerkSqliteRepository.initialize(
        account_id="sim:ema-1", artifacts_root=tmp_path / "synthetic"
    )
    try:
        with pytest.raises(AccountAuthorityIdentityError):
            SqliteAlpacaClerkFacade(
                repo=synthetic_repo,
                read=dummy_broker,
                trade=dummy_broker,
                authority_kind="sqlite",
            )
        assert synthetic_repo.custody_transitions() == []
    finally:
        synthetic_repo.close()
