"""One option order of several legs on the account Clerk's books: a parent row and a row for each leg (#2913).

Each test accepts an entry of several legs straight onto the books, with no
admission and no broker in between, and asserts what the books then say: one
ENTER, one order that was sent, a row for each leg bound to its contract,
and every read that lists orders answering the parent or the legs as it
should. Fills are folded onto the legs' rows through the Clerk's own append.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.bot_history import project_custody_history
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.database_verification import verify_database
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_option_enter
from app.broker.alpaca.clerk.sqlite.exit import newest_reducible_entry
from app.broker.alpaca.clerk.sqlite.facts import EnterAcceptedFacts
from app.broker.alpaca.clerk.sqlite.option_entry import (
    MultiLegOptionEntryFacts,
    held_of_entry,
    option_entry_contracts,
    option_entry_facts,
    option_entry_of_order,
    order_contract,
)
from app.broker.alpaca.clerk.sqlite.option_order_evidence import AskedMultiLegOrder, asked_option_order
from app.broker.alpaca.clerk.sqlite.option_order_legs import (
    MULTI_LEG_PARENT_SQL,
    NOT_A_LEG_SQL,
    OrderLeg,
    is_multi_leg_parent,
    leg_order_ref,
    multi_leg_parent_line,
    order_legs,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    SeveralLegsHaveNoOneSymbol,
    entry_order_symbol,
    entry_order_symbols,
    unresolved_order_refs,
    working_order_refs,
)
from app.broker.alpaca.clerk.sqlite.order_projection import read_open_opposite_side_orders, read_order_details
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg, OrderSide
from tests.broker.alpaca.clerk.sqlite.budgeted_run_fixture import accept_budgeted_test_enter
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.historical_run_fixture import seed_historical_run_started
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import (
    ACCOUNT_ID,
    RUN_ID,
    accept_option_order,
    fill,
    open_option_books,
)
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import (
    PAID_PICKS,
    PAID_SPREAD,
    RECEIVED_PICKS,
    RECEIVED_SPREAD,
    call,
    worked_entry,
)

C580, C585, C590 = call(580).occ_symbol, call(585).occ_symbol, call(590).occ_symbol


@pytest.fixture
def books(tmp_path: Path) -> ClerkSqliteRepository:
    repo = open_option_books(tmp_path, bots={"bot-a": "SPY", "bot-b": "SPY"})
    yield repo
    repo.close()


def _accept(
    repo: ClerkSqliteRepository, entry: MultiLegOptionEntryFacts, *, decision: str = "d1", bot: str = "bot-a"
) -> EnterSubmission:
    return accept_option_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=bot, decision_id=decision, lifecycle_run_id=RUN_ID, entry=entry
    )


def _paid(units: int = 1) -> MultiLegOptionEntryFacts:
    return worked_entry(PAID_SPREAD, PAID_PICKS, units=units, now_ms=NOON)


def _received(units: int = 1) -> MultiLegOptionEntryFacts:
    return worked_entry(RECEIVED_SPREAD, RECEIVED_PICKS, units=units, now_ms=NOON)


def _leg_submission(parent: EnterSubmission, leg_number: int) -> EnterSubmission:
    """A leg's row in the shape the fill helper takes: the parent's effect, the leg's own reference."""
    assert parent.order_ref is not None
    return EnterSubmission(
        command=parent.command, effect_operation_id=parent.effect_operation_id,
        order_ref=leg_order_ref(parent.order_ref, leg_number), created=True,
    )


def _fill_leg(
    repo: ClerkSqliteRepository, parent: EnterSubmission, leg_number: int, *, symbol: str, side: str, contracts: int,
    price: float, execution: str = "first",
) -> str:
    return fill(
        repo, _leg_submission(parent, leg_number), execution_id=f"x-{parent.order_ref}-{leg_number}-{execution}",
        symbol=symbol, side=side, quantity=contracts, price=price,
    )


