"""Tests for the live.log parsers (failures + incidents) and classifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.live_log_failures import (
    _DEFAULT_SOURCE,
    _RULES,
    IncidentCategory,
    IncidentRule,
    IncidentSource,
    _build_default_source,
    classify,
    classify_source,
    extract_facts,
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
    # raw_ts is verbatim from the log (UTC, since the engine logger
    # pins time.gmtime); ts_ms is the same instant as canonical int64
    # ms since Unix epoch UTC.
    assert row.raw_ts == "2026-06-09 14:12:58.021"
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


# ---------------------------------------------------------------------------
# Catalog expansion (codex 2026-06-24 D-decisions): classify() recognises
# the six new categories on their anchor (logger, message) pairs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("logger", "message", "expected"),
    [
        # No exact-message anchor on app.broker.ibkr.client: PR-3 demoted
        # the "IBKR connectivity lost" emit to INFO, so parse_incidents
        # never sees that row in production. The ib_async.wrapper Error
        # 1100/1101/1102/2110 path above is the canonical
        # BROKER_DISCONNECT anchor.
        # BROKER_DISCONNECT — ib_async.client TCP-level disconnect.
        (
            "ib_async.client",
            "Peer closed connection.",
            IncidentCategory.BROKER_DISCONNECT,
        ),
        # DATA_FARM_DEGRADED — IBKR data-farm warnings via our client.
        (
            "app.broker.ibkr.client",
            "IBKR data farm degraded (code 2103)",
            IncidentCategory.DATA_FARM_DEGRADED,
        ),
        (
            "app.broker.ibkr.client",
            "IBKR data farm degraded (code 2105)",
            IncidentCategory.DATA_FARM_DEGRADED,
        ),
        # BROKER_EVENT_LOG_WRITE_FAILED — INFRA-side filesystem failure.
        (
            "app.broker.ibkr.client",
            "Could not append IBKR broker event log: /app/_broker/events.jsonl read-only",
            IncidentCategory.BROKER_EVENT_LOG_WRITE_FAILED,
        ),
        # FOREIGN_FILL_DROPPED — cross-restart resolver missed.
        (
            "app.engine.live.intent_ledger",
            "Dropping IBKR fill for unknown order_id=42",
            IncidentCategory.FOREIGN_FILL_DROPPED,
        ),
        # SHUTDOWN_FLATTEN_FAILED — recovery flatten cascade.
        (
            "app.engine.live.run",
            "Recovery flatten itself failed during shutdown",
            IncidentCategory.SHUTDOWN_FLATTEN_FAILED,
        ),
        (
            "app.engine.live.run",
            "broker.cancel_open_orders failed during shutdown_flatten",
            IncidentCategory.SHUTDOWN_FLATTEN_FAILED,
        ),
        (
            "app.engine.live.run",
            "broker.cancel_open_orders failed during fatal halt",
            IncidentCategory.SHUTDOWN_FLATTEN_FAILED,
        ),
        # CONTROL_PLANE_LEASE_LOST — child-watchdog lease loss.
        (
            "app.engine.live.child_watchdog",
            "CONTROL_PLANE_LEASE_LOST: lease expired after 45s",
            IncidentCategory.CONTROL_PLANE_LEASE_LOST,
        ),
        # SIDECAR_SCHEMA_DRIFT — both anchors.
        (
            "app.engine.live.live_portfolio",
            "live-state sidecar write failed: extra fields forbidden",
            IncidentCategory.SIDECAR_SCHEMA_DRIFT,
        ),
        (
            "app.engine.live.live_portfolio",
            "LiveStateSidecarCorruptError: missing required field",
            IncidentCategory.SIDECAR_SCHEMA_DRIFT,
        ),
    ],
)
def test_classify_recognises_new_categories(
    logger: str, message: str, expected: IncidentCategory
) -> None:
    assert classify(logger, message) == expected


# ---------------------------------------------------------------------------
# classify_source() — default-per-category map + D3 refinement + D6
# UNKNOWN-source derivation from the logger namespace.
# ---------------------------------------------------------------------------


def test_default_source_map_covers_every_category() -> None:
    """Round-trip guard: every ``IncidentCategory`` value must have a
    ``_DEFAULT_SOURCE`` entry so a new enum addition can't silently
    fall through ``classify_source()``.
    """
    missing = [c for c in IncidentCategory if c not in _DEFAULT_SOURCE]

    assert missing == [], (
        f"_DEFAULT_SOURCE is missing entries for: {[c.value for c in missing]}"
    )


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (IncidentCategory.BROKER_DISCONNECT, IncidentSource.BROKER),
        (IncidentCategory.BROKER_RECONNECT_FAILED, IncidentSource.BROKER),
        (IncidentCategory.LOST_FILL, IncidentSource.BROKER),
        (IncidentCategory.OUTSIDE_MUTATION, IncidentSource.BROKER),
        (IncidentCategory.SUBSCRIPTION_STALE, IncidentSource.BROKER),
        (IncidentCategory.DATA_FARM_DEGRADED, IncidentSource.BROKER),
        (IncidentCategory.FOREIGN_FILL_DROPPED, IncidentSource.BROKER),
        (IncidentCategory.ENGINE_FATAL, IncidentSource.APP),
        (IncidentCategory.PORTFOLIO_INIT_FAIL, IncidentSource.APP),
        (IncidentCategory.RECONCILE_MISSING, IncidentSource.APP),
        (IncidentCategory.COLD_START_DIVERGENCE, IncidentSource.APP),
        (IncidentCategory.SIDECAR_SCHEMA_DRIFT, IncidentSource.APP),
        (IncidentCategory.BROKER_EVENT_LOG_WRITE_FAILED, IncidentSource.INFRA),
        (IncidentCategory.CONTROL_PLANE_LEASE_LOST, IncidentSource.INFRA),
        (IncidentCategory.OPERATOR_HALT, IncidentSource.OPERATOR),
    ],
)
def test_classify_source_returns_default_for_known_categories(
    category: IncidentCategory, expected: IncidentSource
) -> None:
    # Logger / message / traceback don't matter for the default map; they
    # only matter for the SHUTDOWN_FLATTEN_FAILED refinement and the
    # UNKNOWN logger heuristic. Pass empty strings to prove the map is
    # what the function is reading.
    assert classify_source(category, logger="", message="", traceback=None) == expected


@pytest.mark.parametrize(
    "marker",
    [
        "NotConnectedError",
        "ConnectionError",
        "Socket disconnect",
        "Peer closed connection",
        "IBKRBarStreamError",
        "IBKR client is not connected",
    ],
)
def test_classify_source_refines_shutdown_flatten_failed_to_broker(marker: str) -> None:
    """D3: any of the six broker-side markers in the traceback flips
    SHUTDOWN_FLATTEN_FAILED's source from the APP default to BROKER.
    """
    traceback = (
        "Traceback (most recent call last):\n"
        '  File "/app/engine/live/run.py", line 1, in flatten\n'
        f"    raise {marker}('lost')\n"
    )

    source = classify_source(
        IncidentCategory.SHUTDOWN_FLATTEN_FAILED,
        logger="app.engine.live.run",
        message="Recovery flatten itself failed",
        traceback=traceback,
    )

    assert source == IncidentSource.BROKER


def test_classify_source_keeps_shutdown_flatten_failed_as_app_when_engine_side() -> None:
    """No broker marker in either message or traceback ⇒ stays APP."""
    traceback = (
        "Traceback (most recent call last):\n"
        '  File "/app/engine/live/run.py", line 1, in flatten\n'
        "    raise ValueError('portfolio invariant violated')\n"
    )

    source = classify_source(
        IncidentCategory.SHUTDOWN_FLATTEN_FAILED,
        logger="app.engine.live.run",
        message="Recovery flatten itself failed",
        traceback=traceback,
    )

    assert source == IncidentSource.APP


def test_classify_source_refines_shutdown_flatten_via_message_alone() -> None:
    """Marker in the message (no traceback at all) also flips to BROKER."""
    source = classify_source(
        IncidentCategory.SHUTDOWN_FLATTEN_FAILED,
        logger="app.engine.live.run",
        message="Recovery flatten itself failed: Peer closed connection.",
        traceback=None,
    )

    assert source == IncidentSource.BROKER


@pytest.mark.parametrize(
    ("logger", "expected"),
    [
        ("ib_async.client", IncidentSource.BROKER),
        ("ib_async.wrapper", IncidentSource.BROKER),
        ("app.broker.ibkr.client", IncidentSource.BROKER),
        ("app.broker.ibkr.bars", IncidentSource.BROKER),
        ("app.engine.live.child_watchdog", IncidentSource.INFRA),
        ("app.engine.live.run", IncidentSource.APP),
        ("app.engine.live.live_portfolio", IncidentSource.APP),
        ("__main__", IncidentSource.APP),
        ("app.misc", IncidentSource.UNKNOWN),
        ("third_party.lib", IncidentSource.UNKNOWN),
    ],
)
def test_classify_source_for_unknown_derives_from_logger_namespace(
    logger: str, expected: IncidentSource
) -> None:
    """D6: UNKNOWN-category rows still get a source via the logger
    namespace heuristic so the cockpit can badge them.
    """
    assert (
        classify_source(IncidentCategory.UNKNOWN, logger=logger, message="anything", traceback=None)
        == expected
    )


# ---------------------------------------------------------------------------
# extract_facts() — hybrid-C named values per category (codex D1).
# ---------------------------------------------------------------------------


def test_extract_facts_returns_empty_dict_for_categories_without_extractor() -> None:
    """Categories that have no fact extractor return an empty dict so
    the frontend renders the template verbatim.
    """
    facts = extract_facts(
        IncidentCategory.OPERATOR_HALT,
        message="poison_sentinel.operator_declared: operator halted bot via CLI",
    )

    assert facts == {}


@pytest.mark.parametrize(
    ("code", "expected_tws_code"),
    [(1100, 1100), (1101, 1101), (1102, 1102), (2110, 2110)],
)
def test_extract_facts_pulls_tws_code_for_broker_disconnect(
    code: int, expected_tws_code: int
) -> None:
    facts = extract_facts(
        IncidentCategory.BROKER_DISCONNECT,
        message=f"Error {code}, reqId -1: lost",
    )

    assert facts == {"tws_code": expected_tws_code}


def test_extract_facts_pulls_tws_code_from_traceback_for_broker_disconnect() -> None:
    """The header message may not carry the code (e.g. our app.broker.ibkr.client
    'IBKR connectivity lost' anchor); the traceback fallback should still
    populate ``tws_code`` when it appears further down.
    """
    facts = extract_facts(
        IncidentCategory.BROKER_DISCONNECT,
        message="IBKR connectivity lost",
        traceback="ib_async.wrapper Error 1100, reqId -1: connectivity lost",
    )

    assert facts == {"tws_code": 1100}


@pytest.mark.parametrize("code", [2103, 2105])
def test_extract_facts_pulls_tws_code_for_data_farm_degraded(code: int) -> None:
    # Production emit shape — ``app.broker.ibkr.client._on_ib_error`` folds
    # the TWS code into the message string with this exact pattern so the
    # incident-table classifier can read it from ``%(message)s``.
    facts = extract_facts(
        IncidentCategory.DATA_FARM_DEGRADED,
        message=f"IBKR data farm degraded (code {code})",
    )

    assert facts == {"tws_code": code}


def test_extract_facts_does_not_pull_path_from_unrelated_traceback() -> None:
    """Regression for the path-extraction broadness: a broker-event-log
    write failure whose emit line has no path must NOT pick up an
    unrelated ``.jsonl`` path that happens to appear in a downstream
    traceback frame. The extractor is line-local, so the only way a
    path lands in dynamic_facts is if the emit site put it there.
    """
    facts = extract_facts(
        IncidentCategory.BROKER_EVENT_LOG_WRITE_FAILED,
        message="Could not append IBKR broker event log: read-only fs",
        traceback=(
            "Traceback (most recent call last):\n"
            '  File "/app/broker/ibkr/client.py", line 286, in _record_broker_event\n'
            "    fh.write(json.dumps(payload, sort_keys=True))\n"
            "OSError: [Errno 30] Read-only file system: '/app/other/file.jsonl'\n"
        ),
    )

    assert facts == {}


def test_extract_facts_does_not_pull_path_for_sidecar_drift_from_unrelated_json() -> None:
    """Same line-local discipline for SIDECAR_SCHEMA_DRIFT — an unrelated
    ``.json`` path in a downstream stack frame must not pose as the
    sidecar's path. The marker substring + path must share a line.
    """
    facts = extract_facts(
        IncidentCategory.SIDECAR_SCHEMA_DRIFT,
        message="live-state sidecar write failed: extra fields forbidden",
        traceback=(
            "Traceback (most recent call last):\n"
            '  File "/app/engine/live/live_portfolio.py", line 1, in persist\n'
            "ValidationError: extra fields forbidden\n"
            # An unrelated json path further down the trace — must not be
            # picked up as the sidecar path.
            'cached_settings_path="/app/cache/settings.json"\n'
        ),
    )

    assert facts == {}


def test_extract_facts_pulls_order_id_for_foreign_fill_dropped() -> None:
    facts = extract_facts(
        IncidentCategory.FOREIGN_FILL_DROPPED,
        message="Dropping IBKR fill for unknown order_id=42",
    )

    assert facts == {"order_id": "42"}


def test_extract_facts_returns_empty_when_order_id_pattern_absent() -> None:
    """The classifier may match the row on the bare phrase even if the
    runtime didn't include an order_id; facts should stay empty so the
    frontend falls back to the template literal.
    """
    facts = extract_facts(
        IncidentCategory.FOREIGN_FILL_DROPPED,
        message="Dropping IBKR fill for unknown order_id (id missing)",
    )

    assert facts == {}


def test_extract_facts_pulls_path_for_broker_event_log_write_failed() -> None:
    facts = extract_facts(
        IncidentCategory.BROKER_EVENT_LOG_WRITE_FAILED,
        message="Could not append IBKR broker event log: /app/_broker/events.jsonl read-only",
    )

    assert facts == {"path": "/app/_broker/events.jsonl"}


def test_extract_facts_pulls_path_for_sidecar_schema_drift() -> None:
    facts = extract_facts(
        IncidentCategory.SIDECAR_SCHEMA_DRIFT,
        message="live-state sidecar write failed at /app/artifacts/live_runs/abc/state.json",
    )

    assert facts == {"path": "/app/artifacts/live_runs/abc/state.json"}


# ---------------------------------------------------------------------------
# parse_incidents() — end-to-end sanity check that the new fields land on
# every row.
# ---------------------------------------------------------------------------


def test_parse_incidents_populates_incident_source_and_dynamic_facts() -> None:
    """Three rows spanning three sources: BROKER (1100 disconnect),
    INFRA (broker-event-log write fail), and UNKNOWN-derived-to-APP
    (engine logger with no catalog match).
    """
    text = (
        "2026-06-09 14:12:58,021 ERROR ib_async.wrapper Error 1100, reqId -1: lost\n"
        "2026-06-09 14:12:58,087 WARNING app.broker.ibkr.client "
        "Could not append IBKR broker event log: /app/_broker/events.jsonl read-only\n"
        "2026-06-09 14:12:58,127 ERROR app.engine.live.run unexpected state in driver\n"
    )

    rows = parse_incidents(text)

    assert len(rows) == 3

    # BROKER_DISCONNECT row carries the tws_code fact.
    assert rows[0].incident_category == IncidentCategory.BROKER_DISCONNECT
    assert rows[0].incident_source == IncidentSource.BROKER
    assert rows[0].dynamic_facts == {"tws_code": 1100}

    # BROKER_EVENT_LOG_WRITE_FAILED row is INFRA and carries the path.
    assert rows[1].incident_category == IncidentCategory.BROKER_EVENT_LOG_WRITE_FAILED
    assert rows[1].incident_source == IncidentSource.INFRA
    assert rows[1].dynamic_facts == {"path": "/app/_broker/events.jsonl"}

    # Unrecognised engine row falls through to UNKNOWN; D6 derives APP.
    assert rows[2].incident_category == IncidentCategory.UNKNOWN
    assert rows[2].incident_source == IncidentSource.APP
    assert rows[2].dynamic_facts == {}


def test_parse_incidents_classifies_shutdown_flatten_failed_source_per_d3() -> None:
    """A shutdown-flatten row with a broker-side marker in its traceback
    flips to BROKER per D3; the same row without that marker stays APP.
    """
    broker_side = (
        "2026-06-09 14:12:58,021 ERROR app.engine.live.run "
        "Recovery flatten itself failed\n"
        "Traceback (most recent call last):\n"
        '  File "/app/run.py", line 1, in flatten\n'
        "ib_async.wrapper.NotConnectedError: socket is closed\n"
    )
    engine_side = (
        "2026-06-09 14:12:58,021 ERROR app.engine.live.run "
        "Recovery flatten itself failed\n"
        "Traceback (most recent call last):\n"
        '  File "/app/run.py", line 1, in flatten\n'
        "ValueError: portfolio invariant violated\n"
    )

    broker_rows = parse_incidents(broker_side)
    engine_rows = parse_incidents(engine_side)

    assert len(broker_rows) == 1
    assert broker_rows[0].incident_category == IncidentCategory.SHUTDOWN_FLATTEN_FAILED
    assert broker_rows[0].incident_source == IncidentSource.BROKER

    assert len(engine_rows) == 1
    assert engine_rows[0].incident_category == IncidentCategory.SHUTDOWN_FLATTEN_FAILED
    assert engine_rows[0].incident_source == IncidentSource.APP


# ---------------------------------------------------------------------------
# _RULES table invariants. The refactor (issue #669) collapsed five
# parallel structures into one declarative table; these tests lock in
# the load-time guarantees that the derived maps depend on.
# ---------------------------------------------------------------------------


def test_rules_table_covers_every_non_unknown_category() -> None:
    """Every IncidentCategory except UNKNOWN must have at least one rule.

    UNKNOWN is the terminal fall-through and is added to _DEFAULT_SOURCE
    explicitly by ``_build_default_source``; it deliberately has no rule.
    """
    categories_in_rules = {rule.category for rule in _RULES}
    expected = set(IncidentCategory) - {IncidentCategory.UNKNOWN}

    missing = expected - categories_in_rules
    assert missing == set(), (
        f"_RULES is missing rows for: {sorted(c.value for c in missing)}"
    )

    assert IncidentCategory.UNKNOWN not in categories_in_rules, (
        "UNKNOWN must not appear in _RULES — it's the terminal fall-through"
    )


def test_build_default_source_raises_on_inconsistent_source() -> None:
    """Two rules sharing a category must share a source — the builder
    raises at module load to keep ``classify_source(category, …)`` from
    silently picking whichever rule landed last.
    """
    bad_rules = (
        IncidentRule(
            category=IncidentCategory.BROKER_DISCONNECT,
            source=IncidentSource.BROKER,
            matches=lambda _logger, _message: False,
        ),
        IncidentRule(
            category=IncidentCategory.BROKER_DISCONNECT,
            source=IncidentSource.APP,  # conflict
            matches=lambda _logger, _message: False,
        ),
    )

    with pytest.raises(RuntimeError, match=r"Inconsistent IncidentRule\.source"):
        _build_default_source(bad_rules)
