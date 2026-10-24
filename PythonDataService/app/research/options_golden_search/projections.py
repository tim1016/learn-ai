"""Server-authored Options study evidence views; display projections never decide gates (#3332).

The one gate written here, ``FINAL_BATCH_GATE``, only marks a final batch as unfinished (#3340).
"""
from __future__ import annotations

from dataclasses import asdict, fields
from decimal import Decimal, localcontext
from typing import Any

from app.options_backtest import fills
from app.research.options_golden_search.evaluator import EvaluationResult
from app.research.options_golden_search.marks import MARK_RULE_V1, value_session
from app.research.options_golden_search.metrics import OMIT_AT_DEFAULT, QualificationMetrics
from app.research.options_golden_search.policy import Metrics, Verdict


def gates(verdict: Verdict, evidence_scope: str | None = None) -> list[dict[str, Any]]:
    return [{"gate_id": f"{evidence_scope or out.scope}:{out.rule}", "status": out.status,"actual":out.actual,
        "required":out.threshold,"overridable":out.overridable,"reason":out.reason or ("Meets the locked rule." if out.status=="meets_rules" else "Does not meet the locked rule.")}
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
        data = asdict(value)
        for item in fields(value):
            # A field added after results were retained is absent while it holds its default, so old hashes hold.
            if item.metadata.get(OMIT_AT_DEFAULT) and data[item.name] == item.default:
                del data[item.name]
        return exact_data(data)
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
            for point in value_session(session.observations, result.mark_rule).observations:
                if point.equity_usd is not None:
                    peak = max(peak,point.equity_usd)
                    equity.append({"timestamp_ms":point.at_ms,"equity_usd":str(point.equity_usd),"drawdown_usd":str(peak-point.equity_usd)})
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
        "drawdown_fraction":str(m.max_drawdown_fraction),"drawdown_percent":_drawdown_percent(m.max_drawdown_fraction),"sharpe":m.sharpe,
        "completed_sessions":m.completed_traded_sessions,"recovery":("Still recovering: at least " if m.ongoing_recovery else "Longest underwater spell: ")+str(m.recoveries[-1].below_peak_sessions if m.ongoing_recovery and m.recoveries else m.max_recovery_sessions)+" sessions",
        "gate_outcomes":gates(verdict),"evidence":[{"label":"Display curve","value":f"{len(equity)} of {raw_points} minute observations; full retained trace determines every metric."}, *[{"label":name.replace("_"," "),"value":str(value)} for name,value in exact_data(m).items() if not isinstance(value,list | dict)]],
        "monthly":[{"month_ms":month.first_session_open_ms,"net_profit_usd":str(month.net_profit_usd),"completed_sessions":month.completed_sessions} for month in m.monthly],
        "equity":equity,"sessions":[session_summary(session) for session in result.sessions]}


FINAL_BATCH_GATE = "final:batch"


def final_scenario(result: EvaluationResult, verdict: Verdict, digest: str, scenario: str) -> dict[str, Any]:
    return {"candidate_hash": digest, "scenario": scenario, "verdict": verdict.status,
        "net_profit_usd": str(result.metrics.net_profit_usd), "gate_outcomes": gates(verdict, "final:" + scenario)}


def pending_final_candidate(result: EvaluationResult, verdict: Verdict, digest: str, *, original: bool) -> dict[str, Any]:
    """A committed final base run shown before its mandatory batch finishes; it never meets the rules."""
    view = candidate(result, verdict, digest, original=original)
    view["gate_outcomes"].append({"gate_id": FINAL_BATCH_GATE, "status": "not_enough_evidence",
        "actual": None, "required": "every frozen final candidate and cost scenario", "overridable": False,
        "reason": "This base run completed. The mandatory final batch has not finished."})
    if view["verdict"] == "meets_rules":
        view["verdict"] = "not_enough_evidence"
    return view


def _drawdown_percent(fraction: Decimal) -> str:
    with localcontext(fills.EXACT):
        return str(fraction * 100)


_MARK_RULE_ROWS = ("mark_rule", "carried_held_marks", "carried_share_of_required_held_marks", "longest_carried_run")


def _mark_rule_rows(metrics: Any) -> list[dict[str, str]]:
    """The mark-rule disclosure of a result valued under a rule after v1; a v1 result has none to show (ADR 0080)."""
    if getattr(metrics, "mark_rule", MARK_RULE_V1) == MARK_RULE_V1:
        return []
    return [{"label": name.replace("_", " "), "value": str(getattr(metrics, name))} for name in _MARK_RULE_ROWS]


def rejected_candidate(metrics: Metrics, verdict: Verdict, digest: str) -> dict[str, Any]:
    """Diagnostic scalar view; full traces remain retained without loading them for this table."""
    return {"candidate_hash": digest, "label": "Candidate " + digest[:8], "is_original": False,
        "verdict": verdict.status, "net_profit_usd": str(metrics.net_profit_usd), "drawdown_usd": None,
        "drawdown_fraction": str(metrics.max_drawdown_fraction), "drawdown_percent": _drawdown_percent(metrics.max_drawdown_fraction),
        "sharpe": metrics.sharpe, "completed_sessions": metrics.completed_traded_sessions,
        "recovery": "Recovery ongoing" if metrics.ongoing_recovery else f"Longest underwater spell: {metrics.max_recovery_sessions} sessions",
        "gate_outcomes": gates(verdict), "evidence": _mark_rule_rows(metrics), "monthly": [], "equity": [], "sessions": []}


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


def approval_reviews(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Project the same complete failure set the approval command must acknowledge."""
    from app.research.options_golden_search.approval import approval_failures

    reviews = {}
    for candidate in row.get("final_results", []):
        digest = candidate["candidate_hash"]
        try:
            failures = approval_failures(row, digest)
        except ValueError as error:
            reviews[digest] = {"failures": [], "blocked_reason": str(error)}
            continue
        technical = any(gate["status"] == "could_not_evaluate" or not gate.get("overridable", True) for gate in failures)
        reviews[digest] = {"failures": failures,
            "blocked_reason": "Technical invalidity cannot receive an owner exception." if technical else None}
    return reviews
