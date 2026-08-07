"""Durable per-bot decision-receipt journal for the broker-v2 panel (S0).

One receipt is appended per bar evaluation — the SIGNAL station's canonical
evidence source and the trader headline's data feed. This is *not* a log; it
is a durable, fsync'd, append-only JSONL journal backed by the canonical
``JsonlWal`` primitive.

On-disk layout (one file per bot under the Clerk's artifact tree)::

    <ALPACA_CLERK_DIR>/accounts/<account_id>/bots/<sid>/decision_journal.jsonl

Each line is a ``DecisionReceipt`` serialised with ``model_dump_json()``.
The seq field is a monotone integer used by the bounded read API.

Outcome vocabulary (closed set, per spec §9):

- ``enter_intent`` — bar evaluation produced a market-entry signal
- ``exit_intent``  — bar evaluation produced a market-exit signal
- ``entered``   — legacy receipt value retained for historical reads
- ``exited``    — legacy receipt value retained for historical reads
- ``no_action`` — bar evaluated; conditions not met for entry or exit
- ``blocked``   — a gate (session, hold, risk) prevented evaluation/submission

Custody/effect outcomes are deliberately absent from new signal receipts. SQLite
Clerk transitions remain the authority for submitted, uncertain, filled, and flat.

Read API:

- ``tail(n)``             — the n most-recent receipts (bounded; n ≤ MAX_TAIL)
- ``by_transaction(ref)`` — all receipts associated with an intent_id / order_ref
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.journal import ClerkSettings
from app.services.jsonl_wal import JsonlWal

# ── Constants ────────────────────────────────────────────────────────────────

DECISION_JOURNAL_FILENAME = "decision_journal.jsonl"
MAX_TAIL = 500  # hard ceiling on tail reads — never a full scan

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_RESERVED = frozenset({".", ".."})

DecisionOutcome = Literal[
    "enter_intent",
    "exit_intent",
    "entered",
    "exited",
    "no_action",
    "blocked",
]


# ── Receipt model ─────────────────────────────────────────────────────────────


class DecisionReceipt(BaseModel):
    """One bar-evaluation receipt appended to the per-bot decision journal.

    ``seq`` is a monotone integer used as the pagination cursor for the
    bounded tail read.  All temporal fields are ``int64 ms UTC``.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    ts_ms: int = Field(ge=0, description="Bar evaluation wall-clock (int64 ms UTC)")
    bar_ref: str = Field(
        description=(
            "Opaque bar reference: '<symbol>@<bar_close_ms_utc>' — enough for "
            "the panel to locate the bar on the chart pane."
        )
    )
    outcome: DecisionOutcome
    reason_code: str = Field(
        description=(
            "Code-like label rendered through the frontend receiptLabel pipe. "
            "Example: 'EMA_CROSS_ABOVE', 'SESSION_CLOSED', 'HOLD_ACTIVE'."
        )
    )
    # Intent tracing — present when the outcome produced an order intent.
    # Absent (empty string) on no_action and blocked outcomes.
    intent_id: str = ""
    order_ref: str = ""
    # Indicator snapshot at the moment of evaluation.
    # Stored as a plain dict to remain schema-agnostic across strategy kinds.
    # Values must be JSON-serialisable scalars (float | int | str | None).
    indicator_snapshot: dict[str, float | int | str | None] = Field(
        default_factory=dict,
        description="Indicator values at bar close — for the operator lens panel.",
    )


# ── Path helpers ──────────────────────────────────────────────────────────────


def _safe(value: str, kind: str) -> str:
    if value in _RESERVED or not _SAFE_COMPONENT.match(value):
        raise ValueError(f"unsafe {kind} path component: {value!r}")
    return value


def decision_journal_path(*, account_id: str, sid: str, root: Path) -> Path:
    """Return the decision journal path for one bot (traversal-safe)."""
    a = _safe(account_id, "account_id")
    s = _safe(sid, "sid")
    return root.resolve() / "accounts" / a / "bots" / s / DECISION_JOURNAL_FILENAME


# ── Single-file WAL ──────────────────────────────────────────────────────────


