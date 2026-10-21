"""Resolve an exact Options evidence subject without running research or opening final (#3395)."""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any, Literal

from app.options_backtest import saved_runs
from app.options_backtest.describe import leg_sentences
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_bots.golden import GoldenOptionsSetups
from app.options_bots.golden_proof import setup_sha256
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search.protocol import OptionsSearchProtocol
from app.research.persistence.db import with_connection
from app.schemas.golden_option_setups import GoldenOptionSetupView
from app.schemas.options_backtest_runs import OptionsBacktestRunDetailResponse
from app.schemas.options_evidence import (
    OptionsEvidenceApproval,
    OptionsEvidenceDimension,
    OptionsEvidencePeriod,
    OptionsEvidenceRelation,
    OptionsEvidenceSearchSetting,
    OptionsEvidenceWorkspace,
)
from app.schemas.options_golden_search import StudyDetail


def approval(version: GoldenOptionSetupView, digest: str, run_id: int) -> OptionsEvidenceApproval:
    evidence = version.evidence
    linked = bool(evidence.options_search_study_id and evidence.study_manifest_sha256 and evidence.verdict)
    source = "search" if linked else "not_recorded" if evidence.options_search_study_id else "backtest"
    return OptionsEvidenceApproval(
        golden_id=version.id, source_run_id=version.source_run_id, setup_sha256=version.setup_sha256,
        source=source, identity_matches=version.setup_sha256 == digest and version.source_run_id == run_id,
        proof_status=version.status, proof_explanation=version.status_explanation, note=version.note,
        study_id=evidence.options_search_study_id, study_manifest_sha256=evidence.study_manifest_sha256,
        research_verdict=evidence.verdict, failed_rules=evidence.failed_rules or [],
        waived_gate_ids=evidence.waived_gate_ids, exposure=evidence.exposure,
    )


def describe(setup: OptionsBacktestSetup) -> list[str]:
    return [sentence for leg in setup.legs for sentence in leg_sentences(leg)]


def run_workspace(stored: OptionsBacktestRunDetailResponse, versions: list[GoldenOptionSetupView], *,
                  golden_id: str | None = None) -> OptionsEvidenceWorkspace:
    if stored.setup is None:
        raise ValueError(stored.setup_cannot_open_reason)
    record, setup = stored, stored.setup
    digest = setup_sha256(setup)
    approvals = [approval(version, digest, record.id) for version in versions]
    valid = [entry for entry in approvals if entry.identity_matches]
    linked = [entry for entry in valid if entry.source == "search"]
    mismatch = golden_id is not None and not valid
    if mismatch:
        setup = versions[0].setup
        digest = versions[0].setup_sha256
    if linked:
        research = OptionsEvidenceDimension(status="recorded", explanation="Recorded search qualification; inspect the exact manifest and every owner exception below.")
    else:
        research = OptionsEvidenceDimension(status="not_recorded", explanation="No linked search qualification.")
    summary = None if mismatch else stored.figures
    if summary is None:
        replay_status, lead = "unavailable", "Replay performance is unavailable."
    elif Decimal(summary.net_profit_usd) > 0:
        replay_status, lead = "profitable", "Profitable in replay."
    elif Decimal(summary.net_profit_usd) < 0:
        replay_status, lead = "loss", "Loss in replay."
    else:
        replay_status, lead = "zero", "Break-even in replay."
    observed = summary.sessions_tested if summary else None
    complete = observed == record.sessions_requested and record.status == "complete"
    return OptionsEvidenceWorkspace(
        subject_kind="golden" if golden_id else "run", subject_id=golden_id or str(record.id),
        evidence_identity=f"run:{record.id}:{digest}", name=record.setup_name or f"Run {record.id}",
        setup=setup, setup_sha256=digest, source_run_id=None if mismatch else record.id,
        study_id=linked[0].study_id if len(linked) == 1 else None,
        period=OptionsEvidencePeriod(first_session_open_ms=record.first_session_open_ms,
            last_session_open_ms=record.last_session_open_ms, scheduled_sessions=record.sessions_requested,
            observed_sessions=observed),
        headline="Source setup identity mismatch. Replay evidence cannot be attributed to this Golden version." if mismatch else lead + (" Research qualification is missing." if not linked else " Recorded search qualification is available."),
        replay=OptionsEvidenceDimension(status=replay_status, explanation=(
            f"{summary.net_profit_usd} USD after recorded modeled costs across {observed} observed sessions. Descriptive replay evidence."
            if summary else f"Run status: {record.status}. No retained summary is available.")),
        coverage=OptionsEvidenceDimension(status="complete" if complete else "partial" if observed is not None else "unavailable",
            explanation=f"{observed if observed is not None else 'Unknown'} observed / {record.sessions_requested} scheduled sessions. Session coverage does not certify minute valuation coverage."),
        research=research,
        final=OptionsEvidenceDimension(status="recorded_in_manifest" if linked else "not_recorded",
            explanation="See each recorded approval's final exposure and manifest." if linked else "Final research evidence is not recorded for this replay."),
        approval=OptionsEvidenceDimension(status="recorded" if valid else "identity_mismatch" if approvals else "not_recorded",
            explanation="Approved from search." if linked else "Approved from backtest." if any(v.source == "backtest" for v in valid) else "Approval origin is not recorded."),
        approvals=approvals, relations=[], settings_description=describe(setup),
    )


