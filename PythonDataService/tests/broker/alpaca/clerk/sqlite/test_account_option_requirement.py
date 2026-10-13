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

from app.broker.alpaca.clerk.sqlite import account_option_requirement, reads
from app.broker.alpaca.clerk.sqlite.account_option_requirement import account_requirement_cents
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.enter import accept_option_enter
from app.broker.alpaca.clerk.sqlite.entry_buying_power import COMPARED_KIND, EntryBuyingPowerWatch
from app.broker.alpaca.clerk.sqlite.facts import EntryBuyingPowerComparedFacts
from app.broker.alpaca.clerk.sqlite.hashchain import GENESIS, PAYLOAD_COLUMNS, canonical_payload, compute_row_hash
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.option_order_evidence import multi_leg_order_evidence
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.services.option_buying_power_views import option_buying_power_checks
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import (
    ACCOUNT_ID,
    RUN_ID,
    accept_option_order,
    fill,
    open_option_books,
)
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import PAID_SPREAD, call, worked_entry
from tests.broker.alpaca.clerk.sqlite.test_multi_leg_evidence import _order
from tests.broker.alpaca.clerk.test_simulated_multi_leg_orders import _bind
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

# The record of one option entry as the Clerk stored it before the figures existed (90937b3c): sorted keys, no
# whitespace. The chain hashes a row over this text, so the text and the row's hash are pinned as literals.
_STORED_BEFORE_THE_FIGURES = (
    '{"commitment_cents":19406,"compared_at_ms":6,"contracts_asked":1,"contracts_filled":1,'
    '"cost_of_filled_cents":19400,"fall_cents":19400,"first_began_ms":1,"first_buying_power":"100000",'
    '"first_ended_ms":2,"holds":false,"premium_paid_cents":19400,"reason_code":"JUDGMENT_NOT_SWITCHED_ON",'
    '"result":"unknown","second_began_ms":4,"second_buying_power":"99806.00","second_ended_ms":5,'
    '"sent_at_ms":3,"set_aside_for_filled_cents":19406}'
)
_ROW_HASH_BEFORE_THE_FIGURES = "1d8f9218476dce066726a831a902a0d22e8ab9e4a2ffb9bbd106dbfd837665d1"
_RECORD_BEFORE_THE_FIGURES = json.loads(_STORED_BEFORE_THE_FIGURES)


def _row_of(facts_json: str) -> dict[str, object]:
    """The custody row a comparison record is written as, with the fields the chain hashes."""
    row: dict[str, object] = dict.fromkeys(PAYLOAD_COLUMNS)
    row.update(
        transition_kind=COMPARED_KIND, custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
        operation_state="succeeded", clerk_observed_at_ms=6, summary_code="JUDGMENT_NOT_SWITCHED_ON",
        facts_json=facts_json,
    )
    return row


def test_a_record_written_before_the_figures_existed_reads_and_hashes_as_it_did() -> None:
    read = EntryBuyingPowerComparedFacts.from_facts_json(_STORED_BEFORE_THE_FIGURES)

    assert (read.account_requirement_before_cents, read.account_requirement_after_cents) == (None, None)
    # Written back it is the same text, so the chain hashes the same row.
    assert read.to_facts_json() == _STORED_BEFORE_THE_FIGURES
    assert compute_row_hash(GENESIS, canonical_payload(_row_of(read.to_facts_json()))) == _ROW_HASH_BEFORE_THE_FIGURES


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


async def _entered_beside_another_bots_spread(
    world, *, judged: bool, buying_power_after: str, picks: dict | None = None, plan=None,
) -> tuple[ClerkSqliteRepository, EntryBuyingPowerComparedFacts]:
    """Bot B holds a sold $5 spread of two; bot A then enters ``plan`` at ``picks``; its comparison is recorded.

    Bot A's reading of the account is begun before its entry exists and its second after its fills are folded.
    The two rounds are the watch's: the first takes the second reading (and records bot B's earlier entry as one
    a restart left), the second records bot A's.
    """
    books, _port = world
    held_by_b, held_evidence = await _order(world, received=True, decision="d0", sid="bot-b")
    fold_order_evidence(books, effect_operation_id=held_by_b.effect_operation_id, order=held_evidence)
    account = _OptionAccount()
    watch = EntryBuyingPowerWatch(repo=books, read=account, intake=ReentrantAsyncLock(), judged=lambda: judged)
    key = ("bot-a", "d1")
    await watch.finish_first(key, watch.begin_first())

    if picks is None:
        accepted, evidence = await _order(world, received=True, decision="d1", sid="bot-a")
    else:
        accepted, evidence = await _enter(world, plan, picks, decision="d1", sid="bot-a")
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=evidence)
    account.buying_power = Decimal(buying_power_after)
    watch.sent(key, order_ref=accepted.order_ref, sent_at_ms=NOON)
    assert await watch.round(last_pass_at_ms=NOON + 1, last_pass_clean=True) == (held_by_b.order_ref,)
    assert await watch.round(last_pass_at_ms=NOON + 1, last_pass_clean=True) == (accepted.order_ref,)
    recorded = EntryBuyingPowerComparedFacts.from_facts_json(books.first_effect_transition(
        effect_operation_id=accepted.effect_operation_id, transition_kind=COMPARED_KIND,
    )["facts_json"])
    return books, recorded


