"""One question about an order reads one row (#1942).

The reconciliation sweep held the uvicorn loop at 100 % CPU for about nine
minutes on the paper account's ledger (10,153 custody transitions, 373
orders). ``/health`` timed out, the podman healthcheck failed twenty times
running, and the SQLite execution lease expired and revived itself (ADR 0050)
because its heartbeat is a coroutine on the same loop.

The cost was shape, not volume: seven helpers wanted a single fact out of an
order's history and each fetched *every* transition for that order and scanned
the list in Python — per order, per effect, per pass, against a table that only
grows. ``first_order_transition`` / ``last_order_transition`` ask the index for
the row instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite import writes
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.order_evidence import entry_order_symbol
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.sqlite.conftest import _broker_leg, _clock_at, _FakeTradePort

ACCOUNT_ID = "PA_LOOKUPS"
SID = "sid-lookups"
RUN_ID = "run-lookups"


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    r = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_at(1_700_000_000_000),
        lease_ttl_ms=300_000,
    )
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield r
    r.close()


async def _an_order_with_history(repo: ClerkSqliteRepository) -> str:
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="enter-1",
        lifecycle_run_id=RUN_ID,
        leg=_broker_leg(quantity=10.0),
        trade=_FakeTradePort(),
    )
    assert submission.order_ref is not None
    return submission.order_ref


@pytest.mark.asyncio
async def test_reading_one_fact_from_an_order_reads_one_row(
    repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``entry_order_symbol`` wants one transition, so it must fetch one."""
    order_ref = await _an_order_with_history(repo)
    history = repo.transitions_for_order(order_ref)
    assert len(history) > 1, "the fixture must give the order more than one transition to skip"

    rows_materialized = 0
    real_row_to_payload = writes.row_to_payload

    def counting_row_to_payload(row: Any) -> dict:
        nonlocal rows_materialized
        rows_materialized += 1
        return real_row_to_payload(row)

    monkeypatch.setattr(writes, "row_to_payload", counting_row_to_payload)

    assert entry_order_symbol(repo, order_ref) == "SPY"
    assert rows_materialized == 1, (
        f"read {rows_materialized} rows to answer a question about one; "
        f"the order has {len(history)} transitions and a real ledger has thousands"
    )


@pytest.mark.asyncio
async def test_first_and_last_agree_with_reading_the_whole_history(repo: ClerkSqliteRepository) -> None:
    """The indexed lookups must answer exactly what the scans they replaced did."""
    order_ref = await _an_order_with_history(repo)
    history = repo.transitions_for_order(order_ref)
    kinds = {transition["transition_kind"] for transition in history}
    assert len(kinds) >= 1

    for kind in kinds:
        of_kind = [t for t in history if t["transition_kind"] == kind]
        assert repo.first_order_transition(order_ref=order_ref, transition_kind=kind) == of_kind[0]
        assert repo.last_order_transition(order_ref=order_ref, transition_kind=kind) == of_kind[-1]

    # Kindless: the order's own first and last, whatever they are.
    assert repo.first_order_transition(order_ref=order_ref) == history[0]
    assert repo.last_order_transition(order_ref=order_ref) == history[-1]


@pytest.mark.asyncio
async def test_an_absent_kind_and_an_unknown_order_both_answer_none(repo: ClerkSqliteRepository) -> None:
    """The scans returned "not found" by falling off the end; these must too."""
    order_ref = await _an_order_with_history(repo)

    assert repo.first_order_transition(order_ref=order_ref, transition_kind="NO_SUCH_KIND") is None
    assert repo.last_order_transition(order_ref=order_ref, transition_kind="NO_SUCH_KIND") is None
    assert repo.first_order_transition(order_ref="no-such-order") is None
    assert repo.last_order_transition(order_ref="no-such-order") is None


@pytest.mark.asyncio
async def test_the_lookup_short_circuits_on_the_index(repo: ClerkSqliteRepository) -> None:
    """``sequence`` is the rowid, so the index yields order for free — no temp b-tree."""
    order_ref = await _an_order_with_history(repo)
    plan = repo._conn.execute(
        f"EXPLAIN QUERY PLAN SELECT {', '.join(writes.TRANSITION_COLUMNS)} FROM custody_transitions "
        "WHERE order_ref = ? AND transition_kind = ? ORDER BY sequence ASC LIMIT 1",
        (order_ref, "ENTER_ACCEPTED"),
    ).fetchall()
    detail = " ".join(str(row[-1]) for row in plan)

    assert "ix_custody_transitions_order_ref" in detail, detail
    assert "TEMP B-TREE" not in detail.upper(), f"the sort is not free: {detail}"