def _rows(repo: ClerkSqliteRepository, sql: str, *params: object) -> list[tuple]:
    with repo._write_lock:
        return [tuple(row) for row in repo._conn.execute(sql, params)]


# ── the rows ──────────────────────────────────────────────────────────────


def test_one_entry_of_two_legs_is_one_command_one_effect_one_parent_and_a_row_for_each_leg(
    books: ClerkSqliteRepository,
) -> None:
    accepted = _accept(books, _paid(units=2))

    parent = accepted.order_ref
    assert _rows(books, "SELECT COUNT(*) FROM commands WHERE action = 'ENTER'") == [(1,)]
    assert _rows(books, "SELECT COUNT(*) FROM effect_operations WHERE kind = 'ENTER'") == [(1,)]
    assert _rows(books, "SELECT order_ref, role, client_order_id, broker_order_id, broker_state FROM orders ORDER BY rowid") == [
        (parent, "ENTRY", parent, None, None),
        (f"{parent}~1", "OTHER", f"{parent}~1", None, None),
        (f"{parent}~2", "OTHER", f"{parent}~2", None, None),
    ]
    # Every row is under the one ENTER; only the parent is linked to it as its order.
    assert _rows(books, "SELECT DISTINCT effect_operation_id FROM orders") == [(accepted.effect_operation_id,)]
    assert _rows(books, "SELECT order_ref, role FROM operation_order_links") == [(parent, "ENTRY")]
    with books._write_lock:
        assert order_legs(books._conn, parent) == (
            OrderLeg(f"{parent}~1", parent, 1, "long_call", C585, "buy", "open", 1, 2),
            OrderLeg(f"{parent}~2", parent, 2, "short_call", C590, "sell", "open", 1, 2),
        )
    assert books.order_legs(parent) == order_legs(books._conn, parent)
    assert books.leg_parent(f"{parent}~2") == parent
    assert books.leg_parent(parent) is None


def test_each_leg_is_bound_to_its_own_contract_and_the_parent_to_none(books: ClerkSqliteRepository) -> None:
    accepted = _accept(books, _paid())

    assert order_contract(books, accepted.order_ref) is None
    assert order_contract(books, f"{accepted.order_ref}~1") == ContractTerms(symbol=C585, multiplier=100)
    assert order_contract(books, f"{accepted.order_ref}~2") == ContractTerms(symbol=C590, multiplier=100)
    assert _rows(books, "SELECT symbol, multiplier FROM option_contracts ORDER BY symbol") == [(C585, 100), (C590, 100)]


def test_a_leg_row_is_never_edited_or_deleted(books: ClerkSqliteRepository) -> None:
    accepted = _accept(books, _paid())

    with books._write_lock:
        with pytest.raises(sqlite3.IntegrityError, match="never edited"):
            books._conn.execute("UPDATE option_order_legs SET contracts = 9 WHERE parent_order_ref = ?", (accepted.order_ref,))
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            books._conn.execute("DELETE FROM option_order_legs WHERE parent_order_ref = ?", (accepted.order_ref,))


def test_a_leg_reference_is_the_parents_with_its_number_and_is_never_made_of_a_leg() -> None:
    assert leg_order_ref("learn-ai/bot/v1:abc", 3) == "learn-ai/bot/v1:abc~3"
    for parent, number in (("", 1), ("learn-ai/bot/v1:abc~1", 1), ("ref", 0), ("ref", 5), ("ref", True)):
        with pytest.raises(ValueError):
            leg_order_ref(parent, number)  # type: ignore[arg-type]


# ── the facts ─────────────────────────────────────────────────────────────


