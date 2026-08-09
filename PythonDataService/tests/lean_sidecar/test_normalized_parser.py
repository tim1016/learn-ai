"""Tests for the Phase 3a LEAN output parser.

Fixtures under ``tests/lean_sidecar/fixtures/`` are real LEAN outputs
harvested from a Phase 2a trusted-sample run on the pinned image
(sha256:9788...). When a future LEAN bump changes the output schema,
regenerate the fixtures via the documented pin-and-run flow and bump
``NORMALIZED_PARSER_VERSION``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.lean_sidecar.normalized_parser import (
    NORMALIZED_PARSER_VERSION,
    NormalizedOrderEvent,
    NormalizedParserError,
    NormalizedResult,
    parse_workspace,
    write_normalized_result,
)
from app.lean_sidecar.workspace import resolve_workspace

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLE_SUMMARY = FIXTURE_DIR / "lean_sample_summary.json"
SAMPLE_ORDER_EVENTS = FIXTURE_DIR / "lean_sample_order_events.json"


def _populate_workspace_with_fixtures(workspace_root: Path, algo_id: str = "MyAlgorithm") -> None:
    """Drop the fixture files into a workspace's output/ at the LEAN-expected names."""
    output_dir = workspace_root / "workspace" / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(SAMPLE_SUMMARY, output_dir / f"{algo_id}-summary.json")
    # The legacy schema fixture predates retention of LEAN's full result. It is
    # sufficient for parser-shape tests, so duplicate it as the matching full
    # artifact. Full-result-specific behavior is exercised by the focused
    # fixtures below.
    shutil.copy(SAMPLE_SUMMARY, output_dir / f"{algo_id}.json")
    shutil.copy(SAMPLE_ORDER_EVENTS, output_dir / f"{algo_id}-order-events.json")


