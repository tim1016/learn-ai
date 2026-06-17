"""Tests for the live.log parsers (failures + incidents) and classifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.live_log_failures import (
    IncidentCategory,
    classify,
    parse_failures,
    parse_incidents,
)


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


# ---------------------------------------------------------------------------
# classify() — backend-owned regex catalog (single source of truth for
# incident categorisation; the frontend never re-classifies).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("logger", "message", "expected"),
    [
        # BROKER_DISCONNECT — ib_async surfaces 1100/1101/1102/2110 from
        # ib_async.wrapper. Both ERROR-level and WARNING-level lines route
        # through the same classifier (parse_incidents widens the level
        # gate; classify itself doesn't read level).
        (
            "ib_async.wrapper",
            "Error 1100, reqId -1: Connectivity between IB and Trader Workstation has been lost.",
            IncidentCategory.BROKER_DISCONNECT,
        ),
        (
            "ib_async.wrapper",
            "Error 1101, reqId -1: Connectivity between IB and TWS has been restored - data lost.",
            IncidentCategory.BROKER_DISCONNECT,
        ),
        (
            "ib_async.wrapper",
            "Error 1102, reqId -1: Connectivity between IB and TWS has been restored - data maintained.",
            IncidentCategory.BROKER_DISCONNECT,
        ),
        (
            "ib_async.wrapper",
            "Error 2110, reqId -1: Connectivity broken.",
            IncidentCategory.BROKER_DISCONNECT,
        ),
        # Same code, wrong logger — not BROKER_DISCONNECT. Anchoring on
        # the logger prevents an unrelated module that logs "Error 1100"
        # in prose from being mis-classified.
        (
            "app.misc",
            "wrote Error 1100 to the log on purpose",
            IncidentCategory.UNKNOWN,
        ),
        # BROKER_RECONNECT_FAILED — auto_reconnect_monitor emits
        # "IBKR app-level probe failed; forcing reconnect" on a probe
        # failure that triggers the reconnect loop.
        (
            "app.broker.ibkr.auto_reconnect_monitor",
            "IBKR app-level probe failed; forcing reconnect",
            IncidentCategory.BROKER_RECONNECT_FAILED,
        ),
        # ENGINE_FATAL — emitted by engine.live.run when the engine
        # itself raises an unhandled exception.
        (
            "__main__",
            "[STEP 8] Unhandled exception in engine.run — attempting recovery flatten",
            IncidentCategory.ENGINE_FATAL,
        ),
        # PORTFOLIO_INIT_FAIL — LivePortfolio.__post_init__ fail-fast.
        (
            "app.engine.live.portfolio",
            "LivePortfolio cash_balance cannot be negative at boot",
            IncidentCategory.PORTFOLIO_INIT_FAIL,
        ),
        # RECONCILE_MISSING — readiness gate detail or follow-up log.
        (
            "app.engine.live.readiness",
            "no reconcile receipt — refusing to start",
            IncidentCategory.RECONCILE_MISSING,
        ),
        # Poison-sentinel triggers. The on-the-wire trigger names match
        # halt.PoisonTrigger values verbatim (outside_mutation, lost_fill,
        # cold_start_divergence, operator_declared). The classifier
        # tolerates both the bare token and the "poison_sentinel." prefix.
        (
            "app.engine.live.halt",
            "poison_sentinel.lost_fill: order 117 unfilled past 90s window",
            IncidentCategory.LOST_FILL,
        ),
        (
            "app.engine.live.halt",
            "poison_sentinel.outside_mutation: AAPL position changed without recorded fill",
            IncidentCategory.OUTSIDE_MUTATION,
        ),
        (
            "app.engine.live.halt",
            "poison_sentinel.cold_start_divergence: warm-up holdings disagree with broker",
            IncidentCategory.COLD_START_DIVERGENCE,
        ),
        (
            "app.engine.live.halt",
            "poison_sentinel.operator_declared: operator halted bot via CLI",
            IncidentCategory.OPERATOR_HALT,
        ),
        # SUBSCRIPTION_STALE — live_idempotent absorb_count over its
        # threshold. The exact wording is intentionally flexible; the
        # regex captures both 'absorb_count over threshold' and
        # 'live_idempotent ... absorb' phrasings.
        (
            "app.broker.ibkr.bars",
            "live_idempotent absorb_count over threshold (12 absorbed in 60s)",
            IncidentCategory.SUBSCRIPTION_STALE,
        ),
        # UNKNOWN — anything the catalog doesn't recognise.
        (
            "app.misc",
            "something completely unrelated",
            IncidentCategory.UNKNOWN,
        ),
    ],
)
def test_classify_recognises_seeded_categories(
    logger: str, message: str, expected: IncidentCategory
) -> None:
    assert classify(logger, message) == expected


def test_classify_falls_back_to_traceback_when_header_is_ambiguous() -> None:
    """A generic ``Unhandled exception`` header should resolve via the
    traceback body when it contains a recognisable signature.

    The header alone matches ENGINE_FATAL because of the
    'Unhandled exception in engine.run' substring, so a header that's
    actually ambiguous (no engine.run wording) is what exercises the
    traceback fallback. Here the underlying exception is an IBKR
    Error 1100, surfaced via the ``ib_async.wrapper`` logger token.
    """
    traceback = (
        "Traceback (most recent call last):\n"
        '  File "/app/run.py", line 1190, in _drive_engine\n'
        "    await engine.run(strategy, bars=bars_iter)\n"
        "ib_async.wrapper Error 1100, reqId -1: connectivity lost\n"
    )

    # Header itself isn't recognisable, so the catalog should consult
    # the traceback and resolve to BROKER_DISCONNECT.
    category = classify(
        logger="app.engine.live.run",
        message="engine driver raised",
        traceback=traceback,
    )

    assert category == IncidentCategory.BROKER_DISCONNECT


def test_classify_unknown_when_header_and_traceback_are_both_unrecognised() -> None:
    assert (
        classify(
            logger="app.misc",
            message="something went sideways",
            traceback="Traceback (most recent call last):\n  File '/x.py'\nValueError: nope",
        )
        == IncidentCategory.UNKNOWN
    )


# ---------------------------------------------------------------------------
# parse_incidents() — wider level set + classification, with the same
# header tokenisation and continuation handling as parse_failures.
# ---------------------------------------------------------------------------


def test_parse_incidents_empty_text_returns_empty_list() -> None:
    assert parse_incidents("") == []


def test_parse_incidents_captures_warning_level_broker_disconnect() -> None:
    """ib_async sometimes surfaces Error 1100 at WARNING — operators
    must still see it. parse_failures intentionally drops WARNINGs;
    parse_incidents widens the gate."""
    text = (
        "2026-06-09 14:12:58,021 WARNING ib_async.wrapper Error 1100, reqId -1: lost\n"
        "2026-06-09 14:12:58,127 INFO ib_async.wrapper recovering\n"
    )

    rows = parse_incidents(text)

    assert len(rows) == 1
    row = rows[0]
    assert row.level == "WARNING"
    assert row.logger == "ib_async.wrapper"
    assert row.incident_category == IncidentCategory.BROKER_DISCONNECT
    # parse_failures must still ignore this row.
    assert parse_failures(text) == []


def test_parse_incidents_captures_error_and_critical_and_classifies() -> None:
    text = (
        "2026-06-09 14:12:58,021 ERROR ib_async.wrapper Error 1100, reqId -1: lost\n"
        "2026-06-09 14:12:58,087 CRITICAL app.engine.live.halt "
        "poison_sentinel.lost_fill: order 117 unfilled past 90s window\n"
        "2026-06-09 14:12:58,127 INFO app.engine.live.halt shutting down\n"
    )

    rows = parse_incidents(text)

    assert [r.level for r in rows] == ["ERROR", "CRITICAL"]
    assert rows[0].incident_category == IncidentCategory.BROKER_DISCONNECT
    assert rows[1].incident_category == IncidentCategory.LOST_FILL


def test_parse_incidents_absorbs_traceback_into_drawer_payload() -> None:
    text = (
        "2026-06-09 14:12:58,087 ERROR __main__ [STEP 8] Unhandled exception in engine.run\n"
        "Traceback (most recent call last):\n"
        '  File "/app/run.py", line 1190, in _drive_engine\n'
        "    await engine.run(strategy, bars=bars_iter)\n"
        "app.broker.ibkr.bars.IBKRBarStreamError: IBKR connection lost\n"
        "2026-06-09 14:12:58,127 INFO ib_async.wrapper recovering\n"
    )

    rows = parse_incidents(text)

    assert len(rows) == 1
    row = rows[0]
    assert row.incident_category == IncidentCategory.ENGINE_FATAL
    assert row.traceback is not None
    assert "IBKRBarStreamError" in row.traceback


def test_parse_incidents_unknown_category_for_unrecognised_message() -> None:
    text = "2026-06-09 14:12:58,021 ERROR app.misc something totally novel happened\n"

    rows = parse_incidents(text)

    assert len(rows) == 1
    assert rows[0].incident_category == IncidentCategory.UNKNOWN