def test_the_enter_is_written_with_the_parents_own_line_and_the_order_of_several_legs(
    books: ClerkSqliteRepository,
) -> None:
    accepted = _accept(books, _received(units=2))

    stored = books.first_order_transition(order_ref=accepted.order_ref, transition_kind="ENTER_ACCEPTED")
    facts = json.loads(stored["facts_json"])
    # No symbol, no side, whole structures, and no limit: a net price is not a price a share.
    assert facts["leg"] == {
        "order_class": "mleg", "symbol": None, "side": None, "quantity": 2.0, "order_type": "limit",
        "limit_price": None, "time_in_force": "day", "extended_hours": False,
    }
    assert facts["leg"] == multi_leg_parent_line(units=2, order_type="limit")
    # Received: the signed net limit is below zero, and is kept in the entry's own facts.
    assert facts["option"]["net_limit_price"] == "-3.07"
    assert facts["option"]["order"]["net_limit_price"] == "-3.07"
    assert [(leg["side"], leg["intent"], leg["ratio"]) for leg in facts["option"]["order"]["legs"]] == [
        ("sell", "open", 1), ("buy", "open", 1),
    ]
    # No reservation was asked for: this account has no envelope. Nothing is written of one.
    assert "cash_reference_price" not in facts


def test_the_entry_is_read_back_as_several_legs_with_the_same_reads_a_one_leg_entry_answers(
    books: ClerkSqliteRepository,
) -> None:
    entry = _paid(units=3)
    accepted = _accept(books, entry)

    kept = option_entry_of_order(books, accepted.order_ref)
    assert kept == entry and isinstance(kept, MultiLegOptionEntryFacts)
    assert kept.leg_terms == (ContractTerms(C585, 100), ContractTerms(C590, 100))
    assert [(leg.leg_id, leg.position, leg.ratio) for leg in kept.structure.legs] == [
        ("long_call", "long", 1), ("short_call", "short", 1),
    ]
    assert (kept.units, kept.net_limit_price) == (3, Decimal("1.94"))
    # A leg's own row was accepted as nothing.
    assert option_entry_of_order(books, f"{accepted.order_ref}~1") is None
    # Code that still assumes one leg fails loudly: the type has neither property.
    assert not hasattr(kept, "the_leg") and not hasattr(kept, "contract_terms")


def test_the_two_worked_spreads_keep_their_money_and_what_one_structure_sets_aside() -> None:
    paid, received = _paid(units=2), _received(units=2)

    # Paid: 2 structures x $1.94 x 100 = $388.00 of premium, no requirement.
    assert (paid.money.net_entry_premium_cents, paid.money.broker_requirement_cents) == (38_800, 0)
    assert paid.unit_set_aside == Decimal("194.00")
    # Received: 2 x $3.07 x 100 = $614.00 received; requirement 2 x $5 x 100 = $1,000.00.
    assert (received.money.net_entry_premium_cents, received.money.broker_requirement_cents) == (-61_400, 100_000)
    # ($1,000.00 - $614.00) / 2 = $193.00 a structure, above zero though the net limit is below it.
    assert received.unit_set_aside == Decimal("193.00")
    # The commitment is that, for all the units, plus the fee provision.
    for entry in (paid, received):
        assert entry.money.entry_commitment_cents == int(entry.unit_set_aside * 100) * 2 + entry.money.fee_provision_cents


def test_a_one_leg_entry_and_an_entry_of_several_legs_are_told_apart_by_the_orders_own_shape(
    books: ClerkSqliteRepository,
) -> None:
    accepted = _accept(books, _paid())
    stored = books.first_order_transition(order_ref=accepted.order_ref, transition_kind="ENTER_ACCEPTED")

    assert isinstance(option_entry_facts(EnterAcceptedFacts.from_facts_json(stored["facts_json"])), MultiLegOptionEntryFacts)


def test_the_facts_refuse_an_order_that_is_not_the_entrys_legs() -> None:
    entry = _paid()
    other = _received()

    for changed in (
        {"order": other.order},
        {"units": 2},
        {"net_limit_price": Decimal("1.95")},
        {"legs": tuple(reversed(entry.legs))},
        {"cancel_at_ms": entry.cancel_at_ms + 1},
    ):
        with pytest.raises(ValueError):
            MultiLegOptionEntryFacts.model_validate({**entry.model_dump(), **changed})


# ── the reads ─────────────────────────────────────────────────────────────


