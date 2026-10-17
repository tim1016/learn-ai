"""Guarded study commands and read projections for Options Golden Search (#3332)."""
from __future__ import annotations

import asyncio
from decimal import localcontext
from typing import Any
from uuid import uuid4

from app.options_backtest import fills, saved_setups
from app.options_bots.golden_proof import setup_sha256
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search.identity import execution_identity
from app.research.options_golden_search.planning import budget_plan, freeze_protocol
from app.research.options_golden_search.protocol import OptionsSearchProtocol, Policy, declared_knobs, materialize
from app.research.persistence import lifecycle
from app.research.persistence.db import with_connection
from app.research.runs.hashing import hash_payload
from app.schemas.options_golden_search import Command, Declarations, LockRequest, PlanRequest, Preflight, StudyDetail
from app.utils.timestamps import now_ms_utc


async def setup_named(name: str):
    saved = await with_connection(saved_setups.get_setup, name)
    if saved is None or saved.setup is None:
        raise ValueError("Choose a saved setup that can be opened.")
    return saved.setup


async def declarations(name: str) -> Declarations:
    setup = await setup_named(name)
    rows = declared_knobs(setup)
    knobs = []
    for row in rows:
        path, current = row["path"], row["current_value"]
        pieces = path.split(".")
        rule = bool(row["rule_presence"])
        if rule:
            values: list[Any] = [None]
            if current is not None:
                values.append(current)
            elif pieces[-1] == "stop_loss":
                values.append({"kind": "percent", "value": "25"})
            elif pieces[-1] == "momentum":
                values.append({"kind": "percent_up", "value": "25"})
            elif pieces[-1] == "reentry_on_stop":
                values.append({"kind": "at_once", "count": 1})
            else:
                values.append({"anchor": "close", "offset_ms": 3600000})
        elif pieces[-1] == "kind":
            values = (["at_once", "on_momentum"] if "reentry_on_stop" in path else
                      ["percent", "points"] if "stop_loss" in path else
                      ["percent_up", "percent_down", "points_up", "points_down"])
        else:
            values = []
        knobs.append({"path": path, "label": path.replace("legs.", "").replace("_", " "),
            "leg_id": pieces[1] if pieces[0] == "legs" else None, "current_value": current,
            "value_type": "category" if rule or pieces[-1] == "kind" else "integer" if isinstance(current, int) else "decimal",
            "legal_values": values, "min": "0" if pieces[-1] in {"offset_ms", "count"} else "0.0001",
            "max": "20" if pieces[-1] == "count" else None,
            "step": "60000" if pieces[-1] == "offset_ms" else "1" if pieces[-1] == "count" else "0.0001",
            "help": "Include rule Off/On is separate from holding or varying its settings." if rule else "Vary versus hold fixed; search order never changes profit scoring."})
    from datetime import timedelta

    from app.data_lake import catalog_client
    from app.data_lake.types import polygon_mode_for
    from app.utils.session_anchors import et_midnight_ms

    await catalog_client.init_pool()
    coverage = await catalog_client.select_coverage_minute_bars(
        "usa", setup.underlying, "trade", None, None, price_adjustment_mode=polygon_mode_for(adjusted=False)
    )
    held = [row.trading_date for row in coverage if row.trading_date is not None]
    return Declarations.model_validate({"setup_name": name, "setup": setup, "setup_hash": setup_sha256(setup),
        "knobs": knobs, "history_bounds": {"start_ms": et_midnight_ms(min(held)) if held else None,
            "end_ms": et_midnight_ms(max(held) + timedelta(days=1)) if held else None},
        "defaults": {"version": "options-starting-limits-v1", "rationale": "Suggested starting limits; activity and Sharpe are policy hurdles, not confidence or future-profit claims.",
            "policy": {"max_drawdown_percent": "20"}}})


def build_protocol(request: PlanRequest, setup: Any) -> OptionsSearchProtocol:
    policy = request.policy.model_dump(exclude={"max_drawdown_percent"})
    with localcontext(fills.EXACT):
        policy["max_drawdown_fraction"] = request.policy.max_drawdown_percent / 100
    return freeze_protocol(setup, request.knobs, **request.history.model_dump(), policy=Policy(**policy),
        identities=execution_identity(), method=request.method, budget=request.budget, finalist_count=request.finalist_count,
        stress=request.stress, zoom=request.zoom, pair_audits=request.pair_audits)


