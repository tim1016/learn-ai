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
            raise ValueError(
                f"poisoned.flag payload missing or invalid 'trigger': {payload!r}"
            ) from exc
        try:
            halted_at_ms = int(payload["halted_at_ms"])
            last_clean_bar_close_ms = int(payload["last_clean_bar_close_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"poisoned.flag payload missing or invalid timestamp fields: {payload!r}"
            ) from exc
        details = payload.get("details", {})
        if not isinstance(details, dict):
            raise ValueError(
                f"poisoned.flag 'details' must be a dict, got {type(details).__name__}"
            )
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
    """
    path = run_dir / POISONED_FLAG_FILENAME
    if path.exists():
        raise FileExistsError(
            f"poisoned.flag already exists at {path}; refusing to overwrite "
            f"(the first halt's reason takes precedence)"
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(reason.to_json_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
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
        raise ValueError(
            f"poisoned.flag at {path} must contain a JSON object, got {type(payload).__name__}"
        )
    return PoisonedHaltReason.from_json_dict(payload)


def is_run_poisoned(run_dir: Path) -> bool:
    """Cheap presence check — used by callers that don't need the reason payload."""
    return (run_dir / POISONED_FLAG_FILENAME).exists()


def now_ms_utc() -> int:
    """Stable ``int64 ms UTC`` clock helper exposed for halt-callers and tests."""
    return int(time.time() * 1000)