async def _enter(world, plan, picks, *, decision: str, sid: str):
    """An entry of two units at ``picks``, accepted and sent to the simulation, as ``_order`` does for the fixtures."""
    books, port = world
    entry = worked_entry(plan, picks, units=2, now_ms=NOON)
    accepted = accept_option_enter(
        books, account_id=ACCOUNT_ID, strategy_instance_id=sid, decision_id=decision, lifecycle_run_id=RUN_ID,
        entry=entry,
    )
    assert accepted.order_ref is not None
    _bind(port, accepted.order_ref, entry)
    return accepted, multi_leg_order_evidence(
        await port.submit_option_order(entry.order, client_order_id=accepted.order_ref)
    )


@pytest.mark.parametrize("judged", [False, True], ids=["recorded only", "judged"])
async def test_an_entrys_record_holds_the_accounts_requirement_as_each_reading_began(world, judged: bool) -> None:
    """Bot B holds a sold $5 spread of two. Bot A then sells another two: $1,000 of requirement before, $2,000 after."""
    # Two sold spreads at $3.07 are $614.00 received, and $1,000.00 of requirement: Alpaca takes $386.00.
    _books, recorded = await _entered_beside_another_bots_spread(world, judged=judged, buying_power_after="99614.00")

    assert (recorded.account_requirement_before_cents, recorded.account_requirement_after_cents) == (100_000, 200_000)
    # The figures are evidence beside the judgment and never an input to it: what the entry cost, and what
    # Alpaca took, are the same whether it is judged or only recorded.
    assert (recorded.fall_cents, recorded.cost_of_filled_cents) == (38_600, 38_600)
    assert (recorded.result, recorded.reason_code, recorded.holds) == (
        ("consistent", None, False) if judged else ("unknown", "JUDGMENT_NOT_SWITCHED_ON", False)
    )


# Bot A buys back the contracts bot B sold: long the 580 call and short the 585 call, at the $5 width B is short.
_BUYS_IT_BACK = {"long_call": (call(580), "5.31", "5.35"), "short_call": (call(585), "2.20", "2.24")}


async def test_an_entry_that_nets_against_another_bots_spread_shows_the_requirement_it_released(world) -> None:
    """Alpaca's netting: the account's $1,000 goes to nothing, though bot A set aside a debit spread's premium."""
    books, recorded = await _entered_beside_another_bots_spread(
        world, judged=False, buying_power_after="100000", picks=_BUYS_IT_BACK, plan=PAID_SPREAD,
    )

    assert (recorded.account_requirement_before_cents, recorded.account_requirement_after_cents) == (100_000, 0)
    page = option_buying_power_checks(books, strategy_instance_id="bot-a", simulation=False, judged=False)
    [check] = page.checks
    # Served signed, so a release is a negative amount and is worded as one.
    assert check.account_requirement_change is not None
    assert check.account_requirement_change.model_dump() == {"cents": -100_000, "dollars": "-1000.00"}
    assert "fell by $1000.00" in check.account_requirement_sentence
    assert "$1000.00 before and $0.00 after" in check.account_requirement_sentence
    # What the bot set aside is its own premium and fees, and says nothing of the account.
    assert check.set_aside.cents > 0


async def test_evidence_that_fails_unexpectedly_leaves_a_judged_result_as_it_was(
    world, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("an arithmetic nobody expected")

    monkeypatch.setattr(account_option_requirement, "incremental_account_requirement", broken)

    with caplog.at_level(logging.WARNING):
        _books, recorded = await _entered_beside_another_bots_spread(
            world, judged=True, buying_power_after="99614.00"
        )

    # Judged as it would have been, with no figures: the comparison is not failed for evidence's sake.
    assert (recorded.result, recorded.reason_code, recorded.holds) == ("consistent", None, False)
    assert (recorded.account_requirement_before_cents, recorded.account_requirement_after_cents) == (None, None)
    [logged] = [r for r in caplog.records if getattr(r, "action", None) == "option_account_requirement_failed"]
    assert logged.exc_info is not None and "an arithmetic nobody expected" in str(logged.exc_info[1])


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
