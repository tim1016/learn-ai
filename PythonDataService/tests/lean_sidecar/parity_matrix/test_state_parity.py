"""Gate 2 — per-bar state.csv parity comparator."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.lean_sidecar.parity_matrix.state_parity import (
    DEFAULT_INDICATOR_ATOL,
    StateParityResult,
    compare_state,
)

HEADER = "ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal\n"


def _write_state(path: Path, rows: list[str]) -> None:
    path.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")


def test_identical_passes(tmp_path: Path) -> None:
    rows = [
        "1700000000000,100.5,99.1,98.7,55.2,above,HOLD",
        "1700000900000,101.2,99.8,99.0,57.1,above,ENTER",
    ]
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, rows)
    _write_state(b, rows)
    r = compare_state(reference=a, candidate=b)
    assert isinstance(r, StateParityResult)
    assert r.passed is True
    assert r.row_count == 2


def test_indicator_within_atol_passes(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, ["1700000000000,100.5,99.1,98.7,55.2,above,HOLD"])
    _write_state(b, ["1700000000000,100.5,99.100000000001,98.7,55.2,above,HOLD"])
    r = compare_state(reference=a, candidate=b)
    # Default atol=1e-9: 1e-12 drift is within tolerance.
    assert r.passed is True


def test_indicator_exceeds_atol_fails(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, ["1700000000000,100.5,99.1,98.7,55.2,above,HOLD"])
    _write_state(b, ["1700000000000,100.5,99.10001,98.7,55.2,above,HOLD"])
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False
    assert any(f.field == "ema_fast" for f in r.failures)


def test_close_must_be_exact(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, ["1700000000000,100.5,99.1,98.7,55.2,above,HOLD"])
    _write_state(b, ["1700000000000,100.50001,99.1,98.7,55.2,above,HOLD"])
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False
    assert any(f.field == "close" for f in r.failures)


def test_signal_enum_exact(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, ["1700000000000,100.5,99.1,98.7,55.2,above,HOLD"])
    _write_state(b, ["1700000000000,100.5,99.1,98.7,55.2,above,ENTER"])
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False
    assert any(f.field == "signal" for f in r.failures)


def test_cross_state_enum_exact(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, ["1700000000000,100.5,99.1,98.7,55.2,above,HOLD"])
    _write_state(b, ["1700000000000,100.5,99.1,98.7,55.2,below,HOLD"])
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False
    assert any(f.field == "cross_state" for f in r.failures)


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
