"""Grid Search persistence: durable at launch, fenced by attempt, paged on the server."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from app.research.grid_search import repository as repo
from app.research.grid_search.models import CellResult, NewSearch, SearchOwner
from app.research.persistence import fence
from app.research.persistence.schema import SCHEMA_VERSION, ensure_schema

pytestmark = pytest.mark.asyncio


def _new_search(search_id: str, *, symbol: str, strategy_key: str = "sma_crossover", owner: SearchOwner | None = None, expected: int = 3) -> NewSearch:
    return NewSearch(
        id=search_id,
        strategy_key=strategy_key,
        symbol=symbol,
        request={
            "strategy_key": strategy_key,
            "symbol": symbol,
            "param_ranges": {"short_window": {"type": "value_list", "values": [5.0, 10.0]}},
            "start_ms": 1_736_139_600_000,
            "end_ms": 1_743_480_000_000,
            "measure": "sharpe_ratio",
            "min_trades": 5,
        },
        receipt={"code_identity": {"source_digest": "abc"}},
        expected_cells=expected,
        job_id=f"job-{search_id}",
        owner=owner or SearchOwner(),
    )


def _cell(params_hash: str, *, sharpe: float | None = 1.0, trades: int = 10, status: str = "completed") -> CellResult:
    return CellResult(
        params_hash=params_hash,
        params={"short_window": 5},
        status=status,  # type: ignore[arg-type]
        total_trades=trades,
        sharpe_ratio=sharpe,
        total_return_pct=2.0,
        net_profit=200.0,
    )


async def _finish(conn: asyncpg.Connection, search_id: str, attempt: int, status: str = "completed", *, incomplete: bool = False, winner: CellResult | None = None) -> None:
    await repo.finish_search(
        conn,
        search_id,
        attempt,
        status=status,  # type: ignore[arg-type]
        leader_params_hash=winner.params_hash if winner else None,
        leader_params=dict(winner.params) if winner else None,
        incomplete=incomplete,
        failure_reason=None,
    )


async def test_ensure_schema_is_idempotent_and_records_its_version(conn: asyncpg.Connection) -> None:
    await ensure_schema(conn)
    await ensure_schema(conn)

    versions = await conn.fetch("SELECT version FROM research_schema_migrations ORDER BY version")
    assert [row["version"] for row in versions] == list(range(1, SCHEMA_VERSION + 1))


async def test_a_search_is_listable_the_moment_it_is_created(conn: asyncpg.Connection, unique: str) -> None:
    created = await repo.create_search(conn, _new_search(f"s-{unique}", symbol=unique))

    listed = await repo.list_searches(conn, symbol=unique)
    assert [row.id for row in listed] == [f"s-{unique}"]
    assert created.status == "queued" and created.attempt == 0 and created.leader_params is None
    assert created.request["measure"] == "sharpe_ratio"
    assert created.receipt["code_identity"]["source_digest"] == "abc"


async def test_walk_forward_owned_sweeps_are_excluded_from_the_user_history_by_ownership(conn: asyncpg.Connection, unique: str) -> None:
    await repo.create_search(conn, _new_search(f"user-{unique}", symbol=unique))
    await repo.create_search(conn, _new_search(f"wf-{unique}", symbol=unique, owner=SearchOwner(kind="walk_forward", owner_id=f"study-{unique}", fold_index=0, phase="train")))

    assert [row.id for row in await repo.list_searches(conn, symbol=unique)] == [f"user-{unique}"]
    assert [row.id for row in await repo.list_searches(conn, owner_kind="walk_forward", owner_id=f"study-{unique}")] == [f"wf-{unique}"]
    fetched = await repo.get_search(conn, f"wf-{unique}")
    assert fetched is not None and fetched.owner.fold_index == 0 and fetched.owner.phase == "train"


async def test_writes_carry_the_attempt_and_a_stale_attempt_cannot_write(conn: asyncpg.Connection, unique: str) -> None:
    sid = f"s-{unique}"
    await repo.create_search(conn, _new_search(sid, symbol=unique))
    first = await repo.claim_attempt(conn, sid, job_id="job-1")
    await repo.write_cells(conn, sid, first, [_cell("a")])

    second = await repo.claim_attempt(conn, sid, job_id="job-2")  # the resumed run
    await repo.write_cells(conn, sid, second, [_cell("b")])

    assert (first, second) == (1, 2)
    with pytest.raises(fence.StaleAttemptError):
        await repo.write_cells(conn, sid, first, [_cell("a", sharpe=99.0)])
    with pytest.raises(fence.StaleAttemptError):
        await _finish(conn, sid, first)

    cells = {cell.params_hash: cell for cell in await repo.list_all_cells(conn, sid)}
    assert cells["a"].sharpe_ratio == 1.0 and cells["a"].attempt == 1
    assert cells["b"].attempt == 2
    row = await repo.get_search(conn, sid)
    assert row is not None and row.status == "running" and row.completed_cells == 2


async def test_the_fence_holds_across_two_sessions(conn: asyncpg.Connection, second_conn: asyncpg.Connection, unique: str) -> None:
    """Writer A holds attempt 1; B claims attempt 2 on another connection; A's next write is refused."""
    sid = f"s-{unique}"
    await repo.create_search(conn, _new_search(sid, symbol=unique))
    attempt_a = await repo.claim_attempt(conn, sid, job_id="job-a")
    await repo.write_cells(conn, sid, attempt_a, [_cell("a")])

    attempt_b = await repo.claim_attempt(second_conn, sid, job_id="job-b")
    assert attempt_b == attempt_a + 1

    with pytest.raises(fence.StaleAttemptError):
        await repo.write_cells(conn, sid, attempt_a, [_cell("c")])
    await repo.write_cells(second_conn, sid, attempt_b, [_cell("c")])
    assert {cell.params_hash: cell.attempt for cell in await repo.list_all_cells(conn, sid)} == {"a": attempt_a, "c": attempt_b}


