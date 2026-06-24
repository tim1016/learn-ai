"""Module C — IntentWal (thin I/O). ADR-0008 §3, PRD #446.

Append-only JSONL write-ahead log for the submit critical section — the ONLY
filesystem-touching module in this set. Its contract: a ``PENDING_INTENT`` is
durable (fsynced) **before** ``placeOrder`` is called, so a crash mid-submit
always leaves recoverable evidence the order was attempted.

Read contract (ADR-0008 §3): on read, exactly one anomaly is tolerated — a
single **trailing** line with no terminating newline (an un-fsynced tail proves
``placeOrder`` never ran for it, so there is no broker side effect to recover).
*Every other* malformation — a parse failure on a newline-terminated line, a
non-monotonic ``seq`` — is corruption that raises ``IntentWalCorruptError``,
never silently skipped. A *complete*, fsynced ``PENDING_INTENT`` with no
following resolution is returned by ``read_tail`` (it is the caller's
in-flight double-submit window to resolve), never dropped.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.engine.live.intent_events import DropReason, IntentEvent, IntentEventType, IntentKind

# Reuse the canonical parent-dir fsync (single source of truth — same helper
# the live-state sidecar uses for crash-durable renames). Append-mode WALs
# still need the dir entry fsynced on POSIX so a freshly-created WAL file
# survives power loss.
from app.engine.live.live_state_sidecar import _fsync_parent_dir


class IntentWalCorruptError(RuntimeError):
    """Raised by ``read_tail`` on any malformation other than a single
    tolerated trailing partial line. Routes to a ``Poisoned`` cold-start
    outcome — a corrupt WAL cannot be safely folded."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"intent WAL at {path} is corrupt: {detail}")
        self.path = path
        self.detail = detail


class IntentWal:
    """Append-only WAL writer/reader scoped to one ``run_dir``."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._next_seq: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def append(
        self,
        *,
        event_type: IntentEventType,
        intent_id: str,
        bot_order_namespace: str,
        order_ref: str,
        intent_kind: IntentKind = IntentKind.STRATEGY,
        reason: str | None = None,
        order_id: int | None = None,
        perm_id: int | None = None,
        exec_id: str | None = None,
        order_spec: dict[str, Any] | None = None,
        ts_ms: int | None = None,
        # PR 3 / operator-notice — populated only on INTENT_DROPPED_BEFORE_SUBMIT.
        drop_reason: DropReason | None = None,
        # ADR 0009 § 11 — sizing-decision payload, populated only on
        # SIZING_RESOLVED events.
        policy_kind: str | None = None,
        policy_value: str | None = None,
        intended_qty: int | None = None,
        reference_price: str | None = None,
        sizing_provenance_at_resolve_time: str | None = None,
        sized_via: str | None = None,
        symbol: str | None = None,
    ) -> IntentEvent:
        """Append one event with the next per-run ``seq`` and **fsync before
        returning**. The caller may call ``placeOrder`` only after this returns.
        """
        seq = self._allocate_seq()
        # Reviewer finding 2: capture process wall-clock at append time so the
        # fold can compare legacy_sizing_only_cutoff_ms (engine_started_at_ms,
        # process wall-clock) to a field in the same time domain. ts_ms carries
        # bar/strategy time and can precede process start in delayed feeds.
        appended_at_ms = time.time_ns() // 1_000_000
        event = IntentEvent(
            seq=seq,
            event_type=event_type,
            intent_id=intent_id,
            bot_order_namespace=bot_order_namespace,
            order_ref=order_ref,
            intent_kind=intent_kind,
            reason=reason,
            order_id=order_id,
            perm_id=perm_id,
            exec_id=exec_id,
            order_spec=order_spec,
            drop_reason=drop_reason,
            ts_ms=ts_ms,
            appended_at_ms=appended_at_ms,
            policy_kind=policy_kind,
            policy_value=policy_value,
            intended_qty=intended_qty,
            reference_price=reference_price,
            sizing_provenance_at_resolve_time=sizing_provenance_at_resolve_time,
            sized_via=sized_via,
            symbol=symbol,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = event.model_dump_json() + "\n"
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line)
            # Consume the seq the moment the bytes are written — BEFORE flush/
            # fsync. If fsync then raises, the event is durable-but-unconfirmed
            # and a retry uses seq+1; cold-start resolves the orphan PENDING_INTENT
            # against the broker. Incrementing after fsync would reuse the seq on
            # retry and write a duplicate-seq line that poisons the next read.
            self._next_seq = seq + 1
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_parent_dir(self._path)
        return event

    def read_tail(self) -> list[IntentEvent]:
        """Parse every complete event in seq order. See the module read contract.

        Splits at the byte level so a torn trailing line (possibly mid-UTF-8)
        is dropped cleanly rather than raising a decode error.
        """
        if not self._path.exists():
            return []
        raw = self._path.read_bytes()
        if not raw:
            return []
        ends_with_newline = raw.endswith(b"\n")
        byte_lines = raw.split(b"\n")
        if byte_lines and byte_lines[-1] == b"":
            byte_lines.pop()  # the empty tail produced by a final newline

        events: list[IntentEvent] = []
        last_seq = 0
        n = len(byte_lines)
        for idx, bline in enumerate(byte_lines):
            if idx == n - 1 and not ends_with_newline:
                break  # tolerated: single trailing un-fsynced partial line
            try:
                event = IntentEvent.model_validate_json(bline)
            except (ValidationError, ValueError) as exc:
                raise IntentWalCorruptError(
                    self._path, f"unparseable line {idx + 1}: {exc}"
                ) from exc
            if event.seq <= last_seq:
                raise IntentWalCorruptError(
                    self._path,
                    f"non-monotonic seq at line {idx + 1}: {event.seq} after {last_seq}",
                )
            last_seq = event.seq
            events.append(event)
        return events

    def _allocate_seq(self) -> int:
        if self._next_seq is None:
            existing = self.read_tail()
            self._next_seq = (existing[-1].seq + 1) if existing else 1
        return self._next_seq


__all__ = ["IntentWal", "IntentWalCorruptError"]
