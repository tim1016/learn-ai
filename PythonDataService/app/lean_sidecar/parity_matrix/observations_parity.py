"""Gate 1 — observations.csv exact-equality comparator.

Compares per-minute bar consumption between LEAN (pinned) and Engine
Lab (live). Exact equality:
  * ms_utc as int
  * OHLCV as Decimal parsed from string
  * row count and order
  * header exactly ["ms_utc","open","high","low","close","volume"]
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

EXPECTED_HEADER: tuple[str, ...] = (
    "ms_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


@dataclass(frozen=True)
class ObservationsFailure:
    row_index: int  # 0 = first data row; -1 for schema/structural failures
    field: str  # field name or "schema" / "row_count"
    reason: str


@dataclass(frozen=True)
class ObservationsParityResult:
    passed: bool
    row_count: int
    failures: list[ObservationsFailure] = field(default_factory=list)


def _load(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = [r for r in reader if r]
    return header, rows


def compare_observations(*, reference: Path, candidate: Path) -> ObservationsParityResult:
    ref_h, ref_rows = _load(reference)
    cand_h, cand_rows = _load(candidate)
    failures: list[ObservationsFailure] = []

    if tuple(ref_h) != EXPECTED_HEADER:
        failures.append(
            ObservationsFailure(
                row_index=-1,
                field="schema",
                reason=f"schema mismatch: reference header {ref_h!r} != expected {list(EXPECTED_HEADER)!r}",
            )
        )
    if tuple(cand_h) != EXPECTED_HEADER:
        failures.append(
            ObservationsFailure(
                row_index=-1,
                field="schema",
                reason=f"schema mismatch: candidate header {cand_h!r} != expected {list(EXPECTED_HEADER)!r}",
            )
        )
    if failures:
        return ObservationsParityResult(passed=False, row_count=0, failures=failures)

    if len(ref_rows) != len(cand_rows):
        failures.append(
            ObservationsFailure(
                row_index=-1,
                field="row_count",
                reason=f"row_count mismatch: reference has {len(ref_rows)} rows; candidate has {len(cand_rows)}",
            )
        )
        return ObservationsParityResult(
            passed=False,
            row_count=min(len(ref_rows), len(cand_rows)),
            failures=failures,
        )

    for i, (r, c) in enumerate(zip(ref_rows, cand_rows, strict=True)):
        # ms_utc int
        try:
            if int(r[0]) != int(c[0]):
                failures.append(
                    ObservationsFailure(
                        row_index=i,
                        field="ms_utc",
                        reason=f"{r[0]} != {c[0]}",
                    )
                )
                continue
        except ValueError as e:
            failures.append(
                ObservationsFailure(
                    row_index=i,
                    field="ms_utc",
                    reason=f"unparseable ({e})",
                )
            )
            continue
        # OHLCV Decimal
        for idx, name in enumerate(("open", "high", "low", "close", "volume"), start=1):
            try:
                if Decimal(r[idx]) != Decimal(c[idx]):
                    failures.append(
                        ObservationsFailure(
                            row_index=i,
                            field=name,
                            reason=f"{r[idx]} != {c[idx]}",
                        )
                    )
            except InvalidOperation as e:
                failures.append(
                    ObservationsFailure(
                        row_index=i,
                        field=name,
                        reason=f"unparseable ({e})",
                    )
                )

    return ObservationsParityResult(
        passed=not failures,
        row_count=len(ref_rows),
        failures=failures,
    )
