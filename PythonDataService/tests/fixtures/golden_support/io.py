"""Timestamp normalizer and Arrow IPC read/write for golden fixtures.

All timestamps in the fixture system are int64 milliseconds UTC.
normalize_timestamp() is the single conversion boundary inside this module —
no other place in golden_support should convert timestamps.

Arrow IPC (.arrow / .feather) is the storage format for new numeric arrays.
Existing Parquet fixtures are left alone (additive, not migrative).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.ipc as ipc


# ── Timestamp normalizer ──────────────────────────────────────────────────────


class TimestampNormalizationError(ValueError):
    """Raised when a timestamp cannot be unambiguously normalized to int64 ms UTC."""


def normalize_timestamp(value: Any, *, source_precision: str = "ms") -> int:
    """Normalize a timestamp to int64 milliseconds since Unix epoch UTC.

    Parameters
    ----------
    value:
        Accepted types:
        - ``int`` or ``float``: interpreted as milliseconds UTC by default.
          Use ``source_precision`` to specify a different unit.
        - ``str``: must be an ISO-8601 string with an explicit UTC offset
          (Z or +00:00). Ambiguous/naive strings are rejected.
        - ``pandas.Timestamp``: must be tz-aware; naive is rejected.
    source_precision:
        Unit for numeric input. One of "ms" (default), "us", "ns", "s".
        Ignored for string and Timestamp inputs (which carry their own unit).

    Returns
    -------
    int
        Milliseconds since Unix epoch UTC.

    Raises
    ------
    TimestampNormalizationError
        For ambiguous, naive, or lossy inputs.
    """
    if isinstance(value, bool):
        raise TimestampNormalizationError(f"bool is not a timestamp: {value!r}")

    if isinstance(value, (int, float)):
        return _numeric_to_ms(value, source_precision)

    if isinstance(value, str):
        return _iso_string_to_ms(value)

    # pandas.Timestamp (imported lazily to avoid hard dep in test-only module)
    type_name = type(value).__name__
    if type_name == "Timestamp":
        return _pandas_timestamp_to_ms(value)

    raise TimestampNormalizationError(
        f"Cannot normalize {type_name!r} to int64 ms UTC. "
        "Accepted: int, float, ISO-8601 string with UTC designator, pd.Timestamp (tz-aware)."
    )


_PRECISION_FACTORS: dict[str, int] = {
    "s": 1_000,
    "ms": 1,
    "us": 0,  # divide by 1000
    "ns": 0,  # divide by 1_000_000
}

_PRECISION_DIVISORS: dict[str, int] = {
    "s": 1,
    "ms": 1,
    "us": 1_000,
    "ns": 1_000_000,
}


def _numeric_to_ms(value: int | float, precision: str) -> int:
    if precision not in _PRECISION_DIVISORS:
        raise TimestampNormalizationError(
            f"Unknown source_precision {precision!r}. Choose from: {list(_PRECISION_DIVISORS)}"
        )
    if precision == "ms":
        return int(value)
    if precision == "s":
        return int(value) * 1_000
    if precision == "us":
        return int(value) // 1_000
    if precision == "ns":
        return int(value) // 1_000_000
    raise TimestampNormalizationError(f"Unhandled precision: {precision!r}")


def _iso_string_to_ms(value: str) -> int:
    from datetime import datetime, timezone

    if not (value.endswith("Z") or "+00:00" in value or "+0000" in value):
        raise TimestampNormalizationError(
            f"Naive or ambiguous ISO string rejected: {value!r}. "
            "Include explicit UTC designator (Z or +00:00)."
        )
    s = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise TimestampNormalizationError(f"Cannot parse ISO string {value!r}: {exc}") from exc
    if dt.tzinfo is None:
        raise TimestampNormalizationError(f"Parsed datetime has no tzinfo: {value!r}")
    utc_dt = dt.astimezone(timezone.utc)
    return int(utc_dt.timestamp() * 1000)


def _pandas_timestamp_to_ms(ts: Any) -> int:
    if ts.tzinfo is None:
        raise TimestampNormalizationError(
            f"Naive pd.Timestamp rejected: {ts!r}. Localize to UTC first."
        )
    return int(ts.value // 1_000_000)


# ── Arrow IPC read/write ──────────────────────────────────────────────────────


def write_arrow(table: pa.Table, path: Path) -> None:
    """Write a PyArrow Table to Arrow IPC file format (.arrow / .feather)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with ipc.new_file(str(path), table.schema) as writer:
        writer.write_table(table)


def read_arrow(path: Path) -> pa.Table:
    """Read a PyArrow Table from Arrow IPC file format."""
    with ipc.open_file(str(path)) as reader:
        return reader.read_all()


def read_json_fixture(path: Path) -> Any:
    """Read a JSON fixture file."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_fixture(data: Any, path: Path, *, indent: int = 2) -> None:
    """Write a JSON fixture file (pretty-printed, UTF-8, LF line endings)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=indent) + "\n", encoding="utf-8")
