"""Gate 2 — per-bar state.csv parity comparator.

Per-bar agreement:
  * ts_ms_utc, close, cross_state, signal — exact equality
  * ema_fast, ema_slow, rsi — Decimal abs-diff within DEFAULT_INDICATOR_ATOL

State files MUST emit full-precision Decimal strings; this comparator
parses them with Decimal arithmetic so atol=1e-9 holds without float drift.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

EXPECTED_COLUMNS: tuple[str, ...] = (
    "ts_ms_utc",
    "close",
    "ema_fast",
    "ema_slow",
    "rsi",
    "cross_state",
    "signal",
)
DEFAULT_INDICATOR_ATOL: Decimal = Decimal("1e-9")
_INDICATOR_FIELDS: tuple[str, ...] = ("ema_fast", "ema_slow", "rsi")
_VALID_CROSS_STATES: frozenset[str] = frozenset({"above", "below", "equal"})
_VALID_SIGNALS: frozenset[str] = frozenset({"HOLD", "ENTER", "EXIT"})


@dataclass(frozen=True)
class StateFailure:
    row_index: int
    field: str
    reason: str


@dataclass(frozen=True)
class StateParityResult:
    passed: bool
    row_count: int
    failures: list[StateFailure] = field(default_factory=list)


def _load(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return header, rows


def compare_state(
    *,
    reference: Path,
    candidate: Path,
    indicator_atol: Decimal = DEFAULT_INDICATOR_ATOL,
) -> StateParityResult:
    ref_h, ref_rows = _load(reference)
    cand_h, cand_rows = _load(candidate)
    failures: list[StateFailure] = []

    if tuple(ref_h) != EXPECTED_COLUMNS:
        failures.append(
            StateFailure(
                row_index=-1,
                field="schema",
                reason=f"reference header {ref_h!r} != expected {list(EXPECTED_COLUMNS)!r}",
            )
        )
    if tuple(cand_h) != EXPECTED_COLUMNS:
        failures.append(
            StateFailure(
                row_index=-1,
                field="schema",
                reason=f"candidate header {cand_h!r} != expected {list(EXPECTED_COLUMNS)!r}",
            )
        )
    if failures:
        return StateParityResult(passed=False, row_count=0, failures=failures)

    if len(ref_rows) != len(cand_rows):
        failures.append(
            StateFailure(
                row_index=-1,
                field="row_count",
                reason=f"reference has {len(ref_rows)} rows; candidate has {len(cand_rows)}",
            )
        )
        return StateParityResult(
            passed=False,
            row_count=min(len(ref_rows), len(cand_rows)),
            failures=failures,
        )

    for i, (r, c) in enumerate(zip(ref_rows, cand_rows, strict=True)):
        # Exact equality for ts, close, cross_state, signal.
        if r["ts_ms_utc"] != c["ts_ms_utc"]:
            failures.append(
                StateFailure(
                    row_index=i,
                    field="ts_ms_utc",
                    reason=f"{r['ts_ms_utc']} != {c['ts_ms_utc']}",
                )
            )
            continue
        try:
            if Decimal(r["close"]) != Decimal(c["close"]):
                failures.append(
                    StateFailure(
                        row_index=i,
                        field="close",
                        reason=f"{r['close']} != {c['close']}",
                    )
                )
        except (InvalidOperation, TypeError, KeyError) as e:
            failures.append(
                StateFailure(
                    row_index=i,
                    field="close",
                    reason=f"unparseable ({e})",
                )
            )
        for name in _INDICATOR_FIELDS:
            try:
                diff = abs(Decimal(r[name]) - Decimal(c[name]))
                if diff > indicator_atol:
                    failures.append(
                        StateFailure(
                            row_index=i,
                            field=name,
                            reason=(f"abs_diff={diff} > atol={indicator_atol} ({r[name]} vs {c[name]})"),
                        )
                    )
            except (InvalidOperation, TypeError, KeyError) as e:
                failures.append(
                    StateFailure(
                        row_index=i,
                        field=name,
                        reason=f"unparseable ({e})",
                    )
                )
        if r["cross_state"] != c["cross_state"]:
            failures.append(
                StateFailure(
                    row_index=i,
                    field="cross_state",
                    reason=f"{r['cross_state']} != {c['cross_state']}",
                )
            )
        if r["signal"] != c["signal"]:
            failures.append(
                StateFailure(
                    row_index=i,
                    field="signal",
                    reason=f"{r['signal']} != {c['signal']}",
                )
            )
        # Enum validity check (independent of reference/candidate agreement).
        if r["cross_state"] not in _VALID_CROSS_STATES:
            failures.append(
                StateFailure(
                    row_index=i,
                    field="cross_state",
                    reason=(f"reference value {r['cross_state']!r} not in valid set {sorted(_VALID_CROSS_STATES)}"),
                )
            )
        if c["cross_state"] not in _VALID_CROSS_STATES:
            failures.append(
                StateFailure(
                    row_index=i,
                    field="cross_state",
                    reason=(f"candidate value {c['cross_state']!r} not in valid set {sorted(_VALID_CROSS_STATES)}"),
                )
            )
        if r["signal"] not in _VALID_SIGNALS:
            failures.append(
                StateFailure(
                    row_index=i,
                    field="signal",
                    reason=(f"reference value {r['signal']!r} not in valid set {sorted(_VALID_SIGNALS)}"),
                )
            )
        if c["signal"] not in _VALID_SIGNALS:
            failures.append(
                StateFailure(
                    row_index=i,
                    field="signal",
                    reason=(f"candidate value {c['signal']!r} not in valid set {sorted(_VALID_SIGNALS)}"),
                )
            )

    return StateParityResult(
        passed=not failures,
        row_count=len(ref_rows),
        failures=failures,
    )