async def prepare(request: PlanRequest) -> tuple[Preflight, OptionsSearchProtocol | None, dict[str, Any] | None]:
    setup = await setup_named(request.setup_name)
    if setup_sha256(setup) != request.expected_setup_hash:
        raise repo.Conflict("The saved setup changed after it was displayed. Reload it before planning.")
    if request.parent_study_id is not None:
        parent = await with_connection(repo.get, request.parent_study_id)
        if parent["underlying"] != setup.underlying:
            raise ValueError("A fork must retain the original underlying.")
    blockers: list[str] = []
    warnings: list[str] = []
    snapshot: dict[str, Any] | None = None
    protocol: OptionsSearchProtocol | None = None
    budget = None
    try:
        protocol = await asyncio.to_thread(build_protocol, request, setup)
        budget = budget_plan(protocol)
        if not budget.fits:
            blockers.append("The complete declared work exceeds the evaluation cap. Reduce the search space before lock.")
        warnings.extend(budget.warnings)
        from app.research.golden_search.proof import default_blob_store
        from app.research.options_golden_search.snapshots import capture_snapshot

        if not blockers:
            captured = await asyncio.to_thread(capture_snapshot, protocol, default_blob_store())
            snapshot = captured.model_dump(mode="json")
            if any("/listing/" in path or path.startswith("equity/") for path in captured.absent):
                blockers.append("Required underlying or listing history is absent. Capture it before locking this study.")
            protocol = protocol.model_copy(update={"snapshot_id": captured.snapshot_id})
    except ValueError as exc:
        blockers.append(str(exc))
    windows = [] if protocol is None else [protocol.development, *[w for f in protocol.folds for w in (f.training, f.forward)], protocol.final]
    from math import prod

    with localcontext(fills.EXACT):
        dollars = str(setup.starting_cash_usd * request.policy.max_drawdown_percent / 100)
    classification = "history_unknown"
    if protocol is not None:
        classification = await with_connection(repo.exposure, setup.underlying, protocol.final.start_ms, protocol.final.end_ms)
    receipt = protocol.protocol_hash if protocol and snapshot else hash_payload(request.model_dump(mode="json"))
    return Preflight(preflight_hash=receipt, can_lock=not blockers and snapshot is not None,
        blockers=blockers, warnings=warnings, drawdown_limit_usd=dollars,
        combination_count=prod(len(k.values) for k in request.knobs if k.mode == "search"),
        legal_combination_count=None, invalid_combination_count=None,
        required_evaluations=budget.unique_upper_bound if budget else 0,
        reserved_evaluations=budget.mandatory_downstream if budget else 0,
        estimated_setup_sessions=budget.replay_session_upper_bound if budget else 0,
        mark_coverage="Pinned artifact inventory; held-minute coverage is verified separately for each candidate.",
        exposure=classification, identity=protocol.identities["selection"] if protocol else "unavailable",
        windows=[{"label": w.key, "start_ms": w.start_ms,"end_ms": w.end_ms,"scheduled_sessions":len(w.session_ms),"required_traded_sessions":w.activity_floor} for w in windows]), protocol, snapshot


