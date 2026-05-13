"""§ 7 intra-day fatal-halt infrastructure.

This module owns the *fatal* halt machinery — distinct from the
next-session morning gate in ``pre_flight.py`` (§ 6.4). The two
differ in semantics:

  - ``pre_flight.check_no_halt_flag`` reads ``halt.flag``, written by
    the daily reconciler when the prior day produced an engine-class
    divergence or fill breach. The current run *paused for one
    session* and may resume on a later day after the operator
    investigates.

  - ``halt.write_poisoned_flag`` writes ``poisoned.flag``, written
    intra-day by the LiveEngine when broker-state divergence is
    detected (foreign execId/permId, lost fill, etc.). The current
    run *will never resume on the same run_id* — the receipt is
    contaminated and a fresh ``run_id`` is required after the
    operator manually reconciles the account (§ 7.2 #4–5).

Phase C-2c-a (this PR) ships only the on-disk flag I/O, so the
``cmd_start`` and LiveEngine integrations that read/write it can
arrive in their own focused PRs without conflicting on file-level
edits to ``run.py`` or ``live_engine.py``. The detection logic
(outside-mutation, lost-fill) and the operator-facing
``emergency-flatten`` subcommand follow in C-2c-b and C-2c-c
respectively.
"""

from __future__ import annotations

import enum
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

POISONED_FLAG_FILENAME = "poisoned.flag"


class FatalHaltError(RuntimeError):
    """Raised when an intra-day fatal halt fires (§ 7).

    Carries the ``PoisonedHaltReason`` so the top-level CLI handler
    can surface the trigger and timestamp in its exit message. The
    exception is the signal that the LiveEngine has already done its
    fatal-halt cleanup (cancelled Python-owned orders, flushed
    writers, written ``poisoned.flag``); the runner just needs to
    propagate the failure with an appropriate exit code.
    """

    def __init__(self, reason: PoisonedHaltReason) -> None:
        super().__init__(
            f"FatalHaltError({reason.trigger.value} at {reason.halted_at_ms}ms UTC; details={reason.details})"
        )
        self.reason = reason


class PoisonedHaltTrigger(enum.StrEnum):
    """The two intra-day fatal triggers from spec § 7.1.

    ``OUTSIDE_MUTATION`` fires when the broker reports a fill whose
    ``(execId, permId)`` is not linked to a Python-owned
    ``client_order_id`` — meaning some other actor (a manual TWS
    click, a different client, a stuck order from a prior session)
    transacted on the same account. Defense-in-depth on top of § 5's
    isolation invariant.

    ``LOST_FILL`` fires when a Python-owned order has no matching
    execution within its expected fill window (next-bar-open + slack)
    or remains unfilled at end-of-day. Indicates broker-state
    divergence the other direction — we placed an order, the broker
    doesn't show its lifecycle.
    """

    OUTSIDE_MUTATION = "outside_mutation"
    LOST_FILL = "lost_fill"