def study_workspace(row: dict[str, Any]) -> OptionsEvidenceWorkspace:
    protocol = OptionsSearchProtocol.model_validate(row["protocol"])
    detail = StudyDetail.from_record(row)
    baseline = next((c for c in detail.candidates if c.is_original), None)
    sessions = len(baseline.sessions) if baseline and baseline.sessions else None
    baseline_blocked = bool(baseline and baseline.verdict == "could_not_evaluate")
    research = row.get("research")
    selection = research.get("selection") if research else None
    technical = any(c.verdict == "could_not_evaluate" for c in detail.candidates)
    missing_marks = any("missing_held_mark:" in g.reason for c in detail.candidates for g in c.gate_outcomes if g.gate_id.endswith(":technical_validity"))
    if not selection:
        research_status, headline = "pending", "Research has not established a result yet."
        explanation = "No retained selection result is recorded."
    elif not selection.get("complete", False) or (selection.get("winner") is None and technical):
        research_status, headline = "inconclusive", "This search did not establish a result."
        explanation = "Research inconclusive: missing valuation evidence." if missing_marks else "Research inconclusive: retained candidate evidence could not be evaluated."
    elif selection.get("winner") is None:
        research_status, headline = "does_not_meet_rules", "No candidate met the frozen research rules."
        explanation = "The retained selection has no eligible winner."
    else:
        research_status, headline = "procedure", "Research evidence for the frozen search procedure."
        explanation = "Inspect development, forward and final outcomes separately; a selection winner alone does not qualify research."
    exposed = row.get("final_opened_at_ms") is not None or bool(row.get("finalists"))
    final_status = "not_opened" if not exposed else "unavailable" if not detail.final_results else "failed" if any(c.verdict != "meets_rules" for c in detail.final_results) else "meets_rules"
    return OptionsEvidenceWorkspace(
        subject_kind="study", subject_id=row["id"], evidence_identity=f"study:{row['id']}:{row['protocol_hash']}:{row['revision']}",
        name=row["setup_name"], setup=protocol.setup, setup_sha256=setup_sha256(protocol.setup),
        source_run_id=None, study_id=row["id"],
        period=OptionsEvidencePeriod(first_session_open_ms=protocol.development.session_ms[0],
            last_session_open_ms=protocol.development.session_ms[-1], scheduled_sessions=len(protocol.development.session_ms), observed_sessions=sessions),
        headline=headline,
        replay=OptionsEvidenceDimension(status="partial" if baseline_blocked else "procedure", explanation="Baseline diagnostics belong to this search's development scope; they are not a full-period verdict on a different setup."),
        coverage=OptionsEvidenceDimension(status="partial" if baseline_blocked else "not_recorded", explanation=f"Baseline: {sessions if sessions is not None else 'Unknown'} observed / {len(protocol.development.session_ms)} scheduled development sessions."),
        research=OptionsEvidenceDimension(status=research_status, explanation=explanation),
        final=OptionsEvidenceDimension(status=final_status, explanation="The final test was not opened." if not exposed else "Final exposure is permanent; inspect retained outcomes separately."),
        approval=OptionsEvidenceDimension(status="recorded" if detail.qualification_id else "not_recorded", explanation="Recorded Golden approval: " + detail.qualification_id if detail.qualification_id else "No Golden approval is recorded for this study."),
        approvals=[], relations=[], settings_description=describe(protocol.setup),
        frozen_search_settings=[OptionsEvidenceSearchSetting(path=k.path, mode=k.mode,
            values=[value if isinstance(value, str) else json.dumps(value, sort_keys=True) for value in k.model_dump(mode="json")["values"]]) for k in protocol.knobs],
    )


async def read_run(run_id: int) -> OptionsBacktestRunDetailResponse:
    stored = await with_connection(saved_runs.get_run, run_id, is_live=saved_runs.not_asked)
    if stored is None:
        raise LookupError("The source Options replay is not retained.")
    return OptionsBacktestRunDetailResponse.from_stored(stored)


async def read(kind: Literal["run", "golden", "study"], subject_id: str, *,
               inspected_study_id: str | None = None, comparator_run_id: int | None = None) -> OptionsEvidenceWorkspace:
    if kind == "study":
        view = study_workspace(await with_connection(repo.get, subject_id))
    else:
        service = GoldenOptionsSetups()
        if kind == "golden":
            version = await service.get(subject_id)
            stored = await read_run(version.source_run_id)
            view = run_workspace(stored, [version], golden_id=subject_id)
        else:
            run_id = int(subject_id)
            stored, versions = await asyncio.gather(read_run(run_id), service.list(source_run_id=run_id, limit=200))
            view = run_workspace(stored, versions)
    if inspected_study_id is not None:
        other = study_workspace(await with_connection(repo.get, inspected_study_id))
        matches = other.setup_sha256 == view.setup_sha256
        view.relations.append(OptionsEvidenceRelation(kind="inspected_search", subject_id=inspected_study_id,
            setup_sha256=other.setup_sha256, settings_match=matches,
            explanation="Inspected search; comparison creates no recorded ancestry. " + ("Exact baseline settings match." if matches else "Settings mismatch: this search's baseline is a different setup.")))
    if comparator_run_id is not None:
        other = await read_run(comparator_run_id)
        if other.setup is None:
            raise ValueError(other.setup_cannot_open_reason)
        digest = setup_sha256(other.setup)
        view.relations.append(OptionsEvidenceRelation(kind="historical_comparator", subject_id=str(comparator_run_id),
            setup_sha256=digest, settings_match=digest == view.setup_sha256,
            explanation="Historical comparator selected explicitly; this relationship establishes no parent, seed or winner."))
    return view
