"""Golden Search read models: the StudySummary / StudyDetail dicts and every stage view the workbench renders (#2696).

Formula (candidate run detail): from a detail run's session-close daily
equity ``E_t`` and starting capital ``C``: cumulative return
``E_t / C − 1``; drawdown ``E_t / max_{s<=t} E_s − 1``; performance by month
as ``evidence.monthly_results``. Everything else here re-shapes stored
stage output and attaches the closed copy of ``guidance``; no number is
computed in the browser.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Compare
  candidates and weaknesses"; the series math is
  ``app/research/golden_search/evidence.py``.
Canonical implementation: this file (shapes); evidence.py (series math).
Validated against: tests/research/golden_search/test_views.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.research.golden_search.actions import permitted
from app.research.golden_search.declarations import SearchDeclaration, declaration_for, knob_values, scalar
from app.research.golden_search.evidence import CANDIDATE_LABELS, drawdown_series, monthly_results
from app.research.golden_search.guidance import (
    candidate_flags,
    candidate_guidance,
    costs_sentence,
    fixed_sentence,
    knob_stop_explanation,
    params_sentence,
    study_guidance,
    validation_explanation,
)
from app.research.golden_search.models import CommandName, EvaluationRecord, StudyRow
from app.research.golden_search.protocol import GoldenSearchProtocol
from app.research.golden_search.repository import metrics_of
from app.research.golden_search.selection import Metrics, ineligibility
from app.research.golden_search.zoom import ProcedureResult

NOT_EVALUATED = "NOT_EVALUATED"


def _stage_incomplete(results: Mapping[str, Any]) -> bool:
    return any(
        bool((results.get(key) or {}).get("incomplete")) or bool((results.get(key) or {}).get("pair_maps_incomplete"))
        for key in ("search", "recent", "validation", "evidence")
    )


def study_summary(row: StudyRow, *, presented: str) -> dict[str, Any]:
    exam = row.results.get("exam") or {}
    qualification = row.results.get("qualification") or {}
    return {
        "id": row.id,
        "parent_study_id": row.parent_study_id,
        "strategy_key": row.strategy_key,
        "symbol": row.symbol,
        "state": row.state,
        "presented_status": presented,
        "revision": row.revision,
        "created_at_ms": row.created_at_ms,
        "updated_at_ms": row.updated_at_ms,
        "protocol_hash": row.protocol_hash,
        "method": row.protocol["method"],
        "consumed_evaluations": row.consumed_evaluations,
        "budget_cap": row.budget_cap,
        "cache_hits": row.cache_hits,
        "invalid_points": row.invalid_points,
        "incomplete": row.incomplete or _stage_incomplete(row.results),
        "failure_reason": row.failure_reason,
        "hidden": row.hidden,
        "exposure_claim": exam.get("claim"),
        "exam_outcome": exam.get("outcome"),
        "qualification_id": qualification.get("qualification_id") if qualification.get("status") == "ready" else None,
    }


def study_detail(
    row: StudyRow,
    *,
    presented: str,
    refusals: Mapping[CommandName, str | None],
    progress: Mapping[str, Any] | None,
    dispatch: Mapping[str, Any] | None,
) -> dict[str, Any]:
    protocol = GoldenSearchProtocol.from_dict(row.protocol)
    declaration = declaration_for(row.strategy_key)
    exam = row.results.get("exam") or {}
    return {
        **study_summary(row, presented=presented),
        "protocol": dict(row.protocol),
        "receipt": receipt_summary(row),
        "permitted_actions": permitted(dict(refusals)),
        "action_refusals": {command: reason for command, reason in refusals.items() if reason is not None},
        "guidance": study_guidance(
            state=row.state,
            presented_status=presented,
            exam_outcome=exam.get("outcome"),
            claim=exam.get("claim"),
            exposure_state=exam.get("exposure_state"),
            exam_locked=row.exam_locked,
            failure_reason=row.failure_reason,
        ),
        "progress": None if progress is None else dict(progress),
        "dispatch": None if dispatch is None else dict(dispatch),
        "results": results_view(row, protocol, declaration),
        "decision": decision_view(row.decision),
        "candidate_key": row.candidate_key,
        "exam_locked": row.exam_locked,
        "scope": scope_view(row, protocol),
    }


def receipt_summary(row: StudyRow) -> dict[str, Any]:
    receipt = row.receipt
    intervals = receipt["intervals"]
    identity = receipt["code_identity"]
    return {
        "data_start_ms": intervals["data_start_ms"],
        "development_start_ms": intervals["development_start_ms"],
        "development_end_ms": intervals["development_end_ms"],
        "final_start_ms": intervals["final_start_ms"],
        "final_end_ms": intervals["final_end_ms"],
        "run_up_sessions": receipt["run_up"]["run_up_sessions"],
        "snapshot_digest": receipt["data_snapshot_digest"],
        "code": {"git_revision": identity["git_revision"], "tree_state": identity["tree_state"]},
        "program_version": receipt["program_version"],
    }


def decision_view(decision: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if decision is None or "kind" not in decision:
        return None
    return {"kind": decision["kind"], "note": decision.get("note", ""), "at_ms": decision["at_ms"]}


def scope_view(row: StudyRow, protocol: GoldenSearchProtocol) -> dict[str, Any]:
    """The comparison scope every step shows above its numbers."""
    return {
        "development_label_start_ms": protocol.development_start_ms,
        "development_end_ms": protocol.development_end_ms,
        "final_start_ms": protocol.final_start_ms,
        "final_end_ms": protocol.final_end_ms,
        "final_state": "opened_once" if row.exam_locked else "locked",
        "capital": protocol.execution.initial_cash,
        "costs_sentence": costs_sentence(protocol.execution),
    }


def results_view(row: StudyRow, protocol: GoldenSearchProtocol, declaration: SearchDeclaration | None) -> dict[str, Any]:
    results = row.results
    search = results.get("search")
    recent = results.get("recent")
    pair_maps = list(search.get("pair_maps", [])) if search is not None else []
    return {
        "search": None if search is None or declaration is None else procedure_view(search, protocol, declaration, pair_maps=pair_maps),
        "recent": None if recent is None or declaration is None else procedure_view(recent, protocol, declaration),
        "validation": None if "validation" not in results else validation_view(results["validation"]),
        "evidence": None
        if "evidence" not in results or declaration is None
        else evidence_view(results["evidence"], protocol, declaration, pair_maps=pair_maps),
        "exam": None if "exam" not in results else exam_view(results["exam"]),
        "qualification": qualification_view(row),
    }


# ── Procedures ───────────────────────────────────────────────────────────


def _knob_stop_reason(result: ProcedureResult, knob: str) -> str:
    if result.stop_reason in ("budget", "no_eligible") or result.method == "grid":
        return result.stop_reason
    rounds = [round_ for round_ in result.rounds if round_.knob == knob]
    if rounds and rounds[-1].quantization_limit:
        return "quantization_limit"
    return "pass_limit" if result.stop_reason == "pass_limit" else "no_improvement"


def knob_summary(result: ProcedureResult, protocol: GoldenSearchProtocol, declaration: SearchDeclaration) -> list[dict[str, Any]]:
    """Each searched knob: where it started, the value the procedure kept, and why its search stopped."""
    start = knob_values(declaration, protocol.seed)
    kept = knob_values(declaration, result.winner)
    rows = []
    for plan in protocol.search_knobs:
        knob = declaration.knob(plan.name)
        reason = _knob_stop_reason(result, plan.name)
        rows.append(
            {
                "knob": knob.name,
                "label": knob.label,
                "unit": knob.unit,
                "start_value": scalar(knob, start[knob.name]),
                "retained_value": scalar(knob, kept[knob.name]),
                "moved": kept[knob.name] != start[knob.name],
                "stop_reason": reason,
                "stop_explanation": knob_stop_explanation(result.method, reason),
            }
        )
    return rows


def passes_completed(result: ProcedureResult) -> int:
    """Whole Zoom passes behind the result; a pass the budget interrupted does not count (Grid: one pass, or none)."""
    if result.method == "grid":
        return 0 if result.stop_reason == "budget" else 1
    passes = len({round_.pass_index for round_ in result.rounds})
    return max(0, passes - 1) if result.stop_reason == "budget" else passes


def procedure_view(
    record: Mapping[str, Any],
    protocol: GoldenSearchProtocol,
    declaration: SearchDeclaration,
    *,
    pair_maps: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    stored = record["procedure"]
    result = ProcedureResult.from_dict(stored)
    view: dict[str, Any] = {
        "winner": dict(stored["winner"]),
        "winner_hash": stored["winner_hash"],
        "winner_metrics": stored["winner_metrics"],
        "stop_reason": stored["stop_reason"],
        "stop_explanation": result.stop_explanation,
        "rounds": list(stored["rounds"]),
        "edge_hits": list(stored["edge_hits"]),
        "evaluations": len(stored["evaluated_hashes"]),
        "incomplete": bool(record["incomplete"]),
        "knob_summary": knob_summary(result, protocol, declaration),
        "counts": dict(record["counts"]),
        "passes_completed": passes_completed(result),
        "window": dict(record["window"]),
    }
    if pair_maps is not None:
        view["pair_maps"] = [dict(item) for item in pair_maps]
        view["pair_maps_incomplete"] = bool(record.get("pair_maps_incomplete"))
    return view


# ── Validation ───────────────────────────────────────────────────────────


def validation_view(stored: Mapping[str, Any]) -> dict[str, Any]:
    folds = [
        {
            "fold_index": fold["fold_index"],
            "train_start_ms": fold["train_start_ms"],
            "train_end_ms": fold["train_end_ms"],
            "test_start_ms": fold["test_start_ms"],
            "test_end_ms": fold["test_end_ms"],
            "status": fold["status"],
            "winner": fold["winner"],
            "winner_hash": fold["winner_hash"],
            "train_metrics": fold["train_metrics"],
            "test_metrics": fold["test_metrics"],
            "incumbent_test_metrics": fold["incumbent_test_metrics"],
            "failure_reason": fold["failure_reason"],
            "failure_code": fold["failure_code"],
        }
        for fold in stored["folds"]
    ]
    verdict = stored.get("verdict")
    pills = (
        {
            "judged": f"{verdict['defined_folds']} of {verdict['successful_folds']} folds judged",
            "test_trades": verdict["oos_trade_count"],
            "median_retention": verdict["study_retention"],
        }
        if verdict is not None
        else {"judged": f"0 of {len(folds)} folds judged", "test_trades": 0, "median_retention": None}
    )
    return {
        "folds": folds,
        "verdict": verdict,
        "linked": list(stored.get("linked", [])),
        "incumbent_linked": list(stored.get("incumbent_linked", [])),
        "summary_pills": pills,
        "explanation": validation_explanation(None if verdict is None else verdict["based_on"], len(folds)),
        "incomplete": bool(stored.get("incomplete")),
    }


# ── Evidence ─────────────────────────────────────────────────────────────


def _losing(neighbors: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        row["status"] == "tested" and row["metrics"] is not None and (row["metrics"].get("net_profit") or 0.0) < 0
        for hood in neighbors
        for row in hood["rows"]
    )


def evidence_view(
    stored: Mapping[str, Any],
    protocol: GoldenSearchProtocol,
    declaration: SearchDeclaration,
    *,
    pair_maps: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    policy = protocol.policy
    candidates = stored["candidates"]
    incumbent_hash = next((item["point_hash"] for item in candidates if item["key"] == "incumbent"), None)
    findings: dict[str, list[str]] = {}
    for finding in stored.get("findings", []):
        if finding.get("candidate") is not None:
            findings.setdefault(finding["candidate"], []).append(finding["code"])
    views = []
    for item in candidates:
        metrics = None if item["development_metrics"] is None else Metrics.from_dict(item["development_metrics"])
        rule = NOT_EVALUATED if metrics is None else ineligibility(metrics, policy)
        same_as_incumbent = item["key"] != "incumbent" and item["point_hash"] == incumbent_hash
        codes = (["SAME_AS_INCUMBENT"] if same_as_incumbent else []) + findings.get(item["key"], [])
        views.append(
            {
                "key": item["key"],
                "label": CANDIDATE_LABELS[item["key"]],
                "point": dict(item["point"]),
                "point_hash": item["point_hash"],
                "same_as": [other["key"] for other in candidates if other is not item and other["point_hash"] == item["point_hash"]],
                "development_metrics": item["development_metrics"],
                "eligible": rule is None,
                "ineligibility": rule,
                "neighbors": list(item["neighbors"]),
                "stress": list(item["stress"]),
                "edge_hits": list(item["edge_hits"]),
                "guidance": candidate_guidance(
                    key=item["key"],
                    same_as_incumbent=same_as_incumbent,
                    ineligibility=rule,
                    losing_neighbors=_losing(item["neighbors"]),
                    neighbors_audited=bool(item["neighbors_audited"]),
                ),
                "flags": candidate_flags(
                    ineligibility=rule,
                    total_trades=None if metrics is None else metrics.total_trades,
                    min_trades=policy.min_trades,
                    drawdown_ceiling=policy.max_drawdown_ceiling,
                    finding_codes=codes,
                ),
                "params_sentence": params_sentence(declaration, item["point"]),
                "fixed_sentence": fixed_sentence(declaration, protocol.knobs),
                "exam_eligible": item["key"] != "incumbent" and not same_as_incumbent,
            }
        )
    window = stored["window"]
    return {
        "candidates": views,
        "pair_maps": [dict(item) for item in pair_maps],
        "recommendation": dict(stored["recommendation"]),
        "scope": {
            "window": dict(window),
            "capital": protocol.execution.initial_cash,
            "costs": {
                "fill_mode": protocol.execution.fill_mode,
                "commission_per_order": protocol.execution.commission_per_order,
                "slippage_per_share": protocol.execution.slippage_per_share,
            },
        },
        "incomplete": bool(stored.get("incomplete")),
    }


# ── Exam and qualification ───────────────────────────────────────────────


def exam_view(stored: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_key": stored["candidate_key"],
        "candidate_point": dict(stored["candidate_point"]),
        "window": dict(stored["window"]),
        "claim": stored["claim"],
        "exposure_state": stored["exposure_state"],
        "outcome": stored["outcome"],
        "checks": list(stored["checks"]),
        "retention": stored["retention"],
        "candidate_metrics": stored["candidate_metrics"],
        "incumbent_metrics": stored["incumbent_metrics"],
    }


def qualification_view(row: StudyRow) -> dict[str, Any] | None:
    if row.state == "qualification_pending":
        return {"status": "pending", "qualification_id": None, "failure_reason": None, "deploy": None}
    stored = row.results.get("qualification")
    if stored is None:
        return None
    deploy = None
    if stored["status"] == "ready":
        point = row.results["exam"]["candidate_point"]
        deploy = {
            "program_key": row.strategy_key,
            "symbol": row.symbol,
            "parameters": {name: value for name, value in point.items() if name != "symbol"},
            "program_version": row.receipt["program_version"],
        }
    return {
        "status": stored["status"],
        "qualification_id": stored["qualification_id"],
        "failure_reason": stored["failure_reason"],
        "deploy": deploy,
    }


# ── Candidate detail ─────────────────────────────────────────────────────


def run_detail(record: EvaluationRecord) -> dict[str, Any]:
    """One detail run's metrics and its daily series."""
    window = {"start_ms": record.window_start_ms, "end_ms": record.window_end_ms}
    detail = record.detail_json
    if detail is None:
        return {"window": window, "metrics": metrics_of(record).as_dict(), "cumulative_return": [], "daily_equity": [], "drawdown": [], "monthly": [], "trades": []}
    capital = float(detail["initial_cash"])
    daily = [(int(ms), float(equity)) for ms, equity in detail["daily_equity"]]
    trades = list(detail["trades"])
    return {
        "window": window,
        "metrics": metrics_of(record).as_dict(),
        "cumulative_return": [{"ms": ms, "value": equity / capital - 1.0} for ms, equity in daily],
        "daily_equity": [{"ms": ms, "equity": equity} for ms, equity in daily],
        "drawdown": [{"ms": ms, "drawdown": value} for ms, value in drawdown_series(daily)],
        "monthly": [month.as_dict() for month in monthly_results(daily, capital, [int(trade["exit_ms"]) for trade in trades])],
        "trades": trades,
    }


def candidate_detail(
    key: str, point: Mapping[str, Any], *, development: EvaluationRecord | None, exam: EvaluationRecord | None
) -> dict[str, Any]:
    return {
        "candidate_key": key,
        "point": dict(point),
        "development": None if development is None else run_detail(development),
        "exam": None if exam is None else run_detail(exam),
    }
