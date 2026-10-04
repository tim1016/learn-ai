"""Bounded SQLite read/write API for strategy-decision receipts.

Decision receipts are trader-facing strategy evidence. They are intentionally
separate from Clerk custody transitions: recording a decision neither claims
broker custody nor changes account control revision.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource
from app.broker.alpaca.clerk.sqlite.reads import FAILED_EFFECT_STATES
from app.schemas.decision_explanation import DecisionExplanationRecord
from app.utils.session_anchors import MAX_TIMESTAMP_MS

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

logger = logging.getLogger(__name__)

MAX_DECISION_RECEIPT_READ = 500
MAX_DECISION_RECEIPTS_PER_STRATEGY = 1_000

JsonScalar: TypeAlias = str | int | float | bool | None  # noqa: UP040 (Python 3.11)
JsonValue: TypeAlias = (  # noqa: UP040 (Python 3.11)
    JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
)
DecisionOutcome = Literal[
    "enter_intent",
    "exit_intent",
    "entered",
    "exited",
    "no_action",
    "blocked",
    # FR-016: replay recreated a staged candidate with no Clerk disposition
    # -- the process crashed after `SignalSession.advance()` staged it but
    # before intake captured it. DISCARD is applied and no effect is ever
    # created; see ADR 0042 (every staged candidate gets a Clerk disposition)
    # and the `CANDIDATE_UNCAPTURED_AT_CRASH` reason code.
    "candidate_uncaptured_at_crash",
    # Issue #1827: `SignalSession.advance` refused a decision bar outright
    # (TIMEFRAME_MISMATCH / NON_MONOTONIC_DECISION_CLOCK / UNSETTLED_STAGE),
    # so no decision cycle ran at all. Distinct from `no_action`, where a
    # cycle ran and decided nothing: this row has no evaluation identity
    # because no evaluation exists. Written once per reason per runtime and
    # excluded from replay alignment -- see
    # `app/services/bot_decision_quarantine.py`.
    "decision_bar_quarantined",
]

# Named because three modules outside this one must recognise this specific
# outcome -- the journal that writes it, the replay proof that must exclude
# it from decision alignment, and the panel station that must not render it
# as a satisfied signal. A literal repeated across those is a literal that
# eventually disagrees with itself.
QUARANTINE_OUTCOME: DecisionOutcome = "decision_bar_quarantined"

# Named for the same reason: the runner writes it, and a Resume's replay must
# recognise it as the one decision receipt written by a replay rather than
# when its bucket was decided -- so the only one that can sit out of bar
# order (#2850).
CRASH_CANDIDATE_OUTCOME: DecisionOutcome = "candidate_uncaptured_at_crash"

# Named for the same reason: these are the outcomes a strategy's position
# moved on, which a Resume's replay reapplies, the replay proof aligns on and
# the store reads to find the position still open at the start of its window
# (#2859).
ENTER_OUTCOMES: frozenset[DecisionOutcome] = frozenset({"enter_intent", "entered"})
EXIT_OUTCOMES: frozenset[DecisionOutcome] = frozenset({"exit_intent", "exited"})


class DecisionReceipt(BaseModel):
    """Trader-facing view of one SQLite decision receipt."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    ts_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    bar_ref: str
    outcome: DecisionOutcome
    reason_code: str
    intent_id: str = ""
    order_ref: str = ""
    # The run this decision belongs to, as its facts name it: a lifecycle run
    # id on runner-written rows, ``<sid>:<lifecycle>`` on Clerk effect rows.
    run_id: str | None = None
    decision_bar_close_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    # #2639: the decision bar, the bot's own values and each rule's
    # pass/fail. ``None`` on a row written before decisions recorded them,
    # or -- with ``explanation_unreadable`` -- on one this build cannot read.
    explanation: DecisionExplanationRecord | None = None
    explanation_unreadable: bool = False
    # Stored causal identity, written by
    # ``append_atomic_decision_receipt_row`` in this same module
    # (``decision_id`` == ``evaluation_id``). It belongs on the receipt row
    # it describes, not in a structure paired alongside it. A row with
    # neither key renders both ``None`` -- an explicit absence the panel
    # states, never a link inferred from timing or proximity.
    decision_id: str | None = None
    effect_operation_id: str | None = None


