"""Bounded SQLite read/write API for strategy-decision receipts.

Decision receipts are trader-facing strategy evidence. They are intentionally
separate from Clerk custody transitions: recording a decision neither claims
broker custody nor changes account control revision.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

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
    # created; see `docs/prds/sealed-signal-program-to-governed-alpaca-bot.md`
    # section 13.4 and the `CANDIDATE_UNCAPTURED_AT_CRASH` reason code.
    "candidate_uncaptured_at_crash",
]


class DecisionReceipt(BaseModel):
    """Trader-facing view of one SQLite decision receipt."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    ts_ms: int = Field(ge=0)
    bar_ref: str
    outcome: DecisionOutcome
    reason_code: str
    intent_id: str = ""
    order_ref: str = ""
    indicator_snapshot: dict[str, float | int | str | None] = Field(default_factory=dict)


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
    evidence) so the three shapes never drift apart -- CLAUDE.md guiding
    philosophy #5, one canonical implementation per concept.
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


def update_decision_receipt_for_bar(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    bar_ref: str,
    outcome: str,
    order_ref: str | None,
    facts_json: str,
) -> DecisionReceiptResource:
    """Replace the final outcome for an already-recorded closed-bar decision."""
    if _bar_ref(facts_json) != bar_ref:
        raise ValueError("final decision receipt facts must preserve the closed bar reference")
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _receipt_for_bar(
            conn,
            strategy_instance_id=strategy_instance_id,
            bar_ref=bar_ref,
        )
        if existing is None:
            raise DecisionReceiptConflictError(
                f"closed bar {bar_ref!r} has no decision receipt to update"
            )
        conn.execute(
            "UPDATE decision_receipts SET outcome = ?, order_ref = ?, facts_json = ? "
            "WHERE strategy_instance_id = ? AND seq = ?",
            (outcome, order_ref, facts_json, strategy_instance_id, existing.seq),
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return DecisionReceiptResource(
        strategy_instance_id=existing.strategy_instance_id,
        seq=existing.seq,
        outcome=outcome,
        symbol=existing.symbol,
        intent_id=existing.intent_id,
        order_ref=order_ref,
        observed_at_ms=existing.observed_at_ms,
        facts_json=facts_json,
    )


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

    def update_final_outcome(
        self,
        *,
        bar_ref: str,
        outcome: DecisionOutcome,
        facts: Mapping[str, JsonValue],
        order_ref: str | None = None,
    ) -> DecisionReceiptResource:
        """Replace one closed bar's provisional receipt with its final outcome."""
        return self._repository.update_decision_receipt_for_bar(
            strategy_instance_id=self._strategy_instance_id,
            bar_ref=bar_ref,
            outcome=outcome,
            order_ref=order_ref,
            facts_json=canonicalize(dict(facts)),
        )

    def tail(self, n: int) -> list[DecisionReceiptResource]:
        """Return the bounded newest suffix in ascending sequence order."""
        return self._repository.decision_receipt_tail(
            strategy_instance_id=self._strategy_instance_id,
            limit=_read_limit(n),
        )

    def by_transaction(
        self,
        transaction_ref: str,
        *,
        limit: int = MAX_DECISION_RECEIPT_READ,
    ) -> list[DecisionReceiptResource]:
        """Return a bounded receipt suffix matching an intent or order ref."""
        if not transaction_ref:
            raise ValueError("transaction_ref must be non-empty")
        return self._repository.decision_receipts_by_transaction(
            strategy_instance_id=self._strategy_instance_id,
            transaction_ref=transaction_ref,
            limit=_read_limit(limit),
        )


def _read_limit(requested: int) -> int:
    if requested <= 0:
        raise ValueError(f"receipt read limit must be positive; got {requested}")
    return min(requested, MAX_DECISION_RECEIPT_READ)
