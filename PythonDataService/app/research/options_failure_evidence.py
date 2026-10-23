"""Diagnose retained Options evidence without replay, capture or mutable lake reads (#3399).

Coverage passes through canonical metrics from options_golden_search/metrics.py.
Counts describe retained records; this projection computes no financial statistic.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.data_lake.path_policy import OptionMinuteBarPath
from app.options_backtest.observation import HeldMark
from app.options_backtest.results import Fill
from app.research import options_candidate_evidence as reader
from app.research.golden_search.proof import BlobStore, ProofMismatchError
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search.marks import value_session
from app.research.options_golden_search.metrics import SessionEvidence
from app.research.options_golden_search.snapshots import Snapshot, SnapshotRunData
from app.research.options_golden_search.stages import default_blob_store
from app.research.persistence.db import with_connection
from app.schemas.options_failure_evidence import OptionsFailureEvent, OptionsFailureEvidence, OptionsFailureInspection
from app.utils.session_anchors import et_date_at_ms


def event(evidence: reader.RetainedOptionsEvidence, session: SessionEvidence, mark: HeldMark,
          at_ms: int, code: str) -> OptionsFailureEvent:
    index = evidence.result.sessions.index(session)
    pins = evidence.result.pins[index].option_days if index < len(evidence.result.pins) else ()
    digest = next((pin.sha256 for pin in pins if pin.contract == mark.contract), None)
    closing = next((trade.exit.bar_close_ms for trade in session.outcome.trades
        if trade.leg_id == mark.leg_id and trade.contract == mark.contract and isinstance(trade.exit, Fill)
        and trade.entry.bar_close_ms <= at_ms < trade.exit.bar_close_ms), None)
    return OptionsFailureEvent(id=f"{code}:{at_ms}:{mark.leg_id}", code=code,
        session_open_ms=session.session_open_ms, at_ms=at_ms, leg_id=mark.leg_id, contract=mark.contract.occ_symbol,
        retained_path=str(OptionMinuteBarPath(mark.contract.root, mark.contract.occ_symbol, et_date_at_ms(session.session_open_ms)).relative_path()),
        expected_input_sha256=digest, close_filled_at_ms=closing,
        explanation="A held option has no contemporaneous valuation. Cause not established; a later real close does not repair minute risk."
            if code == "missing_held_mark" else "The retained session ends with an open position. A mark is not a real closing fill.")


def events(evidence: reader.RetainedOptionsEvidence) -> list[OptionsFailureEvent]:
    items = []
    for session in evidence.result.sessions:
        # Under mark rule v2 a carried minute is valued, so only a position that has traded nothing is still missing.
        for point in value_session(session.observations, evidence.result.mark_rule).observations:
            items.extend(event(evidence, session, mark, point.at_ms, "missing_held_mark") for mark in point.held if mark.close is None)
        if session.observations:
            last = session.observations[-1]
            items.extend(event(evidence, session, mark, last.at_ms, "unclosed_position") for mark in last.held)
    return sorted(items, key=lambda item: (item.session_open_ms, item.at_ms, item.leg_id, item.code))


def project(evidence: reader.RetainedOptionsEvidence, row: dict[str, Any], *, offset: int = 0, limit: int = 50) -> OptionsFailureEvidence:
    metrics = evidence.result.metrics
    folds = (row.get("research") or {}).get("folds", [])
    items = events(evidence)
    final_opened = bool(row.get("finalists")) or bool(row.get("final_results")) or row.get("phase") in {"final", "approval", "qualified"}
    final_state = "opened" if final_opened else "not_opened" if row.get("phase") in {"locked", "research", "compare"} else "not_recorded"
    return OptionsFailureEvidence(study_id=evidence.study_id, setup_name=row["setup_name"],
        candidate_hash=reader.setup_sha256(evidence.setup), scope=evidence.scope,
        evidence_identity=evidence.evidence_identity, evidence_blob_sha256=evidence.evidence_blob_sha256,
        start_ms=evidence.window.start_ms, end_ms=evidence.window.end_ms,
        scheduled_sessions=len(evidence.window.session_ms), replayed_sessions=len(evidence.result.sessions),
        traded_sessions=metrics.completed_traded_sessions, required_held_marks=metrics.required_held_marks,
        present_held_marks=metrics.required_held_marks - metrics.missing_held_marks - (metrics.carried_held_marks or 0),
        missing_held_marks=metrics.missing_held_marks, forward_fold_count=len(evidence.protocol.folds),
        **reader.mark_rule_disclosure(evidence.result),
        eligible_fold_winners=sum(bool(fold.get("winner_hash")) for fold in folds), final_exposure=final_state,
        risk_complete=not metrics.technical_failures and len(evidence.result.sessions) == len(evidence.window.session_ms),
        technical_failures=list(metrics.technical_failures), items=items[offset:offset + limit],
        total=len(items), offset=offset, limit=limit)


def inspect_record(evidence: reader.RetainedOptionsEvidence, row: dict[str, Any], session_open_ms: int,
                   at_ms: int, leg_id: str, *, blobs: BlobStore | None = None) -> OptionsFailureInspection:
    session = next((session for session in evidence.result.sessions if session.session_open_ms == session_open_ms), None)
    valued = value_session(session.observations, evidence.result.mark_rule).observations if session else ()
    point = next((point for point in valued if point.at_ms == at_ms), None)
    mark = next((mark for mark in point.held if mark.leg_id == leg_id and mark.close is None), None) if point else None
    if session is None or mark is None:
        raise LookupError("This exact missing held mark is not in the retained evaluation.")
    failure = event(evidence, session, mark, at_ms, "missing_held_mark")
    snapshot = Snapshot.model_validate(row["snapshot"])
    expected = failure.expected_input_sha256
    pinned = snapshot.manifest.get(failure.retained_path)

    def outcome(code: str, explanation: str, *, verified: bool = False, contains_bar: bool | None = None) -> OptionsFailureInspection:
        return OptionsFailureInspection(study_id=evidence.study_id, candidate_hash=reader.setup_sha256(evidence.setup),
            evidence_identity=evidence.evidence_identity, event_id=failure.id, code=code, retained_path=failure.retained_path,
            expected_input_sha256=expected, snapshot_input_sha256=pinned, input_verified=verified,
            contains_eligible_trade_bar=contains_bar, explanation=explanation)

    if snapshot.snapshot_id != evidence.protocol.snapshot_id or (expected is not None and pinned != expected):
        return outcome("retained_input_changed", "The retained input receipt disagrees with the frozen snapshot identity. No substitute file was read.")
    if failure.retained_path in snapshot.absent:
        return outcome("retained_input_recorded_absent", "The frozen snapshot records this input as absent. Repairing the lake cannot rewrite this study.")
    if pinned is None or expected is None:
        return outcome("retained_input_not_recorded", "The retained evidence has no matching file receipt. The cause of the missing valuation remains unknown.")
    try:
        captured = SnapshotRunData(evidence.setup.underlying, snapshot, blobs or default_blob_store()).option_day(mark.contract, et_date_at_ms(session_open_ms))
    except ProofMismatchError as exc:
        if exc.code == "BLOB_MISSING":
            return outcome("retained_file_unavailable", "The receipted archived file is unavailable. Current lake data cannot replace it.")
        if exc.code == "BLOB_CORRUPT":
            return outcome("retained_file_corrupt", "The archived file no longer matches its receipted bytes. No fallback was read.")
        raise
    except (OSError, ValueError) as exc:
        return outcome("retained_file_unreadable", f"The receipted archived file cannot be decoded ({type(exc).__name__}). Its missing valuation remains unresolved.")
    contains = captured is not None and any(bar.close_ms == at_ms for bar in captured.value)
    if contains:
        return outcome("retained_mark_input_disagreement", "A matching eligible bar is present in the verified file, but the retained trace has no mark. This discrepancy requires investigation; the historical result is unchanged.", verified=True, contains_bar=True)
    return outcome("no_eligible_trade_bar", "The verified retained file has no eligible trade bar for this minute. This establishes a sparse input, not its upstream cause; downloading again is not a promised repair.", verified=True, contains_bar=False)


async def read(study_id: str, digest: str, scope: reader.Scope, *, offset: int = 0, limit: int = 50) -> OptionsFailureEvidence:
    evidence = await reader.load(study_id, digest, scope)
    row = await with_connection(repo.get, study_id)
    return await asyncio.to_thread(project, evidence, row, offset=offset, limit=limit)


async def inspect(study_id: str, digest: str, scope: reader.Scope, session_open_ms: int, at_ms: int, leg_id: str) -> OptionsFailureInspection:
    evidence = await reader.load(study_id, digest, scope)
    row = await with_connection(repo.get, study_id)
    return await asyncio.to_thread(inspect_record, evidence, row, session_open_ms, at_ms, leg_id)
