"""Read descriptive robustness from existing canonical evidence, without new statistics (#3401)."""
from __future__ import annotations

import asyncio
from typing import Any

from app.options_bots.golden_proof import setup_sha256
from app.research import options_candidate_evidence as retained
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search.policy import classify
from app.research.options_golden_search.projections import exact_data
from app.research.options_golden_search.protocol import materialize
from app.research.options_golden_search.stages import summary_metrics
from app.research.persistence.db import with_connection
from app.schemas.options_backtest_runs import OptionsBacktestCapitalEvidenceResponse
from app.schemas.options_research_lineage import OptionsLineageFinalist
from app.schemas.options_robustness import (
    OptionsCandidateRobustness,
    OptionsNeighbor,
    OptionsNeighborPage,
    OptionsRobustnessMetrics,
)


def project(evidence: retained.RetainedOptionsEvidence, row: dict[str, Any]) -> OptionsCandidateRobustness:
    metrics, protocol = evidence.result.metrics, evidence.protocol
    digest = setup_sha256(evidence.setup)
    final = next((c for c in row.get("final_results", []) if c["candidate_hash"] == digest), None) if digest in row.get("finalists", []) else None
    return OptionsCandidateRobustness(
        study_id=evidence.study_id, candidate_hash=digest, evidence_identity=evidence.evidence_identity,
        evidence_blob_sha256=evidence.evidence_blob_sha256, scope=evidence.scope,
        start_ms=evidence.window.start_ms, end_ms=evidence.window.end_ms,
        observed_sessions=len(evidence.result.sessions), scheduled_sessions=len(evidence.window.session_ms),
        complete=len(evidence.result.sessions) == len(evidence.window.session_ms) and not metrics.technical_failures,
        metrics=OptionsRobustnessMetrics.model_validate(exact_data(metrics)),
        capital=OptionsBacktestCapitalEvidenceResponse.from_evidence(metrics.capital_evidence) if metrics.capital_evidence is not None else None,
        cost_scenarios=[{**scenario.model_dump(mode="json"), "mandatory": protocol.policy.stress_gate} for scenario in protocol.stress],
        final_window={"key": protocol.final.key, "start_ms": protocol.final.start_ms, "end_ms": protocol.final.end_ms,
            "scheduled_sessions": len(protocol.final.session_ms), "activity_floor": protocol.final.activity_floor},
        exposed_final_outcome=OptionsLineageFinalist(candidate_hash=digest, state="retained", verdict=final["verdict"],
            gate_outcomes=final.get("gate_outcomes", []), evidence=final.get("evidence", [])) if final else None,
    )


async def read(study_id: str, digest: str, scope: retained.Scope) -> OptionsCandidateRobustness:
    row = await with_connection(repo.get, study_id)
    _, _, _, key = retained.resolve(row, digest, scope)
    record = await with_connection(repo.get_evaluation, study_id, key)
    evidence = await asyncio.to_thread(retained.load_record, row, record, digest, scope)
    return project(evidence, row)


async def neighbors(study_id: str, digest: str, scope: retained.Scope, *, offset: int = 0, limit: int = 50) -> OptionsNeighborPage:
    """One declared dimension varies; scalar summaries never load every neighbor's trace."""
    row = await with_connection(repo.get, study_id)
    protocol, _, _, _ = retained.resolve(row, digest, scope)
    baseline = setup_sha256(protocol.setup)
    points = (row.get("research") or {}).get("selection", {}).get("points", {})
    selected = {} if digest == baseline else points[digest]
    admitted = {baseline, *points}
    items: list[OptionsNeighbor] = []
    keys: list[str | None] = []
    ordinal = 0
    for knob in protocol.search_knobs:
        for value in knob.values:
            if offset <= ordinal < offset + limit:
                dimension = knob.path.replace(".", " · ").replace("_", " ")
                try:
                    setup = materialize(protocol, {**selected, knob.path: value})
                except ValueError:
                    items.append(OptionsNeighbor(ordinal=ordinal, dimension=dimension,
                        label="Invalid with this candidate's remaining settings.", candidate_hash=None,
                        is_selected=False, state="invalid", verdict=None, net_profit_usd=None))
                    keys.append(None)
                else:
                    neighbor_hash = setup_sha256(setup)
                    key = retained.resolve(row, neighbor_hash, "development")[3] if neighbor_hash in admitted else None
                    items.append(OptionsNeighbor(ordinal=ordinal, dimension=dimension, label=retained.label(setup, neighbor_hash == baseline),
                        candidate_hash=neighbor_hash, is_selected=neighbor_hash == digest, state="untested", verdict=None, net_profit_usd=None))
                    keys.append(key)
            ordinal += 1
    summaries = await with_connection(repo.evaluation_summaries, study_id, [key for key in keys if key is not None]) if keys else {}
    for item, key in zip(items, keys, strict=True):
        summary = summaries.get(key) if key is not None else None
        if summary is not None:
            verdict = classify(summary_metrics(summary["metrics"]), protocol.policy, protocol.development, "development", complete=summary["complete"])
            item.state, item.verdict, item.net_profit_usd = "retained", verdict.status, summary["metrics"]["net_profit_usd"]
    return OptionsNeighborPage(study_id=study_id, protocol_hash=row["protocol_hash"], selected_candidate_hash=digest,
        start_ms=protocol.development.start_ms, end_ms=protocol.development.end_ms, objective=protocol.policy.rank_by,
        items=items, total=ordinal, offset=offset, limit=limit)
