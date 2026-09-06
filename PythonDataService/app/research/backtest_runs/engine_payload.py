"""The engine backtest's persist payload — a pure function of the engine response.

This is the row-shaping half of what the engine router's study auto-save
used to do before POSTing to .NET: per-trade dollar P&L under the fee
policy the engine actually executed, the strict dual-curve equity report,
the frozen validation-analytics envelope, and the headline statistics taken
from the engine's canonical ``statistics`` (never from the LEAN-parity
projection first — before verdict v2 that preference displayed one Sharpe
while readiness scored another). It stays pure so those rules are tested
directly on the payload; the write is :mod:`app.research.backtest_runs.service`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from app.engine.results.equity_downsample import (
    RealizedEquityTrade,
    build_realized_equity_envelope,
    build_run_equity_envelope,
    from_engine_curve,
)
from app.lean_sidecar.config import COMPATIBILITY_PROFILE_US_EQUITY_RAW_IBKR_V1
from app.research.backtest_runs.records import RunPayloadError
from app.research.documentation.analytical_metric_catalog import metric_documentation_context_for_source
from app.research.parity.ibkr_commission import IbkrEquityCommissionModel
from app.services.engine_validation_analytics import build_validation_analytics_envelope
from app.utils.timestamps import now_ms_utc

if TYPE_CHECKING:
    from app.routers.engine import EngineBacktestResponse, EngineTradeResponse

REALIZED_EQUITY_ATOL = 1e-6


def persisted_trade_net_pnl(
    *,
    trade: EngineTradeResponse,
    commission_per_order: float,
    compatibility_profile: Literal["us-equity-raw-ibkr-v1"] | None,
) -> float:
    """Return persisted round-trip dollar P&L under the executed fee policy.

    Formula: net P&L = quantity * (exit_fill - entry_fill) - entry_fee - exit_fee.
    Reference: the Strategy Lab realized-equity accounting contract in
      ``docs/references/realized-equity-staircase-v1.md``; compatibility fees
      use the QuantConnect IBKR equity tier cited by the canonical model.
    Canonical implementation: gross trade P&L is carried by ``EngineTradeResponse``;
      IBKR fees delegate to ``app.research.parity.ibkr_commission.IbkrEquityCommissionModel``.
    Validated against:
      ``tests/integration/test_engine_persistence_quantity_pnl.py::test_engine_run_payload_uses_executed_ibkr_fees_for_compatibility_runs``.
    """
    gross_pnl = Decimal(str(trade.pnl_pts)) * Decimal(trade.quantity)
    if compatibility_profile == COMPATIBILITY_PROFILE_US_EQUITY_RAW_IBKR_V1:
        fee_model = IbkrEquityCommissionModel()
        fees = fee_model.fee(
            quantity=trade.quantity,
            fill_price=Decimal(str(trade.entry_price)),
        ) + fee_model.fee(
            quantity=trade.quantity,
            fill_price=Decimal(str(trade.exit_price)),
        )
    else:
        fees = Decimal("2") * Decimal(str(commission_per_order))
    return float(gross_pnl - fees)


def build_engine_run_payload(
    *,
    response: EngineBacktestResponse,
    symbol: str,
    start_date: str,
    end_date: str,
    resolution: str,
    parameters: Mapping[str, Any],
    duration_ms: int,
    commission_per_order: float = 0.0,
    compatibility_profile: Literal["us-equity-raw-ibkr-v1"] | None = None,
    requested_engine: Literal["python", "lean", "both"] = "python",
    parity_group_id: str | None = None,
) -> dict[str, Any]:
    """Shape a completed engine response into the canonical persist payload.

    Raises :class:`RunPayloadError` when the response cannot back a strict
    run report — no producer timestamps for the flat realized curve, or a
    realized ledger that does not reconcile with the headline final equity
    within ``atol=1e-6, rtol=0``. The caller treats that as a persistence
    failure; the completed backtest response is unaffected.
    """
    stats = response.statistics
    lp = response.lean_statistics.portfolio if response.lean_statistics else None
    lt = response.lean_statistics.trade if response.lean_statistics else None

    def canonical_stat(key: str, fallback: float | int | None) -> float | int | None:
        return stats.get(key, fallback)

    try:
        trades = [
            {
                "trade_number": t.trade_number,
                "entry_ms_utc": t.entry_time,
                "exit_ms_utc": t.exit_time,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                # Dollar P&L net of the fee policy the engine actually ran.
                # Compatibility runs pin the IBKR tier even when the legacy
                # flat-fee control is zero.
                "pnl": persisted_trade_net_pnl(
                    trade=t,
                    commission_per_order=commission_per_order,
                    compatibility_profile=compatibility_profile,
                ),
                "signal_reason": t.signal_reason,
                "is_synthetic_exit": t.is_synthetic_exit,
            }
            for t in response.trades
        ]
        equity_curve = _strict_equity_report(response, trades)
    except (KeyError, TypeError, ValueError) as exc:
        raise RunPayloadError(f"engine run report preparation failed: {exc}") from exc

    return {
        "source": "engine",
        "lean_run_id": None,
        "requested_engine": requested_engine,
        "parity_group_id": parity_group_id,
        "strategy_name": response.strategy_name,
        "symbol": symbol,
        "parameters": dict(parameters),
        "start_date": start_date,
        "end_date": end_date,
        "timespan": resolution,
        "fill_mode": response.fill_mode,
        "duration_ms": duration_ms,
        "total_trades": response.total_trades,
        "winning_trades": response.winning_trades,
        "losing_trades": response.losing_trades,
        "win_rate": response.win_rate,
        "total_pnl": response.net_profit,
        "starting_cash": response.initial_cash,
        "final_equity": response.final_equity,
        "total_fees": response.total_fees,
        "max_drawdown": canonical_stat("max_drawdown_pct", lp.drawdown if lp else None),
        "sharpe_ratio": canonical_stat("sharpe_ratio", lp.sharpe_ratio if lp else None),
        "sortino_ratio": canonical_stat("sortino_ratio", lp.sortino_ratio if lp else None),
        "profit_factor": canonical_stat("profit_factor", lt.profit_factor if lt else None),
        "lean_statistics": response.lean_statistics.model_dump(mode="json") if response.lean_statistics else None,
        "data_policy_json": response.data_policy.model_dump_json() if response.data_policy else None,
        "commission_per_order": commission_per_order,
        # The Python engine models no brokerage; the LEAN-side convention is
        # recorded so the compare view's soft-match treats it correctly.
        "brokerage_policy": "algorithm_default",
        "run_verdict_json": response.run_verdict.model_dump_json() if response.run_verdict else None,
        "verdict_version": response.run_verdict.verdict_version if response.run_verdict else None,
        "verdict_grade": response.run_verdict.grade if response.run_verdict else None,
        "verdict_signal": response.run_verdict.signal if response.run_verdict else None,
        "equity_curve_json": json.dumps(equity_curve),
        # Frozen at run time — the persisted run is the single render source
        # for the workbench and the run report. Null when the analytics
        # computation rejected the run's output (honest missing).
        "validation_analytics_json": (
            json.dumps(
                build_validation_analytics_envelope(
                    response.validation_analytics,
                    engine="python",
                    computed_at_ms=now_ms_utc(),
                )
            )
            if response.validation_analytics
            else None
        ),
        "insight_summary_json": json.dumps(response.insight_summary) if response.insight_summary else None,
        "metric_documentation_json": json.dumps(metric_documentation_context_for_source("engine"), sort_keys=True),
        "trades": trades,
    }


def _strict_equity_report(response: EngineBacktestResponse, trades: list[dict[str, Any]]) -> dict[str, Any]:
    mark_to_market = from_engine_curve(
        response.equity_curve,
        trade_timestamps={t["entry_ms_utc"] for t in trades} | {t["exit_ms_utc"] for t in trades},
    )
    mark_points = [int(point["t"]) for point in mark_to_market["points"]]
    # A zero-trade run still has consolidated chart bars. They provide the
    # producer-authored session bounds for its required flat realized curve;
    # never synthesize a timestamp from an ISO date or UTC midnight here.
    bar_points = [int(bar["t"]) for bar in response.chart_bars if bar.get("t") is not None]
    candidates = [*mark_points, *bar_points, *(int(t["entry_ms_utc"]) for t in trades), *(int(t["exit_ms_utc"]) for t in trades)]
    if not candidates:
        raise ValueError("Engine run has no timestamps for the strict run report")
    realized = build_realized_equity_envelope(
        initial_cash=Decimal(str(response.initial_cash)),
        trades=[
            RealizedEquityTrade(
                trade_number=index + 1,
                exit_ms_utc=int(trade["exit_ms_utc"]),
                pnl=Decimal(str(trade["pnl"])),
            )
            for index, trade in enumerate(trades)
        ],
        start_ms_utc=min(candidates),
        end_ms_utc=max(candidates),
    )
    realized_final_equity = realized["points"][-1]["e"]
    if not math.isclose(realized_final_equity, response.final_equity, rel_tol=0.0, abs_tol=REALIZED_EQUITY_ATOL):
        raise ValueError(
            "realized equity does not reconcile with the completed engine final equity within atol=1e-6, rtol=0"
        )
    return build_run_equity_envelope(mark_to_market=mark_to_market, realized=realized)
