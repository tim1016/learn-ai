"""Parser for [BAR] heartbeat lines in live.log.

Formula: Regex extraction of ISO datetime, consolidator_emitted count, and snapshot state from [BAR] log lines.
Reference: PythonDataService/app/engine/live/live_engine.py lines 516-521
Canonical implementation: app/services/live_log_parser.py
Validated against: PythonDataService/tests/test_live_log_parser.py
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

_BAR_PATTERN = re.compile(
    r"\[BAR\]\s+"
    r"(\d{4}-\d{2}-\d{2}T[\d:.]+)"  # ISO datetime
    r"\s+consolidator_emitted=(\d+)"
    r"\s+snapshot=(set|None)"
)


class BarEvent(BaseModel):
    """A parsed [BAR] heartbeat line."""

    ts_ms: int
    consolidator_emitted: int
    snapshot_set: bool
    raw_text: str


class RawLine(BaseModel):
    """An unparsed or non-[BAR] log line."""

    ts_ms: int | None
    raw_text: str


def _iso_to_ms(iso: str) -> int:
    """Parse ISO datetime string to int64 ms UTC. Assumes UTC if no tz."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def parse_bar_line(line: str) -> BarEvent | RawLine:
    """Parse one log line. Returns BarEvent if it contains a [BAR] entry, RawLine otherwise."""
    m = _BAR_PATTERN.search(line)
    if m is None:
        return RawLine(ts_ms=None, raw_text=line.rstrip())

    iso_str, emitted_str, snapshot_str = m.group(1), m.group(2), m.group(3)
    return BarEvent(
        ts_ms=_iso_to_ms(iso_str),
        consolidator_emitted=int(emitted_str),
        snapshot_set=(snapshot_str == "set"),
        raw_text=line.rstrip(),
    )


def parse_log_tail(
    lines: list[str],
) -> tuple[list[BarEvent | RawLine], Literal["ok", "degraded", "no_bars_yet"]]:
    """Parse a list of log lines.

    Returns parsed events and heartbeat_parse_status:
    - "no_bars_yet": no [BAR] lines found
    - "degraded": some lines failed to parse (RawLine in results for lines containing [BAR])
    - "ok": all [BAR] lines parsed successfully
    """
    parsed: list[BarEvent | RawLine] = [parse_bar_line(line) for line in lines]
    bar_lines = [line for line in lines if "[BAR]" in line]
    failed_bar_lines = [r for r, line in zip(parsed, lines, strict=False) if "[BAR]" in line and isinstance(r, RawLine)]

    if not bar_lines:
        status: Literal["ok", "degraded", "no_bars_yet"] = "no_bars_yet"
    elif failed_bar_lines:
        status = "degraded"
    else:
        status = "ok"

    return parsed, status
