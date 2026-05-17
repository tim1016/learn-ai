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
    shutil.copy(SAMPLE_ORDER_EVENTS, output_dir / f"{algo_id}-order-events.json")


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
        result = parse_workspace(ws)
        assert result.total_order_events == 0
        assert result.order_events == []

    def test_malformed_summary_raises(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_badsummary", tmp_path)
        ws.ensure_layout()
        (ws.output_dir / "MyAlgorithm-summary.json").write_text("not json {", encoding="utf-8")
        with pytest.raises(NormalizedParserError, match="could not read"):
            parse_workspace(ws)

    def test_array_summary_rejected(self, tmp_path: Path) -> None:
        ws = resolve_workspace("parser_unit_arrsumm", tmp_path)
        ws.ensure_layout()
        (ws.output_dir / "MyAlgorithm-summary.json").write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(NormalizedParserError, match="not a JSON object"):
            parse_workspace(ws)


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
