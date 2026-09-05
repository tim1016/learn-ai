"""Grid Search persistence: durable at launch, fenced by attempt, paged on the server."""

from __future__ import annotations

import asyncpg
import pytest

from app.research.grid_search import repository as repo
from app.research.grid_search.models import CellResult, NewSearch, SearchOwner
from app.research.grid_search.schema import SCHEMA_VERSION, ensure_schema

pytestmark = pytest.mark.asyncio


def _new_search(search_id: str = "s1", *, owner: SearchOwner | None = None, expected: int = 3) -> NewSearch:
    return NewSearch(
        id=search_id,
        strategy_key="sma_crossover",
        symbol="SPY",
        request={"grid": {"short_window": [5, 10]}, "measure": "sharpe_ratio"},
        receipt={"code_identity": {"source_digest": "abc"}},
        expected_cells=expected,
        job_id="job-1",
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


async def test_ensure_schema_is_idempotent_and_records_its_version(conn: asyncpg.Connection) -> None:
    await ensure_schema(conn)
    await ensure_schema(conn)

    versions = await conn.fetch("SELECT version FROM research_schema_migrations ORDER BY version")
    assert [row["version"] for row in versions] == [SCHEMA_VERSION]


async def test_a_search_is_listable_the_moment_it_is_created(conn: asyncpg.Connection) -> None:
    created = await repo.create_search(conn, _new_search())

    listed = await repo.list_searches(conn)
    assert [row.id for row in listed] == ["s1"]
    assert created.status == "queued"
    assert created.attempt == 0
    assert created.request["measure"] == "sharpe_ratio"
    assert created.receipt["code_identity"]["source_digest"] == "abc"


async def test_walk_forward_owned_sweeps_are_excluded_from_the_user_history_by_ownership(conn: asyncpg.Connection) -> None:
    await repo.create_search(conn, _new_search("user-1"))
    await repo.create_search(conn, _new_search("wf-1", owner=SearchOwner(kind="walk_forward", owner_id="study-9", fold_index=0, phase="train")))

    assert [row.id for row in await repo.list_searches(conn)] == ["user-1"]
    assert [row.id for row in await repo.list_searches(conn, owner_kind="walk_forward", owner_id="study-9")] == ["wf-1"]
    fetched = await repo.get_search(conn, "wf-1")
    assert fetched is not None and fetched.owner.fold_index == 0 and fetched.owner.phase == "train"


async def test_writes_carry_the_attempt_and_a_stale_attempt_cannot_write(conn: asyncpg.Connection) -> None:
    await repo.create_search(conn, _new_search())
    first = await repo.claim_attempt(conn, "s1", job_id="job-1")
    await repo.write_cells(conn, "s1", first, [_cell("a")])

    second = await repo.claim_attempt(conn, "s1", job_id="job-2")  # the resumed run
    await repo.write_cells(conn, "s1", second, [_cell("b")])

    assert (first, second) == (1, 2)
    with pytest.raises(repo.StaleAttemptError):
        await repo.write_cells(conn, "s1", first, [_cell("a", sharpe=99.0)])
    with pytest.raises(repo.StaleAttemptError):
        await repo.finish_search(conn, "s1", first, status="completed", leader_params_hash="a", incomplete=False, failure_reason=None)

    cells = {cell.params_hash: cell for cell in await repo.list_all_cells(conn, "s1")}
    assert cells["a"].sharpe_ratio == 1.0 and cells["a"].attempt == 1
    assert cells["b"].attempt == 2
    row = await repo.get_search(conn, "s1")
    assert row is not None and row.status == "running" and row.completed_cells == 2


async def test_rewriting_a_cell_overwrites_rather_than_appends(conn: asyncpg.Connection) -> None:
    await repo.create_search(conn, _new_search())
    attempt = await repo.claim_attempt(conn, "s1", job_id=None)
    await repo.write_cells(conn, "s1", attempt, [_cell("a", sharpe=1.0), _cell("b", status="failed", sharpe=None)])
    await repo.write_cells(conn, "s1", attempt, [_cell("a", sharpe=3.0)])

    cells = await repo.list_all_cells(conn, "s1")
    row = await repo.get_search(conn, "s1")

    assert [(cell.params_hash, cell.sharpe_ratio) for cell in cells] == [("a", 3.0), ("b", None)]
    assert row is not None and (row.completed_cells, row.failed_cells) == (1, 1)
    assert await repo.existing_params_hashes(conn, "s1") == {"a", "b"}


async def test_a_completed_search_is_immutable(conn: asyncpg.Connection) -> None:
    await repo.create_search(conn, _new_search())
    attempt = await repo.claim_attempt(conn, "s1", job_id=None)
    await repo.finish_search(conn, "s1", attempt, status="completed", leader_params_hash=None, incomplete=False, failure_reason=None)

    with pytest.raises(repo.SearchNotClaimableError):
        await repo.claim_attempt(conn, "s1", job_id=None)


async def test_a_cancelled_search_keeps_its_cells_and_can_be_finished_later(conn: asyncpg.Connection) -> None:
    await repo.create_search(conn, _new_search())
    attempt = await repo.claim_attempt(conn, "s1", job_id=None)
    await repo.write_cells(conn, "s1", attempt, [_cell("a")])
    await repo.finish_search(conn, "s1", attempt, status="cancelled", leader_params_hash="a", incomplete=True, failure_reason=None)

    row = await repo.get_search(conn, "s1")
    assert row is not None and row.status == "cancelled" and row.incomplete and row.completed_cells == 1

    resumed = await repo.claim_attempt(conn, "s1", job_id="job-3")
    assert resumed == 2
    refreshed = await repo.get_search(conn, "s1")
    assert refreshed is not None and refreshed.status == "running" and not refreshed.incomplete
    assert await repo.existing_params_hashes(conn, "s1") == {"a"}


async def test_delete_cascades_and_fences_a_stale_writer(conn: asyncpg.Connection) -> None:
    await repo.create_search(conn, _new_search())
    attempt = await repo.claim_attempt(conn, "s1", job_id=None)
    await repo.write_cells(conn, "s1", attempt, [_cell("a")])

    assert await repo.delete_search(conn, "s1") is True
    assert await repo.get_search(conn, "s1") is None
    assert await conn.fetchval("SELECT COUNT(*) FROM research_grid_search_cells") == 0
    with pytest.raises(repo.StaleAttemptError):
        await repo.write_cells(conn, "s1", attempt, [_cell("b")])
    assert await repo.delete_search(conn, "s1") is False


async def test_cells_page_and_sort_on_the_server_with_nulls_last(conn: asyncpg.Connection) -> None:
    await repo.create_search(conn, _new_search(expected=5))
    attempt = await repo.claim_attempt(conn, "s1", job_id=None)
    await repo.write_cells(
        conn,
        "s1",
        attempt,
        [
            _cell("h1", sharpe=0.5),
            _cell("h2", sharpe=2.5),
            _cell("h3", sharpe=None, status="failed"),
            _cell("h4", sharpe=1.5),
            _cell("h5", sharpe=None, trades=0),
        ],
    )

    first = await repo.list_cells(conn, "s1", sort_by="sharpe_ratio", direction="desc", page=1, page_size=2)
    second = await repo.list_cells(conn, "s1", sort_by="sharpe_ratio", direction="desc", page=2, page_size=2)
    third = await repo.list_cells(conn, "s1", sort_by="sharpe_ratio", direction="desc", page=3, page_size=2)
    ascending = await repo.list_cells(conn, "s1", sort_by="sharpe_ratio", direction="asc", page=1, page_size=5)

    assert first.total == 5
    assert [cell.params_hash for cell in first.cells] == ["h2", "h4"]
    assert [cell.params_hash for cell in second.cells] == ["h1", "h3"]  # nulls last, then params_hash
    assert [cell.params_hash for cell in third.cells] == ["h5"]
    assert [cell.params_hash for cell in ascending.cells] == ["h1", "h4", "h2", "h3", "h5"]
    with pytest.raises(ValueError, match="unknown sort column"):
        await repo.list_cells(conn, "s1", sort_by="params_json; DROP TABLE research_grid_searches")


async def test_history_filters_narrow_by_strategy_symbol_and_status(conn: asyncpg.Connection) -> None:
    await repo.create_search(conn, _new_search("a"))
    await repo.create_search(conn, NewSearch(id="b", strategy_key="rsi_mean_reversion", symbol="QQQ", request={}, receipt={}, expected_cells=1, job_id=None))
    attempt = await repo.claim_attempt(conn, "b", job_id=None)
    await repo.finish_search(conn, "b", attempt, status="completed", leader_params_hash=None, incomplete=False, failure_reason=None)

    assert [row.id for row in await repo.list_searches(conn, strategy_key="rsi_mean_reversion")] == ["b"]
    assert [row.id for row in await repo.list_searches(conn, symbol="SPY")] == ["a"]
    assert [row.id for row in await repo.list_searches(conn, status="completed")] == ["b"]