def test_the_order_of_the_enter_is_the_parent_whichever_row_the_table_gives_first(books: ClerkSqliteRepository) -> None:
    accepted = _accept(books, _paid())

    found = books.order_for_effect_operation(accepted.effect_operation_id)
    assert found is not None and (found.order_ref, found.role) == (accepted.order_ref, "ENTRY")
    # Asked of the ENTER, in whatever order SQLite walks its rows: never a leg.
    assert _rows(
        books, f"SELECT o.order_ref FROM orders o WHERE o.effect_operation_id = ? AND {NOT_A_LEG_SQL}",
        accepted.effect_operation_id,
    ) == [(accepted.order_ref,)]
    assert _rows(books, f"SELECT o.order_ref FROM orders o WHERE {MULTI_LEG_PARENT_SQL}") == [(accepted.order_ref,)]
    with books._write_lock:
        assert is_multi_leg_parent(books._conn, accepted.order_ref)
        assert not is_multi_leg_parent(books._conn, f"{accepted.order_ref}~1")


def test_the_parent_reads_with_no_symbol_and_each_leg_as_its_own_contract(books: ClerkSqliteRepository) -> None:
    accepted = _accept(books, _received(units=2))
    parent = accepted.order_ref

    with books._write_lock:
        details = read_order_details(books._conn, (parent, f"{parent}~1", f"{parent}~2"))
    assert (details[parent].symbol, details[parent].side, details[parent].quantity, details[parent].limit_price) == (
        None, None, 2.0, None,
    )
    assert details[parent].order_type == "limit"
    # A leg: its contract, its side, the contracts asked of it, the parent's kind of order, no limit.
    assert (details[f"{parent}~1"].symbol, details[f"{parent}~1"].side, details[f"{parent}~1"].quantity) == (C580, "sell", 2.0)
    assert (details[f"{parent}~2"].symbol, details[f"{parent}~2"].side, details[f"{parent}~2"].quantity) == (C585, "buy", 2.0)
    assert {details[f"{parent}~{n}"].limit_price for n in (1, 2)} == {None}
    assert {details[f"{parent}~{n}"].order_type for n in (1, 2)} == {"limit"}


def test_no_read_of_open_orders_takes_the_parent_for_an_order_in_a_real_symbol(books: ClerkSqliteRepository) -> None:
    """The wash-trade check reads open orders by symbol and side: it sees each leg in its contract, and no parent."""
    accepted = _accept(books, _paid())

    with books._write_lock:
        for symbol in ("SPY", C585, C590):
            for side in (OrderSide.BUY, OrderSide.SELL):
                found = read_open_opposite_side_orders(books._conn, symbol=symbol, side=side)
                assert accepted.order_ref not in [order.order_ref for order in found]
        # The bought leg is an open buy of the 585 call: it is found by a sale of that contract.
        opposite = read_open_opposite_side_orders(books._conn, symbol=C585, side=OrderSide.SELL)
    assert [order.order_ref for order in opposite] == [f"{accepted.order_ref}~1"]


def test_what_the_books_asked_for_is_told_three_ways(books: ClerkSqliteRepository) -> None:
    several = _accept(books, _received(units=2))

    asked = asked_option_order(books, several.order_ref)
    assert asked == AskedMultiLegOrder(
        units=2, legs=((C580, "sell"), (C585, "buy")), order_type="limit", net_limit_price=Decimal("-3.07")
    )
    # What an answer must name: the units and each leg's contract and side. Never a leg's own quantity.
    assert asked.what_was_traded() == (2, frozenset({(C580, "sell"), (C585, "buy")}))
    # A leg was never asked of a broker by itself: asking about one is refused by name.
    with pytest.raises(ValueError, match="never about a leg"):
        asked_option_order(books, f"{several.order_ref}~1")
    # An order no contract is bound to, and that has no legs, is in shares.
    assert asked_option_order(books, "no-such-order") is None