class DecisionReceiptConflictError(ValueError):
    """One closed bar was replayed with a different strategy decision."""


@dataclass(frozen=True)
class AtomicDecisionReceipt:
    """Receipt fields committed in the same transaction as a custody effect."""

    outcome: Literal["enter_intent", "exit_intent"]
    symbol: str
    decision_id: str
    observed_at_ms: int
    facts_json: str


def append_decision_receipt_row(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    outcome: str,
    symbol: str | None,
    intent_id: str | None,
    order_ref: str | None,
    observed_at_ms: int,
    facts_json: str,
) -> DecisionReceiptResource:
    """Allocate and insert one receipt in its own SQLite transaction.

    The repository performs the lease/poison checks and holds its application
    write coordinator before invoking this helper. Keeping allocation and SQL
    here makes this module the sole owner of receipt persistence without
    widening the repository's transaction responsibilities.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        receipt = _append_decision_receipt_in_transaction(
            conn,
            strategy_instance_id=strategy_instance_id,
            outcome=outcome,
            symbol=symbol,
            intent_id=intent_id,
            order_ref=order_ref,
            observed_at_ms=observed_at_ms,
            facts_json=facts_json,
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return receipt


def append_atomic_decision_receipt_row(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    run_id: str,
    effect_operation_id: str,
    order_ref: str | None,
    receipt: AtomicDecisionReceipt,
) -> DecisionReceiptResource:
    """Insert effect-bearing evidence inside an already-open custody transaction."""
    facts_json = _enrich_atomic_receipt_facts(
        receipt,
        run_id=run_id,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        retention_class="protected_effect",
        resolved_to_existing_effect=False,
    )
    return _append_decision_receipt_in_transaction(
        conn,
        strategy_instance_id=strategy_instance_id,
        outcome=receipt.outcome,
        symbol=receipt.symbol,
        intent_id=receipt.decision_id,
        order_ref=order_ref,
        observed_at_ms=receipt.observed_at_ms,
        facts_json=facts_json,
    )


def _enrich_atomic_receipt_facts(
    receipt: AtomicDecisionReceipt,
    *,
    run_id: str | None,
    effect_operation_id: str | None,
    order_ref: str | None,
    retention_class: str,
    resolved_to_existing_effect: bool,
) -> str:
    """Shape one atomic receipt's facts exactly as the durable row stores
    them.

    Shared by :func:`append_atomic_decision_receipt_row` (the write path),
    :func:`atomic_decision_receipt_conflicts_with_existing` (the pre-write
    divergence check ``commit_first_transition`` runs before it will ever
    treat a replay as idempotent), and
    :func:`append_competing_decision_receipt_row` (a losing EXIT's own
    evidence) so the three shapes never drift apart.
    ``effect_operation_id`` is a causal link either way: whether this
    decision produced that effect itself (``resolved_to_existing_effect``
    False) or lost a race and merely resolved to one that already existed
    (True) is the one bit a causal reader needs to tell the two apart.
    """
    facts = json.loads(receipt.facts_json)
    if not isinstance(facts, dict):
        raise ValueError("atomic decision receipt facts must be a JSON object")
    facts.update(
        {
            "decision_id": receipt.decision_id,
            "evaluation_id": receipt.decision_id,
            "run_id": run_id,
            "effect_operation_id": effect_operation_id,
            "order_ref": order_ref,
            "retention_class": retention_class,
            "resolved_to_existing_effect": resolved_to_existing_effect,
        }
    )
    return canonicalize(facts)


def atomic_decision_receipt_conflicts_with_existing(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    run_id: str | None,
    effect_operation_id: str | None,
    receipt: AtomicDecisionReceipt,
) -> bool:
    """Whether replaying ``receipt`` under an already-durable command
    diverges from the decision evidence that command already captured.

    ``commit_first_transition``'s existing-same shortcut returns before
    ever calling :meth:`ClerkSqliteRepository.append_transition` again, so
    it never re-enters ``_commit_transition_row`` and never reaches
    :func:`append_atomic_decision_receipt_row`'s own conflict detection --
    a same-key/same-payload replay short-circuits before rebuilding the
    transition at all. This performs the identical ``_same_decision``
    comparison at that one shortcut, so a differing receipt is surfaced as
    a durable conflict instead of silently discarded. No stored receipt at
    all for an already-durable command counts as a divergence too: a
    receipt supplied now that was never captured originally is exactly the
    same "evidence would otherwise vanish" shape, just with nothing to
    diff against.
    """
    candidate_facts_json = _enrich_atomic_receipt_facts(
        receipt,
        run_id=run_id,
        effect_operation_id=effect_operation_id,
        order_ref=None,
        retention_class="protected_effect",
        resolved_to_existing_effect=False,
    )
    existing = _receipt_for_decision(
        conn,
        strategy_instance_id=strategy_instance_id,
        decision_id=receipt.decision_id,
    )
    if existing is None:
        return True
    return not _same_decision(
        existing,
        outcome=receipt.outcome,
        symbol=receipt.symbol,
        intent_id=receipt.decision_id,
        facts_json=candidate_facts_json,
    )


def append_competing_decision_receipt_row(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    run_id: str | None,
    resolved_effect_operation_id: str,
    receipt: AtomicDecisionReceipt,
) -> DecisionReceiptResource:
    """Durably capture a losing decision that resolved to an already-active
    effect instead of creating one of its own.

    FR-022: a competing EXIT returns typed existing custody. FR-018
    requires every Clerk intake outcome -- including this one -- to
    atomically capture its decision receipt: the evaluation that lost the
    race still happened and needs durable evidence, including the link a
    causal reader needs to explain why it produced no new effect of its
    own. Reuses :func:`append_decision_receipt_row`'s own
    idempotent/conflict-detecting insert: a byte-identical replay is a
    no-op, a diverging one raises ``DecisionReceiptConflictError`` rather
    than silently losing evidence.
    """
    facts_json = _enrich_atomic_receipt_facts(
        receipt,
        run_id=run_id,
        effect_operation_id=resolved_effect_operation_id,
        order_ref=None,
        retention_class="protected_competing_exit",
        resolved_to_existing_effect=True,
    )
    return append_decision_receipt_row(
        conn,
        strategy_instance_id=strategy_instance_id,
        outcome=receipt.outcome,
        symbol=receipt.symbol,
        intent_id=receipt.decision_id,
        order_ref=None,
        observed_at_ms=receipt.observed_at_ms,
        facts_json=facts_json,
    )


def _append_decision_receipt_in_transaction(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    outcome: str,
    symbol: str | None,
    intent_id: str | None,
    order_ref: str | None,
    observed_at_ms: int,
    facts_json: str,
) -> DecisionReceiptResource:
    decision_id = _decision_id(facts_json) or intent_id
    bar_ref = _bar_ref(facts_json)
    decision_existing = (
        _receipt_for_decision(
            conn,
            strategy_instance_id=strategy_instance_id,
            decision_id=decision_id,
        )
        if decision_id is not None
        else None
    )
    bar_existing = (
        _receipt_for_bar(
            conn,
            strategy_instance_id=strategy_instance_id,
            bar_ref=bar_ref,
        )
        if bar_ref is not None
        else None
    )
    if (
        decision_existing is not None
        and bar_existing is not None
        and decision_existing.seq != bar_existing.seq
    ):
        raise DecisionReceiptConflictError(
            "decision identity and closed bar resolve to different durable receipts"
        )
    existing = decision_existing or bar_existing
    if existing is not None:
        if _same_decision(
            existing,
            outcome=outcome,
            symbol=symbol,
            intent_id=intent_id,
            facts_json=facts_json,
        ):
            return existing
        raise DecisionReceiptConflictError(
            f"decision {decision_id or bar_ref!r} already has a different decision receipt"
        )
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM decision_receipts "
        "WHERE strategy_instance_id = ?",
        (strategy_instance_id,),
    ).fetchone()
    assert row is not None
    seq = int(row[0])
    conn.execute(
        "INSERT INTO decision_receipts "
        "(strategy_instance_id, seq, outcome, symbol, intent_id, order_ref, "
        "observed_at_ms, facts_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            strategy_instance_id,
            seq,
            outcome,
            symbol,
            intent_id,
            order_ref,
            observed_at_ms,
            facts_json,
        ),
    )
    conn.execute(
        "DELETE FROM decision_receipts "
        "WHERE strategy_instance_id = ? "
        "AND COALESCE(json_extract(facts_json, '$.retention_class'), '') NOT LIKE 'protected_%' "
        "AND seq <= (SELECT COALESCE(MAX(seq), 0) - ? FROM decision_receipts "
        "WHERE strategy_instance_id = ?)",
        (
            strategy_instance_id,
            MAX_DECISION_RECEIPTS_PER_STRATEGY,
            strategy_instance_id,
        ),
    )
    return DecisionReceiptResource(
        strategy_instance_id=strategy_instance_id,
        seq=seq,
        outcome=outcome,
        symbol=symbol,
        intent_id=intent_id,
        order_ref=order_ref,
        observed_at_ms=observed_at_ms,
        facts_json=facts_json,
    )


def rotated_out_before_ms(window: Sequence[DecisionReceiptResource]) -> int | None:
    """The decision-bar close before which a bucket's receipt may have rotated out of ``window`` (#2850).

    ``window`` is one :meth:`SqliteDecisionReceipts.retained_window` read:
    the newest ``MAX_DECISION_RECEIPTS_PER_STRATEGY`` rows, which is the
    depth the prune above keeps ordinary rows to.

    ``None`` unless the window has dropped a receipt. Sequences start at 1
    and are never reused, so a window that still opens at sequence 1 holds
    every receipt the instance wrote: a bucket missing from it was never
    receipted, however old it is, and stays a crash candidate.

    Once the window has dropped receipts, this is the decision-bar close of
    the oldest row left that was written when its bucket was decided. One
    runner decides an instance's buckets in bar order and receipts each
    before the next, so:

    * a bucket that closed before that row's came before it, so the receipt
      it got when it was decided, if any, is either among the dropped -- its
      absence proves nothing -- or a row passed over below, which is still
      in the window and found by identity, never judged by this boundary;
    * the bucket a crash left staged was reached after every decision
      receipted before the crash, so at the Resume that follows it closed
      after that row's and is not exempt.

    What this gives up: a bucket a crash left staged that no Resume named
    while the boundary was still behind it can no longer be told from a
    rotated-out one, and is left unflagged.

    Two kinds of row are passed over on the way to that row. A quarantine
    row has no evaluation identity, and crash evidence is written by a later
    Resume for a bucket that run only replayed: neither was written when its
    bucket was decided.

    Only that oldest decided row is read. If it records no close, a later
    row's close is not read in its place: a later boundary exempts more, and
    a bucket a crash left staged between the two rows would go unnamed. The
    wall clock a row was written at is never read either: a bucket decided
    late closed before it, and so can the bucket a crash left staged.

    A rotated window whose oldest decided row gives no close has no
    boundary: nothing is exempt, and a Resume can fail the way it did before
    #2850. That is logged, naming the instance and the rows that were read.
    """
    if not window or window[0].seq == 1:
        return None
    oldest_decided = next((receipt for receipt in window if _written_when_decided(receipt)), None)
    if oldest_decided is not None:
        close_ms = json.loads(oldest_decided.facts_json).get("decision_bar_close_ms")
        if isinstance(close_ms, int):
            return close_ms
    logger.warning(
        "decision receipts have rotated out but the oldest retained decision records no bar close",
        extra={
            "action": "decision_receipt_rotation_boundary_unknown",
            "strategy_instance_id": window[0].strategy_instance_id,
            "first_retained_seq": window[0].seq,
            "last_retained_seq": window[-1].seq,
        },
    )
    return None


def run_first_closes_ms(receipts: Sequence[DecisionReceiptResource]) -> frozenset[int]:
    """The decision-bar close of every run's first decision in ``receipts``, but the first run's (#2859).

    ``receipts`` is in sequence order. A run starts flat: the runner drops
    whatever position its warmup rebuilt before it takes its first live bar,
    and that bar belongs to the first bucket the run decides. A Resume that
    replays these decisions drops the position the run before it left at
    that bucket, and no earlier: the earlier run keeps its position up to
    there, through the bucket a crash may have left it staging.

    A run is read from the run id its own decisions carry, never from a gap
    in time. A row :func:`rotated_out_before_ms` passes over was not written
    when its bucket was decided, so it does not say which run decided that
    bucket; a row that names no run or no bar close is not evidence of
    either. The first run in ``receipts`` is left out: nothing here says
    where it started.

    A run that receipted no decision leaves no boundary here.
    """
    closes: set[int] = set()
    deciding_run: str | None = None
    for receipt in receipts:
        if not _written_when_decided(receipt):
            continue
        facts = json.loads(receipt.facts_json)
        run = _lifecycle_run_id(facts.get("run_id"), strategy_instance_id=receipt.strategy_instance_id)
        close_ms = facts.get("decision_bar_close_ms")
        if run is None or not isinstance(close_ms, int):
            continue
        if deciding_run is not None and run != deciding_run:
            closes.add(close_ms)
        deciding_run = run
    return frozenset(closes)


def _entry_held_into(
    window: Sequence[DecisionReceiptResource],
    older: Sequence[DecisionReceiptResource],
) -> DecisionReceiptResource | None:
    """The ENTER among ``older`` whose position was still open where ``window`` starts (#2859).

    ``older`` is the newest rows stored from before ``window``, in sequence
    order. Only the newest ENTER or EXIT among them matters: an EXIT is
    taken as a close, whatever came before it
    (:meth:`SqliteDecisionReceipts.entry_held_into` says what that gives
    up). An ENTER is still open
    at the window's start only when the run that made it also made the
    window's oldest decision -- any other run started flat after it. When
    either row names no run that cannot be shown, and nothing is returned.
    """
    at_window_start = next((receipt for receipt in window if _written_when_decided(receipt)), None)
    last_moved_on = _last_moved_on(older)
    if at_window_start is None or last_moved_on is None or last_moved_on.outcome not in ENTER_OUTCOMES:
        return None
    entering_run = _run_of(last_moved_on)
    if entering_run is None or entering_run != _run_of(at_window_start):
        return None
    return last_moved_on


def _last_moved_on(receipts: Sequence[DecisionReceiptResource]) -> DecisionReceiptResource | None:
    """The newest decision in ``receipts`` the position moved on: an ENTER or an EXIT."""
    return next(
        (
            receipt
            for receipt in reversed(receipts)
            if receipt.intent_id is not None and receipt.outcome in ENTER_OUTCOMES | EXIT_OUTCOMES
        ),
        None,
    )


def _run_of(receipt: DecisionReceiptResource) -> str | None:
    """The lifecycle run ``receipt`` names, or ``None`` when it names none."""
    return _lifecycle_run_id(
        json.loads(receipt.facts_json).get("run_id"),
        strategy_instance_id=receipt.strategy_instance_id,
    )


def _written_when_decided(receipt: DecisionReceiptResource) -> bool:
    """Whether ``receipt`` was written when its bucket was decided, by the run that decided it."""
    return receipt.intent_id is not None and receipt.outcome != CRASH_CANDIDATE_OUTCOME


def _lifecycle_run_id(run_id: object, *, strategy_instance_id: str) -> str | None:
    """The lifecycle run a receipt names, whichever writer wrote it.

    The runner records the lifecycle run id; the Clerk records its own run
    id on an effect, ``<instance>:<lifecycle run>``.
    """
    if not isinstance(run_id, str) or not run_id:
        return None
    return run_id.removeprefix(f"{strategy_instance_id}:")


def _bar_ref(facts_json: str) -> str | None:
    facts = json.loads(facts_json)
    if not isinstance(facts, dict):
        raise ValueError("decision receipt facts must be a JSON object")
    value = facts.get("bar_ref")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("decision receipt bar_ref must be a non-empty string when provided")
    return value


def _decision_id(facts_json: str) -> str | None:
    facts = json.loads(facts_json)
    if not isinstance(facts, dict):
        raise ValueError("decision receipt facts must be a JSON object")
    value = facts.get("decision_id") or facts.get("evaluation_id")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("decision receipt identity must be a non-empty string")
    return value


def _receipt_for_decision(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    decision_id: str,
) -> DecisionReceiptResource | None:
    row = conn.execute(
        "SELECT strategy_instance_id, seq, outcome, symbol, intent_id, order_ref, "
        "observed_at_ms, facts_json FROM decision_receipts "
        "WHERE strategy_instance_id = ? "
        "AND COALESCE(json_extract(facts_json, '$.decision_id'), "
        "json_extract(facts_json, '$.evaluation_id'), intent_id) = ? "
        "ORDER BY seq DESC LIMIT 1",
        (strategy_instance_id, decision_id),
    ).fetchone()
    return None if row is None else DecisionReceiptResource(**dict(row))


def _receipt_for_bar(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    bar_ref: str | None,
) -> DecisionReceiptResource | None:
    if bar_ref is None:
        return None
    row = conn.execute(
        "SELECT strategy_instance_id, seq, outcome, symbol, intent_id, order_ref, "
        "observed_at_ms, facts_json FROM decision_receipts "
        "WHERE strategy_instance_id = ? AND json_extract(facts_json, '$.bar_ref') = ? "
        "ORDER BY seq DESC LIMIT 1",
        (strategy_instance_id, bar_ref),
    ).fetchone()
    return None if row is None else DecisionReceiptResource(**dict(row))


def _same_decision(
    existing: DecisionReceiptResource,
    *,
    outcome: str,
    symbol: str | None,
    intent_id: str | None,
    facts_json: str,
) -> bool:
    existing_facts = json.loads(existing.facts_json)
    candidate_facts = json.loads(facts_json)
    stable_keys = ("bar_ref", "decision_id", "evaluation_id", "run_id", "effect_operation_id")
    return (
        existing.outcome == outcome
        and existing.symbol == symbol
        and existing.intent_id == intent_id
        and all(existing_facts.get(key) == candidate_facts.get(key) for key in stable_keys)
    )


class SqliteDecisionReceipts:
    """One strategy instance's typed, bounded decision-receipt surface."""

    def __init__(
        self,
        repository: ClerkSqliteRepository,
        *,
        strategy_instance_id: str,
    ) -> None:
        if not strategy_instance_id:
            raise ValueError("strategy_instance_id must be non-empty")
        self._repository = repository
        self._strategy_instance_id = strategy_instance_id

    def append(
        self,
        *,
        outcome: DecisionOutcome,
        symbol: str | None,
        observed_at_ms: int,
        facts: Mapping[str, JsonValue],
        intent_id: str | None = None,
        order_ref: str | None = None,
    ) -> DecisionReceiptResource:
        """Append one receipt and atomically assign its per-bot sequence."""
        return self._repository.append_decision_receipt(
            strategy_instance_id=self._strategy_instance_id,
            outcome=outcome,
            symbol=symbol,
            intent_id=intent_id,
            order_ref=order_ref,
            observed_at_ms=observed_at_ms,
            facts_json=canonicalize(dict(facts)),
        )

    def tail(self, n: int) -> list[DecisionReceiptResource]:
        """Return the bounded newest suffix in ascending sequence order."""
        return self._repository.decision_receipt_tail(
            strategy_instance_id=self._strategy_instance_id,
            limit=_read_limit(n),
        )

    def retained_window(self) -> list[DecisionReceiptResource]:
        """Return every ordinary receipt still retained, in ascending sequence order.

        ``MAX_DECISION_RECEIPT_READ`` bounds presentation reads; it is not
        the retention window. Pruning keeps the newest
        ``MAX_DECISION_RECEIPTS_PER_STRATEGY`` sequences (plus ``protected_*``
        rows beyond them), and FR-016 crash replay must see that whole window
        or a decision older than the presentation cap looks uncaptured
        (#1740). Older ``protected_*`` rows are not returned: a Resume does
        replay buckets older than this window (#2314), and it leaves them
        out of crash-candidate flagging rather than reading their
        dispositions (#2850). The one older disposition it does read is
        :meth:`entry_held_into`.
        """
        return self._repository.decision_receipt_tail(
            strategy_instance_id=self._strategy_instance_id,
            limit=MAX_DECISION_RECEIPTS_PER_STRATEGY,
        )

    def ran_before(self, lifecycle_run_id: str) -> bool:
        """Whether the Clerk has a run of this instance on record besides ``lifecycle_run_id`` (#2859).

        A run is recorded before its runner can exist, so one that died on
        its first decision is on record though it left no receipt.
        """
        return self._repository.strategy_ran_before(
            strategy_instance_id=self._strategy_instance_id,
            lifecycle_run_id=lifecycle_run_id,
        )

    def entry_held_into(
        self, window: Sequence[DecisionReceiptResource]
    ) -> DecisionReceiptResource | None:
        """The ENTER still open where ``window`` starts, when it is older than the window (#2859).

        ``window`` is one :meth:`retained_window` read. An ENTER receipt is
        protected, so it stays stored after the window has moved past it.
        Without it a Resume replays the window flat, never restages the EXIT
        of a position held for longer than the window, and a crash that left
        that EXIT staged goes unnamed.

        The read is bounded: the newest ``MAX_DECISION_RECEIPTS_PER_STRATEGY``
        rows stored from before the window, which are protected rows only
        (the prune keeps no ordinary row there). A position held through
        more refusals than that is not found, and that is logged.

        An ENTER receipt alone does not say a position was opened: the Clerk
        writes it when it accepts the ENTER and never amends it. So the
        ENTER's effect is read with it (:meth:`_entered`), and an ENTER whose
        effect ended failed is not returned.

        An EXIT's effect is not read: every EXIT is taken as a close. An EXIT
        refused as it is sent ends failed, the runner is answered as for a
        refusal and the run goes on holding. But an EXIT that is sent,
        committed by the runner and ended unfilled afterwards ends failed
        too, with the run flat in its own state. The effect does not say
        which. The one stored fact that does, the holding a decision's
        explanation records, is display evidence (ADR 0043), which replay
        does not read. What this gives up: a position still held after an
        EXIT refused as it was sent is replayed flat, as every position older
        than the window was before this read, and a crash that left its EXIT
        staged goes unnamed. Passing a failed EXIT over would instead replay
        a position the run had closed, and name an EXIT no live run staged.
        """
        if not window or window[0].seq == 1:
            return None
        older = self._repository.decision_receipt_tail(
            strategy_instance_id=self._strategy_instance_id,
            limit=MAX_DECISION_RECEIPTS_PER_STRATEGY,
            before_seq=window[0].seq,
        )
        held = _entry_held_into(window, older)
        if (
            held is None
            and len(older) == MAX_DECISION_RECEIPTS_PER_STRATEGY
            and _last_moved_on(older) is None
        ):
            logger.warning(
                "no entry or exit among the newest receipts stored before the retained window",
                extra={
                    "action": "decision_receipt_held_entry_unknown",
                    "strategy_instance_id": self._strategy_instance_id,
                    "first_read_seq": older[0].seq,
                    "first_retained_seq": window[0].seq,
                },
            )
        return held if held is not None and self._entered(held) else None

    def _entered(self, entry: DecisionReceiptResource) -> bool:
        """Whether the ENTER that ``entry`` receipts has an effect on record that did not end failed (#2859).

        The Clerk can accept an ENTER and then not send it: its own check
        just before broker contact refuses it, or the broker does. The
        effect ends failed, the runner is answered as it is for a refusal
        and discards the candidate, and the strategy stays flat -- while the
        receipt, written at acceptance, still reads ``enter_intent``.
        Replayed as an entry, it stages an EXIT no live run staged.

        An ENTER whose order was sent and ended unfilled afterwards ends
        failed too, though the runner had committed it and the run went on
        holding it in its own state. Replay takes that one as flat as well,
        which is how it replayed every ENTER older than the window before
        this read existed.

        The read is one effect row by its id. A receipt that names no
        effect on record does not show a position either: that is logged,
        and the ENTER is not returned.
        """
        effect_operation_id = json.loads(entry.facts_json).get("effect_operation_id")
        effect = (
            self._repository.effect_operation(effect_operation_id)
            if isinstance(effect_operation_id, str)
            else None
        )
        if effect is None:
            logger.warning(
                "the entry stored before the retained window names no effect on record",
                extra={
                    "action": "decision_receipt_held_entry_effect_unknown",
                    "strategy_instance_id": self._strategy_instance_id,
                    "entry_seq": entry.seq,
                    "effect_operation_id": effect_operation_id,
                },
            )
            return False
        return effect.state not in FAILED_EFFECT_STATES


def _read_limit(requested: int) -> int:
    if requested <= 0:
        raise ValueError(f"receipt read limit must be positive; got {requested}")
    return min(requested, MAX_DECISION_RECEIPT_READ)
