"""Golden Search study lifecycle against the ephemeral database, with a fake engine and a fake approval (#2696).

Live Postgres only (``POSTGRES_URL_IS_EPHEMERAL=1``). Every test seeds its
own thin lake under a symbol of its own, so the exposure ledger — global by
design — never couples two tests.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import date
from pathlib import Path

import asyncpg
import pytest
import redis

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.jobs.progress import JobCancelled
from app.research.golden_search import repository as repo
from app.research.golden_search import service
from app.research.golden_search.evaluator import (
    RETRY_ALLOWANCE,
    CapabilityError,
    EvaluationCapability,
    EvaluationRequest,
    EvaluationResult,
    StudyEvaluator,
)
from app.research.golden_search.models import GoldenSearchRefusal, StudyRow
from app.research.golden_search.planning import registry_incumbent
from app.research.golden_search.selection import Metrics
from app.research.golden_search.stages import StageRefusedError
from app.research.persistence import lifecycle
from app.research.persistence.fence import StaleAttemptError
from app.research.sweep.identity import CodeIdentity, resolve_code_identity
from tests._helpers.golden_search_study import (
    DEVELOPMENT,
    FINAL,
    Driver,
    FakeApproval,
    FakeEngine,
    SimulatedCrash,
    plan_request,
    seed_lake,
    smooth_score,
    unique_symbol,
    window_ms,
)

FINAL_START_MS = window_ms(FINAL)[0]


@pytest.fixture
def symbol() -> str:
    return unique_symbol()


@pytest.fixture
def lake(tmp_path: Path, symbol: str) -> Path:
    return seed_lake(tmp_path, symbol)


@pytest.fixture
def driver(lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection) -> Driver:
    # ``conn`` skips the suite unless an ephemeral database is attested; the study schema is ensured through it.
    # Finish re-hashes the receipted artifacts from the roots the receipt names.
    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [lake])
    return Driver(roots=[lake])


async def test_configured_frequency_survives_lock_selection_views_and_final_test(driver: Driver, symbol: str) -> None:
    row = await driver.to_candidate(symbol, rate=100)
    detail = await driver.detail(row)
    assert detail["activity"] == row.receipt["activity"]
    windows = {window["key"]: window for window in detail["activity"]["windows"]}
    assert windows["development"]["minimum_trades"] == 24
    assert windows["final"]["minimum_trades"] == 9
    assert all(fold["status"] == "completed" for fold in detail["results"]["validation"]["folds"])
    # A frequency plan's forward minimum is the one the receipt froze for all forward tests.
    assert (await service.test_over_time_charts(row.id))["forward_minimum"] == windows["forward"]["minimum_trades"]
    row = await driver.advance(row, "open_exam", {"acknowledge_final_test": True})
    check = next(item for item in row.results["exam"]["fits"][0]["checks"] if item["code"] == "SAMPLE_FLOOR")
    assert check["label"] == "Expected trade frequency"
    assert "minimum is 9" in check["detail"]


async def test_lifecycle_runs_from_lock_to_an_approved_golden_configuration(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    row = await driver.lock(symbol)
    detail = await driver.detail(row)
    assert detail["state"] == "locked" and detail["presented_status"] == "idle"
    # The Plan charts read the frozen windows and the receipt's estimate; nothing is used before the search.
    planned = await service.plan_step_charts(row.id)
    assert [window["kind"] for window in planned["windows"]][:2] == ["run_up", "development"]
    assert {stage["used"] for stage in planned["workload"]["stages"]} == {0} and planned["coverage"]["status"] in ("measured", "missing")
    assert detail["permitted_actions"] == ["continue", "run_research", "close", "revise"]
    assert detail["guidance"]["headline"] == "Ready to search"
    assert detail["scope"]["final_state"] == "locked"
    assert detail["scope"]["data_source"] == "Historical research: Polygon, split adjusted, regular sessions"
    assert detail["exposure_preview"] is None

    row = await driver.advance(row, "continue")
    detail = await driver.detail(row)
    assert detail["state"] == "awaiting_validation"
    search = detail["results"]["search"]
    assert [summary["knob"] for summary in search["knob_summary"]] == ["gap", "hold_bars"]
    assert search["winner"]["gap"] == pytest.approx(0.3, abs=1e-12) and search["winner"]["hold_bars"] == 7
    assert search["counts"]["evaluated"] == search["evaluations"]
    assert search["pair_maps"][0]["x_knob"] == "hold_bars" and len(search["pair_maps"][0]["cells"]) == 25
    # The Search charts replay the recorded path over the stored evaluations and end at the winner.
    searched = (await service.search_step_charts(row.id))["procedures"][0]
    tried = searched["convergence"]["tried"]
    assert searched["key"] == "search" and searched["convergence"]["status"] == "measured" and tried[0]["knob"] is None
    assert tried[-1]["best_so_far"] == pytest.approx(search["winner_metrics"]["sharpe_ratio"], abs=1e-9, rel=0)
    assert [(move["name"], move["retained"]) for move in searched["moves"]] == [("gap", pytest.approx(0.3, abs=1e-12)), ("hold_bars", 7)]
    assert sum(point["winner"] for point in searched["points"]) == 1 and len(searched["points"]) >= search["evaluations"]
    # Before testing over time runs, its charts show the receipt's planned folds and the search's winner.
    planned = await service.test_over_time_charts(row.id)
    assert planned["planned"] and [fold["status"] for fold in planned["folds"]] == ["planned", "planned"]
    assert [(fold["train_start_ms"], fold["test_end_ms"]) for fold in planned["folds"]] == [(fold["train_start_ms"], fold["test_end_ms"]) for fold in row.receipt["folds"]]
    assert planned["test_trades_total"] is None and planned["forward_minimum"] == row.protocol["policy"]["min_trades"]
    assert [(knob["name"], knob["all_period"], knob["folds"]) for knob in planned["drift"]] == [("gap", pytest.approx(0.3, abs=1e-12), [None, None]), ("hold_bars", 7, [None, None])]

    row = await driver.advance(row, "continue")
    detail = await driver.detail(row)
    assert detail["state"] == "awaiting_candidate"
    validation = detail["results"]["validation"]
    assert [fold["status"] for fold in validation["folds"]] == ["completed", "completed"]
    assert all(fold["incumbent_test_metrics"] is not None for fold in validation["folds"])
    # The charts read the stored folds: each fold's retention is the verdict's, and the trades add up to its count.
    charts = await service.test_over_time_charts(row.id)
    assert not charts["planned"] and charts["test_trades_total"] == validation["verdict"]["oos_trade_count"]
    assert charts["below_minimum"] is (charts["test_trades_total"] < charts["forward_minimum"])
    for fold, stored in zip(charts["folds"], validation["folds"], strict=True):
        train, test = stored["train_metrics"]["sharpe_ratio"], stored["test_metrics"]["sharpe_ratio"]
        assert fold["retention"] == (pytest.approx(test / train, abs=1e-9, rel=0) if train > 0 else None)
    assert charts["drift"][0]["folds"] == pytest.approx([fold["winner"]["gap"] for fold in validation["folds"]], abs=1e-12, rel=0)
    evidence = detail["results"]["evidence"]
    keys = [candidate["key"] for candidate in evidence["candidates"]]
    assert keys == ["incumbent", "all_period", "recent"]

    # The lock box says what opening the test would record, before it is opened.
    assert detail["exposure_preview"]["state"] == "not_opened"
    # Both fits chose one set of settings, so the lock names one run for the two of them.
    assert evidence["fits_to_test"] == [{"key": "all_period", "same_as": ["recent"], "label": "All-period and recent fit"}]
    row = await driver.advance(row, "open_exam", {"acknowledge_final_test": True})
    detail = await driver.detail(row)
    assert detail["state"] == "awaiting_review" and row.candidate_key is None
    assert detail["exposure_preview"] is None
    exam = detail["results"]["exam"]
    # Both fits chose the same settings here, so one result stands for both and there is no choice to note.
    (fit,) = exam["fits"]
    assert (fit["key"], fit["same_as"], fit["label"]) == ("all_period", ["recent"], "All-period and recent fit")
    assert (exam["exposure_state"], exam["claim"], fit["outcome"]) == ("not_opened", "confirmatory", "meets_rules")
    assert (exam["pick"], exam["chosen_key"], exam["selection_note"]) == ("after_test", None, None)
    assert detail["exam_outcome"] == "meets_rules"
    assert detail["guidance"]["headline"] == "Approve the settings you want to use"
    assert detail["scope"]["final_state"] == "opened_once"

    row = await driver.advance(row, "approve", {**APPROVE, "note": "Meets the rules."})
    detail = await driver.detail(row)
    assert detail["state"] == "approved" and row.candidate_key == "all_period"
    assert detail["results"]["exam"]["chosen_key"] == "all_period"
    qualification = detail["results"]["qualification"]
    assert qualification["status"] == "ready" and qualification["deploy"]["symbol"] == symbol
    assert "symbol" not in qualification["deploy"]["parameters"]
    assert detail["consumed_evaluations"] <= row.receipt["estimate"]["total_max"]
    assert await repo.consumed_outside_evaluator(conn, row.id, "proof") == 3
    # Every run the study used lands on the stage that planned it: pair audits under the search, the proof's draw under the proof.
    used = {stage["stage"]: stage["used"] for stage in (await service.plan_step_charts(row.id))["workload"]["stages"]}
    assert used["proof"] == 3 and sum(used.values()) == detail["consumed_evaluations"]


APPROVE = {
    "candidate_key": "all_period",
    "note": "Reviewed.",
    "acknowledge_missing_parity": True,
    "acknowledge_research_weakness": False,
    "expected_default_qualification_id": None,
}


# ── Commands: idempotency, revision, permissions ─────────────────────────


async def test_lock_with_the_same_key_returns_the_same_study_and_a_new_plan_conflicts(driver: Driver, symbol: str) -> None:
    key = driver.key()
    first = await service.lock_study(plan_request(symbol), idempotency_key=key, roots=driver.roots)
    again = await service.lock_study(plan_request(symbol), idempotency_key=key, roots=driver.roots)
    assert again.id == first.id and again.revision == first.revision

    with pytest.raises(GoldenSearchRefusal) as refused:
        await service.lock_study(plan_request(symbol, budget_cap=4000), idempotency_key=key, roots=driver.roots)
    assert (refused.value.code, refused.value.kind) == ("IDEMPOTENCY_CONFLICT", "conflict")


async def test_a_repeated_command_returns_its_outcome_and_a_stale_or_reused_one_is_refused(driver: Driver, symbol: str) -> None:
    row = await driver.lock(symbol)
    key = driver.key()
    first = await driver.command(row, "continue", idempotency_key=key)
    replay = await driver.command(row, "continue", idempotency_key=key)
    assert replay.replayed and replay.study.revision == first.study.revision == row.revision + 1
    # The stage still waits for its worker, so the original dispatch is offered again.
    assert replay.dispatch == first.dispatch

    with pytest.raises(GoldenSearchRefusal) as reused:
        await driver.command(row, "close", {"note": "x"}, idempotency_key=key)
    assert reused.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(GoldenSearchRefusal) as stale:
        await driver.command(row, "close", {"note": "x"})
    assert stale.value.code == "STALE_REVISION" and stale.value.study is not None and stale.value.study.revision == first.study.revision

    await driver.run(first)
    after = await driver.command(row, "continue", idempotency_key=key)
    assert after.replayed and after.dispatch is None  # the stage was claimed; its dispatch is spent


async def test_a_command_the_state_does_not_permit_is_refused_with_the_views_reason(driver: Driver, symbol: str) -> None:
    row = await driver.lock(symbol)
    detail = await driver.detail(row)
    with pytest.raises(GoldenSearchRefusal) as refused:
        await driver.command(row, "open_exam", {"acknowledge_final_test": True})
    assert refused.value.code == "COMMAND_NOT_PERMITTED"
    assert str(refused.value) == detail["action_refusals"]["open_exam"]


async def test_an_unclaimed_stage_reads_queued_cannot_be_finished_and_cancel_then_finish_redispatches(driver: Driver, symbol: str) -> None:
    row = await driver.lock(symbol)
    outcome = await driver.command(row, "continue")
    detail = await driver.detail(outcome.study)
    assert detail["presented_status"] == "queued" and "finish" not in detail["permitted_actions"]
    assert "waiting for its worker" in detail["action_refusals"]["finish"]
    assert "cancel" in detail["permitted_actions"]

    cancelled = await driver.command(outcome.study, "cancel")
    assert cancelled.study.status == "cancelled" and cancelled.study.stage_token is None
    with pytest.raises(GoldenSearchRefusal) as stale_token:
        await service.bind_dispatch(row.id, stage_token=outcome.dispatch["payload"]["stage_token"], job_id="job-old")
    assert stale_token.value.code == "STAGE_TOKEN_MISMATCH"

    finished = await driver.command(cancelled.study, "finish")
    assert finished.dispatch is not None and finished.study.status == "queued"
    token = finished.dispatch["payload"]["stage_token"]
    assert await service.bind_dispatch(row.id, stage_token=token, job_id="job-a") == "bound"
    assert await service.bind_dispatch(row.id, stage_token=token, job_id="job-a") == "redelivery"
    with pytest.raises(GoldenSearchRefusal) as taken:
        await service.bind_dispatch(row.id, stage_token=token, job_id="job-b")
    assert taken.value.code == "NOTHING_PENDING"


async def test_cancelling_a_bound_stage_asks_its_worker_and_an_unreachable_job_store_records_nothing(
    conn: asyncpg.Connection, driver: Driver, symbol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = await driver.lock(symbol)
    outcome = await driver.command(row, "continue")
    await service.bind_dispatch(row.id, stage_token=outcome.dispatch["payload"]["stage_token"], job_id="job-live")
    driver.live = True
    bound = await service.get_row(row.id)

    def unreachable(job_id: str) -> None:
        raise redis.ConnectionError("down")

    monkeypatch.setattr(lifecycle, "request_cancel", unreachable)
    key = driver.key()
    with pytest.raises(GoldenSearchRefusal) as refused:
        await driver.command(bound, "cancel", idempotency_key=key)
    assert (refused.value.code, refused.value.kind) == ("JOB_STORE_UNREACHABLE", "unavailable")
    assert (await service.get_row(row.id)).revision == bound.revision
    assert await repo.get_command(conn, row.id, key) is None

    asked: list[str] = []
    monkeypatch.setattr(lifecycle, "request_cancel", asked.append)
    cancelled = await driver.command(bound, "cancel", idempotency_key=key)
    assert asked == ["job-live"] and cancelled.study.status == "queued"  # the worker acknowledges by cancelling


# ── Run research (#2811, ADR 0074 decision 1 amendment) ─────────────────


async def test_run_research_locks_and_reaches_compare_in_one_job_spending_what_stepping_spends(driver: Driver, symbol: str) -> None:
    stepped = await driver.to_candidate(symbol)
    outcome = await driver.lock_and_run(symbol)
    assert outcome.dispatch is not None
    assert (outcome.study.state, outcome.study.status, outcome.study.run_to_compare) == ("search_running", "queued", True)

    stage = await driver.run(outcome, job_id="job-research")
    done = await service.get_row(outcome.study.id)
    assert (stage.stage, done.state, done.status) == ("validation", "awaiting_candidate", "completed")
    # One job ran both stages, one attempt each, and stopped at Compare: no candidate, no final test.
    assert (done.job_id, done.attempt, done.candidate_key, done.exam_locked) == ("job-research", 2, None, False)
    assert done.consumed_evaluations == stepped.consumed_evaluations
    assert done.results["validation"]["verdict"] == stepped.results["validation"]["verdict"]
    assert [item["key"] for item in done.results["evidence"]["candidates"]] == [item["key"] for item in stepped.results["evidence"]["candidates"]]
    # The evidence stage stored each candidate's concentration from its detail run, and the summary reads it.
    summaries = (await driver.detail(done))["decision_summaries"]
    assert [summary["candidate_key"] for summary in summaries] == [item["key"] for item in done.results["evidence"]["candidates"]]
    for item, summary in zip(done.results["evidence"]["candidates"], summaries, strict=True):
        measure = item["concentration"]
        # Two trades of 70% and 30%: without the best one, 30% of the net profit is left.
        assert measure["status"] == "meets" and len(measure["best_trades"]) == 1
        assert measure["without_best_trades"] == pytest.approx(0.3 * measure["net_profit"], abs=0.01, rel=0)
        row = next(row for row in summary["rows"] if row["key"] == "concentration")
        assert row["status"] == "meets" and row["text"].startswith("Still profitable without its best month")


async def test_a_retried_run_research_resolves_to_the_same_study_and_run(driver: Driver, symbol: str) -> None:
    key = driver.key()
    first = await driver.lock_and_run(symbol, key=key)
    again = await driver.lock_and_run(symbol, key=key)
    # A lost answer: the retry finds the same study and is offered the same dispatch while no worker holds it.
    assert again.study.id == first.study.id and again.dispatch == first.dispatch and again.replayed
    assert first.dispatch is not None
    await service.bind_dispatch(first.study.id, stage_token=first.dispatch["payload"]["stage_token"], job_id="job-once")
    claimed = await driver.lock_and_run(symbol, key=key)
    assert claimed.study.id == first.study.id and claimed.dispatch is None


async def test_a_run_whose_job_never_started_is_authorized_again_and_the_lost_token_is_withdrawn(driver: Driver, symbol: str) -> None:
    lost = await driver.lock_and_run(symbol)
    assert lost.dispatch is not None
    queued = await driver.detail(lost.study)
    assert queued["presented_status"] == "queued" and "run_research" in queued["permitted_actions"]
    assert "Start research again" in queued["guidance"]["detail"]

    again = await driver.command(lost.study, "run_research")
    assert again.dispatch is not None and again.dispatch != lost.dispatch
    with pytest.raises(GoldenSearchRefusal) as withdrawn:
        await service.bind_dispatch(lost.study.id, stage_token=lost.dispatch["payload"]["stage_token"], job_id="job-late")
    assert withdrawn.value.code == "STAGE_TOKEN_MISMATCH"
    await driver.run(again)
    assert (await service.get_row(lost.study.id)).state == "awaiting_candidate"


def test_the_run_a_lock_starts_is_keyed_apart_from_every_lock_key() -> None:
    # Lock keys are global, so this is checked on the derivation rather than by locking a fixed key twice.
    for key in ("run-research-at-lock", "k-1", "x" * 200):
        derived = service._run_at_lock_key(key)
        assert derived != key and derived.startswith("run-research:") and derived == service._run_at_lock_key(key)


async def test_a_cancel_that_lands_before_the_search_closes_leaves_the_study_paused(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    outcome = await driver.lock_and_run(symbol)
    # The owner's Cancel clears the intent under the row lock; the worker reads it there when the search closes.
    await conn.execute("UPDATE research_golden_search_studies SET run_to_compare = FALSE WHERE id = $1", outcome.study.id)

    stage = await driver.run(outcome)
    row = await service.get_row(outcome.study.id)
    assert (stage.stage, row.state, row.status, row.pending_stage) == ("search", "awaiting_validation", "completed", None)


async def test_an_unfinished_search_pauses_the_run_and_says_why(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    outcome = await driver.lock_and_run(symbol)
    await conn.execute("UPDATE research_golden_search_studies SET budget_cap = 11 WHERE id = $1", outcome.study.id)

    await driver.run(outcome)
    row = await service.get_row(outcome.study.id)
    assert (row.state, row.incomplete, row.run_to_compare) == ("awaiting_validation", True, True)
    assert (await driver.detail(row))["guidance"]["headline"] == "Research paused after the search"


async def test_an_interrupted_run_keeps_its_intent_and_finish_resumes_it_through_compare(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    outcome = await driver.lock_and_run(symbol)
    assert outcome.dispatch is not None
    token = outcome.dispatch["payload"]["stage_token"]
    await service.bind_dispatch(outcome.study.id, stage_token=token, job_id="job-lost")
    # The worker claims the stage and its process dies: the attempt stays running with no live job.
    await repo.claim_stage(conn, outcome.study.id, stage_token=token, job_id="job-lost")
    interrupted = await driver.detail(await service.get_row(outcome.study.id))
    assert interrupted["presented_status"] == "interrupted" and interrupted["run_to_compare"]
    assert interrupted["guidance"]["headline"] == "Your research was interrupted"
    assert {"finish", "run_research"} <= set(interrupted["permitted_actions"])

    resumed = await driver.command(await service.get_row(outcome.study.id), "finish")
    await driver.run(resumed)
    assert (await service.get_row(outcome.study.id)).state == "awaiting_candidate"


async def test_cancel_ends_the_run_and_only_an_explicit_resume_runs_on_to_compare(driver: Driver, symbol: str, monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = await driver.lock_and_run(symbol)
    assert outcome.dispatch is not None
    await service.bind_dispatch(outcome.study.id, stage_token=outcome.dispatch["payload"]["stage_token"], job_id="job-cancel")
    driver.live = True
    monkeypatch.setattr(lifecycle, "request_cancel", lambda job_id: None)
    cancelled = await driver.command(await service.get_row(outcome.study.id), "cancel")
    assert not cancelled.study.run_to_compare

    def cancelled_flag() -> None:
        raise JobCancelled("cancelled")

    with pytest.raises(JobCancelled):
        await driver.run(outcome, job_id="job-cancel", cancel_check=cancelled_flag)
    driver.live = False
    stopped = await service.get_row(outcome.study.id)
    assert (stopped.state, stopped.status, stopped.run_to_compare) == ("search_running", "cancelled", False)

    resumed = await driver.command(stopped, "run_research")
    assert resumed.study.run_to_compare and resumed.dispatch is not None
    await driver.run(resumed)
    assert (await service.get_row(outcome.study.id)).state == "awaiting_candidate"


async def test_a_superseded_worker_cannot_hand_the_search_on(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    outcome = await driver.lock_and_run(symbol)
    assert outcome.dispatch is not None
    token = outcome.dispatch["payload"]["stage_token"]
    await service.bind_dispatch(outcome.study.id, stage_token=token, job_id="job-old")
    _, attempt = await repo.claim_stage(conn, outcome.study.id, stage_token=token, job_id="job-old")
    await conn.execute("UPDATE research_golden_search_studies SET attempt = attempt + 1 WHERE id = $1", outcome.study.id)

    with pytest.raises(StaleAttemptError):
        await repo.finish_or_advance(conn, outcome.study.id, attempt, changes={"status": "completed"}, advance={"pending_stage": "validation"})
    row = await service.get_row(outcome.study.id)
    assert (row.state, row.pending_stage) == ("search_running", "search")


# ── Budget, crash and Finish ─────────────────────────────────────────────


async def test_a_budget_reached_mid_stage_keeps_evidence_marks_it_incomplete_and_never_overspends(
    conn: asyncpg.Connection, driver: Driver, symbol: str
) -> None:
    row = await driver.lock(symbol)
    # A cap tighter than the plan's bound (lock refuses that); development stages may use cap - 6.
    await conn.execute("UPDATE research_golden_search_studies SET budget_cap = 12 WHERE id = $1", row.id)
    row = await driver.advance(await service.get_row(row.id), "continue")

    assert row.state == "awaiting_validation" and row.consumed_evaluations == 6
    detail = await driver.detail(row)
    search = detail["results"]["search"]
    assert search["stop_reason"] == "budget" and search["incomplete"] and detail["incomplete"]
    # The seed, three gap values, then two hold values before the seventh evaluation found no budget.
    assert search["counts"]["evaluated"] == 6
    assert all(item["stop_explanation"] == "Budget reached" for item in search["knob_summary"])
    assert all(cell["status"] == "untested" for cell in search["pair_maps"][0]["cells"] if cell["status"] != "invalid")
    assert detail["results"]["recent"]["stop_reason"] == "budget"
    assert await conn.fetchval("SELECT COUNT(*) FROM research_golden_search_evaluations WHERE study_id = $1", row.id) == 6


async def test_finish_after_cancel_replays_from_the_cache_without_spending_twice(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    reference = await driver.advance(await driver.lock(symbol), "continue")
    row = await driver.lock(symbol)
    outcome = await driver.command(row, "continue")
    calls = 0

    def cancel_after_five() -> None:
        nonlocal calls
        calls += 1
        if calls > 5:
            raise JobCancelled("cancelled")

    with pytest.raises(JobCancelled):
        await driver.run(outcome, cancel_check=cancel_after_five)
    stopped = await service.get_row(row.id)
    assert (stopped.state, stopped.status, stopped.incomplete) == ("search_running", "cancelled", True)
    spent = stopped.consumed_evaluations

    resumed = await driver.command(stopped, "finish")
    await driver.run(resumed)
    done = await service.get_row(row.id)
    assert done.state == "awaiting_validation"
    assert done.consumed_evaluations == reference.consumed_evaluations
    # Every evaluation the cancelled attempt recorded came back from the cache.
    assert done.cache_hits >= reference.cache_hits + spent
    assert done.results["search"]["procedure"] == reference.results["search"]["procedure"]


async def test_a_crash_between_reservation_and_result_reruns_that_evaluation_without_new_budget(
    conn: asyncpg.Connection, driver: Driver, symbol: str
) -> None:
    reference = await driver.advance(await driver.lock(symbol), "continue")
    row = await driver.lock(symbol)
    outcome = await driver.command(row, "continue")
    driver.engine = FakeEngine(crash_on_call=4)
    with pytest.raises(BaseException) as crashed:
        await driver.run(outcome)
    assert type(crashed.value).__name__ == "SimulatedCrash"
    pending = await conn.fetch("SELECT evaluation_key FROM research_golden_search_evaluations WHERE study_id = $1 AND status = 'pending'", row.id)
    assert len(pending) == 1
    crashed_row = await service.get_row(row.id)
    assert crashed_row.status == "running" and crashed_row.consumed_evaluations == 4

    detail = await driver.detail(crashed_row)  # the worker is gone: interrupted
    assert detail["presented_status"] == "interrupted" and "finish" in detail["permitted_actions"]
    driver.engine.crash_on_call = None
    await driver.run(await driver.command(crashed_row, "finish"))
    done = await service.get_row(row.id)
    assert done.consumed_evaluations == reference.consumed_evaluations
    retried = await conn.fetchrow(
        "SELECT status, retries FROM research_golden_search_evaluations WHERE study_id = $1 AND evaluation_key = $2",
        row.id,
        pending[0]["evaluation_key"],
    )
    assert (retried["status"], retried["retries"]) == ("completed", 1)


def _moved_identity() -> CodeIdentity:
    return dataclasses.replace(resolve_code_identity(), source_digest="0" * 64)


async def test_no_stage_starts_under_code_other_than_the_study_was_locked_with(driver: Driver, symbol: str) -> None:
    row = await driver.lock(symbol)
    moved = _moved_identity()

    detail = await service.detail(row, liveness=driver.liveness, identity=moved)
    assert "continue" not in detail["permitted_actions"]
    assert "code changed since launch" in detail["action_refusals"]["continue"]
    with pytest.raises(GoldenSearchRefusal) as refused:
        await driver.command(row, "continue", identity=moved)
    assert refused.value.code == "COMMAND_NOT_PERMITTED" and "code changed since launch" in str(refused.value)

    # A stage authorized under the locked code but claimed by a process running other code scores nothing.
    outcome = await driver.command(row, "continue")
    with pytest.raises(StageRefusedError):
        await driver.run(outcome, identity=moved)
    assert driver.engine.calls == []
    stopped = await service.get_row(row.id)
    assert (stopped.state, stopped.status, stopped.consumed_evaluations) == ("search_running", "failed", 0)
    assert "code changed since launch" in (stopped.failure_reason or "")
    refusals = (await service.detail(stopped, liveness=driver.liveness, identity=moved))["action_refusals"]
    assert "code changed since launch" in refusals["finish"]

    # Back on the locked code, Finish runs the stage.
    await driver.run(await driver.command(stopped, "finish"))
    assert (await service.get_row(row.id)).state == "awaiting_validation"


async def test_closing_a_stopped_stage_seals_its_attempt_against_a_worker_still_running(
    conn: asyncpg.Connection, driver: Driver, symbol: str
) -> None:
    row = await driver.lock(symbol)
    outcome = await driver.command(row, "continue")
    driver.engine = FakeEngine(crash_on_call=3)
    with pytest.raises(BaseException):
        await driver.run(outcome)
    interrupted = await service.get_row(row.id)
    assert (await driver.detail(interrupted))["presented_status"] == "interrupted"

    closed = (await driver.command(interrupted, "close", {"note": "Abandoned."})).study
    assert (closed.state, (await driver.detail(closed))["presented_status"]) == ("closed", "completed")
    # The attempt that was presented as interrupted cannot write again: not a result, not a state.
    with pytest.raises(StaleAttemptError):
        await repo.update_study_fenced(conn, row.id, interrupted.attempt, changes={"state": "awaiting_validation"})
    pending = await conn.fetchval("SELECT evaluation_key FROM research_golden_search_evaluations WHERE study_id = $1 AND status = 'pending'", row.id)
    with pytest.raises(StaleAttemptError):
        await repo.complete_evaluation(conn, row.id, interrupted.attempt, pending, metrics=Metrics.failed("late"), detail=None)
    assert (await service.get_row(row.id)).state == "closed"


async def test_an_evaluation_that_keeps_killing_its_worker_is_recorded_failed_after_its_retries(
    conn: asyncpg.Connection, driver: Driver, symbol: str
) -> None:
    row = await driver.lock(symbol)
    seed = row.protocol["seed"]

    def dies_on_the_seed(request: EvaluationRequest) -> EvaluationResult:
        if request.point == seed and request.stage == "search":
            raise SimulatedCrash("the worker died mid-evaluation")
        return driver.engine(request)

    outcome = await driver.command(row, "continue")
    for _ in range(RETRY_ALLOWANCE + 1):
        with pytest.raises(SimulatedCrash):
            await driver.run(outcome, execute=dies_on_the_seed)
        outcome = await driver.command(await service.get_row(row.id), "finish")
    await driver.run(outcome, execute=dies_on_the_seed)  # the allowance is spent: the seed is not dispatched again

    done = await service.get_row(row.id)
    seeds = await conn.fetch(
        "SELECT status, retries, error FROM research_golden_search_evaluations WHERE study_id = $1 AND stage = 'search' AND point_json = $2::jsonb",
        row.id,
        json.dumps(seed),
    )
    assert [(item["status"], item["retries"], item["error"]) for item in seeds] == [("failed", RETRY_ALLOWANCE, repo.RETRY_EXHAUSTED)]
    assert done.state == "awaiting_validation"
    assert done.consumed_evaluations == await conn.fetchval("SELECT COUNT(*) FROM research_golden_search_evaluations WHERE study_id = $1", row.id)


async def test_a_cancel_mid_evaluation_never_spends_the_retry_allowance(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    reference = await driver.advance(await driver.lock(symbol), "continue")
    row = await driver.lock(symbol)

    def cancelled_while_waiting(request: EvaluationRequest) -> EvaluationResult:
        raise JobCancelled("cancelled while waiting for the engine gate")

    outcome = await driver.command(row, "continue")
    for _ in range(RETRY_ALLOWANCE + 1):
        with pytest.raises(JobCancelled):
            await driver.run(outcome, execute=cancelled_while_waiting)
        outcome = await driver.command(await service.get_row(row.id), "finish")
    await driver.run(outcome)

    done = await service.get_row(row.id)
    assert done.results["search"]["procedure"] == reference.results["search"]["procedure"]
    assert done.consumed_evaluations == reference.consumed_evaluations
    assert await conn.fetchval(
        "SELECT MAX(retries) FROM research_golden_search_evaluations WHERE study_id = $1", row.id
    ) == 0


async def test_a_superseded_attempt_can_neither_record_nor_spend(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    row = await driver.lock(symbol)
    driver.engine = FakeEngine(crash_on_call=3)
    with pytest.raises(SimulatedCrash):
        await driver.run(await driver.command(row, "continue"))
    stale = await service.get_row(row.id)
    pending = await conn.fetchval("SELECT evaluation_key FROM research_golden_search_evaluations WHERE study_id = $1 AND status = 'pending'", row.id)

    resumed = await driver.command(stale, "finish")
    token = resumed.dispatch["payload"]["stage_token"]
    await service.bind_dispatch(row.id, stage_token=token, job_id="job-next")
    _, attempt = await repo.claim_stage(conn, row.id, stage_token=token, job_id="job-next")
    assert attempt == stale.attempt + 1

    old = _evaluator(stale, stale.attempt, driver)
    calls = len(driver.engine.calls)
    with pytest.raises(StaleAttemptError):
        await asyncio.to_thread(old.evaluate, [{**stale.protocol["seed"], "gap": 0.55}], window=_development(stale), stage="search")
    with pytest.raises(StaleAttemptError):
        await repo.complete_evaluation(conn, row.id, stale.attempt, pending, metrics=Metrics.failed("late"), detail=None)
    assert len(driver.engine.calls) == calls
    assert (await service.get_row(row.id)).consumed_evaluations == stale.consumed_evaluations


async def test_a_spent_development_budget_still_leaves_the_exam_and_proof_their_reservation(
    conn: asyncpg.Connection, driver: Driver, symbol: str
) -> None:
    # Two different fits, so the final test needs all three of its reserved runs.
    driver.engine.score = _recent_prefers_a_wide_gap
    row = await driver.to_candidate(symbol)
    assert row.receipt["estimate"]["reserved_for_exam_and_proof"] == 6
    # A cap of exactly what development spent plus the reserve: 3 for the final test's incumbent and two fits, 3 for the proof.
    spent = row.consumed_evaluations
    await conn.execute("UPDATE research_golden_search_studies SET budget_cap = $2 WHERE id = $1", row.id, spent + 6)

    row = await driver.advance(await service.get_row(row.id), "open_exam", {"acknowledge_final_test": True})
    fits = row.results["exam"]["fits"]
    assert len(fits) == 2 and (row.state, row.consumed_evaluations) == ("awaiting_review", spent + 3)
    assert all(fit["metrics"]["status"] == "completed" for fit in fits)

    row = await driver.advance(row, "approve", APPROVE)
    assert (row.state, row.consumed_evaluations, row.budget_cap) == ("approved", spent + 6, spent + 6)


async def test_development_stages_never_spend_the_exam_and_proof_reservation(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    row = await driver.lock(symbol)
    # The plan's bound fits 5000; a cap of 12 leaves the development stages 6 and holds 3 + 3 back.
    await conn.execute("UPDATE research_golden_search_studies SET budget_cap = 12 WHERE id = $1", row.id)
    row = await driver.advance(await service.get_row(row.id), "continue")
    row = await driver.advance(row, "continue")

    assert (row.state, row.consumed_evaluations) == ("awaiting_candidate", 6)
    assert all(fold["failure_code"] == "BUDGET" for fold in row.results["validation"]["folds"])


# ── Leakage and capability ───────────────────────────────────────────────


async def test_results_after_a_folds_training_window_never_change_its_selection(driver: Driver, symbol: str) -> None:
    base = await driver.to_candidate(symbol)
    folds = base.results["validation"]["folds"]
    cut = folds[0]["train_end_ms"]

    def later_flipped(point: dict, window: tuple[int, int], scenario: str) -> float:
        # From fold 0's training end on, the landscape prefers a wide gap instead.
        return smooth_score(point, window, scenario) if window[0] < cut else 2.0 - 8.0 * (float(point.get("gap", 0.2)) - 0.6) ** 2

    mutated_driver = Driver(roots=driver.roots)
    mutated_driver.engine.score = later_flipped
    mutated = await mutated_driver.to_candidate(symbol)
    other = mutated.results["validation"]["folds"]

    assert other[0]["winner"] == folds[0]["winner"] and other[0]["train_metrics"] == folds[0]["train_metrics"]
    assert other[1]["winner"] != folds[1]["winner"]  # the mutation reaches every later window


async def test_every_fold_searches_from_the_protocol_seed_never_from_the_all_period_winner(
    conn: asyncpg.Connection, driver: Driver, symbol: str
) -> None:
    row = await driver.to_candidate(symbol)
    seed_gap = row.protocol["seed"]["gap"]
    assert row.results["search"]["procedure"]["winner"]["gap"] != seed_gap  # the all-period search moved

    rounds = [trial for trial in await repo.list_trials(conn, row.id, kind="round") if trial["stage"] == "validation"]
    first_round = {}
    for trial in rounds:
        first_round.setdefault(trial["fold_index"], trial["payload"])
    assert sorted(first_round) == [0, 1]
    assert all((payload["knob"], payload["current_before"]) == ("gap", seed_gap) for payload in first_round.values())


async def test_final_interval_results_change_nothing_before_the_exam(driver: Driver, symbol: str) -> None:
    base = await driver.to_candidate(symbol)

    def final_flipped(point: dict, window: tuple[int, int], scenario: str) -> float:
        return -5.0 if window[0] >= FINAL_START_MS else smooth_score(point, window, scenario)

    mutated_driver = Driver(roots=driver.roots)
    mutated_driver.engine.score = final_flipped
    mutated = await mutated_driver.to_candidate(symbol)

    for key in ("search", "recent", "validation", "evidence"):
        assert mutated.results[key] == base.results[key], key
    assert all(request.window[1] <= FINAL_START_MS for request in mutated_driver.engine.calls)


async def test_the_capability_refuses_a_final_interval_evaluation_in_a_development_stage(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    row = await driver.lock(symbol)
    outcome = await driver.command(row, "continue")
    token = outcome.dispatch["payload"]["stage_token"]
    await service.bind_dispatch(row.id, stage_token=token, job_id="job-cap")
    claimed, attempt = await repo.claim_stage(conn, row.id, stage_token=token, job_id="job-cap")
    evaluator = _evaluator(claimed, attempt, driver)

    final = (claimed.protocol["final_start_ms"], claimed.protocol["final_end_ms"])
    with pytest.raises(CapabilityError):
        await asyncio.to_thread(evaluator.evaluate, [claimed.protocol["seed"]], window=final, stage="search")
    assert driver.engine.calls == []
    assert await conn.fetchval("SELECT COUNT(*) FROM research_golden_search_evaluations WHERE study_id = $1", row.id) == 0


async def test_a_proof_draw_lost_before_its_checkpoint_is_not_drawn_again_by_a_later_attempt(
    conn: asyncpg.Connection, driver: Driver, symbol: str
) -> None:
    row = await driver.lock(symbol)
    outcome = await driver.command(row, "continue")
    token = outcome.dispatch["payload"]["stage_token"]
    await service.bind_dispatch(row.id, stage_token=token, job_id="job-draw")
    claimed, attempt = await repo.claim_stage(conn, row.id, stage_token=token, job_id="job-draw")

    def draw(key: str, count: int):  # type: ignore[no-untyped-def]
        return repo.consume_budget(conn, row.id, attempt, count, limit=claimed.budget_cap, step="proof", once_key=key, retry_allowance=2)

    # The first attempt drew the proof, then died before saving its checkpoint; each retry reuses the draw,
    # until the step has been redone as often as allowed.
    assert [await draw("approval:proof", 2) for _ in range(4)] == ["drawn", "reused", "reused", "retries_spent"]
    assert await draw("approval:run", 1) == "drawn"

    assert await conn.fetchval("SELECT consumed_evaluations FROM research_golden_search_studies WHERE id = $1", row.id) == 3


# ── Exposure ─────────────────────────────────────────────────────────────


async def test_two_concurrent_final_tests_on_one_symbol_cannot_both_claim_an_unopened_interval(
    driver: Driver, symbol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await driver.to_candidate(symbol)
    second = await driver.to_candidate(symbol)
    read_ledger = repo.exposure_overlaps
    reads = 0

    async def first_claim_lingers(conn: asyncpg.Connection, **kwargs: object) -> repo.ExposureOverlaps:
        nonlocal reads
        reads += 1
        overlaps = await read_ledger(conn, **kwargs)  # type: ignore[arg-type]
        if reads == 1:
            # The other request arrives while this claim is read but not yet written.
            await asyncio.sleep(0.5)
        return overlaps

    monkeypatch.setattr(repo, "exposure_overlaps", first_claim_lingers)
    opened = await asyncio.gather(
        driver.command(first, "open_exam", {"acknowledge_final_test": True}),
        driver.command(second, "open_exam", {"acknowledge_final_test": True}),
    )
    claims = sorted((outcome.study.results["exam"]["exposure_state"], outcome.study.results["exam"]["claim"]) for outcome in opened)
    assert claims == [("not_opened", "confirmatory"), ("previously_used", "exploratory")]


async def test_outside_research_on_the_final_interval_makes_its_history_unknown(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    from app.research.backtest_runs import repository as backtest_repo
    from app.research.backtest_runs.records import record_from_payload
    from tests.research.backtest_runs.payloads import engine_payload

    row = await driver.to_candidate(symbol)
    await backtest_repo.insert_run(conn, record_from_payload(engine_payload(symbol=symbol, start_date="2025-04-07", end_date="2025-04-11")))

    row = await driver.advance(row, "open_exam", {"acknowledge_final_test": True})
    detail = await driver.detail(row)
    exam = detail["results"]["exam"]
    assert (exam["exposure_state"], exam["claim"]) == ("history_unknown", "exploratory")
    assert detail["guidance"]["headline"] == "This test's history is unknown"

    with pytest.raises(GoldenSearchRefusal) as weak:
        await driver.command(row, "approve", APPROVE)
    assert weak.value.code == "WEAKNESS_ACKNOWLEDGEMENT_REQUIRED"
    row = await driver.advance(row, "approve", {**APPROVE, "acknowledge_research_weakness": True})
    assert row.state == "approved"
    research = driver.approval.requests[-1].research
    assert research["research_override"] and research["weakness"] == ["EXPOSURE_HISTORY_UNKNOWN"]


async def test_exposure_rows_survive_hiding_the_study(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    row = await driver.advance(await driver.to_candidate(symbol), "open_exam", {"acknowledge_final_test": True})
    await service.hide(row.id, liveness=driver.liveness)

    assert [item["kind"] for item in await repo.list_exposures(conn, study_id=row.id)] == ["reserved", "result"]
    assert row.id not in [item["id"] for item in await service.summaries(symbol=symbol, liveness=driver.liveness)]
    hidden = await service.summaries(symbol=symbol, include_hidden=True, liveness=driver.liveness)
    assert any(item["id"] == row.id and item["hidden"] for item in hidden)


# ── Candidates, revision and approval outcomes ───────────────────────────


async def test_a_recent_fit_equal_to_the_all_period_fit_collapses_to_one_candidate(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    row = await driver.to_candidate(symbol)
    candidates = {item["key"]: item for item in (await driver.detail(row))["results"]["evidence"]["candidates"]}
    assert candidates["all_period"]["point_hash"] == candidates["recent"]["point_hash"]
    assert candidates["all_period"]["same_as"] == ["recent"] and candidates["recent"]["same_as"] == ["all_period"]
    details = await conn.fetchval(
        "SELECT COUNT(*) FROM research_golden_search_evaluations WHERE study_id = $1 AND detail AND point_hash = $2",
        row.id,
        candidates["recent"]["point_hash"],
    )
    assert details == 1  # one detail run serves both


RECENT_START_MS = window_ms((date(2025, 3, 1), DEVELOPMENT[1]))[0]


def _recent_prefers_a_wide_gap(point: dict, window: tuple[int, int], scenario: str) -> float:
    """The recent window alone peaks at gap 0.6, and on the final interval a wider gap does better, so both fits beat the incumbent."""
    gap = float(point.get("gap", 0.2))
    if window == (RECENT_START_MS, FINAL_START_MS):
        return 2.0 - 8.0 * (gap - 0.6) ** 2
    if window[0] >= FINAL_START_MS:
        return 1.0 + gap
    return smooth_score(point, window, scenario)


async def _two_fits_to_review(driver: Driver, symbol: str, score=_recent_prefers_a_wide_gap) -> StudyRow:  # type: ignore[no-untyped-def]
    driver.engine.score = score
    row = await driver.to_candidate(symbol)
    return await driver.advance(row, "open_exam", {"acknowledge_final_test": True})


async def test_both_fits_sit_the_final_test_and_the_owner_names_one_afterwards(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    row = await _two_fits_to_review(driver, symbol)
    detail = await driver.detail(row)
    exam = detail["results"]["exam"]
    assert [(fit["key"], fit["same_as"], fit["label"]) for fit in exam["fits"]] == [("all_period", [], "All-period fit"), ("recent", [], "Recent fit")]
    # The lock promised exactly these fits.
    assert [(fit["key"], fit["label"]) for fit in detail["results"]["evidence"]["fits_to_test"]] == [(fit["key"], fit["label"]) for fit in exam["fits"]]
    assert exam["fits"][0]["point"] != exam["fits"][1]["point"]
    # Each fit is judged on its own run; the incumbent ran once beside them.
    assert [fit["outcome"] for fit in exam["fits"]] == ["meets_rules", "meets_rules"]
    assert exam["fits"][0]["metrics"]["sharpe_ratio"] != exam["fits"][1]["metrics"]["sharpe_ratio"]
    assert (await service.runs(row.id, group="final"))["total"] == 3
    assert (exam["pick"], exam["chosen_key"]) == ("after_test", None)
    assert exam["selection_note"].startswith("2 fits ran on the final test.")
    # The interval is used once, with a reservation and a result for each fit.
    ledger = await repo.list_exposures(conn, study_id=row.id)
    assert sorted((item["kind"], item["payload"]["candidate_key"]) for item in ledger) == [
        ("reserved", "all_period"), ("reserved", "recent"), ("result", "all_period"), ("result", "recent"),
    ]
    assert (await service.exposure_view(symbol, *window_ms(FINAL)))["ledger_overlaps"] == 1
    for key in ("all_period", "recent", "incumbent"):
        assert (await service.candidate(row.id, key))["exam"] is not None

    with pytest.raises(GoldenSearchRefusal) as unnamed:
        await driver.command(row, "approve", {key: value for key, value in APPROVE.items() if key != "candidate_key"})
    assert (unnamed.value.code, unnamed.value.field) == ("PAYLOAD_INVALID", "candidate_key")
    with pytest.raises(GoldenSearchRefusal) as untested:
        await driver.command(row, "approve", {**APPROVE, "candidate_key": "incumbent"})
    assert untested.value.code == "PAYLOAD_INVALID"

    row = await driver.advance(row, "approve", {**APPROVE, "candidate_key": "recent"})
    detail = await driver.detail(row)
    assert (row.state, row.candidate_key) == ("approved", "recent")
    request = driver.approval.requests[-1]
    assert request.candidate_point == exam["fits"][1]["point"]
    assert (request.research["candidate_key"], request.research["fits_tested"], request.research["pick"]) == ("recent", 2, "after_test")
    assert detail["results"]["exam"]["selection_note"] == "Chosen after the final test, from 2 fits."
    assert detail["results"]["qualification"]["deploy"]["parameters"]["gap"] == exam["fits"][1]["point"]["gap"]
    picks = await repo.list_trials(conn, row.id, kind="pick")
    assert [trial["payload"]["candidate_key"] for trial in picks] == ["recent"]


async def test_a_fit_that_failed_the_final_test_needs_the_written_acceptance_and_the_other_does_not(driver: Driver, symbol: str) -> None:
    def wide_gap_loses_on_the_final_interval(point: dict, window: tuple[int, int], scenario: str) -> float:
        if window[0] >= FINAL_START_MS and float(point.get("gap", 0.2)) > 0.45:
            return -1.0
        return _recent_prefers_a_wide_gap(point, window, scenario)

    row = await _two_fits_to_review(driver, symbol, wide_gap_loses_on_the_final_interval)
    detail = await driver.detail(row)
    fits = {fit["key"]: fit for fit in detail["results"]["exam"]["fits"]}
    assert (fits["all_period"]["outcome"], fits["recent"]["outcome"]) == ("meets_rules", "does_not_meet_rules")
    assert fits["all_period"]["weakness"] == [] and [item["code"] for item in fits["recent"]["weakness"]] == ["EXAM_DOES_NOT_MEET_RULES"]
    # The study reads by its strongest result until a fit is named.
    assert detail["exam_outcome"] == "meets_rules" and detail["guidance"]["headline"] == "Approve the settings you want to use"

    with pytest.raises(GoldenSearchRefusal) as weak:
        await driver.command(row, "approve", {**APPROVE, "candidate_key": "recent"})
    assert weak.value.code == "WEAKNESS_ACKNOWLEDGEMENT_REQUIRED"
    row = await driver.advance(row, "approve", APPROVE)
    assert row.state == "approved" and driver.approval.requests[-1].research["weakness"] == []


async def test_a_study_kept_after_two_fits_ran_says_how_many_ran_and_promises_no_approval(driver: Driver, symbol: str) -> None:
    row = await _two_fits_to_review(driver, symbol)
    assert (await driver.detail(row))["results"]["exam"]["selection_note"].endswith("chosen after the final test, from 2 fits.")

    row = await driver.advance(row, "retain", {"kind": "keep_current", "note": "Neither is worth the change."})

    assert (await driver.detail(row))["results"]["exam"]["selection_note"] == "2 fits ran on the final test."


async def test_a_retried_approval_must_name_the_fit_it_started_with(driver: Driver, symbol: str) -> None:
    row = await _two_fits_to_review(driver, symbol)
    driver.approval = FakeApproval(fail_with=("PROOF_MISMATCH", "The restored replay differs from the lake replay."))
    row = await driver.advance(row, "approve", APPROVE)
    assert (row.state, row.candidate_key) == ("qualification_failed", "all_period")

    driver.approval = FakeApproval()
    with pytest.raises(GoldenSearchRefusal) as other:
        await driver.command(row, "approve", {**APPROVE, "candidate_key": "recent"})
    assert (other.value.code, other.value.kind) == ("FIT_ALREADY_CHOSEN", "conflict")
    row = await driver.advance(row, "approve", APPROVE)
    assert (row.state, row.candidate_key) == ("approved", "all_period")


# ── Every run keeps its curve ────────────────────────────────────────────


async def test_every_run_keeps_its_daily_curve_and_the_runs_read_groups_them(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    row = await _two_fits_to_review(driver, symbol)

    recorded = await conn.fetchrow(
        "SELECT COUNT(*) AS runs, COUNT(equity_close_ms) AS curves FROM research_golden_search_evaluations WHERE study_id = $1 AND status = 'completed'",
        row.id,
    )
    assert recorded["runs"] == recorded["curves"] > 0  # whatever step asked for it
    page = await service.runs(row.id, page_size=500)
    groups = {group["key"]: group for group in page["groups"]}
    assert set(groups) == {"search", "pair_audit", "recent", "fold_training", "fold_test", "candidate", "neighbor", "stress", "final"}
    assert all(group["runs"] == group["completed"] == group["with_curve"] for group in groups.values())
    assert page["total"] == sum(group["runs"] for group in groups.values()) == recorded["runs"] == len(page["rows"])

    final = (await service.runs(row.id, group="final", sort="sharpe_ratio", descending=True))["rows"]
    assert [run["label"] for run in final] == ["Final test · Recent fit", "Final test · All-period fit", "Final test · Current settings"]
    tests = (await service.runs(row.id, group="fold_test", fold_index=0))["rows"]
    # The fold's winner is the settings the all-period search also chose, so its run carries both names.
    assert sorted(run["label"] for run in tests) == ["Fold 1 test · All-period fit · Fold winner", "Fold 1 test · Current settings"]
    stressed = (await service.runs(row.id, group="stress"))["rows"]
    assert {run["scenario"] for run in stressed} == {"slippage_1c", "commission_1"} and "Extra $1 per order" in stressed[-1]["label"]

    # The stored curve is the one the candidate's detail read draws.
    recent = final[0]
    curves = await service.run_curves(row.id, [recent["evaluation_key"]])
    (curve,) = curves["curves"]
    detail = (await service.candidate(row.id, "recent"))["exam"]
    assert curve["close_ms"] == [point["ms"] for point in detail["cumulative_return"]]
    assert curve["cumulative_return"] == pytest.approx([point["value"] for point in detail["cumulative_return"]], abs=1e-12, rel=0)
    assert curve["drawdown"] == pytest.approx([point["drawdown"] for point in detail["drawdown"]], abs=1e-12, rel=0)
    assert curves["not_recorded"] == []
    assert curve["deepest_drawdown"] == min(point["drawdown"] for point in detail["drawdown"])


async def test_each_step_draws_its_own_runs_with_the_line_each_one_is(driver: Driver, symbol: str) -> None:
    row = await _two_fits_to_review(driver, symbol)

    search = await service.curve_set(row.id, curve_set="search")
    roles = [line["role"] for line in search["lines"]]
    assert roles.count("winner") == 1 and roles.count("incumbent") == 1 and roles.count("other") == len(roles) - 2
    winner = next(line for line in search["lines"] if line["role"] == "winner")
    assert winner["params_sentence"].startswith("Gap $0.30") and len(winner["curve"]["close_ms"]) == len(winner["curve"]["cumulative_return"]) > 0
    # Two lines never read the same: a scored point is told apart by its settings.
    assert len({(line["label"], line["params_sentence"]) for line in search["lines"]}) == len(search["lines"])
    assert (search["left_out"], search["not_recorded"], search["notes"]) == (0, 0, [])
    recent = await service.curve_set(row.id, curve_set="recent")
    assert [line["role"] for line in recent["lines"]].count("winner") == 1

    folds = await service.curve_set(row.id, curve_set="fold_tests")
    assert [line["label"] for line in folds["lines"]] == ["Fold 1 winner", "Fold 1 current settings", "Fold 2 winner", "Fold 2 current settings"]
    # Each fold's line covers its own test window and no other; none reaches the final interval.
    assert folds["lines"][0]["curve"]["close_ms"][-1] < folds["lines"][2]["curve"]["close_ms"][0]
    assert all(line["curve"]["close_ms"][-1] < row.protocol["final_start_ms"] for line in folds["lines"])

    candidate = await service.curve_set(row.id, curve_set="candidate", candidate_key="all_period")
    by_role: dict[str, list[str]] = {}
    for line in candidate["lines"]:
        by_role.setdefault(line["role"], []).append(line["label"])
        assert line["curve"]["close_ms"][-1] < row.protocol["final_start_ms"]  # development only, though the fit also sat the final test
    assert by_role["candidate"] == ["All-period fit"]
    assert sorted(by_role["stress"]) == ["Extra $1 per order", "Extra 1¢/share slippage"]
    # Gap and hold are searched: one step either side of each.
    assert sorted(by_role["neighbor"]) == sorted(
        f"{knob} one step {side} ({value:g})"
        for knob, values in (("Crossover gap", (0.25, 0.35)), ("Hold time", (6, 8)))
        for side, value in zip(("below", "above"), values, strict=True)
    )
    # The incumbent is a candidate too: its own run and its stresses, and no neighbors, because none were audited.
    incumbent = await service.curve_set(row.id, curve_set="candidate", candidate_key="incumbent")
    assert sorted({line["role"] for line in incumbent["lines"]}) == ["candidate", "stress"]

    for query in ({"curve_set": "candidate"}, {"curve_set": "search", "candidate_key": "recent"}):
        with pytest.raises(GoldenSearchRefusal) as refused:
            await service.curve_set(row.id, **query)
        assert refused.value.code == "RUNS_QUERY_INVALID"


async def test_a_step_drawn_while_its_runs_are_still_coming_counts_no_missing_curve(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    row = await driver.advance(await driver.lock(symbol), "continue")
    one = (await service.runs(row.id, group="search"))["rows"][-1]
    await conn.execute(
        "UPDATE research_golden_search_evaluations SET status = 'pending', equity_close_ms = NULL, equity_values = NULL WHERE study_id = $1 AND evaluation_key = $2",
        row.id,
        one["evaluation_key"],
    )

    search = await service.curve_set(row.id, curve_set="search")

    # The run in flight has no line yet, and is not reported as one that kept no curve.
    assert search["not_recorded"] == 0
    assert len(search["lines"]) == (await service.runs(row.id, group="search"))["total"] - 1


async def test_a_run_recorded_before_runs_kept_their_curve_reads_not_recorded(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    row = await driver.advance(await driver.lock(symbol), "continue")
    first, second = (await service.runs(row.id, group="search"))["rows"][:2]
    await conn.execute(
        "UPDATE research_golden_search_evaluations SET equity_close_ms = NULL, equity_values = NULL WHERE study_id = $1 AND evaluation_key = $2",
        row.id,
        first["evaluation_key"],
    )

    curves = await service.run_curves(row.id, [first["evaluation_key"], second["evaluation_key"]])

    assert curves["not_recorded"] == [first["evaluation_key"]]
    assert [curve["evaluation_key"] for curve in curves["curves"]] == [second["evaluation_key"]]
    listed = {run["evaluation_key"]: run["has_curve"] for run in (await service.runs(row.id, group="search"))["rows"]}
    assert (listed[first["evaluation_key"]], listed[second["evaluation_key"]]) == (False, True)
    with pytest.raises(GoldenSearchRefusal) as none_named:
        await service.run_curves(row.id, [])
    assert none_named.value.code == "RUNS_QUERY_INVALID"


async def test_fits_that_return_the_current_settings_leave_nothing_to_test(driver: Driver, symbol: str) -> None:
    def current_settings_win(point: dict, window: tuple[int, int], scenario: str) -> float:
        return 2.0 - 8.0 * (float(point.get("gap", 0.2)) - 0.2) ** 2 - ((float(point.get("hold_bars", 5)) - 5.0) / 4.0) ** 2

    driver.engine.score = current_settings_win
    row = await driver.to_candidate(symbol)
    detail = await driver.detail(row)
    assert detail["results"]["evidence"]["fits_to_test"] == []
    assert "open_exam" not in detail["permitted_actions"] and "retain" in detail["permitted_actions"]
    with pytest.raises(GoldenSearchRefusal) as refused:
        await driver.command(row, "open_exam", {"acknowledge_final_test": True})
    assert refused.value.code == "COMMAND_NOT_PERMITTED" and "nothing to test" in str(refused.value)


async def test_revise_forks_a_linked_study_and_leaves_the_original_untouched(driver: Driver, symbol: str) -> None:
    row = await driver.advance(await driver.lock(symbol), "continue")
    outcome = await driver.command(row, "revise", {"protocol": plan_request(symbol, budget_cap=4000)})
    original = await service.get_row(row.id)

    assert outcome.study.id != row.id and outcome.study.parent_study_id == row.id and outcome.study.state == "locked"
    assert (original.revision, original.state) == (row.revision, row.state)


async def test_a_revision_keeps_the_strategy_and_stock_of_the_study_it_revises(driver: Driver, symbol: str) -> None:
    row = await driver.lock(symbol)

    with pytest.raises(GoldenSearchRefusal) as refused:
        await driver.command(row, "revise", {"protocol": plan_request(unique_symbol(), budget_cap=4000)})

    assert refused.value.code == "REVISION_SUBJECT_CHANGED"
    assert (await service.get_row(row.id)).revision == row.revision


async def test_two_concurrent_revisions_under_one_key_fork_one_study(conn: asyncpg.Connection, driver: Driver, symbol: str) -> None:
    row = await driver.lock(symbol)
    key = driver.key()
    payload = {"protocol": plan_request(symbol, budget_cap=4000)}

    answers = await asyncio.gather(
        driver.command(row, "revise", payload, idempotency_key=key),
        driver.command(row, "revise", payload, idempotency_key=key),
        return_exceptions=True,
    )

    forks = [answer for answer in answers if isinstance(answer, service.CommandOutcome)]
    refused = [answer for answer in answers if not isinstance(answer, service.CommandOutcome)]
    assert forks and all(isinstance(item, GoldenSearchRefusal) and item.code == "IDEMPOTENCY_CONFLICT" for item in refused)
    children = await conn.fetch("SELECT id FROM research_golden_search_studies WHERE parent_study_id = $1", row.id)
    assert [child["id"] for child in children] == [forks[0].study.id]


async def test_a_technical_approval_failure_reads_back_failed_and_can_be_retried(driver: Driver, symbol: str) -> None:
    row = await driver.advance(await driver.to_candidate(symbol), "open_exam", {"acknowledge_final_test": True})
    driver.approval = FakeApproval(fail_with=("PROOF_MISMATCH", "The restored replay differs from the lake replay."))
    row = await driver.advance(row, "approve", APPROVE)
    detail = await driver.detail(row)
    assert row.state == "qualification_failed"
    assert detail["guidance"] == {
        "headline": "Qualification failed · current default unchanged",
        "detail": "The restored replay differs from the lake replay.",
    }
    assert detail["results"]["qualification"]["status"] == "failed" and "approve" in detail["permitted_actions"]

    driver.approval = FakeApproval()
    row = await driver.advance(row, "approve", APPROVE)
    assert row.state == "approved"
    # The retry resumed from the proof the failed attempt saved; only the saved run was new work.
    assert driver.approval.checkpoints[0].proof is not None


def _development(row: StudyRow) -> tuple[int, int]:
    return (row.protocol["development_start_ms"], row.protocol["development_end_ms"])


def _evaluator(row: StudyRow, attempt: int, driver: Driver) -> StudyEvaluator:
    development = _development(row)
    return StudyEvaluator(
        study_id=row.id,
        attempt=attempt,
        strategy_key=row.strategy_key,
        context_digest=row.receipt["context_digest"],
        run_up_sessions=row.receipt["run_up"]["run_up_sessions"],
        capability=EvaluationCapability(allowed=(development,)),
        budget_limit=row.budget_cap - 5,
        execute=driver.engine,
    )


# ── Defaults ─────────────────────────────────────────────────────────────


async def _no_lake_history(symbol: str) -> None:
    return None


async def test_defaults_offer_the_ready_default_qualification_as_the_incumbent(
    conn: asyncpg.Connection, driver: Driver, symbol: str, unique: str
) -> None:
    from app.research.backtest_runs import repository as backtest_repo
    from app.research.backtest_runs.records import record_from_payload
    from app.research.golden_search import qualifications
    from app.research.golden_search.declarations import canonical_point
    from app.research.golden_validation import service as golden_validation
    from tests.research.backtest_runs.payloads import engine_payload

    study = await driver.lock(symbol)
    run_id = (await backtest_repo.insert_run(conn, record_from_payload(engine_payload(symbol=symbol)))).run_id
    designated = await golden_validation.designate(
        conn, source_run_id=run_id, command_id=f"d-{unique}", label="Golden Search study", rationale="Approved.", actor="local:owner"
    )
    reviewed = await golden_validation.review(
        conn,
        golden_run_id=designated.golden_run.id,
        command_id=f"r-{unique}",
        expected_evidence_revision=designated.evidence.revision,
        decision="accept",
        reason="Approved.",
        quantconnect_backtest_id=None,
        authorized_program_version=None,
        actor="local:owner",
    )
    assert reviewed.latest_review is not None
    point = canonical_point("ema_crossover_signal", symbol, {"gap": 0.35, "hold_bars": 7})
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None
    await qualifications.insert_qualification(
        conn,
        qualification_id=f"q-{unique}",
        program_key="ema_crossover_signal",
        program_version=contract.program_version,
        parameter_schema_version="ema-crossover-signal-params/v3",
        symbol=symbol,
        params=point,
        artifact_digest="a" * 64,
        wiring_digest="w" * 64,
        study_id=study.id,
        golden_run_id=reviewed.golden_run.id,
        golden_review_id=reviewed.latest_review.id,
        proof={"schema_version": 1},
        research={"exam_outcome": "meets_rules"},
        note="Approved.",
        approved_by="local:owner",
        created_at_ms=1_000,
    )
    await qualifications.set_default_cas(
        conn,
        program_key="ema_crossover_signal",
        symbol=symbol,
        qualification_id=f"q-{unique}",
        expected_qualification_id=None,
        reason="approved",
        actor="local:owner",
        now_ms=1_000,
    )

    ready = await service.defaults(
        "ema_crossover_signal", symbol, lake_coverage=_no_lake_history, running_digest=lambda contract: "a" * 64
    )
    assert ready["incumbent"] == {"source": "qualification", "qualification_id": f"q-{unique}", "params": point}
    assert ready["incumbent_label"].startswith("Golden configuration")
    # The qualified tuple is the benchmark, never where the folds' searches start: it was chosen on
    # data those folds test. The plan starts from the registry point, and a plan seeded from it is refused.
    registry = registry_incumbent("ema_crossover_signal", symbol).params
    assert ready["seed"] == registry != point
    assert ready["exposure"]["state"] in ("not_opened", "previously_used", "history_unknown")
    plan = {key: value for key, value in ready.items() if key not in ("final_months", "final_sessions_cut", "range_note", "incumbent_label", "incumbent_sentence", "exposure")}
    leaked = await service.preflight({**plan, "seed": point}, roots=driver.roots)
    assert [item["code"] for item in leaked["refusals"]] == ["SEED_IS_QUALIFIED"]

    stale = await service.defaults(
        "ema_crossover_signal", symbol, lake_coverage=_no_lake_history, running_digest=lambda contract: "b" * 64
    )
    assert stale["incumbent"]["source"] == "registry"
    assert stale["incumbent_label"] == "Registry validated point (the Golden Search default is not ready)"


async def test_preflight_answers_refusals_as_data_with_the_run_up_and_exposure(driver: Driver, symbol: str) -> None:
    answer = await service.preflight(plan_request(symbol), roots=driver.roots)
    assert answer["refusals"] == []
    assert answer["run_up"]["run_up_sessions"] >= 1 and answer["estimate"]["budget_cap"] == 5000
    assert [fold["fold_index"] for fold in answer["folds"]] == [0, 1]
    assert answer["exposure"]["state"] == "not_opened"

    refused = await service.preflight(plan_request(symbol, budget_cap=10), roots=driver.roots)
    assert [item["code"] for item in refused["refusals"]] == ["WORKLOAD_LIMIT"] and refused["run_up"] is None


# ── Grid, failed folds, decisions and reads ──────────────────────────────


def _grid_plan(symbol: str) -> dict:
    request = plan_request(symbol, method="grid")
    for knob in request["knobs"]:
        if knob["name"] == "gap":
            knob["step"] = 0.1
        if knob["name"] == "hold_bars":
            knob["step"] = 2.0
    return request


async def test_a_grid_search_scores_every_listed_combination_once(driver: Driver, symbol: str) -> None:
    row = await service.lock_study(_grid_plan(symbol), idempotency_key=driver.key(), roots=driver.roots)
    row = await driver.advance(row, "continue")

    search = (await driver.detail(row))["results"]["search"]
    assert search["counts"] == {"evaluated": 7 * 6, "cached": 0, "invalid": 0}
    assert search["stop_explanation"].startswith("Tested every valid combination")
    assert {item["stop_explanation"] for item in search["knob_summary"]} == {"Every listed value tested"}
    # Hold 6 and 8 tie around the peak at 7; the canonical ranking breaks the tie by point hash.
    assert search["passes_completed"] == 1 and search["winner"]["hold_bars"] in (6, 8)


async def test_a_fold_without_an_eligible_training_winner_is_recorded_failed_never_skipped(driver: Driver, symbol: str) -> None:
    january = window_ms((DEVELOPMENT[0], DEVELOPMENT[0].replace(month=2)))

    def january_loses(point: dict, window: tuple[int, int], scenario: str) -> float:
        return -1.0 if window[1] <= january[1] + 3 * 24 * 3_600_000 and window[0] >= january[0] else smooth_score(point, window, scenario)

    driver.engine.score = january_loses
    row = await driver.to_candidate(symbol)
    validation = (await driver.detail(row))["results"]["validation"]

    first, second = validation["folds"]
    assert (first["status"], first["failure_code"], first["winner"]) == ("failed", "NO_ELIGIBLE_CANDIDATE", None)
    assert first["incumbent_test_metrics"] is not None  # the benchmark still ran on the test window
    assert second["status"] == "completed"
    assert validation["verdict"]["label"] == "could not be judged"
    assert validation["linked"][0]["linked_return"] is None and validation["linked"][1]["linked_return"] is None
    # Only the first fold is missing; the second completed but sits after the break.
    assert [point["fold_missing"] for point in validation["linked"]] == [True, False]


async def test_keeping_the_current_settings_ends_the_study_without_opening_the_final_test(driver: Driver, symbol: str) -> None:
    row = await driver.to_candidate(symbol)
    row = await driver.advance(row, "retain", {"kind": "keep_current", "note": "The fit is fragile."})
    detail = await driver.detail(row)

    assert detail["state"] == "retained" and detail["decision"]["kind"] == "keep_current"
    assert detail["guidance"]["detail"].endswith("The final test has not been opened.")
    assert detail["permitted_actions"] == ["revise"]
    with pytest.raises(GoldenSearchRefusal) as refused:
        await driver.command(row, "retain", {"kind": "maybe", "note": ""})
    assert refused.value.code == "COMMAND_NOT_PERMITTED"

    closed = await driver.advance(await driver.lock(symbol), "close", {"note": "Wrong plan."})
    assert closed.state == "closed" and (await driver.detail(closed))["guidance"]["headline"] == "Study closed"


async def test_candidate_detail_and_the_evaluation_ledger_read_back_after_the_exam(driver: Driver, symbol: str) -> None:
    row = await driver.advance(await driver.to_candidate(symbol), "open_exam", {"acknowledge_final_test": True})

    chosen = await service.candidate(row.id, "all_period")
    assert chosen["development"]["window"] == {"start_ms": row.protocol["development_start_ms"], "end_ms": row.protocol["development_end_ms"]}
    assert chosen["development"]["cumulative_return"] and chosen["development"]["monthly"]
    # Each fake trade enters an hour before it exits a minute before the close: four 15-minute bars close in between.
    charts = chosen["development"]["trade_charts"]
    assert charts["status"] == "measured" and [trade["bars_held"] for trade in charts["trades"]] == [4, 4]
    assert charts["entry_rsi"]["status"] == "measured" and chosen["exam"]["trade_charts"] is None
    assert chosen["exam"]["window"] == {"start_ms": row.protocol["final_start_ms"], "end_ms": row.protocol["final_end_ms"]}
    # The final test sits beside development, measure by measure, with the change.
    comparison = {item["key"]: item for item in chosen["exam"]["comparison"]}
    sharpe = comparison["sharpe_ratio"]
    assert sharpe["change"] == pytest.approx(sharpe["final"] - sharpe["development"], abs=1e-9, rel=0)
    assert chosen["development"]["comparison"] is None
    # Development's rate is over development's own trading years: the same as Compare's.
    compared = next(item for item in (await driver.detail(row))["results"]["evidence"]["candidates"] if item["key"] == "all_period")
    assert comparison["trades_per_year"]["development"] == pytest.approx(compared["trades_per_year"], abs=1e-9, rel=0)
    assert (await service.candidate(row.id, "recent"))["exam"] is not None  # the same point
    assert (await service.candidate(row.id, "incumbent"))["exam"] is not None  # the benchmark

    # One fit (both chose the same settings) and the incumbent.
    final = await service.runs(row.id, group="final")
    assert final["total"] == 2 and all(run["has_curve"] for run in final["rows"])
