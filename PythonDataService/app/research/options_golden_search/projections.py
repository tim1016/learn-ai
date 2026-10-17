"""Server-authored Options study evidence views; display projections never decide gates (#3332)."""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, localcontext
from typing import Any

from app.options_backtest import fills
from app.research.options_golden_search.evaluator import EvaluationResult
from app.research.options_golden_search.metrics import QualificationMetrics
from app.research.options_golden_search.policy import Verdict


def gates(verdict: Verdict, evidence_scope: str | None = None) -> list[dict[str, Any]]:
    return [{"gate_id": f"{evidence_scope or out.scope}:{out.rule}", "status": out.status,"actual":out.actual,
        "required":out.threshold,"reason":out.reason or ("Meets the locked rule." if out.status=="meets_rules" else "Does not meet the locked rule.")}
        for out in verdict.outcomes]


def exact_data(value: Any) -> Any:
    """JSON-safe exact evidence, including typed statistical floats."""
    from enum import Enum

    from pydantic import BaseModel
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if hasattr(value, "__dataclass_fields__"):
        return exact_data(asdict(value))
    if isinstance(value, dict):
        return {str(k):exact_data(v) for k,v in value.items()}
    if isinstance(value, list | tuple):
        return [exact_data(v) for v in value]
    return value


def candidate(result: EvaluationResult, verdict: Verdict, digest: str, *, original: bool) -> dict[str, Any]:
    m: QualificationMetrics = result.metrics
    peak = result.sessions[0].cash_before_usd if result.sessions else Decimal(0)
    equity = []
    with localcontext(fills.EXACT):
        for session in result.sessions:
            for point in session.observations:
                if point.equity_usd is not None:
                    peak = max(peak,point.equity_usd)
                    equity.append({"timestamp_ms":point.at_ms,"equity_usd":str(point.equity_usd),"drawdown_usd":str(peak-point.equity_usd)})
        percent = str(m.max_drawdown_fraction * 100)
    from app.engine.results.equity_downsample import EquityCurvePoint, build_equity_curve_envelope
    raw_points = len(equity)
    envelope = build_equity_curve_envelope(
        [EquityCurvePoint(t=point["timestamp_ms"], e=float(point["equity_usd"])) for point in equity],
        cadence="strategy_bar_close", max_points=1000,
        trade_timestamps={max(equity, key=lambda point: Decimal(point["drawdown_usd"]))["timestamp_ms"]} if equity else set(),
    )
    kept = {point["t"] for point in envelope["points"]}
    equity = [point for point in equity if point["timestamp_ms"] in kept]
    return {"candidate_hash":digest,"label":"Original saved setup" if original else "Candidate "+digest[:8],"is_original":original,
        "verdict":verdict.status,"net_profit_usd":str(m.net_profit_usd),"drawdown_usd":str(m.max_drawdown_usd),
        "drawdown_fraction":str(m.max_drawdown_fraction),"drawdown_percent":percent,"sharpe":m.sharpe,
        "completed_sessions":m.completed_traded_sessions,"recovery":("Still recovering: at least " if m.ongoing_recovery else "Longest underwater spell: ")+str(m.recoveries[-1].below_peak_sessions if m.ongoing_recovery and m.recoveries else m.max_recovery_sessions)+" sessions",
        "gate_outcomes":gates(verdict),"evidence":[{"label":"Display curve","value":f"{len(equity)} of {raw_points} minute observations; full retained trace determines every metric."}, *[{"label":name.replace("_"," "),"value":str(value)} for name,value in exact_data(m).items() if not isinstance(value,list | dict)]],
        "monthly":[{"month_ms":month.first_session_open_ms,"net_profit_usd":str(month.net_profit_usd),"completed_sessions":month.completed_sessions} for month in m.monthly],
        "equity":equity,"sessions":[session_summary(session) for session in result.sessions]}


def session_summary(session: Any) -> dict[str, Any]:
    return {"session_open_ms": session.session_open_ms,"net_profit_usd":str(session.outcome.pnl_usd),
        "status":session.outcome.status.value,"detail":f"{len(session.outcome.trades)} leg attempts; cash before {session.cash_before_usd}","attempts":[]}


def session_detail(session: Any) -> dict[str, Any]:
    result = session_summary(session)
    result["attempts"] = [{"leg_id":trade.leg_id,"attempt":trade.entry_number,"contract":trade.contract.occ_symbol,
        "side":trade.entry.side,"contracts":trade.contracts,"entry_ms":trade.entry.bar_close_ms,"exit_ms":trade.exit.bar_close_ms,
        "entry_price_usd":str(trade.entry.filled),"exit_price_usd":str(trade.exit_price),
        "entry_fee_usd":str(trade.entry_fee_usd),"exit_fee_usd":str(trade.exit_fee_usd),
        "net_profit_usd":str(trade.pnl_usd),"exit_reason":trade.exit_reason.value} for trade in session.outcome.trades]
    return result