def test_which_entry_opened_this_contract_is_read_from_the_entrys_legs(books: ClerkSqliteRepository) -> None:
    several = _accept(books, _paid())

    assert entry_order_symbols(books, several.order_ref) == (C585, C590)
    assert option_entry_contracts(books, "bot-a") == (C585, C590)
    # The entry a reduction of either contract targets is the parent.
    for symbol in (C585, C590):
        target = newest_reducible_entry(books, "bot-a", symbol)
        assert target is not None and target.order_ref == several.order_ref
    assert newest_reducible_entry(books, "bot-a", C580) is None
    # Asked for the one symbol it is in, an entry of several legs refuses by name.
    with pytest.raises(SeveralLegsHaveNoOneSymbol):
        entry_order_symbol(books, several.order_ref)


def test_a_one_symbol_entry_answers_its_symbols_as_it_answered_its_symbol(books: ClerkSqliteRepository) -> None:
    one = accept_budgeted_test_enter(
        books, account_id=ACCOUNT_ID, strategy_instance_id="bot-b", decision_id="d-one", lifecycle_run_id=RUN_ID,
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
    )

    assert entry_order_symbols(books, one.order_ref) == (entry_order_symbol(books, one.order_ref),) == ("SPY",)
    target = newest_reducible_entry(books, "bot-b", "spy")
    assert target is not None and target.order_ref == one.order_ref


# ── fills land on the legs ────────────────────────────────────────────────


def test_fills_land_on_the_legs_rows_and_move_each_contracts_position_by_its_sign(books: ClerkSqliteRepository) -> None:
    accepted = _accept(books, _received(units=2))

    _fill_leg(books, accepted, 1, symbol=C580, side="SELL", contracts=2, price=5.31)
    _fill_leg(books, accepted, 2, symbol=C585, side="BUY", contracts=2, price=2.24)

    assert books.attributed_positions_for_strategy("bot-a") == {C580: -2.0, C585: 2.0}
    with books._write_lock:
        records = effective_fill_records(books._conn, account_id=ACCOUNT_ID)
    assert sorted((record.symbol, record.side.value, record.quantity, record.fill_price, record.multiplier) for record in records) == [
        (C580, "sell", 2.0, 5.31, 100), (C585, "buy", 2.0, 2.24, 100),
    ]
    # No fill ever lands on the parent.
    assert books.fills_for_order(accepted.order_ref) == []


def test_a_fill_in_a_legs_contract_on_the_parent_or_on_the_other_leg_is_refused(books: ClerkSqliteRepository) -> None:
    accepted = _accept(books, _paid())

    with pytest.raises(Exception, match="not bound to a contract"):
        fill(books, accepted, execution_id="x-parent", symbol=C585, side="BUY", quantity=1, price=3.14)
    with pytest.raises(Exception, match="which trades"):
        _fill_leg(books, accepted, 1, symbol=C590, side="BUY", contracts=1, price=1.20)
    assert books.attributed_positions_for_strategy("bot-a") == {}


def test_what_is_held_of_an_entry_is_each_legs_contracts_signed_and_the_whole_units_they_come_to(
    books: ClerkSqliteRepository,
) -> None:
    accepted = _accept(books, _received(units=2))

    nothing = held_of_entry(books, accepted.order_ref)
    assert ([leg.held for leg in nothing.legs], nothing.whole.units, nothing.whole.in_step) == ([0, 0], 0, True)

    _fill_leg(books, accepted, 1, symbol=C580, side="SELL", contracts=2, price=5.31)
    _fill_leg(books, accepted, 2, symbol=C585, side="BUY", contracts=1, price=2.24)
    uneven = held_of_entry(books, accepted.order_ref)
    # Two sold and one bought: one whole structure, and one sold contract beyond it.
    assert [(leg.leg_id, leg.symbol, leg.position, leg.held) for leg in uneven.legs] == [
        ("short_call", C580, "short", -2), ("long_call", C585, "long", 1),
    ]
    assert (uneven.whole.units, uneven.whole.in_step, dict(uneven.whole.beyond)) == (1, False, {"short_call": 1, "long_call": 0})

    _fill_leg(books, accepted, 2, symbol=C585, side="BUY", contracts=1, price=2.24, execution="second")
    whole = held_of_entry(books, accepted.order_ref)
    assert (whole.entry_order_ref, whole.whole.units, whole.whole.in_step) == (accepted.order_ref, 2, True)
    with pytest.raises(ValueError, match="not an entry in option contracts"):
        held_of_entry(books, f"{accepted.order_ref}~1")