async def test_a_lock_holder_blocks_a_concurrent_claim_until_it_commits(conn: asyncpg.Connection, second_conn: asyncpg.Connection, unique: str) -> None:
    """The row lock inside write_cells serializes against claim_attempt on another session."""
    sid = f"s-{unique}"
    await repo.create_search(conn, _new_search(sid, symbol=unique))
    attempt = await repo.claim_attempt(conn, sid, job_id="job-a")

    tx = conn.transaction()
    await tx.start()
    await conn.execute("SELECT attempt FROM research_grid_searches WHERE id = $1 FOR UPDATE", sid)
    claim = asyncio.create_task(repo.claim_attempt(second_conn, sid, job_id="job-b"))
    await asyncio.sleep(0.2)
    assert not claim.done()  # blocked behind A's lock
    await tx.commit()
    assert await claim == attempt + 1


async def test_rewriting_a_cell_overwrites_rather_than_appends(conn: asyncpg.Connection, unique: str) -> None:
    sid = f"s-{unique}"
    await repo.create_search(conn, _new_search(sid, symbol=unique))
    attempt = await repo.claim_attempt(conn, sid, job_id=None)
    await repo.write_cells(conn, sid, attempt, [_cell("a", sharpe=1.0), _cell("b", status="failed", sharpe=None)])
    await repo.write_cells(conn, sid, attempt, [_cell("a", sharpe=3.0)])

    cells = await repo.list_all_cells(conn, sid)
    row = await repo.get_search(conn, sid)

    assert [(cell.params_hash, cell.sharpe_ratio) for cell in cells] == [("a", 3.0), ("b", None)]
    assert row is not None and (row.completed_cells, row.failed_cells) == (1, 1)
    assert await repo.existing_params_hashes(conn, sid) == {"a", "b"}


async def test_a_completed_search_is_immutable_to_claims_and_writes(conn: asyncpg.Connection, unique: str) -> None:
    sid = f"s-{unique}"
    await repo.create_search(conn, _new_search(sid, symbol=unique))
    attempt = await repo.claim_attempt(conn, sid, job_id=None)
    winner = _cell("w", sharpe=2.0)
    await repo.write_cells(conn, sid, attempt, [winner])
    await _finish(conn, sid, attempt, winner=winner)

    row = await repo.get_search(conn, sid)
    assert row is not None and row.leader_params_hash == "w" and row.leader_params == {"short_window": 5}
    with pytest.raises(fence.RecordNotClaimableError):
        await repo.claim_attempt(conn, sid, job_id=None)
    with pytest.raises(fence.StaleAttemptError, match="immutable"):
        await repo.write_cells(conn, sid, attempt, [_cell("late")])
    with pytest.raises(fence.StaleAttemptError, match="immutable"):
        await _finish(conn, sid, attempt, status="failed")


