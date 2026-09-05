"""Grid Search orchestration against the ephemeral database with a fake engine.

Parity with the engine boundary is structural (every cell is one
``execute_engine_backtest`` call) and pinned here by asserting the exact
request a cell builds from its receipt; the engine-level equivalence of two
identical requests is the engine's own contract.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from app.jobs.progress import JobCancelled
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.grid_search import engine_adapter, service
from app.research.grid_search import repository as repo
from app.research.grid_search.models import CellResult
from app.research.sweep.grid import RunSpec, ValueListRange
from app.research.sweep.identity import CodeIdentity
from tests._helpers.lean_store import seed_store_day

START, END = date(2025, 1, 6), date(2025, 1, 24)
SESSIONS = expected_sessions(START, END)
DAY_MS = 24 * 60 * 60 * 1000


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    for day in SESSIONS:
        seed_store_day(tmp_path, "SPY", day)
    return tmp_path


def _spec(**overrides) -> service.GridSearchSpec:
    base = dict(
        strategy_key="sma_crossover",
        symbol="SPY",
        param_ranges={"short_window": ValueListRange((2.0, 3.0)), "long_window": ValueListRange((5.0,)), "resolution_minutes": ValueListRange((60.0,))},
        start_ms=service.et_midnight_ms(START),
        end_ms=service.et_midnight_ms(END) + DAY_MS,
        measure="sharpe_ratio",
        min_trades=1,
    )
    base.update(overrides)
    return service.GridSearchSpec(**base)


def _fake_engine(sharpe_by_short: dict[float, float], *, failing: set[float] = frozenset()):
    def _execute(candidate: RunSpec) -> CellResult:
        short = candidate.params["short_window"]
        if short in failing:
            raise RuntimeError(f"boom {short}")
        return CellResult(params_hash=candidate.params_hash, params=dict(candidate.params), status="completed", total_trades=4, sharpe_ratio=sharpe_by_short[short], total_return_pct=1.0, net_profit=10.0)

    return _execute


# ── Window translation ───────────────────────────────────────────────────


def test_the_same_window_expressed_both_ways_selects_the_same_dates() -> None:
    start_ms = service.et_midnight_ms(START)
    end_ms = service.et_midnight_ms(END) + DAY_MS  # half-open: the day after END at ET midnight

    assert service.window_dates(start_ms, end_ms) == (START, END)
    # An end at exactly END's midnight excludes END; one ms later includes it.
    assert service.window_dates(start_ms, service.et_midnight_ms(END)) == (START, date(2025, 1, 23))
    assert service.window_dates(start_ms, service.et_midnight_ms(END) + 1) == (START, END)


# ── Preflight ────────────────────────────────────────────────────────────


def test_preflight_sizes_the_grid_and_plans_the_run_up(lake: Path) -> None:
    pre = service.preflight(_spec(), roots=[lake])

    assert pre.combinations == 2 and pre.total_backtests == 2
    # 60-minute cadence: SMA(5) needs 6 samples; a session holds 6 whole hours → one run-up session.
    assert pre.run_up.carved_from_range and pre.run_up.run_up_sessions == 1
    assert pre.evaluation_start == SESSIONS[1]
    assert pre.estimated_seconds > 0
    assert set(pre.param_ranges) == {"short_window", "long_window", "resolution_minutes"}


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"strategy_key": "nope"}, "UNKNOWN_STRATEGY"),
        ({"strategy_key": "deployment_validation"}, "STRATEGY_NOT_SWEEPABLE"),
        ({"param_ranges": {"short_window": ValueListRange((2.0, 2.0))}}, "GRID_INVALID"),
        ({"param_ranges": {"short_window": ValueListRange((2.0, 2.5))}}, "GRID_INVALID"),
        ({"param_ranges": {"bogus": ValueListRange((1.0,))}}, "GRID_INVALID"),
        ({"resolution": "daily"}, "RESOLUTION_UNSUPPORTED"),
    ],
)
def test_preflight_refuses_with_a_named_code(lake: Path, overrides: dict, code: str) -> None:
    with pytest.raises(service.GridSearchRefusal) as excinfo:
        service.preflight(_spec(**overrides), roots=[lake])
    assert excinfo.value.code == code


def test_preflight_refuses_an_oversized_workload_on_total_backtests(lake: Path) -> None:
    ranges = {"short_window": ValueListRange(tuple(float(v) for v in range(2, 52))), "long_window": ValueListRange(tuple(float(v) for v in range(60, 161)))}
    with pytest.raises(service.GridSearchRefusal) as excinfo:
        service.preflight(_spec(param_ranges=ranges), roots=[lake])
    assert excinfo.value.code == "WORKLOAD_LIMIT"
    assert "5050 backtests exceed the limit of 5000" in str(excinfo.value)


def test_preflight_refuses_a_window_with_missing_sessions_and_names_them(tmp_path: Path) -> None:
    for day in SESSIONS[:-2]:
        seed_store_day(tmp_path, "SPY", day)

    with pytest.raises(service.GridSearchRefusal) as excinfo:
        service.preflight(_spec(), roots=[tmp_path])

    assert excinfo.value.code == "DATA_MISSING"
    assert SESSIONS[-1].isoformat() in str(excinfo.value)


# ── Launch + execute ─────────────────────────────────────────────────────


async def test_launch_is_durable_and_execute_ranks_the_field(conn, lake: Path) -> None:
    record = service.prepare_launch(_spec(), job_id="job-1", roots=[lake])
    created = await service.create(record)
    assert created.status == "queued" and created.expected_cells == 2
    assert created.receipt["interval_table"]["warmup_policy"] == "uniform_run_up"
    assert len(created.receipt["data_snapshot"]["artifacts"]) == len(SESSIONS)

    outcome = await asyncio.to_thread(service.execute, created.id, job_id="job-1", execute_cell=_fake_engine({2.0: 0.5, 3.0: 1.5}))

    row = await repo.get_search(conn, created.id)
    cells = {cell.params["short_window"]: cell for cell in await repo.list_all_cells(conn, created.id)}
    assert outcome.status == "completed" and row is not None and row.status == "completed"
    assert row.leader_params_hash == cells[3.0].params_hash == outcome.leader_params_hash
    assert row.completed_cells == 2 and not row.incomplete


async def test_a_cell_builds_the_engine_request_from_its_receipt(conn, lake: Path) -> None:
    created = await service.create(service.prepare_launch(_spec(), job_id=None, roots=[lake]))
    spec = service.GridSearchSpec.from_request_dict(created.request)
    candidate = RunSpec(symbol="SPY", strategy_key="sma_crossover", params={"short_window": 2.0, "long_window": 5.0, "resolution_minutes": 60.0}, params_hash="x")

    request = engine_adapter.engine_request(created, spec, candidate)

    assert request.warmup_from_date == START.isoformat()
    assert request.from_date == SESSIONS[1].isoformat()
    assert request.to_date == END.isoformat()
    assert request.save_study is False and request.auto_fetch is False
    assert request.params == {"short_window": 2.0, "long_window": 5.0, "resolution_minutes": 60.0, "symbol": "SPY"}


async def test_cancellation_keeps_finished_cells_and_finish_runs_only_the_rest(conn, lake: Path) -> None:
    created = await service.create(service.prepare_launch(_spec(param_ranges={"short_window": ValueListRange((2.0, 3.0, 4.0))}), job_id="job-1", roots=[lake]))
    calls: list[float] = []

    def cancel_after_first_batch() -> None:
        if calls:
            raise JobCancelled("cancelled")

    def engine(candidate: RunSpec) -> CellResult:
        calls.append(candidate.params["short_window"])
        return _fake_engine({2.0: 1.0, 3.0: 2.0, 4.0: 3.0})(candidate)

    with pytest.raises(JobCancelled):
        await asyncio.to_thread(service.execute, created.id, job_id="job-1", execute_cell=engine, cancel_check=cancel_after_first_batch)

    row = await repo.get_search(conn, created.id)
    assert row is not None and row.status == "cancelled" and row.incomplete
    assert row.completed_cells == len(calls) == 3  # one 8-wide batch drained before the poll
    assert row.leader_params_hash is not None  # provisional leader over what finished

    # Finish: nothing left to run, but the record still needs its terminal status.
    finished_calls: list[float] = []

    def resumed_engine(candidate: RunSpec) -> CellResult:
        finished_calls.append(candidate.params["short_window"])
        return _fake_engine({2.0: 1.0, 3.0: 2.0, 4.0: 3.0})(candidate)

    outcome = await asyncio.to_thread(service.execute, created.id, job_id="job-2", execute_cell=resumed_engine)
    assert finished_calls == [] and outcome.status == "completed"
    resumed = await repo.get_search(conn, created.id)
    assert resumed is not None and resumed.status == "completed" and resumed.attempt == 2 and not resumed.incomplete


async def test_a_search_where_every_cell_failed_is_failed_with_the_reason(conn, lake: Path) -> None:
    created = await service.create(service.prepare_launch(_spec(), job_id=None, roots=[lake]))

    outcome = await asyncio.to_thread(service.execute, created.id, job_id=None, execute_cell=_fake_engine({}, failing={2.0, 3.0}))

    row = await repo.get_search(conn, created.id)
    assert outcome.status == "failed" and row is not None and row.status == "failed"
    assert row.failed_cells == 2 and "every combination failed" in (row.failure_reason or "")
    assert all("boom" in (cell.error or "") for cell in await repo.list_all_cells(conn, created.id))


async def test_a_zero_trade_cell_is_recorded_but_never_leads(conn, lake: Path) -> None:
    created = await service.create(service.prepare_launch(_spec(), job_id=None, roots=[lake]))

    def engine(candidate: RunSpec) -> CellResult:
        short = candidate.params["short_window"]
        trades = 0 if short == 3.0 else 4
        return CellResult(params_hash=candidate.params_hash, params=dict(candidate.params), status="completed", total_trades=trades, sharpe_ratio=9.0 if trades == 0 else 0.2)

    outcome = await asyncio.to_thread(service.execute, created.id, job_id=None, execute_cell=engine)

    cells = {cell.params["short_window"]: cell for cell in await repo.list_all_cells(conn, created.id)}
    assert outcome.leader_params_hash == cells[2.0].params_hash


# ── Presentation and Finish rules ────────────────────────────────────────


async def test_a_running_record_with_no_live_job_reads_back_as_interrupted(conn, lake: Path) -> None:
    created = await service.create(service.prepare_launch(_spec(), job_id="job-9", roots=[lake]))
    await repo.claim_attempt(conn, created.id, job_id="job-9")
    row = await repo.get_search(conn, created.id)
    assert row is not None

    assert service.presented_status(row, live=False) == "interrupted"
    assert service.presented_status(row, live=True) == "running"
    assert service.presented_status(row, live=None) == "running"  # Redis unreachable: reconcile, do not declare death


async def test_resume_refusals(conn, lake: Path, monkeypatch) -> None:
    clean = CodeIdentity(git_revision="h", tree_state="clean", source_digest="s" * 64, environment_digest="e" * 64)
    monkeypatch.setattr(service, "resolve_code_identity", lambda: clean)
    created = await service.create(service.prepare_launch(_spec(), job_id="job-1", roots=[lake]))
    await repo.claim_attempt(conn, created.id, job_id="job-1")
    row = await repo.get_search(conn, created.id)
    assert row is not None
    monkeypatch.setattr(service, "_roots_for", lambda row_: [lake])
    identity = CodeIdentity(**row.receipt["code_identity"])

    assert service.resume_refusal(row, live=True, identity=identity) == "the search is still running"
    assert service.resume_refusal(row, live=False, identity=identity) is None
    assert service.resume_refusal(row, live=False, identity=identity, verify_data=True) is None

    moved = CodeIdentity(git_revision=identity.git_revision, tree_state=identity.tree_state, source_digest="0" * 64, environment_digest=identity.environment_digest)
    assert "code changed" in (service.resume_refusal(row, live=False, identity=moved) or "")

    seed_store_day(lake, "SPY", SESSIONS[3], count=100)
    assert service.resume_refusal(row, live=False, identity=identity) is None  # detail view: cheap checks only
    assert "data artifact" in (service.resume_refusal(row, live=False, identity=identity, verify_data=True) or "")


async def test_a_search_from_a_dirty_tree_is_labelled_and_not_resumable(conn, lake: Path, monkeypatch) -> None:
    monkeypatch.setattr(service, "resolve_code_identity", lambda: CodeIdentity(git_revision="h", tree_state="dirty", source_digest="s" * 64, environment_digest="e" * 64))
    created = await service.create(service.prepare_launch(_spec(), job_id="job-1", roots=[lake]))
    await repo.claim_attempt(conn, created.id, job_id="job-1")
    row = await repo.get_search(conn, created.id)
    assert row is not None

    assert service.uncommitted_changes(row) is True
    assert "uncommitted changes" in (service.resume_refusal(row, live=False) or "")
