"""Tests for the live.log ERROR/CRITICAL parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.live_log_failures import parse_failures


def test_parse_failures_empty_text_returns_empty_list() -> None:
    assert parse_failures("") == []


def test_parse_failures_ignores_info_and_warning() -> None:
    text = (
        "2026-06-09 13:47:27,074 INFO __main__ [STEP 0] starting\n"
        "2026-06-09 13:47:27,125 WARNING app.broker.ibkr.client some warning\n"
        "2026-06-09 13:47:27,329 INFO app.broker.ibkr.client connected\n"
    )

    assert parse_failures(text) == []


def test_parse_failures_extracts_single_error_without_traceback() -> None:
    text = (
        "2026-06-09 14:12:58,021 ERROR ib_async.wrapper Error 1100, reqId -1: lost\n"
        "2026-06-09 14:12:58,127 INFO ib_async.wrapper Warning 2105: hmds broken\n"
    )

    rows = parse_failures(text)

    assert len(rows) == 1
    row = rows[0]
    assert row.level == "ERROR"
    assert row.logger == "ib_async.wrapper"
    assert row.message == "Error 1100, reqId -1: lost"
    assert row.traceback is None
    # raw_ts is verbatim from the log; ts_ms parses that as if UTC.
    assert row.raw_ts == "2026-06-09 14:12:58.021"
    # 2026-06-09 14:12:58.021 treated as UTC.
    assert row.ts_ms == 1781014378021


def test_parse_failures_absorbs_multiline_traceback() -> None:
    text = (
        "2026-06-09 14:12:58,087 ERROR __main__ [STEP 8] Unhandled exception\n"
        "Traceback (most recent call last):\n"
        '  File "/app/run.py", line 1190, in _drive_engine\n'
        "    await engine.run(strategy, bars=bars_iter)\n"
        "app.broker.ibkr.bars.IBKRBarStreamError: IBKR connection lost\n"
        "2026-06-09 14:12:58,127 INFO ib_async.wrapper recovering\n"
    )

    rows = parse_failures(text)

    assert len(rows) == 1
    row = rows[0]
    assert row.level == "ERROR"
    assert row.logger == "__main__"
    assert row.message == "[STEP 8] Unhandled exception"
    assert row.traceback is not None
    assert "Traceback (most recent call last):" in row.traceback
    assert "IBKRBarStreamError: IBKR connection lost" in row.traceback


def test_parse_failures_handles_back_to_back_errors() -> None:
    text = (
        "2026-06-09 14:12:58,021 ERROR ib_async.wrapper Error 1100: lost\n"
        "2026-06-09 14:12:58,087 CRITICAL __main__ engine halted\n"
        "2026-06-09 14:12:58,127 INFO __main__ shutdown clean\n"
    )

    rows = parse_failures(text)

    assert [r.level for r in rows] == ["ERROR", "CRITICAL"]
    assert rows[0].traceback is None
    assert rows[1].traceback is None
    assert rows[1].logger == "__main__"
    assert rows[1].message == "engine halted"


def test_parse_failures_continuation_before_first_header_is_dropped() -> None:
    text = (
        "stray garbage that shouldn't crash the parser\n"
        "2026-06-09 14:12:58,021 ERROR ib_async.wrapper Error 1100\n"
    )

    rows = parse_failures(text)

    assert len(rows) == 1
    assert rows[0].traceback is None


def test_parse_failures_truncates_very_long_traceback() -> None:
    huge = "\n".join([f'  File "/x.py", line {i}, in f' for i in range(2000)])
    text = (
        "2026-06-09 14:12:58,021 ERROR __main__ blew up\n"
        f"{huge}\n"
        "2026-06-09 14:12:58,127 INFO __main__ next line\n"
    )

    rows = parse_failures(text)

    assert len(rows) == 1
    assert rows[0].traceback is not None
    assert rows[0].traceback.endswith("… (truncated)")
    assert len(rows[0].traceback) <= 4_100  # cap + sentinel


def test_parse_failures_on_run_157b11c0_live_log_extracts_real_failure() -> None:
    """Smoke test against the actual run log: one ERROR + one ERROR-with-traceback.

    The run died from IBKR Error 1100 and an unhandled exception in
    _drive_engine. Both must appear; ib_async warnings must not.
    """
    run_id = "157b11c0b35de5a3e8ed9313e94434c14eea918475b5ee87890e39f20e7a06e5"
    # Containerised tests see /app/artifacts; host tests see PythonDataService/artifacts.
    candidates = [
        Path("/app/artifacts/live_runs") / run_id / "live.log",
        Path(__file__).resolve().parents[2]
        / "PythonDataService/artifacts/live_runs"
        / run_id
        / "live.log",
    ]
    log_path = next((p for p in candidates if p.exists()), None)
    if log_path is None:
        pytest.skip("run 157b11c0 artifact not present")

    rows = parse_failures(log_path.read_text(encoding="utf-8"))

    levels = [r.level for r in rows]
    assert levels.count("ERROR") >= 2, levels
    # No CRITICAL in this particular run.
    assert "CRITICAL" not in levels
    # The unhandled-exception row carries the IBKRBarStreamError traceback.
    unhandled = [r for r in rows if "Unhandled exception" in r.message]
    assert len(unhandled) == 1
    assert unhandled[0].traceback is not None
    assert "IBKRBarStreamError" in unhandled[0].traceback
