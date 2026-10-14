"""Two store reads that run under the repository's write lock must not cost more than the answer needs (#3216).

* The outside-order read states, per order, when custody first saw it, when it
  was last reviewed and what its newest observation said. Every pass reads
  every outside order, so its cost must not be the journal's length times the
  number of orders.
* The correction lookup asks whether one execution already raised its
  uncertainty. It must stop at the first record that says so.

Cost is counted in the steps SQLite's machine takes, which grow with every row
a query reads; no clock is involved.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.external_orders import acknowledge_external_order, observe_external_order
from app.broker.alpaca.clerk.sqlite.facts import ReconciliationAttemptedFacts
from app.broker.alpaca.clerk.sqlite.models import ExternalOrderResource, TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.sqlite.conftest import _broker_order_fixture

ACCOUNT_ID = "PA-STORE-READS"


@contextmanager
def _steps_taken(conn: sqlite3.Connection) -> Iterator[Callable[[], int]]:
    steps = [0]

    def count() -> int:
        steps[0] += 1
        return 0

    conn.set_progress_handler(count, 1)
    try:
        yield lambda: steps[0]
    finally:
        conn.set_progress_handler(None, 1)


def _append_unrelated_journal_rows(conn: sqlite3.Connection, count: int, *, kind: str = "RECONCILIATION_ATTEMPTED") -> None:
    conn.executemany(
        "INSERT INTO custody_transitions (prev_hash, row_hash, authority_generation, transition_kind, "
        "custody_owner, execution_authority, operation_state, clerk_observed_at_ms, recorded_at_ms, "
        "summary_code, facts_schema_version, facts_json) "
        "VALUES ('p', 'r', 1, ?, 'ACCOUNT_CLERK', 'ACCOUNT_CLERK', 'clean', 1, 1, 'PAD', 1, '{}')",
        [(kind,)] * count,
    )
    conn.commit()


# ── the outside-order read ───────────────────────────────────────────────────


def _observe(repo: ClerkSqliteRepository, order_id: str, *, status: str, filled: float = 0.0, price: float | None = None) -> None:
    observe_external_order(
        repo,
        order=_broker_order_fixture(
            f"alpaca-console:{order_id}",
            order_id=order_id,
            status=status,
            quantity=3.0,
            filled_quantity=filled,
            filled_avg_price=price,
        ),
    )


def _outside_order_ledger(repo: ClerkSqliteRepository) -> None:
    """Five outside orders, each with a different history, and records that must not be read as theirs."""
    _observe(repo, "order-seen-once", status="new")
    _observe(repo, "order-seen-three-times", status="new")
    _observe(repo, "order-seen-three-times", status="partially_filled", filled=1.0, price=10.0)
    _observe(repo, "order-seen-three-times", status="filled", filled=3.0, price=10.5)
    _observe(repo, "order-reviewed", status="new")
    acknowledge_external_order(repo, external_order_id="order-reviewed", operator="operator-1")
    _observe(repo, "order-seen-after-its-review", status="new")
    acknowledge_external_order(repo, external_order_id="order-seen-after-its-review", operator="operator-2")
    _observe(repo, "order-seen-after-its-review", status="filled", filled=3.0, price=9.0)
    # A record of another kind that names an outside order's broker id says nothing of its observations.
    repo.append_transition(
        TransitionInput(
            transition_kind="RECONCILIATION_ATTEMPTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="clean",
            broker_order_id="order-seen-three-times",
            clerk_observed_at_ms=repo.clock(),
            summary_code="RECONCILIATION_ATTEMPTED",
            facts_json=ReconciliationAttemptedFacts(
                trigger="AUTOMATIC", outcome="STILL_UNKNOWN", why="decoy"
            ).to_facts_json(),
        )
    )
    # An outside-order row that no journal record explains: it reads with no provenance.
    repo._conn.execute(
        "INSERT INTO external_orders (external_order_id, broker_order_id, client_order_id, symbol, side, qty, "
        "order_type, limit_price, stop_price, filled_avg_price, observed_at_ms, acknowledged_at_ms, ack_operator, "
        "evidence_refs_json) VALUES ('order-unexplained', 'order-unexplained', 'alpaca-console:unexplained', "
        "'SPY', 'BUY', 1.0, 'market', NULL, NULL, NULL, 5, NULL, NULL, '[\"order-unexplained\"]')"
    )
    repo._conn.commit()


def _provenance_by_definition(conn: sqlite3.Connection, broker_order_id: str) -> dict[str, object]:
    """What the journal says of one outside order, worked out record by record."""
    records = conn.execute(
        "SELECT sequence, transition_kind, recorded_at_ms, facts_json FROM custody_transitions "
        "WHERE broker_order_id = ? ORDER BY sequence",
        (broker_order_id,),
    ).fetchall()
    observed = [row for row in records if row["transition_kind"] == "EXTERNAL_ORDER_OBSERVED"]
    reviewed = [row for row in records if row["transition_kind"] == "EXTERNAL_ORDER_ACKNOWLEDGED"]
    newest = json.loads(observed[-1]["facts_json"]) if observed else {}
    return {
        "observation_sequence": observed[0]["sequence"] if observed else None,
        "observation_recorded_at_ms": observed[0]["recorded_at_ms"] if observed else None,
        "acknowledgement_sequence": reviewed[-1]["sequence"] if reviewed else None,
        "acknowledgement_recorded_at_ms": reviewed[-1]["recorded_at_ms"] if reviewed else None,
        "broker_state": newest.get("broker_state"),
        "filled_quantity": newest.get("filled_quantity"),
    }


def test_the_bulk_and_the_single_outside_order_reads_state_the_same_provenance(tmp_path: Path) -> None:
    """The reconciliation pass compares the two as whole records, so they must agree on every field."""
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        _outside_order_ledger(repo)
        conn = repo._conn
        bulk = reads.external_orders(conn)

        assert {order.broker_order_id for order in bulk} == {
            "order-seen-once",
            "order-seen-three-times",
            "order-reviewed",
            "order-seen-after-its-review",
            "order-unexplained",
        }
        for order in bulk:
            assert reads.external_order(conn, order.external_order_id) == order
            assert reads.external_order_by_broker_order_id(conn, order.broker_order_id) == order
            expected = _provenance_by_definition(conn, order.broker_order_id)
            assert {name: getattr(order, name) for name in expected} == expected, order.broker_order_id
        by_id = {order.broker_order_id: order for order in bulk}
        assert by_id["order-seen-three-times"].broker_state == "filled"
        assert by_id["order-seen-after-its-review"].acknowledgement_sequence is not None
        assert by_id["order-unexplained"] == ExternalOrderResource(
            external_order_id="order-unexplained",
            broker_order_id="order-unexplained",
            client_order_id="alpaca-console:unexplained",
            symbol="SPY",
            side="BUY",
            qty=1.0,
            order_type="market",
            limit_price=None,
            stop_price=None,
            filled_avg_price=None,
            observed_at_ms=5,
            acknowledged_at_ms=None,
            ack_operator=None,
            evidence_refs=("order-unexplained",),
        )
    finally:
        repo.close()


def _steps_to_read_every_outside_order(root: Path, *, orders: int, journal_rows: int) -> int:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=root)
    try:
        for number in range(orders):
            _observe(repo, f"order-{number}", status="new")
        _append_unrelated_journal_rows(repo._conn, journal_rows - orders)
        with _steps_taken(repo._conn) as steps:
            assert len(reads.external_orders(repo._conn)) == orders
        return steps()
    finally:
        repo.close()


def test_reading_every_outside_order_walks_the_journal_once_however_many_orders_there_are(tmp_path: Path) -> None:
    few = _steps_to_read_every_outside_order(tmp_path / "few", orders=2, journal_rows=6000)
    many = _steps_to_read_every_outside_order(tmp_path / "many", orders=14, journal_rows=6000)

    # The same journal read for twelve more orders: one walk of it, not one more walk per order. Each
    # order still costs its own lookups, so the two are not equal; a walk per order would cost seven times.
    assert many < 1.5 * few, (few, many)


# ── the correction lookup ────────────────────────────────────────────────────


def _raise_row(conn: sqlite3.Connection, facts_json: str, *, kind: str = "UNCERTAINTY_RAISED") -> None:
    conn.execute(
        "INSERT INTO custody_transitions (prev_hash, row_hash, authority_generation, transition_kind, "
        "custody_owner, execution_authority, operation_state, clerk_observed_at_ms, recorded_at_ms, "
        "summary_code, facts_schema_version, facts_json) "
        "VALUES ('p', 'r', 1, ?, 'ACCOUNT_CLERK', 'ACCOUNT_CLERK', 'unknown', 1, 1, 'X', 1, ?)",
        (kind, facts_json),
    )


def _raised_by(execution_id: object) -> str:
    return json.dumps({"reason_code": "EXECUTION_COVERAGE_CONFLICT", "cause_facts": {"execution_id": execution_id}})


def test_the_correction_lookup_answers_from_the_raised_records_that_name_the_execution(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        conn = repo._conn
        _raise_row(conn, _raised_by("exec-other"))
        # Another kind of record naming the execution is not a raised uncertainty.
        _raise_row(conn, _raised_by("exec-only-in-a-fill"), kind="EXECUTION_SLICE_FILLED")
        _raise_row(conn, json.dumps({"cause_facts": {"orders": ["order-a"]}}))
        _raise_row(conn, json.dumps({"reason_code": "EXIT_NOT_FLAT"}))
        _raise_row(conn, _raised_by(7))
        # JSON that is no object names no execution. The lookup once failed on it with an AttributeError;
        # no writer produces one.
        _raise_row(conn, "[1, 2]")
        _raise_row(conn, _raised_by("exec-wanted"))
        conn.commit()

        assert {
            execution_id: reads.correction_uncertainty_exists(conn, execution_id)
            for execution_id in ("exec-wanted", "exec-other", "exec-only-in-a-fill", "7", "exec-absent")
        } == {
            "exec-wanted": True,
            "exec-other": True,
            "exec-only-in-a-fill": False,
            "7": False,
            "exec-absent": False,
        }

        # A record that is not JSON stops the lookup only where it is reached: after a match it is never read.
        _raise_row(conn, "{not json")
        conn.commit()
        assert reads.correction_uncertainty_exists(conn, "exec-wanted") is True
        with pytest.raises(sqlite3.OperationalError):
            reads.correction_uncertainty_exists(conn, "exec-absent")
    finally:
        repo.close()


def _steps_to_find_the_first_raised_execution(root: Path, *, raised_after: int) -> int:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=root)
    try:
        conn = repo._conn
        _raise_row(conn, _raised_by("exec-first"))
        for number in range(raised_after):
            _raise_row(conn, _raised_by(f"exec-later-{number}"))
        conn.commit()
        with _steps_taken(conn) as steps:
            assert reads.correction_uncertainty_exists(conn, "exec-first") is True
        return steps()
    finally:
        repo.close()


def test_the_correction_lookup_stops_at_the_first_record_that_names_the_execution(tmp_path: Path) -> None:
    short = _steps_to_find_the_first_raised_execution(tmp_path / "short", raised_after=10)
    long = _steps_to_find_the_first_raised_execution(tmp_path / "long", raised_after=500)

    assert long <= short, (short, long)

