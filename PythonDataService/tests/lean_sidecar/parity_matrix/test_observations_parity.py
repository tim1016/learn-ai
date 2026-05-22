"""Gate 1 — observations.csv exact-equality comparator."""

from __future__ import annotations

from pathlib import Path

from app.lean_sidecar.parity_matrix.observations_parity import (
    ObservationsParityResult,
    compare_observations,
)


def _write(path: Path, rows: list[str]) -> None:
    path.write_text("ms_utc,open,high,low,close,volume\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_identical_passes(tmp_path: Path) -> None:
    rows = [
        "1700000000000,100.50,101.00,100.25,100.75,1000",
        "1700000060000,100.75,101.50,100.50,101.25,1500",
    ]
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write(a, rows)
    _write(b, rows)
    r = compare_observations(reference=a, candidate=b)
    assert isinstance(r, ObservationsParityResult)
    assert r.passed is True
    assert r.row_count == 2
    assert r.failures == []


def test_row_count_mismatch_fails(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write(a, ["1700000000000,100.50,101.00,100.25,100.75,1000"])
    _write(b, [])
    r = compare_observations(reference=a, candidate=b)
    assert r.passed is False
    assert any(f.field == "row_count" for f in r.failures)


def test_timestamp_mismatch_localized(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write(a, ["1700000000000,100.50,101.00,100.25,100.75,1000"])
    _write(b, ["1700000060000,100.50,101.00,100.25,100.75,1000"])
    r = compare_observations(reference=a, candidate=b)
    assert r.passed is False
    assert r.failures[0].row_index == 0
    assert r.failures[0].field == "ms_utc"


def test_close_decimal_drift_fails(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write(a, ["1700000000000,100.50,101.00,100.25,100.75,1000"])
    # Trailing zero differs in source but Decimal-equal: 100.750 == 100.75.
    _write(b, ["1700000000000,100.50,101.00,100.25,100.750,1000"])
    r = compare_observations(reference=a, candidate=b)
    assert r.passed is True  # Decimal equality, not string equality

    _write(b, ["1700000000000,100.50,101.00,100.25,100.7500001,1000"])
    r = compare_observations(reference=a, candidate=b)
    assert r.passed is False
    assert r.failures[0].field == "close"


def test_schema_header_drift_fails(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text("ms_utc,open,high,low,close,volume\n1,1,1,1,1,1\n", encoding="utf-8")
    b.write_text("ms_utc,o,h,l,c,v\n1,1,1,1,1,1\n", encoding="utf-8")
    r = compare_observations(reference=a, candidate=b)
    assert r.passed is False
    assert any(f.field == "schema" for f in r.failures)
