"""Canonical persist payloads for the backtest-run suites — the shape every writer produces."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.utils.session_anchors import et_midnight_ms

ENTRY_MS = 1_736_173_800_000  # 2025-01-06 09:30 ET
EXIT_MS = 1_736_179_200_000  # 2025-01-06 11:00 ET


def trade(
    number: int = 1, *, entry_ms: int = ENTRY_MS, exit_ms: int = EXIT_MS, pnl: float = 20.0, synthetic: bool = False
) -> dict[str, Any]:
    return {
        "trade_number": number,
        "entry_ms_utc": entry_ms,
        "exit_ms_utc": exit_ms,
        "entry_price": 710.0,
        "exit_price": 712.0,
        "quantity": 10.0,
        "pnl": pnl,
        "signal_reason": "EndOfAlgorithm (synthetic exit)" if synthetic else "ema cross",
        "is_synthetic_exit": synthetic,
    }


def equity_curve(final_equity: float = 100_020.0) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "mark_to_market": {
            "cadence": "strategy_bar_close",
            "downsample": {
                "policy": "first_last+trade_marks+running_extrema+stride",
                "raw_points": 2,
                "kept_points": 2,
            },
            "points": [{"t": ENTRY_MS, "e": 100_000.0}, {"t": EXIT_MS, "e": final_equity}],
        },
        "realized": {
            "cadence": "trade_exit",
            "downsample": {
                "policy": "first_last+trade_marks+running_extrema+stride",
                "raw_points": 2,
                "kept_points": 2,
            },
            "points": [{"t": ENTRY_MS, "e": 100_000.0}, {"t": EXIT_MS, "e": final_equity}],
        },
    }


def data_policy(symbol: str = "SPY", *, fixture_id: str | None = None) -> dict[str, Any]:
    return {
        "source": "polygon",
        "symbol": symbol,
        "adjusted": False,
        "session": "regular",
        "input_bars": {"timespan": "minute", "multiplier": 1},
        "strategy_bars": {"timespan": "minute", "multiplier": 15},
        "timestamp_policy": "bar_close_ms_utc",
        "timezone": "America/New_York",
        "provider_kind": "fixture" if fixture_id else "live",
        "fixture_id": fixture_id,
        "fixture_sha256": "a" * 64 if fixture_id else None,
    }


def certificate_evidence_overrides(symbol: str, *, lean: bool) -> dict[str, Any]:
    """Certificate-grade receipts for persistence tests that expect a comparable pair."""
    run_verdict = {
        "verdict_version": 2,
        "status": "complete",
        "composite": 76,
        "grade": "A",
        "signal": "Paper-trade",
        "red_flags": [],
        "dimensions": [{"key": "return_quality", "score": 70, "sub_scores": []}],
        "missing_metrics": [],
        "missing_required_metrics": [],
        "available_required_metrics": 17,
        "required_metrics": 17,
        "normalized_weights": False,
        "parity_signature": {
            "contract_id": "readiness-core-v2",
            "absolute_tolerance": "0.000000000001",
            "status": "complete",
            "composite": 76,
            "grade": "A",
            "signal": "Paper-trade",
            "red_flags": [],
            "missing_required_metrics": [],
            "available_required_metrics": 17,
            "required_metrics": 17,
            "normalized_weights": False,
            "required_inputs": [],
        },
    }
    overrides: dict[str, Any] = {
        "run_verdict_json": json.dumps(run_verdict),
        "data_policy_json": json.dumps(data_policy(symbol, fixture_id="bar-store-v1-exact")),
    }
    if lean:
        overrides.update(
            {
                "fill_mode": "signal_bar_close",
                "lean_statistics": {
                    "namespaces": {
                        "status": "match",
                        "contract_id": "lean-native-statistics-commit",
                        "source_commit": "fixture-commit",
                        "absolute_tolerance": 0.0000500001,
                        "native_metric_count": 66,
                        "formatted_metric_count": 25,
                        "divergences": [],
                    }
                },
            }
        )
    return overrides


def engine_payload(symbol: str = "SPY", **overrides: Any) -> dict[str, Any]:
    """A complete engine-source payload: every column the row has is populated."""
    payload: dict[str, Any] = {
        "source": "engine",
        "lean_run_id": None,
        "requested_engine": "python",
        "parity_group_id": None,
        "strategy_name": "ema_crossover_signal",
        "program_version": "ema-crossover-signal/v1",
        "execution_config_json": json.dumps(
            {
                "compatibility_profile": "us-equity-raw-ibkr-v1",
                "warmup_from_date": None,
                "slippage_per_share": 0.0,
                "session_time_reference_ms": et_midnight_ms(date(2025, 1, 6)),
                "session_time_zone": "America/New_York",
                "session_entry_cutoff_ms": None,
                "force_flat_at_ms": None,
                "limit_penetration": 0.0,
            }
        ),
        "symbol": symbol,
        "parameters": {"symbol": symbol, "gap_bps": 0.0},
        "start_date": "2025-01-06",
        "end_date": "2025-01-10",
        "timespan": "minute",
        "fill_mode": "signal_bar_close",
        "duration_ms": 1234,
        "total_trades": 1,
        "winning_trades": 1,
        "losing_trades": 0,
        "win_rate": 1.0,
        "total_pnl": 20.0,
        "starting_cash": 100_000.0,
        "final_equity": 100_020.0,
        "total_fees": 0.0,
        "max_drawdown": 0.0257,
        "sharpe_ratio": 1.43,
        "sortino_ratio": 2.59,
        "profit_factor": 2.0,
        "commission_per_order": 0.0,
        "brokerage_policy": "algorithm_default",
        "data_policy_json": json.dumps(data_policy(symbol)),
        "lean_statistics": {
            "portfolio": {"sharpe_ratio": 1.54, "drawdown": 0.0191},
            "trade": {"profit_factor": 1.86},
            "runtime": {},
        },
        "lean_analysis_json": None,
        "run_verdict_json": json.dumps(
            {"verdict_version": 2, "status": "complete", "grade": "A", "signal": "Paper-trade"}
        ),
        "verdict_version": 2,
        "verdict_grade": "A",
        "verdict_signal": "Paper-trade",
        "equity_curve_json": json.dumps(equity_curve()),
        "validation_analytics_json": json.dumps(
            {
                "schema_version": 2,
                "computed_at_ms": EXIT_MS,
                "engine": "python",
                "analytics": {"horizons": [], "timing_cells": []},
            }
        ),
        "insight_summary_json": json.dumps({"total": 1}),
        "metric_documentation_json": json.dumps(
            [
                {
                    "metric_id": "sharpe",
                    "variant_id": "sharpe.platform.v1",
                    "producer": "platform",
                    "contract_id": "platform-sharpe-v1",
                }
            ]
        ),
        "trades": [trade()],
    }
    payload.update(overrides)
    return payload


def lean_payload(lean_run_id: str, symbol: str = "SPY", **overrides: Any) -> dict[str, Any]:
    """A LEAN sidecar payload as ``build_persist_payload`` shapes it: no headline KPIs of its own."""
    payload: dict[str, Any] = {
        "lean_run_id": lean_run_id,
        "source": "lean-sidecar",
        "requested_engine": "lean",
        "fill_mode": "lean-sidecar",
        "strategy_name": "ema_crossover",
        "program_version": "ema-crossover-signal/v1",
        "execution_config_json": json.dumps(
            {
                "compatibility_profile": "us-equity-raw-ibkr-v1",
                "warmup_from_date": None,
                "slippage_per_share": 0.0,
                "session_time_reference_ms": et_midnight_ms(date(2025, 1, 6)),
                "session_time_zone": "America/New_York",
                "session_entry_cutoff_ms": None,
                "force_flat_at_ms": None,
                "limit_penetration": 0.0,
            }
        ),
        "symbol": symbol,
        "parameters": {"symbol": symbol, "gap_bps": 0.0},
        "starting_cash": 100_000.0,
        "start_date": "2025-01-06",
        "end_date": "2025-01-10",
        "total_trades": 1,
        "winning_trades": 1,
        "losing_trades": 0,
        "total_pnl": 20.0,
        "total_fees": 1.0,
        "final_equity": 100_020.0,
        "win_rate": 1.0,
        "trades": [trade()],
        "lean_statistics": {
            "portfolio": {"drawdown": 0.0191, "sharpe_ratio": 1.54, "sortino_ratio": 1.0},
            "trade": {"profit_factor": 1.86},
            "runtime": {},
        },
        "metric_documentation_json": json.dumps([]),
        "lean_analysis_json": json.dumps([{"name": "n", "issue": "i", "solutions": ["s"]}]),
        "data_policy_json": json.dumps(data_policy(symbol)),
        "brokerage_policy": "interactive_brokers",
        "commission_per_order": 0.0,
        "equity_curve_json": json.dumps(equity_curve()),
        "validation_analytics_json": None,
        "parity_group_id": None,
        "run_verdict_json": json.dumps({"verdict_version": 2, "status": "complete", "grade": "B", "signal": "Iterate"}),
        "verdict_version": 2,
        "verdict_grade": "B",
        "verdict_signal": "Iterate",
    }
    payload.update(overrides)
    return payload