def initial_record(study_id: str, request: PlanRequest, protocol: OptionsSearchProtocol, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {"id": study_id,"revision":0,"attempt":0,"job_id":None,"phase":"research","status":"idle",
        "protocol_hash":protocol.protocol_hash,"protocol":protocol.model_dump(mode="json"),"snapshot":snapshot,
        "setup_name":request.setup_name,"parent_study_id":request.parent_study_id,"underlying":protocol.setup.underlying,"created_at_ms":now_ms_utc(),
        "progress":{"stage":"Locked plan","reserved":budget_plan(protocol).mandatory_downstream},
        "candidates":[],"final_results":[],"gate_outcomes":[],
        "exposure":{"classification":"history_unknown","explanation":"Exposure is checked atomically when final test opens."},
        "allowed_commands":["run_research","keep_original"],"dispatch":None,"stage_token":None,
        "failure_reason":None,"qualification_id":None,"qualification_status":None,"research":None,"finalists":[],
        "protocol_summary": protocol_summary(protocol)}


async def create(request: LockRequest) -> StudyDetail:
    study_id = "os_" + hash_payload({"lock": request.idempotency_key})[:40]
    digest = hash_payload(request.request.model_dump(mode="json"))
    async def prior(conn):
        return await repo.command_receipt(conn, study_id, request.idempotency_key, digest)
    retry = await with_connection(prior)
    if retry is not None:
        return StudyDetail.from_record(retry)
    review, protocol, snapshot = await prepare(request.request)
    if not review.can_lock or protocol is None or snapshot is None:
        raise ValueError("Cannot lock: " + "; ".join(review.blockers))
    if request.preflight_hash != review.preflight_hash:
        raise repo.Conflict("The setup, code or retained inputs changed after preflight. Review the plan again.")
    value = initial_record(study_id, request.request, protocol, snapshot)
    return StudyDetail.from_record(await with_connection(repo.create, value, request.idempotency_key, digest))


def allowed(value: dict[str, Any]) -> list[str]:
    if value["status"] == "queued" and value.get("job_id") is None:
        return ["resume", "cancel"]
    if value["status"] in {"queued","running"}:
        return ["cancel"]
    if value["phase"] in {"approved","retained"}:
        return []
    if value["status"] in {"interrupted","cancelled"}:
        return ["resume","keep_original"]
    if value["phase"] == "research":
        return ["run_research","keep_original"]
    if value["phase"] == "compare":
        return ["open_final","keep_original"] if value["candidates"] else ["keep_original"]
    if value["phase"] == "approval":
        return ["approve","approve_exception","keep_original"]
    return ["keep_original"]


async def detail(study_id: str) -> StudyDetail:
    value = await with_connection(repo.get, study_id)
    if value["status"] in {"running", "queued"} and value.get("job_id"):
        live = await asyncio.to_thread(lifecycle.job_is_live, value["job_id"])
        if live is False:
            async with repo.edit(study_id) as (conn, current):
                if current["status"] in {"running", "queued"} and current.get("job_id") == value["job_id"]:
                    current.update(status="interrupted",failure_reason="The worker stopped. Resume explicitly with the locked inputs.")
                    current["allowed_commands"] = allowed(current)
                    value = await repo.save(conn, current)
    value["allowed_commands"] = allowed(value)
    if value.get("qualification_id"):
        from app.options_bots.golden import GoldenOptionsSetups
        qualification = await GoldenOptionsSetups().get(value["qualification_id"])
        value["qualification_status"] = qualification.status
    return StudyDetail.from_record(value)


async def command(study_id: str, request: Command) -> StudyDetail:
    excluded = {"idempotency_key"}
    if not request.waived_gate_ids:
        excluded.add("waived_gate_ids")
    request_hash = hash_payload(request.model_dump(mode="json", exclude=excluded))
    async with repo.edit(study_id) as (conn, value):
        prior = await repo.command_receipt(conn, study_id, request.idempotency_key, request_hash)
        if prior is not None:
            return StudyDetail.from_record(prior)
        if request.expected_revision != value["revision"]:
            raise repo.Conflict("This study changed. Refresh before sending another command.")
        if request.command not in allowed(value):
            raise repo.Conflict("This command is not available in the current study phase.")
        if request.command == "cancel":
            value.update(status="cancelled",dispatch=None,stage_token=None)
            # Fencing commits before a worker can persist any later evaluation.
            if value.get("job_id"):
                lifecycle.request_cancel(value["job_id"])
        elif request.command == "keep_original":
            value.update(phase="retained",status="completed",dispatch=None)
        else:
            protocol = OptionsSearchProtocol.model_validate(value["protocol"])
            if await asyncio.to_thread(execution_identity) != protocol.identities:
                raise repo.Conflict("The locked executable identity changed. Fork a new study.")
            if value["phase"] == "research" and request.command in {"run_research", "resume"}:
                await repo.reserve_exposure(conn,value["underlying"],protocol.development.start_ms,protocol.development.end_ms,value["id"])
            if request.command == "open_final":
                candidates = {c["candidate_hash"]:c for c in value["candidates"]}
                selected = tuple(candidates) if request.candidate_hashes is None else request.candidate_hashes
                if len(set(selected)) != len(selected) or not set(selected) <= set(candidates):
                    raise ValueError("Finalists must be distinct members of the recorded development shortlist.")
                value["finalists"] = list(selected)
                baseline = setup_sha256(protocol.setup)
                if baseline not in value["finalists"]:
                    value["finalists"].append(baseline)
                classification = await repo.reserve_exposure(conn,value["underlying"],protocol.final.start_ms,protocol.final.end_ms,value["id"])
                value["exposure"] = {"classification": classification,"explanation": "Not opened in recorded research." if classification=="not_opened" else "This history was used or its earlier use is unknown; approval must retain an explicit exposure exception."}
                value["phase"] = "final"
            if request.command in {"approve","approve_exception"}:
                from app.research.options_golden_search.approval import validate_approval
                validate_approval(value, request)
                value["approval_request"] = request.model_dump(mode="json")
            value.update(status="queued",job_id=None,failure_reason=None,stage_token=uuid4().hex)
            value["dispatch"] = {"study_id":study_id,"stage_token":value["stage_token"]}
        value["allowed_commands"] = allowed(value)
        value = await repo.save(conn, value)
        await repo.record_command(conn,study_id,request.idempotency_key,request_hash,value)
        return StudyDetail.from_record(value)


def protocol_summary(protocol: OptionsSearchProtocol) -> list[dict[str, str]]:
    """An immutable receipt is readable without exposing an implementation object."""
    rows = [{"label": "Setup", "value": f"{protocol.setup.underlying}; capital {protocol.setup.starting_cash_usd} USD; fixed quantities " + ", ".join(f"{leg.leg_id}: {leg.contracts}" for leg in protocol.setup.legs)}]
    for knob in protocol.knobs:
        rows.append({"label": knob.path, "value": f"{knob.mode}; order {knob.priority}; values " + repo.dumps(knob.model_dump(mode="json")["values"])})
    rows.extend({"label": "Policy " + key.replace("_", " "), "value": str(value)} for key, value in protocol.policy.model_dump(mode="json").items())
    for window in [protocol.development, *[w for fold in protocol.folds for w in (fold.training, fold.forward)], protocol.final]:
        rows.append({"label": window.key, "value": f"{window.start_ms} to {window.end_ms} UTC ms; {len(window.session_ms)} sessions; activity floor {window.activity_floor}"})
    rows.extend([{"label": "Snapshot", "value": protocol.snapshot_id}, {"label": "Executable identity", "value": repo.dumps(protocol.identities)},
        {"label": "Work bounds (elapsed limit per executable stage; retries share deadline)", "value": repo.dumps(protocol.budget.model_dump(mode="json"))},
        {"label": "Cost scenarios", "value": repo.dumps([s.model_dump(mode="json") for s in protocol.stress])}])
    return rows


async def read_session(study_id: str, digest: str, window: str, session_open_ms: int) -> dict[str, Any]:
    row = await with_connection(repo.get,study_id)
    collection = row["final_results"] if window=="final" else row["candidates"]
    if not any(candidate["candidate_hash"]==digest for candidate in collection):
        raise LookupError("This candidate has no exposed evidence in the requested window.")
    protocol = OptionsSearchProtocol.model_validate(row["protocol"])
    setup = protocol.setup if setup_sha256(protocol.setup)==digest else materialize(protocol,row["research"]["selection"]["points"][digest])
    scored_window = protocol.final if window=="final" else protocol.development
    identity = {"protocol":row["protocol_hash"],"setup":setup.model_dump(mode="json"),
        "window":scored_window.model_dump(mode="json"),"scenario":"base","snapshot":protocol.snapshot_id,
        "identities":protocol.identities}
    record = await with_connection(repo.get_evaluation,study_id,hash_payload(identity))
    if record is None or record["result"] is None:
        raise LookupError("The retained evaluation is unavailable.")
    def restore() -> dict[str, Any]:
        import json

        from app.research.golden_search.proof import default_blob_store
        from app.research.options_golden_search.metrics import evidence_from_json
        from app.research.options_golden_search.projections import session_detail
        payload = json.loads(default_blob_store().get(record["result"]["evidence_blob"]))
        evidence = next((s for s in payload["sessions"] if s["session_open_ms"]==session_open_ms),None)
        if evidence is None:
            raise LookupError("This candidate has no evidence for that scheduled session.")
        return session_detail(evidence_from_json(evidence))
    return await asyncio.to_thread(restore)
