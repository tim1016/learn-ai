"""Lossless worker transport using the saved-run's existing decimal codecs."""
from __future__ import annotations

from typing import Any

from app.options_backtest import run_json
from app.options_backtest.results import SESSION_OUTCOME_ADAPTER
from app.options_backtest.run import RunResult
from app.options_backtest.session_result import SessionResult
from app.options_backtest.setup import OptionsBacktestSetup


def result_to_wire(result: RunResult) -> dict[str, Any]:
    return {
        "setup": result.setup.model_dump(mode="json"),
        "summary": run_json.summary_to_json(result.summary),
        "slippage": run_json.slippage_to_json(result.slippage),
        "tolerance": run_json.tolerance_to_json(result.slippage_tolerance),
        "sessions": [{
            "session_open_ms": session.session_open_ms, "session_close_ms": session.session_close_ms,
            "cash_before_usd": str(session.cash_before_usd), "cash_after_usd": str(session.cash_after_usd),
            "outcome": SESSION_OUTCOME_ADAPTER.dump_python(session.outcome, mode="json"),
            "underlying_sha256": session.pins.underlying_sha256,
            "listing_sha256": session.pins.listing_sha256,
            "option_days": run_json.option_days_to_json(session.pins),
        } for session in result.sessions],
    }


def result_from_wire(wire: dict[str, Any]) -> RunResult:
    from decimal import Decimal

    return RunResult(
        OptionsBacktestSetup.model_validate(wire["setup"]),
        tuple(SessionResult(
            row["session_open_ms"], row["session_close_ms"], Decimal(row["cash_before_usd"]),
            SESSION_OUTCOME_ADAPTER.validate_python(row["outcome"]), Decimal(row["cash_after_usd"]),
            run_json.pins_from_json(row["underlying_sha256"], row["listing_sha256"], row["option_days"]),
        ) for row in wire["sessions"]),
        run_json.summary_from_json(wire["summary"]),
        run_json.slippage_from_json(wire["slippage"]),
        run_json.tolerance_from_json(wire["tolerance"]),
    )
