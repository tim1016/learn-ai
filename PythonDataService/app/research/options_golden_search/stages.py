"""Fenced Options research, forward, final and approval jobs (#3332, ADR 0080)."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.options_bots.golden_proof import setup_sha256
from app.research.golden_search.proof import BlobStore, default_blob_store
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search.dispatch import pins_from_wire, pins_to_wire
from app.research.options_golden_search.evaluator import EvaluationResult, evaluate_setup, with_input_failures
from app.research.options_golden_search.identity import execution_identity
from app.research.options_golden_search.metrics import (
    evidence_from_json,
    evidence_to_json,
    paired_daily_comparison,
    stitch_fold_evidence,
    summarize_evidence,
)
from app.research.options_golden_search.planning import budget_plan, pooled_window
from app.research.options_golden_search.policy import Metrics, classify
from app.research.options_golden_search.projections import candidate, exact_data, gates, rejected_candidate
from app.research.options_golden_search.protocol import (
    CostScenario,
    OptionsSearchProtocol,
    Window,
    apply_scenario,
    materialize,
)
from app.research.options_golden_search.search import Evaluation, SearchResult, ranking, run_search
from app.research.options_golden_search.service import allowed
from app.research.options_golden_search.snapshots import Snapshot, SnapshotRunData
from app.research.persistence.db import run_sync, with_connection
from app.research.runs.hashing import hash_payload

logger = logging.getLogger(__name__)

REJECTED_DIAGNOSTIC_LIMIT = 50


_SCALAR_METRICS = ("net_profit_usd", "max_drawdown_fraction", "sharpe", "sharpe_unavailable_reason",
    "daily_observation_count", "completed_traded_sessions", "max_recovery_sessions", "ongoing_recovery", "technical_failures")


def metric_summary(metrics: Any) -> dict[str, Any]:
    return {name: exact_data(getattr(metrics, name)) for name in _SCALAR_METRICS}


def summary_metrics(summary: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**{**summary, "net_profit_usd": Decimal(summary["net_profit_usd"]),
        "max_drawdown_fraction": Decimal(summary["max_drawdown_fraction"]),
        "technical_failures": tuple(summary["technical_failures"])})



class StudyEvaluator:
    """One durable reservation per exact evaluation; physical attempts are bounded separately."""

    def __init__(self, row: dict[str, Any], cancel: Callable[[], object], checkpoint: Callable[[int,int], None], *, blobs: BlobStore | None = None, on_child_checkpoint: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.row = row
        self.protocol = OptionsSearchProtocol.model_validate(row["protocol"])
        self.snapshot = Snapshot.model_validate(row["snapshot"])
        if self.snapshot.snapshot_id != self.protocol.snapshot_id:
            raise ValueError("Retained input inventory differs from the locked protocol.")
        self.blobs = blobs or default_blob_store()
        self.cancel = cancel
        self.on_child_checkpoint = on_child_checkpoint or (lambda message: None)
        self._work_cursor = 0
        self.checkpoint = checkpoint
        self.started = time.monotonic()
        self.cache_hits = 0
        self.verify_only = False
        self.stage_limits = budget_plan(self.protocol)

    def check(self) -> bool:
        self.cancel()
        from app.utils.timestamps import now_ms_utc
        deadline = self.row.get("execution_deadline_ms")
        expired = now_ms_utc() > deadline if deadline is not None else (time.monotonic()-self.started)*1000 > self.protocol.budget.max_elapsed_ms
        if expired:
            raise ValueError("The physical elapsed-time limit interrupted the study; work was not truncated.")
        return False

    def work_progress(self, cursor: int) -> None:
        if cursor % 60 == 0:
            self._work_cursor += 60
            from app.utils.timestamps import now_ms_utc
            self.on_child_checkpoint({"task_id": "options-serial", "sequence": self._work_cursor,
                "cursor": self._work_cursor, "cursor_kind": "processed_bars", "phase": "simulation",
                "occurred_at_ms": now_ms_utc(), "generation": self.row["attempt"]})

    def key(self, setup, window: Window, scenario: str) -> tuple[str, dict[str, Any]]:
        identity = {"protocol":self.row["protocol_hash"],"setup":setup.model_dump(mode="json"),
            "window":window.model_dump(mode="json"),"scenario":scenario,"snapshot":self.protocol.snapshot_id,
            "identities":self.protocol.identities}
        return hash_payload(identity), identity

    def _scope(self, window: Window) -> str:
        if window.key.startswith("training_"):
            return "training"
        if window.key.startswith("forward_"):
            return "forward"
        return "final" if window.key=="final" else "development"

    def load(self, stored: dict[str, Any], window: Window) -> EvaluationResult:
        payload = json.loads(self.blobs.get(stored["evidence_blob"]))
        sessions = tuple(evidence_from_json(s) for s in payload["sessions"])
        metrics = summarize_evidence(sessions,starting_capital_usd=self.protocol.setup.starting_cash_usd,
            annualization_sessions=window.annualization_sessions,risk_free_daily_return=self.protocol.policy.risk_free_daily_return)
        input_failures = tuple(payload.get("input_failures", ()))
        metrics = with_input_failures(metrics, input_failures)
        if hash_payload(exact_data(metrics)) != stored["metrics_sha256"] or metric_summary(metrics) != stored["metrics"]:
            raise ValueError("Retained trace no longer reproduces its recorded qualification metrics.")
        pins = pins_from_wire(payload["pins"])
        return EvaluationResult(sessions,pins,metrics,len(window.session_ms),input_failures)

    def __call__(self, setup, window: Window, scenario: str) -> Evaluation:
        return self.evaluate_batch([(setup,window,scenario)])[0]

    def evaluate_batch(self, requests: list[tuple[Any, Window, str]]) -> list[Evaluation]:
        from app.config import settings
        from app.research.golden_search.worker import WorkerDispatchContext, _child_identity
        from app.research.options_golden_search.dispatch import OptionsDispatch, ReadyOptions

        planned = []
        for setup, window, scenario in requests:
            self.check()
            key, identity = self.key(setup, window, scenario)
            cap = self.stage_limits.selection_total + self.stage_limits.baseline + self.stage_limits.forward
            if self.row["phase"] != "research":
                cap = self.protocol.budget.evaluation_cap - self.stage_limits.proof_and_materialization
            if self.verify_only:
                try:
                    recorded = run_sync(with_connection(repo.get_evaluation, self.row["id"], key))
                except LookupError as exc:
                    raise ValueError("Qualification verification requested unrecorded evaluation work.") from exc
                if recorded["identity"] != identity or recorded["result"] is None:
                    raise ValueError("Qualification verification needs the exact completed evaluation identity.")
                cached = recorded["result"]
            else:
                cached = run_sync(with_connection(repo.reserve, self.row["id"], self.row["attempt"], key, identity, cap=cap))
            planned.append((setup, window, scenario, key, cached))

        computed: dict[str, dict[str, Any]] = {}

        def persist(key: str, result: EvaluationResult, actual_reads: list[str], task_gen: int) -> None:
            payload = json.dumps({"sessions": [evidence_to_json(session) for session in result.sessions],
                "pins": pins_to_wire(result.pins), "input_failures": result.input_failures}, separators=(",", ":"), allow_nan=False).encode()
            counts = run_sync(with_connection(repo.counters, self.row["id"]))
            retained_bytes = counts["evidence_bytes"]
            if retained_bytes + len(payload) > self.protocol.budget.max_artifact_bytes:
                raise ValueError("The study evidence exceeds its retained-artifact byte limit.")
            record = {"evidence_blob": self.blobs.put(payload), "evidence_bytes": len(payload),
                      "metrics": metric_summary(result.metrics), "metrics_sha256": hash_payload(exact_data(result.metrics)),
                      "complete": len(result.sessions) == result.scheduled_sessions, "actual_reads": actual_reads}
            run_sync(with_connection(repo.complete, self.row["id"], self.row["attempt"], key, record, task_gen=task_gen))
            computed[key] = record
            async def report(conn):
                async with conn.transaction():
                    current = await repo.fence(conn, self.row["id"], self.row["attempt"])
                    current["progress"] = {"stage": self.row["phase"], "completed": counts["completed"] + 1,
                        "queued": max(0, len(pending)-len(computed)), "running": 0,
                        "cache_hits": self.cache_hits, "execution_attempts": counts["execution_attempts"],
                        "spent": counts["reserved"], "reserved": self.stage_limits.mandatory_downstream if self.row["phase"]=="research" else self.stage_limits.proof_and_materialization,
                        "capacity_message": "Bounded shared research capacity; width " + str(settings.OPTIONS_SEARCH_WORKER_COUNT)}
                    await repo.save(conn, current, bump=False)
            run_sync(with_connection(report))
            self.checkpoint(counts["reserved"], self.protocol.budget.evaluation_cap)

        pending = list({entry[3]: entry for entry in planned if entry[4] is None}.values())
        from app.services.research_runtime_client import is_research_runtime
        if pending and is_research_runtime():
            context = WorkerDispatchContext(strategy_key="options", execution={}, manifest={}, identity=_child_identity(),
                job_id=self.row.get("job_id") or "", generation=self.row["attempt"])
            context.cancel_check = self.check
            context.on_checkpoint = self.on_child_checkpoint
            dispatch = OptionsDispatch(context)
            items = []
            snapshot_wire = self.snapshot.model_dump(mode="json")
            window_wires: dict[str, dict[str, Any]] = {}
            for setup, window, scenario, key, _cached in pending:
                scenario_policy = next((item for item in self.protocol.stress if item.key == scenario), CostScenario(key="base"))
                stressed = apply_scenario(setup, scenario_policy)
                generation = [self.row.get("execution_generations", {}).get(key, 1)]

                def begin(key: str = key, generation: list[int] = generation) -> None:
                    self.check()
                    run_sync(with_connection(repo.begin_execution, self.row["id"], self.row["attempt"], key,
                        max_attempts=self.protocol.budget.max_execution_attempts, task_gen=generation[0]))

                def advance(next_gen: int, key: str = key, generation: list[int] = generation) -> None:
                    run_sync(with_connection(repo.advance_generation, self.row["id"], self.row["attempt"], key, next_gen))
                    generation[0] = next_gen

                if window.key not in window_wires:
                    window_wires[window.key] = window.model_dump(mode="json")
                items.append(ReadyOptions(key=key, attempt=self.row["attempt"], task_gen=generation[0],
                    payload={"setup": stressed.model_dump(mode="json"), "window": window_wires[window.key],
                        "snapshot": snapshot_wire, "blob_root": str(self.blobs.root),
                        "identity": self.protocol.identities, "risk_free_daily_return": self.protocol.policy.risk_free_daily_return,
                        "fee_multiplier": str(scenario_policy.effective_fee_multiplier)}, begin=begin, on_generation=advance))
            try:
                for delivery in dispatch(items):
                    self.check()
                    persist(delivery.key, delivery.result, list(delivery.actual_reads), delivery.task_gen)
            finally:
                dispatch.close()
        else:
            for setup, window, scenario, key, _cached in pending:
                scenario_policy = next((item for item in self.protocol.stress if item.key == scenario), CostScenario(key="base"))
                stressed = apply_scenario(setup, scenario_policy)
                data = SnapshotRunData(setup.underlying, self.snapshot, self.blobs)
                task_gen = self.row.get("execution_generations", {}).get(key, 1)
                run_sync(with_connection(repo.begin_execution, self.row["id"], self.row["attempt"], key,
                    max_attempts=self.protocol.budget.max_execution_attempts, task_gen=task_gen))
                from app.engine.run_gate import one_backtest_in_flight
                with one_backtest_in_flight(while_waiting=self.check):
                    result = evaluate_setup(stressed, window, data, risk_free_daily_return=self.protocol.policy.risk_free_daily_return,
                        fee_multiplier=scenario_policy.effective_fee_multiplier, should_stop=self.check, on_progress=self.work_progress)
                persist(key, result, sorted(data.actual_reads), task_gen)
                del result

        answers = []
        for setup, window, _scenario, key, cached in planned:
            if cached is not None:
                self.cache_hits += 1
            else:
                cached = computed[key]
            metrics = summary_metrics(cached["metrics"])
            verdict = classify(metrics, self.protocol.policy, window, self._scope(window), complete=cached["complete"])
            answers.append(Evaluation(point_hash=setup_sha256(setup), net_profit_usd=metrics.net_profit_usd,
                max_drawdown_fraction=metrics.max_drawdown_fraction, sharpe=metrics.sharpe,
                eligible=verdict.eligible, evidence_id=key))
        return answers

    def on_trial(self, trial, window: Window) -> None:
        if self.verify_only:
            return
        run_sync(with_connection(repo.trial,self.row["id"],self.row["attempt"],
            {"window":window.key,"trial":trial.model_dump(mode="json")}))

    def metrics_for(self, evaluations: list[Evaluation]) -> dict[str, tuple[Metrics, bool]]:
        self.check()
        stored = run_sync(with_connection(repo.evaluation_summaries, self.row["id"], [item.evidence_id for item in evaluations]))
        if set(stored) != {item.evidence_id for item in evaluations}:
            raise ValueError("A rejected Options evaluation has no retained metric summary.")
        self.check()
        return {key: (summary_metrics(record["metrics"]), record["complete"]) for key, record in stored.items()}

    def result_for(self, evaluation: Evaluation, window: Window) -> EvaluationResult:
        stored = run_sync(with_connection(repo.get_evaluation, self.row["id"], evaluation.evidence_id))
        if stored["result"] is None:
            raise ValueError("The selected Options evaluation has no complete retained evidence.")
        return self.load(stored["result"], window)


def selection_record(result: SearchResult, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Retain the frozen evidence shape when verifying studies recorded before bounded diagnostics."""
    value = result.model_dump(mode="json")
    if previous is not None and "rejected" in previous:
        value["rejected"] = [item.model_dump(mode="json") for item in result.rejected]
    return value


