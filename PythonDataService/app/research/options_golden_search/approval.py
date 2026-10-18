"""Publish study evidence and an exact Options version in one fenced transaction (#3332)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.options_backtest import saved_runs
from app.options_backtest.run import run_sessions
from app.options_bots import golden_repository
from app.options_bots.golden import LIMITATIONS
from app.options_bots.golden_proof import build_proof, proof_sha256, setup_sha256
from app.options_bots.golden_repository import GoldenRecord
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search.policy import validate_exception
from app.research.options_golden_search.protocol import OptionsSearchProtocol, materialize
from app.research.options_golden_search.snapshots import SnapshotRunData
from app.research.persistence.db import run_sync, with_connection
from app.research.runs.hashing import hash_payload
from app.schemas.options_golden_search import Command
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc


def final_shortlist(candidates: list[dict[str, Any]], protocol: OptionsSearchProtocol, requested: tuple[str, ...] | None) -> list[str]:
    """Admit only bounded eligible recommendations and always retain the frozen baseline."""
    baseline = setup_sha256(protocol.setup)
    eligible = {candidate["candidate_hash"] for candidate in candidates if candidate["is_original"] or candidate["verdict"] == "meets_rules"}
    selected = tuple(candidate["candidate_hash"] for candidate in candidates if candidate["candidate_hash"] in eligible) if requested is None else requested
    if len(set(selected)) != len(selected) or not set(selected) <= eligible or len(set(selected) - {baseline}) > protocol.finalist_count:
        raise ValueError("Finalists must be distinct eligible members of the bounded development shortlist.")
    return [*selected, *([] if baseline in selected else [baseline])]


def approval_failures(row: dict[str, Any], digest: str) -> list[dict[str, Any]]:
    expected = row["finalists"]
    actual = [candidate["candidate_hash"] for candidate in row["final_results"]]
    if len(set(actual)) != len(actual) or set(actual) != set(expected):
        raise ValueError("Every frozen finalist must finish its mandatory final evidence before approval.")
    if any(candidate["verdict"] == "could_not_evaluate" for candidate in row["final_results"]):
        raise ValueError("The mandatory final batch contains technically unverifiable evidence.")
    final = next((c for c in row["final_results"] if c["candidate_hash"]==digest),None)
    development = next((c for c in row["candidates"] if c["candidate_hash"]==digest),None)
    if final is None or development is None or digest not in row["finalists"]:
        raise ValueError("Approval must name an exact frozen finalist with development and final evidence.")
    outcomes = [*row["gate_outcomes"],*development["gate_outcomes"],*final["gate_outcomes"]]
    if row["exposure"]["classification"] != "not_opened":
        outcomes.append({"gate_id":"final:exposure","status":"not_enough_evidence","actual":row["exposure"]["classification"],
            "required":"not_opened","reason":"Final history is reused or earlier research is unknown."})
    return [g for g in outcomes if g["status"]!="meets_rules"]


@dataclass(frozen=True)
class _ExceptionRule:
    scope: str
    rule: str
    status: str
    overridable: bool


@dataclass(frozen=True)
class _ExceptionReview:
    outcomes: tuple[_ExceptionRule, ...]


def validate_approval(row: dict[str, Any], request: Command) -> None:
    if request.candidate_hash is None:
        raise ValueError("Choose one exact final candidate before approval.")
    failed = approval_failures(row, request.candidate_hash)
    if any(g["status"] == "could_not_evaluate" or not g.get("overridable", True) for g in failed):
        raise ValueError("Technical invalidity cannot receive an owner exception.")
    if request.command == "approve":
        if failed:
            raise ValueError("Normal approval requires all declared rules and fresh recorded final evidence.")
        if request.waived_gate_ids:
            raise ValueError("Normal approval cannot waive rules.")
    if request.command == "approve_exception":
        if not request.comment.strip():
            raise ValueError("An owner exception requires a nonblank comment acknowledging every failed rule.")
        outcomes = []
        for gate in failed:
            scope, separator, rule = gate["gate_id"].rpartition(":")
            if not separator or not scope or not rule:
                raise ValueError("A failed rule lacks an unambiguous scoped identity.")
            outcomes.append(_ExceptionRule(scope, rule, gate["status"], gate.get("overridable", True)))
        validate_exception((_ExceptionReview(tuple(outcomes)),), request.waived_gate_ids, request.comment)


def approve(row: dict[str, Any], evaluator) -> dict[str, Any]:
    request = Command.model_validate(row["approval_request"])
    validate_approval(row,request)
    protocol = OptionsSearchProtocol.model_validate(row["protocol"])
    digest = request.candidate_hash
    baseline = setup_sha256(protocol.setup)
    points = row["research"]["selection"]["points"]
    setup = protocol.setup if digest==baseline else materialize(protocol,points[digest])
    if setup_sha256(setup)!=digest:
        raise ValueError("Approved settings do not match the frozen final point.")
    rows = run_sync(with_connection(repo.evaluations,row["id"]))
    if any(r["result"] is None for r in rows):
        raise ValueError("An unfinished mandatory evaluation cannot become qualification evidence.")
    # Verify metrics from all retained exact facts, not from the UI's rounded projection.
    from app.research.options_golden_search.protocol import Window
    for recorded in rows:
        evaluator.check()
        evaluator.load(recorded["result"],Window.model_validate(recorded["identity"]["window"]))
    verify_recorded_study(row,evaluator)
    source_job = "oss_"+hash_payload({"study":row["id"],"approval":request.idempotency_key})[:40]
    async def find_source(conn):
        return await conn.fetchrow("SELECT id,status FROM research_options_backtest_runs WHERE job_id=$1",source_job)
    source_row = run_sync(with_connection(find_source))
    from decimal import Decimal

    from app.options_backtest.run import DEFAULT_CASH_LADDER, DEFAULT_SLIPPAGE_LADDER

    units = len(set(DEFAULT_SLIPPAGE_LADDER) | {setup.slippage_percent}) + len(set(DEFAULT_CASH_LADDER) | {Decimal(1)}) - 1
    # Materialization and each independent proof replay run their full ordinary ladders.
    physical_units = units * (2 if source_row is not None and source_row["status"] == "complete" else 3)
    async def reserve_proof_work(conn):
        async with conn.transaction():
            current = await repo.fence(conn, row["id"], row["attempt"])
            count = await conn.fetchval("SELECT count(*) FROM research_options_search_evaluations WHERE study_id=$1", row["id"])
            if count + evaluator.stage_limits.proof_and_materialization > protocol.budget.evaluation_cap:
                raise ValueError("Mandatory ordinary proof work no longer fits its reserved budget.")
            attempted = await conn.fetchval("SELECT coalesce(sum(execution_attempts),0) FROM research_options_search_evaluations WHERE study_id=$1", row["id"])
            previous = current.get("proof_work", {}).get("execution_attempts", 0)
            if attempted + previous + physical_units > protocol.budget.max_execution_attempts:
                raise ValueError("The physical proof attempt budget is exhausted before replay.")
            current["proof_work"] = {"reserved_evaluations": evaluator.stage_limits.proof_and_materialization,
                "execution_attempts": previous + physical_units}
            await repo.save(conn, current, bump=False)
    run_sync(with_connection(reserve_proof_work))
    if source_row is not None and source_row["status"]=="complete":
        result = run_sync(with_connection(saved_runs.read_result,source_row["id"]))
        source_id = source_row["id"]
    else:
        result = run_sessions(setup,tuple(et_date_at_ms(ms) for ms in protocol.final.session_ms),
            SnapshotRunData(setup.underlying,evaluator.snapshot,evaluator.blobs),should_stop=evaluator.check,
            on_session=lambda done,total,day: evaluator.work_progress(done*60))
        if source_row is None:
            source_id = run_sync(with_connection(saved_runs.create_run,saved_runs.NewRun(
                job_id=source_job,created_at_ms=now_ms_utc(),setup=setup,setup_name=row["setup_name"],
                first_session_open_ms=protocol.final.session_ms[0],last_session_open_ms=protocol.final.session_ms[-1],
                sessions_requested=len(protocol.final.session_ms))))
        else:
            source_id = source_row["id"]
        run_sync(with_connection(saved_runs.complete_run,source_id,result,finished_at_ms=now_ms_utc()))
    evaluation_key, _ = evaluator.key(setup, protocol.final, "base")
    selected_evidence = next((entry for entry in rows if entry["key"] == evaluation_key), None)
    if selected_evidence is None:
        raise ValueError("The selected final evaluation is missing from retained evidence.")
    scored = evaluator.load(selected_evidence["result"], protocol.final)
    if len(result.sessions) != len(scored.sessions) or result.setup != setup:
        raise ValueError("The ordinary source differs from the scored final setup or coverage.")
    for ordinary, evidence, pin in zip(result.sessions, scored.sessions, scored.pins, strict=True):
        if (ordinary.session_open_ms, ordinary.session_close_ms, ordinary.cash_before_usd, ordinary.outcome, ordinary.pins) != (
            evidence.session_open_ms, evidence.session_close_ms, evidence.cash_before_usd, evidence.outcome, pin
        ):
            raise ValueError("The ordinary source does not reproduce the scored final session outcomes and inputs.")
    evaluator.check()
    proof = build_proof(result,blobs=evaluator.blobs,
        source_data=SnapshotRunData(setup.underlying,evaluator.snapshot,evaluator.blobs),
        should_stop=evaluator.check,on_session=lambda done,total,day:evaluator.work_progress(done*60))
    evaluator.check()
    async def trial_records(conn):
        import json
        records = await conn.fetch("SELECT record FROM research_options_search_trials WHERE study_id=$1 ORDER BY id",row["id"])
        return [json.loads(r["record"]) for r in records]
    manifest = {"version":1,"study_id":row["id"],"protocol":protocol.model_dump(mode="json"),
        "snapshot":row["snapshot"],"selection":row["research"],"trials":run_sync(with_connection(trial_records)),
        "evaluations":rows,"finalists":row["finalists"],"final_results":row["final_results"],
        "procedure_outcomes":row["gate_outcomes"],"exposure":row["exposure"],
        "approved_point":digest,"selection_count":len(row["finalists"]),"exception":request.command=="approve_exception",
        "failed_rules":approval_failures(row,digest),"waived_gate_ids":list(request.waived_gate_ids),
        "comment":request.comment,"execution_proof_sha256":proof_sha256(proof)}
    manifest_hash = hash_payload(manifest)
    record = GoldenRecord(id="ogs_"+hash_payload({"study":row["id"],"command":request.idempotency_key})[:40],
        name=row["setup_name"],source_run_id=source_id,setup=setup.model_dump(mode="json"),setup_sha256=digest,
        rules_digest=proof["rules_digest"],proof=proof,proof_sha256=proof_sha256(proof),
        evidence={"options_search_study_id":row["id"],"study_manifest_sha256":manifest_hash,
            "verdict":"owner_exception" if request.command=="approve_exception" else "meets_rules",
            "failed_rules":manifest["failed_rules"],"waived_gate_ids":manifest["waived_gate_ids"],
            "selection_count":len(row["finalists"]),"exposure":row["exposure"],
            "metric_identity":protocol.identities["selection"],"execution_identity":dict(protocol.identities),
            "starting_cash_usd":str(setup.starting_cash_usd),
            "net_profit_usd":str(result.summary.net_profit_usd),
            "first_session_open_ms":result.sessions[0].session_open_ms,
            "last_session_open_ms":result.sessions[-1].session_open_ms,
            "sessions_tested":result.summary.sessions_tested,"trades":result.summary.trades,
            "slippage_percent":str(setup.slippage_percent),"limitations":[
                "Golden means owner-approved for this exact tested setup; search does not establish global optimality or future profitability.",
                *LIMITATIONS[1:]]},
        note=request.comment or "Approved exact Options Golden Search candidate.",approved_by="owner",created_at_ms=now_ms_utc(),
        command_id="search:"+row["id"]+":"+request.idempotency_key,command_sha256=hash_payload(request.model_dump(mode="json")))
    async def publish(conn):
        async with conn.transaction():
            current = await repo.fence(conn,row["id"],row["attempt"])
            if current["revision"]!=row["revision"]:
                raise repo.Conflict("Study changed during proof; nothing was approved.")
            await golden_repository.insert_record(conn,record)
            await conn.execute("""INSERT INTO research_options_search_approvals(study_id,qualification_id,manifest,manifest_sha256)
                VALUES ($1,$2,$3::jsonb,$4)""",row["id"],record.id,repo.dumps(manifest),manifest_hash)
            counters = await repo.counters(conn,row["id"])
            current["progress"] = {"stage":"approved","completed":counters["completed"],"queued":0,"running":0,
                "spent":counters["reserved"],"reserved":0,"cache_hits":evaluator.cache_hits,
                "execution_attempts":counters["execution_attempts"] + current.get("proof_work",{}).get("execution_attempts",0),
                "capacity_message":"Exact source and independent retained replay proof complete."}
            current.update(phase="approved",status="completed",qualification_id=record.id,qualification_status="ready",
                dispatch=None,stage_token=None,allowed_commands=[])
            return await repo.save(conn,current)
    evaluator.check()
    from app.research.options_golden_search.identity import execution_identity
    if execution_identity() != protocol.identities:
        raise ValueError("Options search rules changed during proof. Nothing was approved.")
    return run_sync(with_connection(publish))


def verify_recorded_study(row: dict[str, Any], evaluator) -> None:
    """Reconstruct selection and every gate from retained facts without authorizing new engine work."""
    import copy

    from app.research.options_golden_search import stages

    evaluator.verify_only = True
    try:
        rebuilt = stages.research(copy.deepcopy(row),evaluator)
        for field in ("research","candidates","gate_outcomes"):
            if hash_payload(rebuilt[field]) != hash_payload(row[field]):
                raise ValueError("Retained study facts do not reproduce the reviewed " + field + ".")
        rebuilt["finalists"] = row["finalists"]
        rebuilt = stages.final(rebuilt,evaluator)
        if hash_payload(rebuilt["final_results"]) != hash_payload(row["final_results"]):
            raise ValueError("Retained study facts do not reproduce the frozen final comparison.")
    finally:
        evaluator.verify_only = False
