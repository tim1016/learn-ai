"""Bounded views over exactly one retained Options evaluation; never replay or read the lake (#3397).

This module is outside the scientific source namespace so transport changes
cannot invalidate a frozen study's executable identity.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from app.options_backtest.describe import describe_setup, leg_sentences
from app.options_backtest.phrases import distance
from app.options_backtest.session_clock import full_session_clock
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_bots.golden_proof import setup_sha256
from app.research.golden_search.proof import BlobStore
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search.evaluator import EvaluationResult
from app.research.options_golden_search.policy import classify
from app.research.options_golden_search.projections import candidate
from app.research.options_golden_search.protocol import OptionsSearchProtocol, Window, materialize
from app.research.options_golden_search.stages import StudyEvaluator, summary_metrics
from app.research.persistence.db import with_connection
from app.research.runs.hashing import hash_payload
from app.schemas.options_candidate_evidence import (
    OptionsCandidateCoverage,
    OptionsCandidateEvidence,
    OptionsCandidateListItem,
    OptionsCandidatePage,
    OptionsCandidateSampling,
)
from app.schemas.options_golden_search import Candidate

Scope = Literal["development", "final"]
MAX_CURVE_POINTS = 2000


@dataclass(frozen=True)
class RetainedOptionsEvidence:
    study_id: str
    protocol: OptionsSearchProtocol
    setup: OptionsBacktestSetup
    window: Window
    scope: Scope
    result: EvaluationResult
    evidence_identity: str
    evidence_blob_sha256: str
    final_outcome: Candidate | None = None


def resolve(row: dict[str, Any], digest: str, scope: Scope) -> tuple[OptionsSearchProtocol, OptionsBacktestSetup, Window, str]:
    protocol = OptionsSearchProtocol.model_validate(row["protocol"])
    baseline = setup_sha256(protocol.setup)
    points = (row.get("research") or {}).get("selection", {}).get("points", {})
    admitted = {baseline, *points}
    if digest not in admitted:
        raise LookupError("This candidate was not admitted by the retained search.")
    if scope == "final" and (digest not in row.get("finalists", []) or not any(c["candidate_hash"] == digest for c in row.get("final_results", []))):
        raise LookupError("Final evidence is not exposed for this exact candidate.")
    setup = protocol.setup if digest == baseline else materialize(protocol, points[digest])
    if setup_sha256(setup) != digest:
        raise ValueError("Retained candidate settings do not match their identity.")
    window = protocol.final if scope == "final" else protocol.development
    identity = {"protocol": row["protocol_hash"], "setup": setup.model_dump(mode="json"),
        "window": window.model_dump(mode="json"), "scenario": "base", "snapshot": protocol.snapshot_id,
        "identities": protocol.identities}
    return protocol, setup, window, hash_payload(identity)


def label(setup: OptionsBacktestSetup, original: bool) -> str:
    legs = " / ".join(" ".join(leg_sentences(leg)) for leg in setup.legs)
    timing = f"Entry {distance(setup.entry)}; exit {distance(setup.exit)}."
    cutoff = f" No re-entry after {distance(setup.no_reentry_after)}." if setup.no_reentry_after else ""
    return ("Original: " if original else "Candidate: ") + timing + cutoff + " " + legs


async def page(study_id: str, *, offset: int = 0, limit: int = 50) -> OptionsCandidatePage:
    row = await with_connection(repo.get, study_id)
    protocol = OptionsSearchProtocol.model_validate(row["protocol"])
    baseline = setup_sha256(protocol.setup)
    points = (row.get("research") or {}).get("selection", {}).get("points", {})
    hashes = [baseline, *sorted(set(points) - {baseline})]
    selected = hashes[offset:offset + limit]
    resolved = [resolve(row, digest, "development") for digest in selected]
    summaries = await with_connection(repo.evaluation_summaries, study_id, [entry[3] for entry in resolved])
    items = []
    for digest, (_, setup, window, key) in zip(selected, resolved, strict=True):
        summary = summaries.get(key)
        verdict = classify(summary_metrics(summary["metrics"]), protocol.policy, window, "development", complete=summary["complete"]) if summary else None
        items.append(OptionsCandidateListItem(candidate_hash=digest, label=label(setup, digest == baseline),
            is_original=digest == baseline, state="retained" if summary else "uncomputed",
            verdict=verdict.status if verdict else None, net_profit_usd=summary["metrics"]["net_profit_usd"] if summary else None))
    return OptionsCandidatePage(items=items, total=len(hashes), offset=offset, limit=limit)


def load_record(row: dict[str, Any], recorded: dict[str, Any], digest: str, scope: Scope, *,
                blobs: BlobStore | None = None) -> RetainedOptionsEvidence:
    protocol, setup, window, key = resolve(row, digest, scope)
    if recorded["key"] != key or hash_payload(recorded["identity"]) != key or recorded.get("result") is None:
        raise LookupError("The exact retained evaluation is unavailable.")
    result = StudyEvaluator(row, lambda: None, lambda _done, _total: None, blobs=blobs).load(recorded["result"], window)
    final_outcome = Candidate.model_validate(next(c for c in row["final_results"] if c["candidate_hash"] == digest)) if scope == "final" else None
    return RetainedOptionsEvidence(row["id"], protocol, setup, window, scope, result, key, recorded["result"]["evidence_blob"], final_outcome)


def project(evidence: RetainedOptionsEvidence, *, session_offset: int = 0, session_limit: int = 50) -> OptionsCandidateEvidence:
    protocol, setup, window, scope, result = evidence.protocol, evidence.setup, evidence.window, evidence.scope, evidence.result
    digest = setup_sha256(setup)
    complete = len(result.sessions) == len(window.session_ms)
    verdict = classify(result.metrics, protocol.policy, window, scope, complete=complete)
    projected = candidate(result, verdict, digest, original=digest == setup_sha256(protocol.setup))
    if evidence.final_outcome is not None:
        # Final qualification includes the frozen stress outcomes (#3397).
        projected.update(evidence.final_outcome.model_dump(include={"verdict", "gate_outcomes", "evidence"}))
    projected["label"] = label(setup, projected["is_original"])
    description = describe_setup(setup, full_session=full_session_clock(as_of_ms=window.start_ms))
    # The legacy display subset omits unknown valuations. Restore explicit break
    # markers without changing its financial values or recomputing any gate.
    missing = {point.at_ms for session in result.sessions for point in session.observations if point.equity_usd is None}
    by_time = {point["timestamp_ms"]: point for point in projected["equity"]}
    by_time.update({at: {"timestamp_ms": at, "equity_usd": None, "drawdown_usd": None} for at in missing})
    curve_available = len(by_time) <= MAX_CURVE_POINTS
    projected["equity"] = [by_time[at] for at in sorted(by_time)] if curve_available else []
    projected["sessions"] = projected["sessions"][session_offset:session_offset + session_limit]
    return OptionsCandidateEvidence(
        study_id=evidence.study_id, candidate_hash=digest, evidence_identity=evidence.evidence_identity,
        evidence_blob_sha256=evidence.evidence_blob_sha256, scope=scope, start_ms=window.start_ms, end_ms=window.end_ms,
        setup=setup, settings_description=[*description.window, *(line for leg in description.legs for line in leg.sentences),
            *description.square_off, *description.costs, *description.notices], candidate=Candidate.model_validate(projected),
        coverage=OptionsCandidateCoverage(scheduled_sessions=len(window.session_ms), replayed_sessions=len(result.sessions),
            traded_sessions=result.metrics.completed_traded_sessions, required_held_marks=result.metrics.required_held_marks,
            missing_held_marks=result.metrics.missing_held_marks, complete=complete and not result.metrics.technical_failures),
        sampling=OptionsCandidateSampling(state="available" if curve_available else "unavailable",
            raw_observations=sum(len(s.observations) for s in result.sessions), displayed_observations=len(projected["equity"]),
            explanation="Retained display subset with every missing valuation preserved as a gap. Full retained evidence determines metrics."
                if curve_available else "This trace has too many gaps for the bounded display. Inspect individual session evidence."),
        session_offset=session_offset, session_limit=session_limit, session_total=len(result.sessions),
        unobserved_sessions=len(window.session_ms) - len(result.sessions),
    )


async def load(study_id: str, digest: str, scope: Scope) -> RetainedOptionsEvidence:
    row = await with_connection(repo.get, study_id)
    _, _, _, key = resolve(row, digest, scope)
    recorded = await with_connection(repo.get_evaluation, study_id, key)
    return await asyncio.to_thread(load_record, row, recorded, digest, scope)


async def read(study_id: str, digest: str, scope: Scope, *, session_offset: int = 0, session_limit: int = 50) -> OptionsCandidateEvidence:
    evidence = await load(study_id, digest, scope)
    return await asyncio.to_thread(project, evidence, session_offset=session_offset, session_limit=session_limit)


async def session(study_id: str, digest: str, scope: Scope, session_open_ms: int) -> dict[str, Any]:
    """The same admission fence applies to candidates beyond the old top-50 summary."""
    evidence = await load(study_id, digest, scope)
    if session_open_ms not in evidence.window.session_ms:
        raise LookupError("This session is outside the declared evidence scope.")
    from app.research.options_golden_search.projections import session_detail
    selected = next((s for s in evidence.result.sessions if s.session_open_ms == session_open_ms), None)
    if selected is None:
        raise LookupError("This scheduled session was not computed in the retained evaluation.")
    return session_detail(selected)