# ── counts and proofs name the order that was sent ────────────────────────


def _acknowledge(repo: ClerkSqliteRepository, accepted: EnterSubmission, order_ref: str, *, state: str, broker_id: str) -> None:
    from app.broker.alpaca.clerk.sqlite.facts import OrderSubmitAckedFacts
    from app.broker.alpaca.clerk.sqlite.models import TransitionInput

    repo.append_transition(
        TransitionInput(
            strategy_instance_id=accepted.command.strategy_instance_id, run_id=accepted.command.run_id,
            command_id=accepted.command.command_id, effect_operation_id=accepted.effect_operation_id,
            order_ref=order_ref, broker_order_id=broker_id, broker_state=state,
            transition_kind="ORDER_SUBMIT_ACKED", custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress", source_event_at_ms=NOON, clerk_observed_at_ms=repo.clock(),
            summary_code="ORDER_SUBMIT_ACKED", facts_json=OrderSubmitAckedFacts(reported_filled_quantity=None).to_facts_json(),
        )
    )


def test_a_working_order_of_several_legs_is_one_working_order_named_by_its_parent(books: ClerkSqliteRepository) -> None:
    accepted = _accept(books, _paid())
    parent = accepted.order_ref
    for order_ref, broker_id in ((f"{parent}~1", "leg-1"), (f"{parent}~2", "leg-2"), (parent, "parent-1")):
        _acknowledge(books, accepted, order_ref, state="new", broker_id=broker_id)

    # Three rows show working, and the STOP proof counts one order: the one that was sent.
    assert working_order_refs(books, "bot-a") == (parent,)
    assert unresolved_order_refs(books, "bot-a") == ()


def test_history_counts_the_order_that_was_sent_once_and_every_legs_fill(books: ClerkSqliteRepository) -> None:
    accepted = _accept(books, _paid())
    parent = accepted.order_ref
    _fill_leg(books, accepted, 1, symbol=C585, side="BUY", contracts=1, price=3.14)
    _fill_leg(books, accepted, 2, symbol=C590, side="SELL", contracts=1, price=1.20)
    for order_ref, broker_id in ((f"{parent}~1", "leg-1"), (f"{parent}~2", "leg-2"), (parent, "parent-1")):
        _acknowledge(books, accepted, order_ref, state="filled", broker_id=broker_id)

    with books._write_lock:
        history = project_custody_history(
            books._conn, fees=None, fees_unavailable="not read here", strategy_instance_ids=("bot-a",)
        )
    [bot] = history.bots
    # One order sent and filled, though three rows say filled; and both legs' fills.
    assert (bot.orders.sent, bot.orders.filled) == (1, 1)
    assert bot.transactions == 2
    [run] = bot.runs
    assert (run.orders.sent, run.orders.filled, run.transactions) == (1, 1, 2)


def test_a_filled_parent_with_every_legs_fill_on_the_books_leaves_the_pass_nothing_to_ask(
    books: ClerkSqliteRepository,
) -> None:
    accepted = _accept(books, _paid())
    parent = accepted.order_ref

    def worklist() -> list[str]:
        with books._write_lock:
            return [effect.effect_operation_id for effect in reads.reconcilable_effect_operations(books._conn)]

    assert worklist() == [accepted.effect_operation_id]
    _fill_leg(books, accepted, 1, symbol=C585, side="BUY", contracts=1, price=3.14)
    for order_ref, broker_id in ((f"{parent}~1", "leg-1"), (f"{parent}~2", "leg-2"), (parent, "parent-1")):
        _acknowledge(books, accepted, order_ref, state="filled", broker_id=broker_id)
    # The parent says filled and holds no fill of its own, which is as it should be. But the sold
    # leg says filled and the books hold no fill of it: the ENTER still wants fresh evidence.
    assert worklist() == [accepted.effect_operation_id]

    _fill_leg(books, accepted, 2, symbol=C590, side="SELL", contracts=1, price=1.20)
    assert worklist() == []


