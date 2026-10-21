"""Explain a frozen Options selection procedure without reading evaluation blobs (#3400).

Transport queries live outside the scientific source namespace so adding these
views cannot invalidate the executable identity of a retained study.
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg

from app.options_bots.golden_proof import setup_sha256
from app.research.options_candidate_evidence import label
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search.protocol import OptionsSearchProtocol, Window, materialize
from app.research.persistence.db import with_connection
from app.schemas.options_research_lineage import (
    OptionsLineageCounts,
    OptionsLineageExposure,
    OptionsLineageFinalist,
    OptionsLineageFold,
    OptionsLineageTrial,
    OptionsLineageTrialPage,
    OptionsLineageWindow,
    OptionsResearchLineage,
)


def readable(value: Any) -> str:
    if value is None:
        return "Off"
    if isinstance(value, dict):
        return "; ".join(f"{key.replace('_', ' ')}: {readable(item)}" for key, item in value.items())
    return str(value)


def window_view(window: Window) -> OptionsLineageWindow:
    return OptionsLineageWindow(key=window.key, start_ms=window.start_ms, end_ms=window.end_ms,
        scheduled_sessions=len(window.session_ms), activity_floor=window.activity_floor)


def admitted_windows(row: dict[str, Any]) -> list[str]:
    protocol = OptionsSearchProtocol.model_validate(row["protocol"])
    return [protocol.development.key, *(w.key for fold in protocol.folds for w in (fold.training, fold.forward)),
        *([protocol.final.key] if row.get("finalists") or row.get("final_opened_at_ms") is not None else [])]


async def receipts(conn: asyncpg.Connection, study_id: str, windows: list[str]) -> dict[str, Any]:
    counts = await conn.fetchrow("""SELECT count(*) AS evaluation_keys,
        count(*) FILTER (WHERE result IS NOT NULL) AS completed_evaluations,
        coalesce(sum(execution_attempts),0) AS physical_starts
        FROM research_options_search_evaluations WHERE study_id=$1 AND identity->'window'->>'key'=ANY($2::text[])""", study_id, windows)
    trials = await conn.fetchrow("""SELECT count(*) AS trial_events,
        count(*) FILTER (WHERE record->'trial'->>'status'='evaluated') AS evaluated_trial_events,
        count(*) FILTER (WHERE record->'trial'->>'status'='cache_hit') AS cache_hit_trial_events,
        count(*) FILTER (WHERE record->'trial'->>'status'='invalid') AS invalid_trial_events
        FROM research_options_search_trials WHERE study_id=$1 AND record->>'window'=ANY($2::text[])""", study_id, windows)
    exposures = await conn.fetch("""SELECT id,start_ms,end_ms,created_at_ms FROM research_options_search_exposures
        WHERE owner_id=$1 ORDER BY id LIMIT 201""", study_id)
    return {"counts": {**dict(counts), **dict(trials)}, "exposures": [dict(record) for record in exposures]}


def project(row: dict[str, Any], retained: dict[str, Any]) -> OptionsResearchLineage:
    protocol = OptionsSearchProtocol.model_validate(row["protocol"])
    selection = (row.get("research") or {}).get("selection")
    counts = dict(retained["counts"])
    missing = counts["evaluation_keys"] == 0 and (row.get("progress") or {}).get("completed", 0) > 0
    counts.update(receipt_state="not_recorded" if missing else "recorded",
        distinct_development_candidates=len(selection["points"]) if selection else None,
        eligible_development_candidates=len(selection["eligible"]) if selection else None,
        rejected_development_candidates=len(selection["points"]) - len(selection["eligible"]) if selection else None)
    if missing:
        counts.update(evaluation_keys=None, completed_evaluations=None, physical_starts=None)
    recorded_folds = {item["index"]: item for item in (row.get("research") or {}).get("folds", [])}
    folds = []
    for fold in protocol.folds:
        observed = recorded_folds.get(fold.index, {})
        selected = observed.get("selection", {})
        winner = selected.get("winner")
        digest = observed.get("winner_hash") or (winner or {}).get("point_hash")
        written = None
        if digest:
            written = protocol.setup if digest == setup_sha256(protocol.setup) else materialize(protocol, selected["points"][digest])
            if setup_sha256(written) != digest:
                raise ValueError("A retained fold winner does not match its exact settings.")
        forward = observed.get("forward")
        folds.append(OptionsLineageFold(index=fold.index, training=window_view(fold.training), forward=window_view(fold.forward),
            state=observed.get("status", "not_recorded"), winner_hash=digest,
            winner_label=label(written, digest == setup_sha256(protocol.setup)) if written else None,
            forward_net_profit_usd=forward.get("net_profit_usd") if forward else None,
            forward_eligible=forward.get("eligible") if forward else None))
    final_results = {item["candidate_hash"]: item for item in row.get("final_results", [])}
    finalists = []
    for digest in row.get("finalists", []):
        result = final_results.get(digest)
        finalists.append(OptionsLineageFinalist(candidate_hash=digest, state="retained" if result else "uncomputed",
            verdict=result["verdict"] if result else None, gate_outcomes=result["gate_outcomes"] if result else [],
            evidence=result["evidence"] if result else []))
    exposures = []
    for item in retained["exposures"][:200]:
        scope = next((w.key for w in (protocol.development, protocol.final)
            if (w.start_ms, w.end_ms) == (item["start_ms"], item["end_ms"])), "other_recorded_interval")
        exposures.append(OptionsLineageExposure(receipt_id=str(item["id"]), start_ms=item["start_ms"], end_ms=item["end_ms"],
            created_at_ms=item["created_at_ms"], scope=scope))
    rules = [{"label": key.replace("_", " "), "value": readable(value)}
        for key, value in protocol.policy.model_dump(mode="json").items() if key not in {"version", "rank_by"}]
    objective = {"net_profit": "Net profit after modeled costs", "sharpe": "Daily Sharpe", "drawdown": "Starting-capital minute drawdown"}[protocol.policy.rank_by]
    return OptionsResearchLineage(study_id=row["id"], protocol_hash=row["protocol_hash"], revision=row["revision"],
        parent_study_id=row.get("parent_study_id"), method=protocol.method, objective=objective,
        selection_explanation="Only candidates meeting every frozen eligibility rule can be selected. Development ranks candidates; each forward fold independently selects on its training window. Pooled folds describe the procedure, not one setup's track record.",
        development=window_view(protocol.development), final=window_view(protocol.final),
        settings=[{**knob.model_dump(mode="json"), "values": [readable(value) for value in knob.model_dump(mode="json")["values"]]} for knob in protocol.knobs],
        rules=rules, counts=OptionsLineageCounts.model_validate(counts), folds=folds, finalists=finalists,
        final_state="opened" if row.get("finalists") or row.get("final_opened_at_ms") is not None else "not_opened",
        stress=[{**scenario.model_dump(mode="json"), "mandatory": protocol.policy.stress_gate} for scenario in protocol.stress],
        exposure=row["exposure"], exposure_ledger=exposures, exposure_ledger_complete=len(retained["exposures"]) <= 200)


async def read(study_id: str) -> OptionsResearchLineage:
    row = await with_connection(repo.get, study_id)
    retained = await with_connection(receipts, study_id, admitted_windows(row))
    return project(row, retained)


async def trial_rows(conn: asyncpg.Connection, study_id: str, windows: list[str], after: int, limit: int) -> list[dict[str, Any]]:
    rows = await conn.fetch("""SELECT id,record,created_at_ms FROM research_options_search_trials
        WHERE study_id=$1 AND record->>'window'=ANY($2::text[]) AND id>$3 ORDER BY id LIMIT $4""", study_id, windows, after, limit + 1)
    return [{**dict(record), "record": json.loads(record["record"])} for record in rows]


def trial_page(row: dict[str, Any], rows: list[dict[str, Any]], limit: int) -> OptionsLineageTrialPage:
    items = []
    for receipt in rows[:limit]:
        value = receipt["record"]["trial"]
        items.append(OptionsLineageTrial(receipt_id=str(receipt["id"]), created_at_ms=receipt["created_at_ms"],
            window=receipt["record"]["window"], ordinal=value["ordinal"], role=value["role"], status=value["status"],
            candidate_hash=value["point_hash"], reason=value.get("reason"),
            settings=[{"label": path, "value": readable(setting)} for path, setting in value["point"].items()]))
    return OptionsLineageTrialPage(study_id=row["id"], protocol_hash=row["protocol_hash"], items=items,
        next_cursor=items[-1].receipt_id if len(rows) > limit else None, limit=limit)


async def trials(study_id: str, *, after: str = "0", limit: int = 50) -> OptionsLineageTrialPage:
    row = await with_connection(repo.get, study_id)
    rows = await with_connection(trial_rows, study_id, admitted_windows(row), int(after), limit)
    return trial_page(row, rows, limit)
