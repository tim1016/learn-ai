"""Gate 2 — per-bar state.csv parity comparator."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.lean_sidecar.parity_matrix.state_parity import (
    DEFAULT_INDICATOR_ATOL,
    StateParityResult,
    compare_state,
)

HEADER = "ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal\n"


def _write_state(path: Path, rows: list[str]) -> None:
    path.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("reference_rows", "candidate_rows", "row_count"),
    [
        (
            (
                "1700000000000,100.5,99.1,98.7,55.2,above,HOLD",
                "1700000900000,101.2,99.8,99.0,57.1,above,ENTER",
            ),
            (
                "1700000000000,100.5,99.1,98.7,55.2,above,HOLD",
                "1700000900000,101.2,99.8,99.0,57.1,above,ENTER",
            ),
            2,
        ),
        # Default atol=1e-9: 1e-12 drift is within tolerance.
        (
            ("1700000000000,100.5,99.1,98.7,55.2,above,HOLD",),
            ("1700000000000,100.5,99.100000000001,98.7,55.2,above,HOLD",),
            1,
        ),
    ],
    ids=["identical-passes", "indicator-within-atol-passes"],
)
def test_matching_rows_pass_with_no_failures(
    tmp_path: Path, reference_rows: tuple[str, ...], candidate_rows: tuple[str, ...], row_count: int
) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, list(reference_rows))
    _write_state(b, list(candidate_rows))
    r = compare_state(reference=a, candidate=b)
    assert isinstance(r, StateParityResult), "compare_state returns a StateParityResult"
    assert r.passed is True, "matching rows pass the comparison"
    assert r.row_count == row_count, "the result counts the compared rows"
    assert r.failures == [], "matching rows report no failures"


@pytest.mark.parametrize(
    ("candidate_row", "field"),
    [
        ("1700000000000,100.5,99.10001,98.7,55.2,above,HOLD", "ema_fast"),
        ("1700000000000,100.50001,99.1,98.7,55.2,above,HOLD", "close"),
        ("1700000000000,100.5,99.1,98.7,55.2,above,ENTER", "signal"),
        ("1700000000000,100.5,99.1,98.7,55.2,below,HOLD", "cross_state"),
    ],
    ids=["indicator-exceeds-atol", "close-must-be-exact", "signal-enum-exact", "cross-state-enum-exact"],
)
def test_a_differing_column_fails_and_names_that_field(tmp_path: Path, candidate_row: str, field: str) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, ["1700000000000,100.5,99.1,98.7,55.2,above,HOLD"])
    _write_state(b, [candidate_row])
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False, "a differing column fails the comparison"
    assert any(f.field == field for f in r.failures), "a failure names the differing field"


def test_schema_drift_fails(tmp_path: Path) -> None:
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    a.write_text(HEADER + "1700000000000,100.5,99.1,98.7,55.2,above,HOLD\n", encoding="utf-8")
    b.write_text(
        "ts_ms_utc,close,ema_fast,ema_slow,rsi,signal\n"  # missing cross_state
        "1700000000000,100.5,99.1,98.7,55.2,HOLD\n",
        encoding="utf-8",
    )
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False
    assert any(f.field == "schema" for f in r.failures)


def test_default_indicator_atol_is_1e_minus_9() -> None:
    assert Decimal("1e-9") == DEFAULT_INDICATOR_ATOL


def test_row_count_mismatch_fails(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(
        a,
        [
            "1700000000000,100.5,99.1,98.7,55.2,above,HOLD",
            "1700000900000,101.2,99.8,99.0,57.1,above,ENTER",
        ],
    )
    _write_state(b, ["1700000000000,100.5,99.1,98.7,55.2,above,HOLD"])
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False
    assert any(f.field == "row_count" for f in r.failures)


@pytest.mark.parametrize(
    ("row", "field"),
    [
        ("1700000000000,100.5,99.1,98.7,55.2,BULLISH,HOLD", "cross_state"),
        ("1700000000000,100.5,99.1,98.7,55.2,above,WAT", "signal"),
    ],
    ids=["invalid-cross-state-in-both-sides", "invalid-signal-in-both-sides"],
)
def test_an_invalid_enum_value_in_both_sides_is_caught_once_per_side(tmp_path: Path, row: str, field: str) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    # Both sides emit the same invalid enum value — equality passes but enum is bad.
    _write_state(a, [row])
    _write_state(b, [row])
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False, "an invalid enum value fails the comparison though both sides agree"
    # Two failures expected — one for each side.
    field_failures = [f for f in r.failures if f.field == field]
    assert len(field_failures) == 2, "one failure for each side"