async def test_a_cancelled_search_keeps_its_cells_and_can_be_finished_later(conn: asyncpg.Connection, unique: str) -> None:
    sid = f"s-{unique}"
    await repo.create_search(conn, _new_search(sid, symbol=unique))
    attempt = await repo.claim_attempt(conn, sid, job_id=None)
    await repo.write_cells(conn, sid, attempt, [_cell("a")])
    await _finish(conn, sid, attempt, status="cancelled", incomplete=True, winner=_cell("a"))

    row = await repo.get_search(conn, sid)
    assert row is not None and row.status == "cancelled" and row.incomplete and row.completed_cells == 1

    resumed = await repo.claim_attempt(conn, sid, job_id="job-3")
    assert resumed == 2
    refreshed = await repo.get_search(conn, sid)
    assert refreshed is not None and refreshed.status == "running" and not refreshed.incomplete
    assert await repo.existing_params_hashes(conn, sid) == {"a"}


async def test_delete_cascades_and_fences_a_stale_writer(conn: asyncpg.Connection, unique: str) -> None:
    sid = f"s-{unique}"
    await repo.create_search(conn, _new_search(sid, symbol=unique))
    attempt = await repo.claim_attempt(conn, sid, job_id=None)
    await repo.write_cells(conn, sid, attempt, [_cell("a")])

    assert await repo.delete_search(conn, sid) is True
    assert await repo.get_search(conn, sid) is None
    assert await conn.fetchval("SELECT COUNT(*) FROM research_grid_search_cells WHERE search_id = $1", sid) == 0
    with pytest.raises(fence.StaleAttemptError):
        await repo.write_cells(conn, sid, attempt, [_cell("b")])
    assert await repo.delete_search(conn, sid) is False


async def test_cells_page_and_sort_on_the_server_with_nulls_last(conn: asyncpg.Connection, unique: str) -> None:
    sid = f"s-{unique}"
    await repo.create_search(conn, _new_search(sid, symbol=unique, expected=5))
    attempt = await repo.claim_attempt(conn, sid, job_id=None)
    await repo.write_cells(
        conn,
        sid,
        attempt,
        [_cell("h1", sharpe=0.5), _cell("h2", sharpe=2.5), _cell("h3", sharpe=None, status="failed"), _cell("h4", sharpe=1.5), _cell("h5", sharpe=None, trades=0)],
    )

    first = await repo.list_cells(conn, sid, sort_by="sharpe_ratio", direction="desc", page=1, page_size=2)
    second = await repo.list_cells(conn, sid, sort_by="sharpe_ratio", direction="desc", page=2, page_size=2)
    third = await repo.list_cells(conn, sid, sort_by="sharpe_ratio", direction="desc", page=3, page_size=2)
    ascending = await repo.list_cells(conn, sid, sort_by="sharpe_ratio", direction="asc", page=1, page_size=5)

    assert first.total == 5
    assert [cell.params_hash for cell in first.cells] == ["h2", "h4"]
    assert [cell.params_hash for cell in second.cells] == ["h1", "h3"]  # nulls last, then params_hash
    assert [cell.params_hash for cell in third.cells] == ["h5"]
    assert [cell.params_hash for cell in ascending.cells] == ["h1", "h4", "h2", "h3", "h5"]
    with pytest.raises(ValueError, match="unknown sort column"):
        await repo.list_cells(conn, sid, sort_by="params_json; DROP TABLE research_grid_searches")


async def test_history_filters_narrow_by_strategy_symbol_status_and_job(conn: asyncpg.Connection, unique: str) -> None:
    await repo.create_search(conn, _new_search(f"a-{unique}", symbol=unique))
    await repo.create_search(conn, _new_search(f"b-{unique}", symbol=unique, strategy_key="rsi_mean_reversion"))
    attempt = await repo.claim_attempt(conn, f"b-{unique}", job_id=None)
    await _finish(conn, f"b-{unique}", attempt)

    assert [row.id for row in await repo.list_searches(conn, symbol=unique, strategy_key="rsi_mean_reversion")] == [f"b-{unique}"]
    assert [row.id for row in await repo.list_searches(conn, symbol=unique, statuses=("completed",))] == [f"b-{unique}"]
    assert [row.id for row in await repo.list_searches(conn, job_id=f"job-a-{unique}")] == [f"a-{unique}"]