@dataclass(frozen=True)
class PoisonedHaltReason:
    """Structured payload persisted to ``poisoned.flag``.

    ``halted_at_ms`` and ``last_clean_bar_close_ms`` are spec
    § 7.2 #3 fields — they let a post-mortem operator reason about
    when the runtime stopped trusting broker state, and what bar
    the receipt's last clean entry corresponds to.
    """

    trigger: PoisonedHaltTrigger
    halted_at_ms: int
    last_clean_bar_close_ms: int
    details: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger.value,
            "halted_at_ms": self.halted_at_ms,
            "last_clean_bar_close_ms": self.last_clean_bar_close_ms,
            "details": dict(self.details),
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> PoisonedHaltReason:
        try:
            trigger = PoisonedHaltTrigger(payload["trigger"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"poisoned.flag payload missing or invalid 'trigger': {payload!r}") from exc
        try:
            halted_at_ms = int(payload["halted_at_ms"])
            last_clean_bar_close_ms = int(payload["last_clean_bar_close_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"poisoned.flag payload missing or invalid timestamp fields: {payload!r}") from exc
        details = payload.get("details", {})
        if not isinstance(details, dict):
            raise ValueError(f"poisoned.flag 'details' must be a dict, got {type(details).__name__}")
        return cls(
            trigger=trigger,
            halted_at_ms=halted_at_ms,
            last_clean_bar_close_ms=last_clean_bar_close_ms,
            details=details,
        )


def write_poisoned_flag(run_dir: Path, reason: PoisonedHaltReason) -> Path:
    """Persist the fatal-halt sentinel under ``run_dir/poisoned.flag``.

    Returns the path written. Refuses to overwrite an existing flag
    — a second halt on the same already-halted run shouldn't be able
    to silently rewrite the original cause; the first halt wins.

    Uses ``open(..., 'x')`` for atomic exclusive create — the
    earlier ``exists() + write_text()`` pattern was a TOCTOU race
    where two near-simultaneous halt callers could both pass the
    ``exists()`` check and the second's write would clobber the
    first. (CodeRabbit P1 from #188.)
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / POISONED_FLAG_FILENAME
    payload = json.dumps(reason.to_json_dict(), indent=2, sort_keys=True)
    try:
        with path.open("x", encoding="utf-8") as fh:
            fh.write(payload)
    except FileExistsError as exc:
        raise FileExistsError(
            f"poisoned.flag already exists at {path}; refusing to overwrite (the first halt's reason takes precedence)"
        ) from exc
    return path


def read_poisoned_flag(run_dir: Path) -> PoisonedHaltReason | None:
    """Return the parsed flag, or ``None`` if no flag is present.

    A malformed flag raises ``ValueError`` rather than returning
    ``None`` — silently ignoring a corrupted halt sentinel would let a
    contaminated run resume, defeating the gate's whole purpose.
    """
    path = run_dir / POISONED_FLAG_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"poisoned.flag at {path} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"poisoned.flag at {path} must contain a JSON object, got {type(payload).__name__}")
    return PoisonedHaltReason.from_json_dict(payload)


def is_run_poisoned(run_dir: Path) -> bool:
    """Cheap presence check — used by callers that don't need the reason payload."""
    return (run_dir / POISONED_FLAG_FILENAME).exists()


def now_ms_utc() -> int:
    """Stable ``int64 ms UTC`` clock helper exposed for halt-callers and tests."""
    return int(time.time() * 1000)


# ──────────────────────────── Detection functions ────────────────────


def check_outside_mutation(
    executions: list[dict],
    owned_client_order_ids: set[str],
    *,
    halted_at_ms: int,
    last_clean_bar_close_ms: int,
) -> PoisonedHaltReason | None:
    """Return a halt reason if any execution lacks a Python-owned ``client_order_id``.

    Spec § 7.1 trigger A: any execution under the DU account whose
    ``(execId, permId)`` is not linked to a Python-owned
    ``client_order_id`` triggers the fatal halt — *regardless of
    clientId*. A ``clientId == 42`` filter is insufficient because
    TWS itself can place orders under ``clientId=0`` when a human
    clicks a button, and those would slip past a same-client check.

    Each ``execution`` row is the dict shape produced by the IBKR
    adapter's execution-stream channel (Phase C-2c-b2): at minimum
    ``client_order_id``, ``exec_id``, ``perm_id``, ``account_id``.
    The function reads only ``client_order_id`` to decide ownership;
    ``exec_id`` / ``perm_id`` are surfaced into the halt reason's
    ``details`` for the post-mortem operator.

    Returns ``None`` when every execution is Python-owned; otherwise
    a ``PoisonedHaltReason`` describing the first offender (the
    ``details`` dict carries the foreign exec_id / perm_id /
    client_id / account_id so the operator can correlate against TWS
    history).
    """
    for execution in executions:
        client_order_id = execution.get("client_order_id")
        if client_order_id in owned_client_order_ids:
            continue
        # Found a foreign execution — halt with the first offender's
        # details. Subsequent foreigns aren't enumerated here; the
        # halt is fatal regardless of count, and broker-side
        # enumeration is the operator's manual reconciliation step.
        return PoisonedHaltReason(
            trigger=PoisonedHaltTrigger.OUTSIDE_MUTATION,
            halted_at_ms=halted_at_ms,
            last_clean_bar_close_ms=last_clean_bar_close_ms,
            details={
                "client_order_id": str(client_order_id) if client_order_id is not None else None,
                "exec_id": execution.get("exec_id"),
                "perm_id": execution.get("perm_id"),
                "account_id": execution.get("account_id"),
                "client_id": execution.get("client_id"),
            },
        )
    return None


def check_lost_fill(
    orders: list[dict],
    executions: list[dict],
    *,
    fill_window_ms: int,
    current_time_ms: int,
    last_clean_bar_close_ms: int,
) -> PoisonedHaltReason | None:
    """Return a halt reason if any Python order is past its fill window without an execution.

    Spec § 7.1 trigger B: a Python order whose ``client_order_id``
    has no matching execution within its expected fill window
    (next-bar-open + slack), or remains unfilled at end-of-day.
    Either case indicates broker-state divergence — we placed the
    order, the broker doesn't show its lifecycle.

    Each ``order`` row carries ``client_order_id`` and ``submitted_at_ms``;
    each ``execution`` carries ``client_order_id`` (matching what the
    LivePortfolio sets at place_order time) PLUS ``remaining`` — the
    order's leftover quantity after this execution. An order is
    "complete" iff some execution sharing its ``client_order_id`` has
    ``remaining == 0``. A partial fill (``remaining > 0``) does NOT
    mark the order complete: a 1-share execution on a 200-share order
    leaves 199 unfilled, and the lost-fill halt must still fire when
    the order ages past its window.

    ``fill_window_ms`` is how long we wait before declaring a fill
    lost — typically the bar period + a few seconds of broker-clock
    slack. ``current_time_ms`` is the wall-clock time of the check
    (the LiveEngine passes the most-recent bar's end_time).

    Returns ``None`` when every submitted order is either complete or
    still within its window; otherwise a ``PoisonedHaltReason`` for
    the first lost order (oldest by submission time).
    """
    complete_client_order_ids: set = {
        ex.get("client_order_id")
        for ex in executions
        if ex.get("remaining") is not None and float(ex.get("remaining")) == 0.0
    }
    overdue: list[dict] = []
    for order in orders:
        client_order_id = order.get("client_order_id")
        if client_order_id is None:
            continue
        if client_order_id in complete_client_order_ids:
            continue
        submitted_at_ms = int(order.get("submitted_at_ms", 0))
        if current_time_ms - submitted_at_ms > fill_window_ms:
            overdue.append(order)
    if not overdue:
        return None
    overdue.sort(key=lambda o: int(o.get("submitted_at_ms", 0)))
    first = overdue[0]
    return PoisonedHaltReason(
        trigger=PoisonedHaltTrigger.LOST_FILL,
        halted_at_ms=current_time_ms,
        last_clean_bar_close_ms=last_clean_bar_close_ms,
        details={
            "client_order_id": str(first.get("client_order_id")),
            "submitted_at_ms": int(first.get("submitted_at_ms", 0)),
            "age_ms": current_time_ms - int(first.get("submitted_at_ms", 0)),
            "fill_window_ms": fill_window_ms,
            "overdue_count": len(overdue),
        },
    )