def research(row: dict[str, Any], evaluator: StudyEvaluator) -> dict[str, Any]:
    protocol = evaluator.protocol
    selection = run_search(protocol,protocol.development,evaluator)
    baseline = evaluator(protocol.setup,protocol.development,"base")
    selected = list(selection.eligible[:protocol.finalist_count])
    if not any(v.point_hash==baseline.point_hash for v in selected):
        selected.append(baseline)
    candidates = []
    for selected_eval in selected:
        result = evaluator.result_for(selected_eval,protocol.development)
        verdict = classify(result.metrics,protocol.policy,protocol.development,"development")
        candidates.append(candidate(result,verdict,selected_eval.point_hash,original=selected_eval.point_hash==baseline.point_hash))
    rejected = [item for item in selection.rejected if item.point_hash != baseline.point_hash]
    previous = row.get("research") if evaluator.verify_only else None
    if previous is not None:
        indexed = {item.point_hash: item for item in rejected}
        shown = [indexed[item["candidate_hash"]] for item in row["candidates"] if item["candidate_hash"] in indexed]
    else:
        shown = sorted(rejected, key=lambda item: ranking(item, protocol.policy.rank_by))[:REJECTED_DIAGNOSTIC_LIMIT]
    summaries = evaluator.metrics_for(shown) if shown else {}
    for item in shown:
        metrics, complete = summaries[item.evidence_id]
        verdict = classify(metrics, protocol.policy, protocol.development, "development", complete=complete)
        candidates.append(rejected_candidate(metrics, verdict, item.point_hash))
    folds = []
    outcomes = []
    forward_evidence = []
    for fold in protocol.folds:
        # Baseline is descriptive; it never changes fold selection.
        evaluator(protocol.setup,fold.training,"base")
        evaluator(protocol.setup,fold.forward,"base")
        fit = run_search(protocol,fold.training,evaluator)
        item: dict[str, Any] = {"index":fold.index,"selection":selection_record(fit, previous["folds"][fold.index]["selection"] if previous else None)}
        if fit.winner is None:
            item.update(status="no_eligible_point",forward=None)
            outcomes.append({"gate_id":f"forward_{fold.index}:selection","status":"does_not_meet_rules","actual":None,"required":"eligible training winner","reason":"No setting met the training rules. This is not a zero-return fold."})
        else:
            winner_setup = materialize(protocol,fit.points[fit.winner.point_hash])
            train_result = evaluator.result_for(fit.winner,fold.training)
            train_verdict = classify(train_result.metrics,protocol.policy,fold.training,"training")
            forward = evaluator(winner_setup,fold.forward,"base")
            result = evaluator.result_for(forward,fold.forward)
            verdict = classify(result.metrics,protocol.policy,fold.forward,"forward")
            outcomes.extend(gates(train_verdict,fold.training.key)+gates(verdict,fold.forward.key))
            forward_evidence.append(result.sessions)
            item.update(status="evaluated",forward=forward.model_dump(mode="json"),winner_hash=fit.winner.point_hash)
        folds.append(item)
    if len(forward_evidence)==len(protocol.folds):
        pooled = stitch_fold_evidence(forward_evidence,starting_capital_usd=protocol.setup.starting_cash_usd)
        window = pooled_window(protocol)
        metrics = summarize_evidence(pooled,starting_capital_usd=protocol.setup.starting_cash_usd,
            annualization_sessions=window.annualization_sessions,risk_free_daily_return=protocol.policy.risk_free_daily_return)
        outcomes.extend(gates(classify(metrics,protocol.policy,window,"pooled_forward")))
    else:
        outcomes.append({"gate_id":"pooled_forward:coverage","status":"not_enough_evidence","actual":str(len(forward_evidence)),"required":str(len(protocol.folds)),"reason":"Procedure evidence requires every declared forward fold."})
    row.update(phase="compare",status="completed",candidates=candidates,gate_outcomes=outcomes,rejected_candidate_count=len(rejected),
        research={"selection":selection_record(selection, previous["selection"] if previous else None),"folds":folds},dispatch=None,stage_token=None)
    return row


