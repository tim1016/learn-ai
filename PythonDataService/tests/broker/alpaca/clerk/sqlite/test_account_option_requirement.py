"""The account's option requirement across an entry, from what the books attribute to its bots (ADR 0076 decision 26).

The books are real, and a fill reaches them through the Clerk's own fill fold
(``option_custody_fixtures``). Holdings here are SPY calls that expire
2026-09-18, a hundred shares a contract unless a test says another multiplier.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.account_option_requirement import account_requirement_cents
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.entry_buying_power import COMPARED_KIND, EntryBuyingPowerWatch
from app.broker.alpaca.clerk.sqlite.facts import EntryBuyingPowerComparedFacts
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import accept_option_order, fill, open_option_books
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import call
from tests.broker.alpaca.clerk.sqlite.test_multi_leg_evidence import _order
from tests.broker.alpaca.clerk.test_simulated_multi_leg_orders import world as _world_fixture

world = _world_fixture

C580, C585, C590 = call(580).occ_symbol, call(585).occ_symbol, call(590).occ_symbol
# A contract of ten shares: no listing a bot would pick has one, and the books must still value it right.
SMALL_C500, SMALL_C505 = "SPY260918C00500000", "SPY260918C00505000"


@pytest.fixture
def books(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    repo = open_option_books(tmp_path, bots={"bot-a": "SPY", "bot-b": "SPY"})
    try:
        yield repo
    finally:
        repo.close()


def _hold(
    books: ClerkSqliteRepository, sid: str, symbol: str, contracts: int, *, multiplier: int = 100
) -> None:
    """``sid`` is left holding ``contracts`` more of ``symbol`` (negative: sold), through the real fill fold."""
    side = "buy" if contracts > 0 else "sell"
    decision = f"{sid}-{side}-{symbol}"
    accepted = accept_option_order(
        books, terms=ContractTerms(symbol, multiplier), side=side, contracts=abs(contracts), decision_id=decision,
        strategy_instance_id=sid,
    )
    fill(
        books, accepted, execution_id=f"x-{decision}", symbol=symbol, side=side.upper(), quantity=abs(contracts),
        price=1.0,
    )


def _held(books: ClerkSqliteRepository) -> tuple[reads.HeldOptionContract, ...]:
    with books.write_fence() as conn:
        return tuple(reads.held_option_contracts(conn))


def _figures(
    books: ClerkSqliteRepository, *, before: tuple[reads.HeldOptionContract, ...],
    after: tuple[reads.HeldOptionContract, ...],
) -> tuple[int, int] | None:
    with books.write_fence() as conn:
        return account_requirement_cents(conn, account_id=books.account_id, before=before, after=after)


def test_a_first_spread_on_a_flat_account_adds_its_whole_requirement(books: ClerkSqliteRepository) -> None:
    _hold(books, "bot-a", C580, -1)
    _hold(books, "bot-a", C585, 1)

    assert _figures(books, before=(), after=_held(books)) == (0, 50_000)


def test_two_bots_holdings_are_valued_together_and_not_one_bot_at_a_time(books: ClerkSqliteRepository) -> None:
    """Bot A holds the sold $5 spread; bot B holds the same two contracts the other way round."""
    _hold(books, "bot-a", C580, -1)
    _hold(books, "bot-a", C585, 1)
    before = _held(books)
    _hold(books, "bot-b", C580, 1)
    _hold(books, "bot-b", C585, -1)
    after = _held(books)

    # Alpaca values the account as one: what A needs, B's position cancels. Apart they would be $500 and $0.
    assert _figures(books, before=before, after=after) == (50_000, 0)
    assert len(after) == 4


def test_bots_that_share_a_contract_are_one_position_of_the_account(books: ClerkSqliteRepository) -> None:
    _hold(books, "bot-a", C580, -1)
    _hold(books, "bot-b", C585, 1)

    # Neither bot holds a spread; the account does.
    assert _figures(books, before=(), after=_held(books)) == (0, 50_000)


def test_each_contract_is_valued_with_the_multiplier_the_books_recorded_for_it(books: ClerkSqliteRepository) -> None:
    _hold(books, "bot-a", C580, -1)
    _hold(books, "bot-a", C585, 1)
    _hold(books, "bot-b", SMALL_C500, -1, multiplier=10)
    _hold(books, "bot-b", SMALL_C505, 1, multiplier=10)

    # $5 wide at a hundred shares, and $5 wide at ten.
    assert _figures(books, before=(), after=_held(books)) == (0, 50_000 + 5_000)


def test_a_loss_with_no_limit_has_no_figure_and_the_reason_is_logged(
    books: ClerkSqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    _hold(books, "bot-a", C580, -1)

    with caplog.at_level(logging.WARNING):
        figures = _figures(books, before=(), after=_held(books))

    assert figures is None
    [logged] = [r for r in caplog.records if getattr(r, "action", None) == "option_account_requirement_unavailable"]
    assert logged.account_id == books.account_id and "more calls than it buys" in logged.error


def test_a_contract_with_no_recorded_terms_has_no_figure_and_is_not_valued_at_a_guess(
    books: ClerkSqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    unrecorded = (reads.HeldOptionContract("bot-a", C590, 1.0),)

    with caplog.at_level(logging.WARNING):
        figures = _figures(books, before=(), after=unrecorded)

    assert figures is None
    assert any("no recorded terms" in getattr(r, "error", "") for r in caplog.records)


def test_a_fraction_of_a_contract_has_no_figure_and_is_not_rounded(books: ClerkSqliteRepository) -> None:
    _hold(books, "bot-a", C585, 1)
    held = _held(books)

    assert _figures(books, before=(), after=(reads.HeldOptionContract("bot-a", held[0].symbol, 1.5),)) is None


# ── the record ───────────────────────────────────────────────────────────

_RECORD_BEFORE_THE_FIGURES = {
    "result": "unknown", "reason_code": "JUDGMENT_NOT_SWITCHED_ON", "contracts_asked": 1, "contracts_filled": 1,
    "commitment_cents": 19_406, "set_aside_for_filled_cents": 19_406, "premium_paid_cents": 19_400,
    "cost_of_filled_cents": 19_400, "first_began_ms": 1, "first_ended_ms": 2, "first_buying_power": "100000",
    "sent_at_ms": 3, "second_began_ms": 4, "second_ended_ms": 5, "second_buying_power": "99806.00",
    "fall_cents": 19_400, "compared_at_ms": 6, "holds": False,
}


def test_a_record_written_before_the_figures_existed_reads_and_hashes_as_it_did() -> None:
    stored = canonicalize(_RECORD_BEFORE_THE_FIGURES)

    read = EntryBuyingPowerComparedFacts.from_facts_json(stored)

    assert (read.account_requirement_before_cents, read.account_requirement_after_cents) == (None, None)
    # Written back it is the same text, so it is the same hash.
    assert read.to_facts_json() == stored
    assert "account_requirement" not in stored


def test_a_record_with_the_figures_carries_both() -> None:
    record = EntryBuyingPowerComparedFacts(
        **_RECORD_BEFORE_THE_FIGURES, account_requirement_before_cents=0, account_requirement_after_cents=50_000,
    )

    again = EntryBuyingPowerComparedFacts.from_facts_json(record.to_facts_json())

    assert again == record
    assert json.loads(record.to_facts_json())["account_requirement_after_cents"] == 50_000


@pytest.mark.parametrize(
    ("before", "after"),
    [(0, None), (None, 0), (-1, 0), (0, 1.5)],
    ids=["no after", "no before", "a negative requirement", "a fraction of a cent"],
)
def test_the_two_figures_are_whole_cents_recorded_together_or_not_at_all(
    before: object, after: object
) -> None:
    with pytest.raises(ValueError, match="account's requirement"):
        EntryBuyingPowerComparedFacts(
            **_RECORD_BEFORE_THE_FIGURES,
            account_requirement_before_cents=before,  # type: ignore[arg-type]
            account_requirement_after_cents=after,  # type: ignore[arg-type]
        )


# ── written from the books as the readings began ─────────────────────────


class _OptionAccount:
    """What the watch reads of the account: an option buying power, and no cash transfers."""

    def __init__(self) -> None:
        self.buying_power = Decimal("100000")

    async def get_option_account(self) -> SimpleNamespace:
        return SimpleNamespace(options_buying_power=self.buying_power)

    async def list_activities(self, **_kwargs: object) -> list[object]:
        return []


@pytest.mark.parametrize("judged", [False, True], ids=["recorded only", "judged"])
async def test_an_entrys_record_holds_the_accounts_requirement_as_each_reading_began(world, judged: bool) -> None:
    """Bot B holds a sold $5 spread of two. Bot A then sells another two: $1,000 of requirement before, $2,000 after."""
    books, _port = world
    held_by_b, held_evidence = await _order(world, received=True, decision="d0", sid="bot-b")
    fold_order_evidence(books, effect_operation_id=held_by_b.effect_operation_id, order=held_evidence)
    account = _OptionAccount()
    watch = EntryBuyingPowerWatch(repo=books, read=account, intake=ReentrantAsyncLock(), judged=lambda: judged)
    key = ("bot-a", "d1")
    await watch.finish_first(key, watch.begin_first())

    accepted, evidence = await _order(world, received=True, decision="d1", sid="bot-a")
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=evidence)
    # Two sold spreads at $3.07 are $614.00 received, and $1,000.00 of requirement: Alpaca takes $386.00.
    account.buying_power = Decimal("99614.00")
    watch.sent(key, order_ref=accepted.order_ref, sent_at_ms=NOON)
    # The first round takes the second reading, and records bot B's earlier entry as one a restart left.
    assert await watch.round(last_pass_at_ms=NOON + 1, last_pass_clean=True) == (held_by_b.order_ref,)
    assert await watch.round(last_pass_at_ms=NOON + 1, last_pass_clean=True) == (accepted.order_ref,)

    recorded = EntryBuyingPowerComparedFacts.from_facts_json(books.first_effect_transition(
        effect_operation_id=accepted.effect_operation_id, transition_kind=COMPARED_KIND,
    )["facts_json"])
    assert (recorded.account_requirement_before_cents, recorded.account_requirement_after_cents) == (100_000, 200_000)
    # The figures are evidence beside the judgment and never an input to it: what the entry cost, and what
    # Alpaca took, are the same whether it is judged or only recorded.
    assert (recorded.fall_cents, recorded.cost_of_filled_cents) == (38_600, 38_600)
    assert (recorded.result, recorded.reason_code, recorded.holds) == (
        ("consistent", None, False) if judged else ("unknown", "JUDGMENT_NOT_SWITCHED_ON", False)
    )


async def test_an_entry_with_no_second_reading_records_no_figures_and_says_nothing_about_them(world) -> None:
    books, _port = world
    accepted, evidence = await _order(world, received=True, decision="d1", sid="bot-a")
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=evidence)
    watch = EntryBuyingPowerWatch(repo=books, read=_OptionAccount(), intake=ReentrantAsyncLock(), judged=lambda: True)
    watch.sent(("bot-a", "d1"), order_ref=accepted.order_ref, sent_at_ms=NOON)

    assert await watch.round(last_pass_at_ms=NOON + 1, last_pass_clean=True) == (accepted.order_ref,)

    recorded = EntryBuyingPowerComparedFacts.from_facts_json(books.first_effect_transition(
        effect_operation_id=accepted.effect_operation_id, transition_kind=COMPARED_KIND,
    )["facts_json"])
    assert recorded.reason_code == "FIRST_READING_FAILED"
    assert (recorded.account_requirement_before_cents, recorded.account_requirement_after_cents) == (None, None)
    assert "account_requirement" not in recorded.to_facts_json()
