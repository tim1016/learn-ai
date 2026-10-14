"""Decision receipt storage tests for the SQLite Clerk authority."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

import app.broker.alpaca.clerk.sqlite.decision_receipts as decision_receipts_module
from app.broker.alpaca.clerk.decision_evidence import EffectDecisionEvidence
from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    CRASH_CANDIDATE_OUTCOME,
    MAX_DECISION_RECEIPT_READ,
    MAX_DECISION_RECEIPTS_PER_STRATEGY,
    QUARANTINE_OUTCOME,
    AtomicDecisionReceipt,
    DecisionOutcome,
    DecisionReceiptConflictError,
    SqliteDecisionReceipts,
    rotated_out_before_ms,
    run_first_closes_ms,
)
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.errors import BrokerRequestInvalid
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from tests._helpers.bot_runner.doubles import _SqliteRuntimeBroker
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

pytestmark = pytest.mark.usefixtures("legacy_run_registration")


@pytest.fixture()
def repository(tmp_path: Path) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    repo.register_strategy_instance(
        strategy_instance_id="spy-bot",
        symbol="SPY",
        config_hash="test-config-hash",
    )
    yield repo
    repo.close()


@pytest.fixture()
def receipts(repository: ClerkSqliteRepository) -> SqliteDecisionReceipts:
    return SqliteDecisionReceipts(repository, strategy_instance_id="spy-bot")


def test_append_assigns_sequence_and_preserves_nullable_transaction_fields(
    receipts: SqliteDecisionReceipts,
) -> None:
    first = receipts.append(
        outcome="no_action",
        symbol=None,
        observed_at_ms=1_700_000_000_000,
        facts={"reason_code": "NO_ACTION", "bar_ref": "SPY@1700000000000"},
    )
    second = receipts.append(
        outcome="enter_intent",
        symbol="SPY",
        observed_at_ms=1_700_000_060_000,
        intent_id="intent-1",
        order_ref=None,
        facts={"bar_ref": "SPY@1700000060000", "reason_code": "STRATEGY_ENTER"},
    )

    assert first.seq == 1
    assert first.symbol is None
    assert first.intent_id is None
    assert first.order_ref is None
    assert second.seq == 2
    assert json.loads(second.facts_json) == {
        "bar_ref": "SPY@1700000060000",
        "reason_code": "STRATEGY_ENTER",
    }


def test_tail_clamps_and_returns_ascending_suffix(
    receipts: SqliteDecisionReceipts,
) -> None:
    for index in range(MAX_DECISION_RECEIPT_READ + 2):
        receipts.append(
            outcome="no_action",
            symbol="SPY",
            observed_at_ms=index,
            facts={"bar_ref": f"SPY@{index}", "reason_code": "NO_ACTION"},
        )

    tail = receipts.tail(MAX_DECISION_RECEIPT_READ + 10)

    assert len(tail) == MAX_DECISION_RECEIPT_READ
    assert tail[0].seq == 3
    assert tail[-1].seq == MAX_DECISION_RECEIPT_READ + 2


def test_retained_window_reads_the_whole_retention_not_the_presentation_cap(
    receipts: SqliteDecisionReceipts,
) -> None:
    """FR-016 replay (``captured_decision_outcomes``) must see every ordinary
    receipt still retained. A one-minute program writes more receipts over
    its warmup than ``MAX_DECISION_RECEIPT_READ`` returns; read through the
    presentation cap and the earliest buckets look uncaptured (#1740)."""
    for index in range(MAX_DECISION_RECEIPT_READ + 2):
        receipts.append(
            outcome="no_action",
            symbol="SPY",
            observed_at_ms=index,
            intent_id=f"{index}:NO_ACTION",
            facts={"bar_ref": f"SPY@{index}", "reason_code": "NO_ACTION"},
        )

    window = receipts.retained_window()

    assert len(window) == MAX_DECISION_RECEIPT_READ + 2
    assert window[0].seq == 1
    assert window[-1].seq == MAX_DECISION_RECEIPT_READ + 2
    assert len(receipts.tail(MAX_DECISION_RECEIPT_READ + 10)) == MAX_DECISION_RECEIPT_READ


def test_append_is_idempotent_per_closed_bar_and_rejects_conflicting_replay(
    receipts: SqliteDecisionReceipts,
) -> None:
    first = receipts.append(
        outcome="enter_intent",
        symbol="SPY",
        observed_at_ms=1,
        intent_id="1:ENTER",
        facts={"bar_ref": "SPY@1", "reason_code": "STRATEGY_ENTER"},
    )

    replay = receipts.append(
        outcome="enter_intent",
        symbol="SPY",
        observed_at_ms=2,
        intent_id="1:ENTER",
        facts={"bar_ref": "SPY@1", "reason_code": "STRATEGY_ENTER"},
    )

    assert replay == first
    with pytest.raises(DecisionReceiptConflictError, match="different decision"):
        receipts.append(
            outcome="exit_intent",
            symbol="SPY",
            observed_at_ms=3,
            intent_id="1:EXIT",
            facts={"bar_ref": "SPY@1", "reason_code": "STRATEGY_EXIT"},
        )


def test_protected_effect_history_survives_retention_pruning(
    repository: ClerkSqliteRepository, receipts: SqliteDecisionReceipts, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.alpaca.clerk.sqlite.decision_receipts as module

    monkeypatch.setattr(module, "MAX_DECISION_RECEIPTS_PER_STRATEGY", 3)
    for index in range(6):
        receipts.append(outcome="no_action", symbol="SPY", observed_at_ms=index, facts={
            "bar_ref": f"SPY@{index}", "retention_class": "protected_effect" if index == 0 else "ordinary",
        })
    retained = repository.decision_receipt_tail(strategy_instance_id="spy-bot", limit=500)
    assert [r.seq for r in retained] == [1, 4, 5, 6]


def test_append_prunes_receipts_beyond_the_retention_bound(
    receipts: SqliteDecisionReceipts,
    repository: ClerkSqliteRepository,
) -> None:
    for index in range(MAX_DECISION_RECEIPTS_PER_STRATEGY + 2):
        receipts.append(
            outcome="no_action",
            symbol="SPY",
            observed_at_ms=index,
            facts={"bar_ref": f"SPY@{index}", "reason_code": "NO_ACTION"},
        )

    retained = repository.decision_receipt_tail(
        strategy_instance_id="spy-bot",
        limit=MAX_DECISION_RECEIPTS_PER_STRATEGY,
    )

    assert len(retained) == MAX_DECISION_RECEIPTS_PER_STRATEGY
    assert retained[0].seq == 3
    assert retained[-1].seq == MAX_DECISION_RECEIPTS_PER_STRATEGY + 2


def test_candidate_uncaptured_at_crash_survives_retention_pruning(
    receipts: SqliteDecisionReceipts,
    repository: ClerkSqliteRepository,
) -> None:
    """FR-016 / AC #9: crash-window evidence must outlive the ordinary
    sequence-tail compaction the same way ``protected_effect`` and
    ``protected_refusal`` already do (decision_receipts.py:218-229)."""
    crash_receipt = receipts.append(
        outcome="candidate_uncaptured_at_crash",
        symbol="SPY",
        observed_at_ms=0,
        intent_id="crash-candidate-1",
        facts={
            "bar_ref": "SPY@0",
            "reason_code": "CANDIDATE_UNCAPTURED_AT_CRASH",
            "retention_class": "protected_crash_evidence",
        },
    )
    for index in range(1, MAX_DECISION_RECEIPTS_PER_STRATEGY + 6):
        receipts.append(
            outcome="no_action",
            symbol="SPY",
            observed_at_ms=index,
            facts={"bar_ref": f"SPY@{index}", "reason_code": "NO_ACTION"},
        )

    retained = repository.decision_receipt_tail(
        strategy_instance_id="spy-bot",
        limit=MAX_DECISION_RECEIPTS_PER_STRATEGY + 10,
    )
    retained_by_seq = {receipt.seq: receipt for receipt in retained}

    # Ordinary tail retention alone would have pruned seq 1 (and seq 2-6):
    # the newest row is seq MAX+6, so anything at or below seq 6 falls
    # outside the last MAX_DECISION_RECEIPTS_PER_STRATEGY rows.
    assert len(retained) == MAX_DECISION_RECEIPTS_PER_STRATEGY + 1
    assert 2 not in retained_by_seq
    survivor = retained_by_seq[crash_receipt.seq]
    assert survivor.outcome == "candidate_uncaptured_at_crash"
    assert json.loads(survivor.facts_json)["retention_class"] == "protected_crash_evidence"


# ---------------------------------------------------------------------------
# How far back the retained window reaches once it has dropped a receipt (#2850).
# ---------------------------------------------------------------------------

_SHORT_WINDOW = 3
_MINUTE_MS = 60_000
_FIRST_CLOSE_MS = 1_790_000_060_000


@pytest.fixture()
def short_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep and read three receipts, so a fourth drops the first."""
    monkeypatch.setattr(decision_receipts_module, "MAX_DECISION_RECEIPTS_PER_STRATEGY", _SHORT_WINDOW)


def _close_ms(minute: int) -> int:
    return _FIRST_CLOSE_MS + minute * _MINUTE_MS


def _receipt_bucket(
    receipts: SqliteDecisionReceipts,
    minute: int,
    *,
    outcome: DecisionOutcome = "no_action",
    records_close: bool = True,
    run_id: str | None = None,
    retention_class: str | None = None,
) -> None:
    """Receipt the bucket that closed at ``minute``, the way a runner or the Clerk does.

    A quarantine row names no evaluation. Every row's wall-clock
    ``observed_at_ms`` is far from any bar close, so a boundary read from it
    would be plainly wrong.
    """
    facts: dict[str, str | int] = {"bar_ref": f"SPY@{minute}"}
    if records_close:
        facts["decision_bar_close_ms"] = _close_ms(minute)
    if run_id is not None:
        facts["run_id"] = run_id
    if retention_class is not None:
        facts["retention_class"] = retention_class
    receipts.append(
        outcome=outcome,
        symbol="SPY",
        observed_at_ms=9_000_000_000_000 + minute,
        intent_id=None if outcome == QUARANTINE_OUTCOME else f"evaluation-{minute}",
        facts=facts,
    )


@pytest.mark.usefixtures("short_window")
def test_the_rotation_boundary_is_the_oldest_retained_decision_s_bar_close(
    receipts: SqliteDecisionReceipts,
) -> None:
    """A window that still opens at sequence 1 has dropped nothing, so a bucket
    missing from it was never receipted and no boundary is named. Once the
    prune drops a receipt, a bucket that closed before the oldest decision
    still retained may have lost its own: the boundary is that decision's bar
    close, not the later wall clock its receipt was written at."""
    for minute in range(_SHORT_WINDOW):
        _receipt_bucket(receipts, minute)
    assert rotated_out_before_ms(receipts.retained_window()) is None

    _receipt_bucket(receipts, _SHORT_WINDOW)

    window = receipts.retained_window()
    assert [receipt.seq for receipt in window] == [2, 3, 4]
    assert rotated_out_before_ms(window) == _close_ms(1)


@pytest.mark.usefixtures("short_window")
def test_the_rotation_boundary_passes_over_rows_not_written_when_their_bucket_was_decided(
    receipts: SqliteDecisionReceipts,
) -> None:
    """Crash evidence is written by a later Resume for an older bucket, and a
    quarantine row names no decision. Neither says how far back the retained
    decisions reach: reading the crash evidence's own (older) bar close as
    the boundary would leave every rotated-out bucket after it flaggable."""
    _receipt_bucket(receipts, 5)
    _receipt_bucket(receipts, 2, outcome=CRASH_CANDIDATE_OUTCOME)
    _receipt_bucket(receipts, 6, outcome=QUARANTINE_OUTCOME, records_close=False)
    _receipt_bucket(receipts, 7)

    window = receipts.retained_window()
    assert [receipt.outcome for receipt in window] == [CRASH_CANDIDATE_OUTCOME, QUARANTINE_OUTCOME, "no_action"]
    assert rotated_out_before_ms(window) == _close_ms(7)


@pytest.mark.usefixtures("short_window")
def test_the_rotation_boundary_is_not_read_from_a_later_decision_when_the_oldest_records_no_close(
    receipts: SqliteDecisionReceipts,
) -> None:
    """A later decision's close is a later boundary, and a later boundary
    exempts more: a bucket a crash left staged between the oldest decision and
    that later one would never be named. So when the oldest retained decision
    records no close there is no boundary at all, even though the decisions
    after it record theirs."""
    _receipt_bucket(receipts, 0)
    _receipt_bucket(receipts, 1, records_close=False)
    _receipt_bucket(receipts, 2)
    _receipt_bucket(receipts, 3)

    window = receipts.retained_window()
    assert [receipt.seq for receipt in window] == [2, 3, 4]
    assert rotated_out_before_ms(window) is None


@pytest.mark.usefixtures("short_window")
def test_a_rotated_window_that_records_no_bar_close_names_no_boundary_and_says_so(
    receipts: SqliteDecisionReceipts, caplog: pytest.LogCaptureFixture
) -> None:
    """The boundary is never guessed from when a receipt was written: a bucket
    decided late closed before that wall clock, so a real crash candidate
    could fall under it and go unflagged. With no close to read nothing is
    exempt, and the log says why the Resume that follows may fail."""
    for minute in range(_SHORT_WINDOW + 1):
        _receipt_bucket(receipts, minute, records_close=False)

    with caplog.at_level(logging.WARNING, logger=decision_receipts_module.__name__):
        boundary = rotated_out_before_ms(receipts.retained_window())

    assert boundary is None
    [record] = [
        record
        for record in caplog.records
        if getattr(record, "action", None) == "decision_receipt_rotation_boundary_unknown"
    ]
    assert record.strategy_instance_id == "spy-bot"
    assert (record.first_retained_seq, record.last_retained_seq) == (2, 4)


def test_a_run_starts_at_its_first_decision_not_at_the_crash_evidence_it_wrote(
    receipts: SqliteDecisionReceipts,
) -> None:
    """A Resume names a bucket an earlier run left staged before it decides
    anything itself, and that bucket can be older than the run in between
    (#2859). Read as the Resume's first decision, the crash evidence would
    put its start before that whole run, and replay would carry the run's
    position into the Resume's own buckets. A row that names no run says
    nothing either way."""
    _receipt_bucket(receipts, 2, run_id="run-2")
    _receipt_bucket(receipts, 3, outcome="enter_intent", run_id="spy-bot:run-2")
    _receipt_bucket(receipts, 4)
    _receipt_bucket(receipts, 1, outcome=CRASH_CANDIDATE_OUTCOME, run_id="run-3")
    _receipt_bucket(receipts, 5, run_id="run-3")
    _receipt_bucket(receipts, 6, run_id="run-3")

    assert run_first_closes_ms(receipts.retained_window()) == frozenset({_close_ms(5)})


# ---------------------------------------------------------------------------
# The position still open where the retained window starts (#2859).
# ---------------------------------------------------------------------------


def _enter(repository: ClerkSqliteRepository, minute: int, *, run: str) -> None:
    """An ENTER the Clerk accepted, through the store's own intake.

    One transaction writes the effect, its order and the protected receipt
    that names the effect, under the Clerk's own run id.
    """
    if repository.active_run("spy-bot") is None:
        submit_start_run(
            repository, account_id="PA-TEST", strategy_instance_id="spy-bot", lifecycle_run_id=run
        )
    accept_enter(
        repository,
        account_id="PA-TEST",
        strategy_instance_id="spy-bot",
        decision_id=f"evaluation-{minute}",
        lifecycle_run_id=run,
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        decision_receipt=AtomicDecisionReceipt(
            outcome="enter_intent",
            symbol="SPY",
            decision_id=f"evaluation-{minute}",
            observed_at_ms=9_000_000_000_000 + minute,
            facts_json=canonicalize(
                {"bar_ref": f"SPY@{minute}", "decision_bar_close_ms": _close_ms(minute)}
            ),
        ),
    )


def _refusal(receipts: SqliteDecisionReceipts, minute: int, *, run: str) -> None:
    _receipt_bucket(receipts, minute, outcome="blocked", run_id=run, retention_class="protected_refusal")


@pytest.mark.usefixtures("short_window")
def test_the_entry_still_held_where_the_window_starts_is_read_past_the_refusals_since(
    repository: ClerkSqliteRepository,
    receipts: SqliteDecisionReceipts,
) -> None:
    """An ENTER receipt is protected, so it is still stored once the window
    has moved past it. The run that entered is the run that made the window's
    oldest decision and nothing closed the position in between: it is still
    open there, and the refusals written since do not hide it."""
    _receipt_bucket(receipts, 0, run_id="run-1")
    _enter(repository, 1, run="run-1")
    _refusal(receipts, 2, run="run-1")
    for minute in (3, 4, 5):
        _receipt_bucket(receipts, minute, run_id="run-1")

    window = receipts.retained_window()
    assert [receipt.seq for receipt in window] == [4, 5, 6]
    held = receipts.entry_held_into(window)

    assert held is not None
    assert (held.seq, held.outcome, held.intent_id) == (2, "enter_intent", "evaluation-1")


@pytest.mark.usefixtures("short_window")
def test_no_entry_is_held_into_the_window_once_its_position_was_closed(
    repository: ClerkSqliteRepository,
    receipts: SqliteDecisionReceipts,
) -> None:
    """Only the last decision the position moved on says what it was at the
    window's start. After an EXIT that is flat: replaying the older ENTER
    there would hold a position the run had already closed."""
    _enter(repository, 0, run="run-1")
    _receipt_bucket(
        receipts, 1, outcome="exit_intent", run_id="spy-bot:run-1", retention_class="protected_effect"
    )
    for minute in (2, 3, 4):
        _receipt_bucket(receipts, minute, run_id="run-1")

    window = receipts.retained_window()
    assert [receipt.seq for receipt in window] == [3, 4, 5]
    assert receipts.entry_held_into(window) is None


@pytest.mark.usefixtures("short_window")
def test_no_entry_is_held_into_a_window_that_another_run_starts(
    repository: ClerkSqliteRepository,
    receipts: SqliteDecisionReceipts,
) -> None:
    """A run starts flat. An ENTER the run before it was stopped holding is
    not the later run's position, however recently it was made."""
    _enter(repository, 0, run="run-1")
    for minute in (1, 2, 3):
        _receipt_bucket(receipts, minute, run_id="run-2")

    window = receipts.retained_window()
    assert [receipt.seq for receipt in window] == [2, 3, 4]
    assert receipts.entry_held_into(window) is None


@pytest.mark.usefixtures("short_window")
def test_an_entry_held_through_more_refusals_than_the_read_reaches_is_not_found_and_says_so(
    repository: ClerkSqliteRepository,
    receipts: SqliteDecisionReceipts,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The read of rows older than the window is bounded. When it ends
    without reaching any ENTER or EXIT, the position at the window's start is
    unknown: nothing is replayed as held, and the log says the read fell
    short rather than leaving a Resume to look flat for no stated reason."""
    _enter(repository, 0, run="run-1")
    for minute in (1, 2, 3):
        _refusal(receipts, minute, run="run-1")
    for minute in (4, 5, 6):
        _receipt_bucket(receipts, minute, run_id="run-1")

    window = receipts.retained_window()
    assert [receipt.seq for receipt in window] == [5, 6, 7]
    with caplog.at_level(logging.WARNING, logger=decision_receipts_module.__name__):
        held = receipts.entry_held_into(window)

    assert held is None
    [record] = [
        record
        for record in caplog.records
        if getattr(record, "action", None) == "decision_receipt_held_entry_unknown"
    ]
    assert (record.first_read_seq, record.first_retained_seq) == (2, 5)


@pytest.mark.usefixtures("short_window")
def test_an_entry_that_names_no_effect_on_record_is_not_held_and_says_so(
    receipts: SqliteDecisionReceipts, caplog: pytest.LogCaptureFixture
) -> None:
    """An ENTER receipt is written when the ENTER is accepted; whether a
    position followed is on its effect. A receipt that names no effect on
    record shows no position: nothing is replayed as held, and the log says
    which row could not be read rather than leaving a Resume flat for no
    stated reason."""
    _receipt_bucket(
        receipts,
        0,
        outcome="enter_intent",
        run_id="spy-bot:run-1",
        retention_class="protected_effect",
    )
    for minute in (1, 2, 3):
        _receipt_bucket(receipts, minute, run_id="run-1")

    window = receipts.retained_window()
    assert [receipt.seq for receipt in window] == [2, 3, 4]
    with caplog.at_level(logging.WARNING, logger=decision_receipts_module.__name__):
        held = receipts.entry_held_into(window)

    assert held is None
    [record] = [
        record
        for record in caplog.records
        if getattr(record, "action", None) == "decision_receipt_held_entry_effect_unknown"
    ]
    assert (record.strategy_instance_id, record.entry_seq, record.effect_operation_id) == (
        "spy-bot",
        1,
        None,
    )


class _RefusingBroker(_SqliteRuntimeBroker):
    """A broker that refuses every order it is sent."""

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        raise BrokerRequestInvalid("the broker refused this order", broker="alpaca")


@pytest.mark.usefixtures("short_window", "wall_clock_in_session")
@pytest.mark.parametrize("refused", [False, True])
async def test_the_real_clerks_enter_is_held_into_the_window_unless_the_broker_refused_it(
    tmp_path: Path, refused: bool
) -> None:
    """The real Clerk writes the ENTER's receipt when it accepts it, before it
    reaches for the broker, and never amends it. When the broker then refuses
    the order the Clerk answers the runner as for a refusal, and the
    strategy stays flat -- but the receipt stored past the window reads
    ``enter_intent`` either way. Only the effect it names says which
    happened, and that is what decides whether the ENTER is held (#2859)."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    broker = _RefusingBroker() if refused else _SqliteRuntimeBroker()
    clerk = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, account_mode="paper")
    binding = BrokerBotBinding(
        exit_terms=DEPLOY_EXIT_TERMS,
        strategy_instance_id="spy-bot",
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        sealed_account_id="PA-TEST",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-1",
        created_at_ms=1,
    )
    await clerk.register_strategy_run(binding)
    receipts = SqliteDecisionReceipts(repo, strategy_instance_id="spy-bot")
    evaluation_id = "a" * 64

    answer = await clerk.execute_for_instance(
        strategy_instance_id="spy-bot",
        run_id="run-1",
        decision_id=evaluation_id,
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=binding.quantity,
        decision_evidence=EffectDecisionEvidence(
            evaluation_id=evaluation_id,
            bar_ref="decision-bar:test:SPY:0",
            symbol="SPY",
            outcome="enter_intent",
            observed_at_ms=9_000_000_000_000,
            decision_bar_close_ms=_close_ms(0),
        ),
    )
    for minute in (1, 2, 3):
        _receipt_bucket(receipts, minute, run_id="run-1")

    window = receipts.retained_window()
    assert [receipt.seq for receipt in window] == [2, 3, 4]
    [entry] = repo.decision_receipt_tail(
        strategy_instance_id="spy-bot", limit=_SHORT_WINDOW, before_seq=window[0].seq
    )
    assert (entry.outcome, entry.intent_id) == ("enter_intent", evaluation_id)
    effect = repo.effect_operation(json.loads(entry.facts_json)["effect_operation_id"])
    assert effect is not None
    if refused:
        assert answer.state is EffectOperationState.REJECTED
        assert effect.state == "failed"
        assert receipts.entry_held_into(window) is None
    else:
        assert answer.state is EffectOperationState.SUBMITTED
        assert effect.state == "in_progress"
        assert receipts.entry_held_into(window) == entry
    repo.close()


def test_receipts_do_not_advance_custody_transition_or_control_revision(
    repository: ClerkSqliteRepository,
    receipts: SqliteDecisionReceipts,
) -> None:
    before = repository.control_meta_snapshot()
    transitions_before = repository.custody_transitions()

    receipts.append(
        outcome="no_action",
        symbol="SPY",
        observed_at_ms=1,
        facts={"reason_code": "NO_ACTION"},
    )

    assert repository.control_meta_snapshot().control_revision == before.control_revision
    assert repository.custody_transitions() == transitions_before


def test_write_rejects_missing_strategy_and_reader_rejects_invalid_bounds(
    repository: ClerkSqliteRepository,
    receipts: SqliteDecisionReceipts,
) -> None:
    unregistered = SqliteDecisionReceipts(repository, strategy_instance_id="missing")

    with pytest.raises(sqlite3.IntegrityError):
        unregistered.append(
            outcome="no_action",
            symbol="SPY",
            observed_at_ms=1,
            facts={"reason_code": "NO_ACTION"},
        )
    with pytest.raises(ValueError, match="positive"):
        receipts.tail(0)
