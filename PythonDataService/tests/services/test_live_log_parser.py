"""Table-driven tests for app.services.live_log_parser.

Formula: Regex extraction of ISO datetime, consolidator_emitted count, and
snapshot state from [BAR] log lines.
Reference: app/services/live_log_parser.py
Canonical implementation: app/services/live_log_parser.py
Validated against: golden fixtures in this file
"""

from __future__ import annotations

import pytest

from app.services.live_log_parser import BarEvent, RawLine, parse_bar_line, parse_log_tail

# ---------------------------------------------------------------------------
# parse_bar_line — parametrized happy / sad paths
# ---------------------------------------------------------------------------

_BAR_LINE_WITH_TS = "2026-01-01T09:35:00+00:00 INFO [BAR] 2026-01-01T09:35:00+00:00 consolidator_emitted=1 snapshot=set"
_BAR_LINE_SNAPSHOT_NONE = (
    "2026-01-01T09:35:00+00:00 INFO [BAR] 2026-01-01T09:35:00+00:00 consolidator_emitted=3 snapshot=None"
)
_BAR_LINE_MULTI_EMITTED = (
    "2026-01-01T09:40:00+00:00 INFO [BAR] 2026-01-01T09:40:00+00:00 consolidator_emitted=5 snapshot=set"
)
_NON_BAR_LINE = "INFO startup complete"
_MALFORMED_BAR_LINE = "[BAR] NOT-A-DATE consolidator_emitted=1 snapshot=set"


@pytest.mark.parametrize(
    "line, expected_type, expected_ts_ms, expected_emitted, expected_snapshot",
    [
        # Happy path: [BAR] with snapshot=set
        (
            _BAR_LINE_WITH_TS,
            BarEvent,
            1735720500000,  # 2026-01-01T09:35:00+00:00
            1,
            True,
        ),
        # [BAR] with snapshot=None → snapshot_set=False
        (
            _BAR_LINE_SNAPSHOT_NONE,
            BarEvent,
            1735720500000,
            3,
            False,
        ),
        # Non-[BAR] line → RawLine with ts_ms=None
        (
            _NON_BAR_LINE,
            RawLine,
            None,
            None,
            None,
        ),
        # Malformed [BAR] line (NOT-A-DATE) → parse fails → RawLine
        (
            _MALFORMED_BAR_LINE,
            RawLine,
            None,
            None,
            None,
        ),
    ],
    ids=["bar_snapshot_set", "bar_snapshot_none", "non_bar_line", "malformed_bar"],
)
def test_parse_bar_line(line, expected_type, expected_ts_ms, expected_emitted, expected_snapshot):
    result = parse_bar_line(line)

    assert isinstance(result, expected_type)

    if expected_type is BarEvent:
        assert result.ts_ms == expected_ts_ms
        assert result.consolidator_emitted == expected_emitted
        assert result.snapshot_set is expected_snapshot
        assert "[BAR]" in result.raw_text
    else:
        assert result.ts_ms is expected_ts_ms


def test_parse_bar_line_raw_text_preserved():
    """raw_text is the stripped input line for both BarEvent and RawLine."""
    line = _BAR_LINE_WITH_TS
    result = parse_bar_line(line)
    assert isinstance(result, BarEvent)
    assert result.raw_text == line.rstrip()


def test_parse_bar_line_multi_emitted():
    result = parse_bar_line(_BAR_LINE_MULTI_EMITTED)
    assert isinstance(result, BarEvent)
    assert result.consolidator_emitted == 5
    assert result.snapshot_set is True


# ---------------------------------------------------------------------------
# parse_log_tail — status classification
# ---------------------------------------------------------------------------


def test_parse_log_tail_empty_list():
    events, status = parse_log_tail([])
    assert events == []
    assert status == "no_bars_yet"


def test_parse_log_tail_only_non_bar_lines():
    lines = ["INFO startup", "DEBUG some debug", "WARNING watch out"]
    events, status = parse_log_tail(lines)
    assert len(events) == 3
    assert all(isinstance(e, RawLine) for e in events)
    assert status == "no_bars_yet"


def test_parse_log_tail_valid_bar_lines():
    lines = [
        _NON_BAR_LINE,
        _BAR_LINE_WITH_TS,
        _BAR_LINE_SNAPSHOT_NONE,
    ]
    events, status = parse_log_tail(lines)
    assert len(events) == 3
    assert isinstance(events[0], RawLine)
    assert isinstance(events[1], BarEvent)
    assert isinstance(events[2], BarEvent)
    assert status == "ok"


def test_parse_log_tail_one_malformed_among_valid():
    """One unparseable [BAR] line among valid ones → degraded."""
    lines = [
        _BAR_LINE_WITH_TS,
        _MALFORMED_BAR_LINE,  # contains [BAR] but date won't parse
    ]
    events, status = parse_log_tail(lines)
    assert len(events) == 2
    # First is BarEvent (valid), second is RawLine (malformed)
    assert isinstance(events[0], BarEvent)
    assert isinstance(events[1], RawLine)
    assert status == "degraded"


def test_parse_log_tail_all_malformed_bar_lines():
    """All [BAR] lines malformed → degraded (not no_bars_yet)."""
    lines = [
        "[BAR] BAD-DATE consolidator_emitted=1 snapshot=set",
        "[BAR] ALSO-BAD consolidator_emitted=2 snapshot=None",
    ]
    _, status = parse_log_tail(lines)
    assert status == "degraded"


def test_parse_log_tail_preserves_order():
    """Events are returned in input order."""
    lines = [_NON_BAR_LINE, _BAR_LINE_WITH_TS, _NON_BAR_LINE]
    events, _ = parse_log_tail(lines)
    assert isinstance(events[0], RawLine)
    assert isinstance(events[1], BarEvent)
    assert isinstance(events[2], RawLine)