def final(row: dict[str, Any], evaluator: StudyEvaluator, *, on_result: Callable[[list[dict[str, Any]]], None] = lambda results: None) -> dict[str, Any]:
    protocol = evaluator.protocol
    points = row["research"]["selection"]["points"]
    outcomes = []
    batch_evaluate = getattr(evaluator,"evaluate_batch",None)
    if batch_evaluate is not None:
        fixed_batch = [(protocol.setup,protocol.final,"base")]
        for digest in row["finalists"]:
            setup = protocol.setup if digest==setup_sha256(protocol.setup) else materialize(protocol,points[digest])
            fixed_batch.extend((setup,protocol.final,scenario) for scenario in ("base", *(s.key for s in protocol.stress)))
        batch_evaluate(fixed_batch)
    baseline_evaluation = evaluator(protocol.setup, protocol.final, "base")
    baseline_result = evaluator.result_for(baseline_evaluation, protocol.final)
    for digest in row["finalists"]:
        setup = protocol.setup if digest==setup_sha256(protocol.setup) else materialize(protocol,points[digest])
        evaluation = evaluator(setup,protocol.final,"base")
        result = evaluator.result_for(evaluation,protocol.final)
        verdict = classify(result.metrics,protocol.policy,protocol.final,"final")
        view = candidate(result,verdict,digest,original=digest==setup_sha256(protocol.setup))
        paired = paired_daily_comparison(result.metrics, baseline_result.metrics)
        view["evidence"].extend([
            {"label": "Final comparison", "value": f"Chosen after final test from {len(row['finalists'])} distinct frozen candidates; this is a multiple-candidate comparison."},
            {"label": "Paired sessions with original", "value": str(paired.paired_common_sessions)},
            {"label": "Paired net difference from original", "value": str(paired.paired_net_difference_usd) + " USD" if paired.paired_net_difference_usd is not None else paired.reason or "Unavailable"},
            {"label": "Improvement over original", "value": "Higher final net profit than original." if paired.candidate_beats_baseline else "No demonstrated improvement." if paired.candidate_beats_baseline is False else paired.reason or "Unavailable"},
        ])
        for scenario in protocol.stress:
            stressed = evaluator(setup,protocol.final,scenario.key)
            stress_result = evaluator.result_for(stressed,protocol.final)
            stress_verdict = classify(stress_result.metrics,protocol.policy,protocol.final,"stress")
            view["evidence"].append({"label":"Cost stress "+scenario.key,"value":str(stress_result.metrics.net_profit_usd)+" USD; "+stress_verdict.status})
            # Technical inability to execute a mandatory stress replay is never a performance exception.
            if protocol.policy.stress_gate or stress_verdict.status=="could_not_evaluate":
                view["gate_outcomes"].extend(gates(stress_verdict,"final:"+scenario.key))
        statuses = [v["status"] for v in view["gate_outcomes"]]
        for status in ("could_not_evaluate","does_not_meet_rules","not_enough_evidence","meets_rules"):
            if status in statuses:
                view["verdict"] = status
                break
        outcomes.append(view)
        on_result(list(outcomes))
    row.update(phase="approval",status="completed",final_results=outcomes,dispatch=None,stage_token=None)
    return row