def _write_full_result_fixture(output_dir: Path) -> None:
    """Write a compact LEAN full+summary pair covering the lossless surface."""
    summary = {
        "statistics": {"Sharpe Ratio": "9.999", "Total Orders": "1"},
        "runtimeStatistics": {"Equity": "$100,100.00"},
        "charts": {
            "Strategy Equity": {
                "series": {
                    "Equity": {
                        "unit": "$",
                        "index": 0,
                        "values": [[1_700_000_000, 100_000.0, 100_000.0, 100_000.0, 100_000.0]],
                    }
                }
            }
        },
    }
    full = {
        "algorithmConfiguration": {
            "name": "local",
            "startDate": "2023-11-14T00:00:00Z",
            "endDate": "2023-11-15T23:59:59Z",
            "tradingDaysPerYear": 252,
        },
        "state": {
            "StartTime": "2026-08-08T23:54:18Z",
            "EndTime": "2026-08-08T23:55:12Z",
            "OrderCount": "2",
            "Status": "Completed",
        },
        "statistics": {"Sharpe Ratio": "-1.012", "Total Orders": "2"},
        "runtimeStatistics": {"Equity": "$100,120.00", "Fees": "-$2.00"},
        "charts": {
            "Strategy Equity": {
                "series": {
                    "Equity": {
                        "unit": "$",
                        "index": 0,
                        "values": [
                            [1_700_000_000, 100_000.0, 100_000.0, 100_000.0, 100_000.0],
                            [1_700_086_400, 100_120.0, 100_120.0, 100_120.0, 100_120.0],
                        ],
                    },
                    "Return": {
                        "unit": "%",
                        "index": 1,
                        "values": [[1_700_086_400, 0.0012]],
                    },
                }
            },
            "Drawdown": {
                "series": {
                    "Equity Drawdown": {
                        "unit": "%",
                        "index": 0,
                        "values": [[1_700_086_400, -0.0026]],
                    }
                }
            },
        },
        "orders": {
            "1": {
                "id": 1,
                "time": "2023-11-14T15:00:00Z",
                "createdTime": "2023-11-14T15:00:00Z",
                "lastFillTime": "2023-11-14T15:00:00Z",
                "price": 100.125,
            },
            "2": {
                "id": 2,
                "time": "2023-11-15T15:00:00Z",
                "createdTime": "2023-11-15T15:00:00Z",
                "lastFillTime": "2023-11-15T15:00:00Z",
                "price": 101.125,
            },
        },
        "profitLoss": {
            "2023-11-15T15:00:00Z": 122.0,
        },
        "rollingWindow": {
            "M1_20231115": {
                "tradeStatistics": {
                    "startDateTime": "2023-11-14T15:00:00Z",
                    "endDateTime": "2023-11-15T15:00:00Z",
                    "profitFactor": "1.5",
                },
                "portfolioStatistics": {"sharpeRatio": "-1.0125"},
                "closedTrades": [],
            }
        },
        "analysis": [
            {
                "name": "FlatEquityCurveAnalysis",
                "issue": "The equity curve is flat for several days in a row.",
                "sample": [
                    {
                        "start": "2023-11-14T04:00:00Z",
                        "end": "2023-11-15T04:00:00",
                        "trading_days": 2,
                    }
                ],
                "solutions": ["Check the warm-up period."],
            },
            {
                "name": "StatisticalSignificanceOfDailyReturnsAnalysis",
                "issue": "The p-value is above 0.05.",
                "sample": {"pValue": 0.0684907141208504},
                "solutions": ["Collect more evidence."],
            },
        ],
        "totalPerformance": {
            "portfolioStatistics": {
                "startEquity": "100000",
                "endEquity": "100120.00",
                "sharpeRatio": "-1.0125",
                "sortinoRatio": "-0.6035",
                "drawdown": "0.026",
            },
            "tradeStatistics": {
                "startDateTime": "2023-11-14T15:00:00Z",
                "endDateTime": "2023-11-15T15:00:00Z",
                "totalNumberOfTrades": 1,
                "profitFactor": "1.9827",
                "sharpeRatio": "0.2259",
                "sortinoRatio": "0.3992",
                "totalFees": "2.00",
            },
            "closedTrades": [
                {
                    "id": "trade-1",
                    "entryTime": "2023-11-14T15:00:00Z",
                    "exitTime": "2023-11-15T15:00:00Z",
                    "entryPrice": 100.125,
                    "exitPrice": 101.125,
                    "profitLoss": 122.0,
                    "duration": "1.00:00:00",
                }
            ],
        },
    }
    (output_dir / "MyAlgorithm-summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (output_dir / "MyAlgorithm.json").write_text(json.dumps(full), encoding="utf-8")
    (output_dir / "data-monitor-report-1.json").write_text(
        json.dumps({"succeeded-data-requests-count": 4, "failed-data-requests-count": 0}),
        encoding="utf-8",
    )


class TestParseWorkspace:
    def test_full_round_trip_against_real_lean_output(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_full", tmp_path)
        ws.ensure_layout()
        _populate_workspace_with_fixtures(ws.root)

        result = parse_workspace(ws)

        assert isinstance(result, NormalizedResult)
        assert result.parser_version == NORMALIZED_PARSER_VERSION
        assert result.algorithm_id == "MyAlgorithm"
        # The fixture run produced 1 trade -> 2 order events
        # (submitted + filled).
        assert result.total_order_events == 2
        assert len(result.order_events) == 2
        # Equity curve has 30 sampled points across the 5-day window.
        assert result.total_equity_points > 0
        assert result.first_equity_ms_utc is not None
        assert result.last_equity_ms_utc is not None
        # Timestamps in int64 ms UTC (not seconds) — the fixture's
        # first equity sample is around 2025-01-06.
        assert 1_736_000_000_000 < result.first_equity_ms_utc < 1_737_000_000_000

    def test_statistics_preserved_as_strings(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_stats", tmp_path)
        ws.ensure_layout()
        _populate_workspace_with_fixtures(ws.root)

        result = parse_workspace(ws)

        # LEAN's stats are kept as strings — version-dependent formatting
        # is the responsibility of the consumer, not the parser.
        assert isinstance(result.statistics["Sharpe Ratio"], str)
        assert "Total Orders" in result.statistics
        assert result.statistics["Total Orders"] == "1"

    def test_order_event_ms_utc_replaces_unix_seconds(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_ts", tmp_path)
        ws.ensure_layout()
        _populate_workspace_with_fixtures(ws.root)

        result = parse_workspace(ws)

        event = result.order_events[0]
        # 1736173860 unix seconds = 1736173860000 ms — the parser
        # multiplied by 1000. The "time" key from LEAN is dropped in
        # favor of "ms_utc".
        assert event.ms_utc == 1736173860000
        # Ensure the parser truly dropped the raw `time` field —
        # otherwise downstream consumers might branch on it.
        event_dump = event.model_dump(mode="json")
        assert "time" not in event_dump

    def test_equity_point_decoded_open_high_low_close(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_eq", tmp_path)
        ws.ensure_layout()
        _populate_workspace_with_fixtures(ws.root)

        result = parse_workspace(ws)

        # First point: starting equity, OHLC all 100000.
        first = result.equity_curve[0]
        assert first.value == 100_000.0
        assert first.open == 100_000.0
        assert first.high == 100_000.0
        assert first.low == 100_000.0

    def test_missing_summary_raises_typed_error(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_nosummary", tmp_path)
        ws.ensure_layout()
        # output/ exists but no *-summary.json under it.
        with pytest.raises(NormalizedParserError, match="summary"):
            parse_workspace(ws)

    def test_missing_output_dir_raises_typed_error(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_nodir", tmp_path)
        # NOT calling ensure_layout — output/ does not exist.
        with pytest.raises(NormalizedParserError, match="output directory"):
            parse_workspace(ws)

    def test_order_events_optional(self, tmp_path: Path) -> None:
        """A run with no orders writes no order-events file; that's a
        zero-order run, not an error."""
        ws = resolve_workspace("parser_unit_noorders", tmp_path)
        ws.ensure_layout()
        # Copy only the summary, not order events.
        shutil.copy(SAMPLE_SUMMARY, ws.output_dir / "MyAlgorithm-summary.json")
        shutil.copy(SAMPLE_SUMMARY, ws.output_dir / "MyAlgorithm.json")
        result = parse_workspace(ws)
        assert result.total_order_events == 0
        assert result.order_events == []

    def test_malformed_summary_raises(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_badsummary", tmp_path)
        ws.ensure_layout()
        (ws.output_dir / "MyAlgorithm.json").write_text("{}", encoding="utf-8")
        (ws.output_dir / "MyAlgorithm-summary.json").write_text("not json {", encoding="utf-8")
        with pytest.raises(NormalizedParserError, match="could not read"):
            parse_workspace(ws)

    def test_array_summary_rejected(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_arrsumm", tmp_path)
        ws.ensure_layout()
        (ws.output_dir / "MyAlgorithm.json").write_text("{}", encoding="utf-8")
        (ws.output_dir / "MyAlgorithm-summary.json").write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(NormalizedParserError, match="not a JSON object"):
            parse_workspace(ws)

    def test_order_events_bound_to_summary_algo_id(self, tmp_path: Path) -> None:
        """Reviewer P1: with a stale order-events file from a previous
        run in the same workspace (different algo id that happens to
        sort first), the parser must NOT pick up the stale events.
        """
        ws = resolve_workspace("parser_unit_stalemix", tmp_path)
        ws.ensure_layout()
        # The current run's summary names algo "MyAlgorithm".
        shutil.copy(SAMPLE_SUMMARY, ws.output_dir / "MyAlgorithm-summary.json")
        shutil.copy(SAMPLE_SUMMARY, ws.output_dir / "MyAlgorithm.json")
        # The current run produced no order-events file (zero orders).
        # But a previous run that used a different algo ("AAlgo")
        # left an events file behind in the same workspace.
        (ws.output_dir / "AAlgo-order-events.json").write_text(
            '[{"orderEventId":99,"orderId":99,"algorithmId":"AAlgo","symbol":"X 2T",'
            '"symbolValue":"X","symbolPermtick":"X","time":1736173860.0,"status":"filled",'
            '"fillPrice":1.0,"fillPriceCurrency":"USD","fillQuantity":1.0,"direction":"buy",'
            '"isAssignment":false,"quantity":1.0}]',
            encoding="utf-8",
        )
        result = parse_workspace(ws)
        # Must report zero events for the current run, not the stale 1.
        assert result.total_order_events == 0
        assert result.order_events == []
        assert result.algorithm_id == "MyAlgorithm"

    def test_null_statistics_treated_as_empty(self, tmp_path: Path) -> None:
        """Reviewer P2: ``statistics: null`` must not raise AttributeError.

        The orchestrator only catches NormalizedParserError; a bare
        AttributeError from a downstream .items() would fail the whole
        trusted-run request instead of returning normalized=None as
        the contract promises.
        """
        ws = resolve_workspace("parser_unit_nullstats", tmp_path)
        ws.ensure_layout()
        body = '{"statistics": null, "runtimeStatistics": null, "charts": {}}'
        (ws.output_dir / "MyAlgorithm-summary.json").write_text(body, encoding="utf-8")
        (ws.output_dir / "MyAlgorithm.json").write_text(body, encoding="utf-8")
        result = parse_workspace(ws)
        assert result.statistics == {}
        assert result.runtime_statistics == {}

    @pytest.mark.parametrize(
        "field,bad_value",
        [
            ("statistics", "not a dict"),
            ("statistics", [1, 2, 3]),
            ("runtimeStatistics", 42),
            ("runtimeStatistics", "string instead of dict"),
        ],
    )
    def test_non_object_statistics_raises_parser_error(self, tmp_path: Path, field: str, bad_value: object) -> None:
        """A non-null non-object value for statistics/runtimeStatistics
        is a schema violation worth surfacing as NormalizedParserError
        — not a silent ``AttributeError`` from a downstream ``.items()``.
        """
        ws = resolve_workspace(
            f"parser_unit_badstats_{abs(hash(field + str(bad_value))) % 10000:04d}",
            tmp_path,
        )
        ws.ensure_layout()
        import json as _json

        body = {"statistics": {}, "runtimeStatistics": {}, "charts": {}}
        body[field] = bad_value  # type: ignore[assignment]
        (ws.output_dir / "MyAlgorithm-summary.json").write_text(_json.dumps(body), encoding="utf-8")
        (ws.output_dir / "MyAlgorithm.json").write_text(_json.dumps(body), encoding="utf-8")
        with pytest.raises(NormalizedParserError, match="must be a JSON object"):
            parse_workspace(ws)

    def test_missing_matching_full_result_raises(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_nofull", tmp_path)
        ws.ensure_layout()
        shutil.copy(SAMPLE_SUMMARY, ws.output_dir / "MyAlgorithm-summary.json")

        with pytest.raises(NormalizedParserError, match="full result"):
            parse_workspace(ws)

    def test_full_result_is_native_source_and_summary_is_separate(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_full_native", tmp_path)
        ws.ensure_layout()
        _write_full_result_fixture(ws.output_dir)

        result = parse_workspace(ws)

        assert result.statistics["Sharpe Ratio"] == "-1.012"
        assert result.total_equity_points == 2
        assert result.total_summary_equity_points == 1
        assert result.equity_curve[-1].value == 100_120.0
        assert result.summary_equity_curve[-1].value == 100_000.0
        assert result.last_equity_ms_utc == 1_700_086_400_000
        assert result.last_summary_equity_ms_utc == 1_700_000_000_000

    def test_preserves_full_native_statistics_orders_rolling_and_analysis(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_lossless", tmp_path)
        ws.ensure_layout()
        _write_full_result_fixture(ws.output_dir)

        result = parse_workspace(ws)

        portfolio = result.total_performance["portfolioStatistics"]
        trade = result.total_performance["tradeStatistics"]
        assert portfolio["sharpeRatio"] == "-1.0125"
        assert trade["profitFactor"] == "1.9827"
        assert trade["sharpeRatio"] == "0.2259"
        assert trade["sortinoRatio"] == "0.3992"
        assert result.total_closed_trades == 1
        assert result.total_orders == 2
        assert result.total_rolling_windows == 1
        assert result.total_analyses == 2
        assert len(result.orders) == 2
        assert len(result.analysis) == 2
        assert set(result.full_charts) == {"Strategy Equity", "Drawdown"}

    def test_normalizes_every_known_native_timestamp_to_ms_utc(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_native_timestamps", tmp_path)
        ws.ensure_layout()
        _write_full_result_fixture(ws.output_dir)

        result = parse_workspace(ws)

        assert result.algorithm_start_ms_utc == 1_699_920_000_000
        assert result.algorithm_end_ms_utc == 1_700_092_799_000
        assert result.state["StartTime"] == 1_786_233_258_000
        assert result.orders["1"]["time"] == 1_699_974_000_000
        assert result.total_performance["closedTrades"][0]["entryTime"] == 1_699_974_000_000
        assert result.total_performance["tradeStatistics"]["endDateTime"] == 1_700_060_400_000
        sample = result.analysis[0].sample
        assert isinstance(sample, list)
        assert sample[0]["start"] == 1_699_934_400_000
        # LEAN omits the timezone suffix on some analysis timestamps. The
        # parser's versioned LEAN-analysis rule treats those values as UTC.
        assert sample[0]["end"] == 1_700_020_800_000
        assert result.profit_loss[0].ms_utc == 1_700_060_400_000

    def test_preserves_numeric_lexemes_and_artifact_receipts(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_receipts", tmp_path)
        ws.ensure_layout()
        _write_full_result_fixture(ws.output_dir)

        result = parse_workspace(ws)

        # JSON floating-point lexemes stay strings on the lossless native
        # surface. Typed display projections may convert them separately.
        assert result.orders["1"]["price"] == "100.125"
        assert result.analysis[1].sample["pValue"] == "0.0684907141208504"
        assert result.data_monitor["succeeded-data-requests-count"] == 4
        assert result.data_monitor["failed-data-requests-count"] == 0
        assert set(result.artifacts) >= {"full_result", "summary_result", "data_monitor"}
        for receipt in result.artifacts.values():
            assert len(receipt.sha256) == 64
            assert receipt.size_bytes > 0


class TestWriteNormalizedResult:
    def test_writes_pretty_sorted_json(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_write", tmp_path)
        ws.ensure_layout()
        _populate_workspace_with_fixtures(ws.root)

        result = parse_workspace(ws)
        dest = write_normalized_result(ws, result)

        assert dest == ws.normalized_dir / "result.json"
        body = dest.read_text(encoding="utf-8")
        parsed = json.loads(body)
        # Sorted keys
        assert list(parsed.keys()) == sorted(parsed.keys())
        # Pretty: at least one indented newline
        assert "\n  " in body
        # No leftover .tmp from the atomic-write
        assert not (dest.with_suffix(dest.suffix + ".tmp")).exists()
        # Round-trip via Pydantic — the written file must parse back
        # into the same NormalizedResult.
        reloaded = NormalizedResult.model_validate(parsed)
        assert reloaded.algorithm_id == result.algorithm_id
        assert reloaded.total_order_events == result.total_order_events


class TestNormalizedOrderEventContract:
    """Catch a regression where LEAN's order-event field names drift."""

    def test_required_fields_aliased_from_lean_camel_case(self) -> None:
        raw = {
            "orderEventId": 7,
            "orderId": 1,
            "algorithmId": "MyAlgorithm",
            "symbol": "SPY 2T",
            "symbolValue": "SPY",
            "symbolPermtick": "SPY",
            "ms_utc": 1_736_173_860_000,
            "status": "filled",
            "direction": "buy",
            "quantity": 1.0,
            "fillPrice": 100.0,
            "fillPriceCurrency": "USD",
            "fillQuantity": 1.0,
            "isAssignment": False,
        }
        event = NormalizedOrderEvent.model_validate(raw)
        assert event.order_event_id == 7
        assert event.symbol_value == "SPY"