def _corrupt_error(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Decision journal corrupt at {path}: {detail}")


class DecisionJournal:
    """Single-writer append-only decision journal for one bot.

    Delegates all fsync/truncate/seq discipline to the canonical
    ``JsonlWal[DecisionReceipt]`` primitive.  Thread-safe: ``JsonlWal``
    is externally called under ``_lock`` to serialise appends.

    One instance should be held per bot by the rollup cache
    (``BotRollupCache``).
    """

    def __init__(self, *, account_id: str, sid: str, root: Path | None = None) -> None:
        settings = ClerkSettings()
        resolved_root = root if root is not None else settings.dir
        path = decision_journal_path(
            account_id=account_id, sid=sid, root=resolved_root
        )
        self._wal: JsonlWal[DecisionReceipt] = JsonlWal(
            path,
            record_model=DecisionReceipt,
            corrupt_error=_corrupt_error,
            seq_of=lambda r: r.seq,
            label="decision_journal",
            trusted_root=resolved_root.resolve(),
        )
        self._lock = threading.Lock()
        self._receipts_by_bar_ref: dict[str, DecisionReceipt] | None = None

    # ── Write ─────────────────────────────────────────────────────────────────

    def append(self, receipt: DecisionReceipt) -> None:
        """Append one receipt; fflush + fsync + fsync parent dir."""
        with self._lock:
            self._wal.append(receipt)
            self._receipts_by_bar_ref = None

    def append_for_bar(
        self,
        *,
        ts_ms: int,
        bar_ref: str,
        outcome: DecisionOutcome,
        reason_code: str,
        intent_id: str = "",
        order_ref: str = "",
        indicator_snapshot: dict[str, float | int | str | None] | None = None,
    ) -> DecisionReceipt:
        """Atomically allocate and append one idempotent receipt per closed bar.

        A repeated delivery of any previously recorded closed bar returns its
        existing receipt when the authored decision is identical. A conflicting
        replay fails closed instead of writing two truths for one bar. The
        bar-reference index is built once per journal instance during cold
        replay and then maintained with each append.
        """
        with self._lock:
            if self._receipts_by_bar_ref is None:
                self._receipts_by_bar_ref = {}
                for recorded in self._wal.read_all():
                    prior = self._receipts_by_bar_ref.setdefault(
                        recorded.bar_ref,
                        recorded,
                    )
                    if prior != recorded:
                        raise _corrupt_error(
                            self._wal.path,
                            f"duplicate conflicting bar_ref {recorded.bar_ref!r}",
                        )
            existing = self._receipts_by_bar_ref.get(bar_ref)
            if existing is not None:
                requested = (
                    outcome,
                    reason_code,
                    intent_id,
                    order_ref,
                    indicator_snapshot or {},
                )
                authored = (
                    existing.outcome,
                    existing.reason_code,
                    existing.intent_id,
                    existing.order_ref,
                    existing.indicator_snapshot,
                )
                if requested != authored:
                    raise ValueError(
                        f"conflicting decision receipt replay for bar_ref {bar_ref!r}"
                    )
                return existing
            receipt = DecisionReceipt(
                seq=self._wal.allocate_seq(),
                ts_ms=ts_ms,
                bar_ref=bar_ref,
                outcome=outcome,
                reason_code=reason_code,
                intent_id=intent_id,
                order_ref=order_ref,
                indicator_snapshot=indicator_snapshot or {},
            )
            self._wal.append(receipt)
            self._receipts_by_bar_ref[bar_ref] = receipt
            return receipt

    def next_seq(self) -> int:
        """Return the seq to assign to the next receipt (1-based)."""
        with self._lock:
            return self._wal.allocate_seq()

    # ── Read ──────────────────────────────────────────────────────────────────

    def tail(self, n: int) -> list[DecisionReceipt]:
        """Return the n most-recent receipts (newest-last; n ≤ MAX_TAIL).

        Bounded: never reads more than MAX_TAIL receipts regardless of n.
        """
        if n <= 0:
            raise ValueError(f"tail n must be positive; got {n}")
        limit = min(n, MAX_TAIL)
        return self._wal.read_tail(limit=limit)

    def by_transaction(self, ref: str) -> list[DecisionReceipt]:
        """All receipts whose intent_id or order_ref matches ``ref``.

        ``ref`` may be an intent_id or order_ref — both fields are checked.
        """
        if not ref:
            raise ValueError("ref must be non-empty")
        return [r for r in self._wal.read_all() if r.intent_id == ref or r.order_ref == ref]