def run(study_id: str, token: str, job_id: str, *, cancel: Callable[[], object], checkpoint: Callable[[int,int], None], on_child_checkpoint: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    row = run_sync(with_connection(repo.claim,study_id,token,job_id))
    try:
        protocol = OptionsSearchProtocol.model_validate(row["protocol"])
        if execution_identity()!=protocol.identities:
            raise ValueError("The executable identity differs from the locked study.")
        evaluator = StudyEvaluator(row,cancel,checkpoint,on_child_checkpoint=on_child_checkpoint)
        if row["phase"]=="research":
            row = research(row,evaluator)
        elif row["phase"]=="final":
            def retain_partial(outcomes: list[dict[str, Any]]) -> None:
                async def persist_partial(conn):
                    async with conn.transaction():
                        current = await repo.fence(conn,row["id"],row["attempt"])
                        current["final_results"] = outcomes
                        await repo.save(conn,current,bump=False)
                run_sync(with_connection(persist_partial))
            row = final(row,evaluator,on_result=retain_partial)
        elif row["phase"]=="approval":
            from app.engine.run_gate import one_backtest_in_flight
            from app.research.options_golden_search.approval import approve
            with one_backtest_in_flight(while_waiting=evaluator.check):
                return approve(row,evaluator)
        else:
            raise ValueError("This phase has no authorized computation.")
        row["progress"] = {"stage":row["phase"],"completed":len(run_sync(with_connection(repo.evaluations,study_id))),
            "cache_hits":evaluator.cache_hits,"reserved":evaluator.stage_limits.proof_and_materialization if row["phase"]=="approval" else 0}
        row["allowed_commands"] = allowed(row)
        if execution_identity() != protocol.identities:
            raise ValueError("The executable identity changed during the stage; no stage transition was published.")
        async def finish(conn):
            async with conn.transaction():
                await repo.fence(conn,study_id,row["attempt"])
                return await repo.save(conn,row)
        return run_sync(with_connection(finish))
    except BaseException as exc:
        interruption_reason = str(exc)
        async def interrupt(conn):
            async with conn.transaction():
                current = await repo.fence(conn,study_id,row["attempt"])
                current.update(status="interrupted",failure_reason=interruption_reason,dispatch=None)
                current["allowed_commands"] = allowed(current)
                await repo.save(conn,current)
        from app.research.persistence.fence import StaleAttemptError
        try:
            run_sync(with_connection(interrupt))
        except StaleAttemptError:
            logger.info("Options stage result fenced by a newer transition", extra={"study_id": study_id})
        raise