# ── the schema, and a rebuild ─────────────────────────────────────────────


def test_a_schema_23_store_gains_the_empty_table_and_keeps_every_row(tmp_path: Path) -> None:
    """What a running Clerk's file meets on its first open by this build."""
    repo = open_option_books(tmp_path)
    try:
        accept_option_order(repo, terms=ContractTerms(C590, 100), side="buy", contracts=1, decision_id="d-one")
        database = repo.db_path
        tables = [row[0] for row in _rows(repo, "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        before = {table: _rows(repo, f"SELECT * FROM {table} ORDER BY rowid") for table in tables if table != "option_order_legs"}
    finally:
        repo.close()
    conn = sqlite3.connect(database)
    try:
        conn.execute("DROP TRIGGER trg_orders_effect_state_insert")
        conn.execute("DROP TRIGGER trg_orders_effect_state_update")
        conn.execute("DROP INDEX ix_orders_entry_broker_state")
        conn.execute("ALTER TABLE orders DROP COLUMN effect_state")
        for column in ("active_run_id", "active_resume_command_id", "options_entries_enabled"):
            conn.execute(f"ALTER TABLE deployment_budgets DROP COLUMN {column}")
        for trigger in ("trg_option_order_legs_never_edited", "trg_option_order_legs_never_deleted"):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute("DROP TABLE option_order_legs")
        conn.execute("UPDATE control_meta SET schema_version = 23 WHERE id = 1")
        conn.commit()
    finally:
        conn.close()

    reopened = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=lambda: NOON)
    try:
        assert _rows(reopened, "SELECT COUNT(*) FROM option_order_legs") == [(0,)]
        after = {table: _rows(reopened, f"SELECT * FROM {table} ORDER BY rowid") for table in before}
        # The version, and the times the file was opened, are the only things that moved.
        for moved in ("control_meta",):
            before.pop(moved), after.pop(moved)
        assert after == before
        assert _rows(reopened, "SELECT schema_version FROM control_meta") == [(26,)]
    finally:
        reopened.close()
    assert verify_database(database, expected_account_id=ACCOUNT_ID).schema_version == 26


def test_the_books_rebuilt_from_their_mirror_hold_the_parent_its_legs_and_their_fills(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=lambda: NOON)

    repo.register_strategy_instance(strategy_instance_id="bot-a", symbol="SPY", config_hash="several-legs")
    seed_historical_run_started(repo, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", lifecycle_run_id=RUN_ID, clock=repo.clock)
    accepted = _accept(repo, _received(units=2))
    _fill_leg(repo, accepted, 1, symbol=C580, side="SELL", contracts=2, price=5.31)
    _fill_leg(repo, accepted, 2, symbol=C585, side="BUY", contracts=2, price=2.24)
    kept = ("orders", "option_order_legs", "order_contracts", "option_contracts", "operation_order_links", "positions")
    before = {table: _rows(repo, f"SELECT * FROM {table} ORDER BY rowid") for table in kept}
    database = repo.db_path
    repo.close()
    database.rename(database.with_suffix(".saved"))

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=lambda: NOON)
    try:
        assert {table: _rows(rebuilt, f"SELECT * FROM {table} ORDER BY rowid") for table in kept} == before
        assert len(before["option_order_legs"]) == 2
        assert held_of_entry(rebuilt, accepted.order_ref).whole.units == 2
        verify_database(rebuilt.db_path, expected_account_id=ACCOUNT_ID)
    finally:
        rebuilt.close()
