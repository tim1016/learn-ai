"""``record_from_payload``: the validation and defaults the retired .NET writers enforced, kept exactly."""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.research.backtest_runs.records import RunPayloadError, record_from_payload, utc_date_iso
from tests.research.backtest_runs.payloads import engine_payload, lean_payload, trade


def test_a_complete_engine_payload_maps_onto_the_row_field_for_field() -> None:
    record = record_from_payload(engine_payload())

    assert record.source == "engine" and record.requested_engine == "python" and record.lean_run_id is None
    assert record.symbol == "SPY" and record.parameters == {"symbol": "SPY", "gap_bps": 0.0}
    assert record.start_date == date(2025, 1, 6) and record.end_date == date(2025, 1, 10)
    assert record.initial_cash == 100_000.0 and record.final_equity == 100_020.0
    # The engine names its headlines; the LEAN projection with different numbers does not win.
    assert (record.max_drawdown, record.sharpe_ratio, record.sortino_ratio, record.profit_factor) == (0.0257, 1.43, 2.59, 2.0)
    assert json.loads(record.lean_statistics_json)["portfolio"]["sharpe_ratio"] == 1.54
    assert json.loads(record.equity_curve_json)["schema_version"] == 2
    [persisted] = record.trades
    assert persisted.trade_number == 1 and persisted.pnl == 20.0 and persisted.quantity == 10.0


def test_the_symbol_is_pinned_into_the_parameters_and_upper_cased() -> None:
    record = record_from_payload(engine_payload(symbol=" spy ", parameters={"gap_bps": 4.0}))

    assert record.symbol == "SPY"
    assert record.parameters == {"gap_bps": 4.0, "symbol": "SPY"}


def test_a_lean_payload_takes_its_headlines_from_the_native_statistics() -> None:
    record = record_from_payload(lean_payload("run-1"))

    assert record.source == "lean-sidecar" and record.lean_run_id == "run-1"
    assert (record.max_drawdown, record.sharpe_ratio, record.sortino_ratio, record.profit_factor) == (0.0191, 1.54, 1.0, 1.86)
    assert record.brokerage_policy == "interactive_brokers"
    assert record.fill_mode == "lean-sidecar"


def test_a_lean_payload_without_native_numbers_records_honest_nulls_not_zeros() -> None:
    record = record_from_payload(lean_payload("run-2", lean_statistics={"portfolio": {}, "trade": {}, "runtime": {}}))

    assert record.max_drawdown == 0.0
    assert record.sharpe_ratio is None and record.sortino_ratio is None and record.profit_factor is None


def test_defaults_fill_what_the_writers_left_out() -> None:
    payload = engine_payload()
    for key in ("fill_mode", "timespan", "duration_ms", "brokerage_policy", "commission_per_order", "requested_engine"):
        payload.pop(key)
    payload["data_policy_json"] = None
    payload["metric_documentation_json"] = None

    record = record_from_payload(payload)

    assert record.fill_mode == "signal_bar_close" and record.timespan == "minute" and record.duration_ms == 0
    assert record.brokerage_policy == "algorithm_default" and record.commission_per_order == 0.0
    assert record.requested_engine is None
    synthesized = json.loads(record.data_policy_json)
    assert synthesized["symbol"] == "SPY" and synthesized["adjusted"] is True and synthesized["strategy_bars"] == {"timespan": "minute", "multiplier": 15}
    assert [context["variant_id"] for context in json.loads(record.metric_documentation_json)] == [
        "sharpe.platform.v1",
        "sortino.platform.v1",
        "maximum_drawdown.platform.v1",
        "profit_factor.platform.v1",
    ]


def test_an_empty_recorded_documentation_context_is_replaced_by_the_source_catalogue() -> None:
    record = record_from_payload(lean_payload("run-3", metric_documentation_json=json.dumps([])))

    assert [context["producer"] for context in json.loads(record.metric_documentation_json)] == ["lean_native"] * 4


def test_lean_brokerage_stays_unknown_when_the_manifest_carried_none() -> None:
    record = record_from_payload(lean_payload("run-4", brokerage_policy=None))

    assert record.brokerage_policy is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"source": "strategy-lab"}, "Expected source"),
        ({"symbol": " "}, "symbol is required"),
        ({"trades": None}, "trades is required"),
        ({"lean_run_id": "x"}, "lean_run_id must be null"),
        ({"requested_engine": "lean"}, "requested_engine must match source"),
        ({"start_date": "2025-01-11"}, "must be <= end_date"),
        ({"start_date": "not-a-date"}, "YYYY-MM-DD"),
        ({"trades": [trade(entry_ms=0)]}, "trades[0].entry_ms_utc"),
        ({"trades": [{**trade(), "exit_ms_utc": None}]}, "trades[0].exit_ms_utc"),
    ],
)
def test_the_retired_400s_are_payload_errors(overrides: dict, message: str) -> None:
    with pytest.raises(RunPayloadError, match=message.replace("[", r"\[").replace("]", r"\]")):
        record_from_payload(engine_payload(**overrides))


def test_a_lean_payload_must_name_its_run_and_engine() -> None:
    with pytest.raises(RunPayloadError, match="lean_run_id is required"):
        record_from_payload({**lean_payload("run-5"), "lean_run_id": None})
    with pytest.raises(RunPayloadError, match="requested_engine must match"):
        record_from_payload(lean_payload("run-6", requested_engine="python"))


def test_a_missing_trade_quantity_defaults_to_one_share() -> None:
    record = record_from_payload(engine_payload(trades=[{**trade(), "quantity": None}]))

    assert record.trades[0].quantity == 1.0


def test_utc_date_iso_reads_the_calendar_date_of_a_utc_midnight_anchor() -> None:
    assert utc_date_iso(1_736_121_600_000) == "2025-01-06"  # 2025-01-06T00:00:00Z
    assert utc_date_iso(1_736_173_800_000) == "2025-01-06"  # 09:30 ET the same day
