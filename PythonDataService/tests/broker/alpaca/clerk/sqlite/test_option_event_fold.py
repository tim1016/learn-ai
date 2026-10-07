"""The fold of ``OPTION_POSITION_ENDED`` works the money out again and refuses what the contract does not give (#2913).

The batch function never hands it a wrong fact. A replay of a damaged log,
or a later caller's mistake, could: so the fold is its own fence.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.simulated_option_settlement import (
    SIMULATED_SETTLEMENT_RULE,
    SettlementPrice,
    SimulatedOutcome,
    SynthesizedOptionSettlement,
)
from app.broker.alpaca.clerk.sqlite import fee_evidence
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.fee_evidence import custody_fee_attribution
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.option_event_folds import OPTION_POSITION_ENDED
from app.broker.alpaca.clerk.sqlite.option_events import book_option_events
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.sqlite.conftest import (
    _broker_order_fixture,
    _FakeReadPort,
    complete_fee_evidence,
    documented_option_activity,
)
from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import (
    bookings,
    books_at,
    contract,
    fifo,
    held,
    holds_option,
    later,
    poll,
)

CALL = "AAPL230721C00150000"


async def _booked_exercise(tmp_path: Path) -> tuple[ClerkSqliteRepository, dict[str, Any]]:
    """Books on which one exercise of two calls was booked, and the facts it was booked with."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL", "bot-b": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=2)
    holds_option(repo, "bot-b", CALL, contracts=2)
    account = _FakeReadPort()
    account.option_events.rows = [
        documented_option_activity("trading_api", "option_exercise", id="exc-1"),
        documented_option_activity("trading_api", "option_exercise", 1, id="trd-1", price="150"),
    ]
    await poll(repo, account)
    [booked] = bookings(repo)
    assert booked["strategy_instance_id"] == "bot-a"
    return repo, json.loads(booked["facts_json"])


def _as_for_bot_b(facts: dict[str, Any], *, observed_at_ms: int = 0, **changes: Any) -> TransitionInput:
    """The same booking handed to the fold again for the other bot, with ``changes`` laid over its facts."""
    key = facts["idempotency_key"].removesuffix("bot-a") + "bot-b"
    altered = {
        **facts, "idempotency_key": key, "option_order_ref": "learn-ai/bot-b/v1:oc-again",
        "close_execution_id": "option-event:close:again:bot-b",
        **(
            {"shares": {**facts["shares"], "order_ref": "learn-ai/bot-b/v1:os-again",
                        "execution_id": "option-event:shares:again:bot-b"}}
            if "shares" in facts else {}
        ),
        **changes,
    }
    return TransitionInput(
        strategy_instance_id="bot-b", command_id=f"cmd:{key}", effect_operation_id=f"effect:{key}",
        order_ref=altered["option_order_ref"], transition_kind=OPTION_POSITION_ENDED, custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK", operation_state="succeeded",
        source_event_at_ms=altered["booked_as_of_ms"], clerk_observed_at_ms=observed_at_ms,
        summary_code="OPTION_EXERCISED",
        facts_json=json.dumps({name: value for name, value in altered.items() if value is not None}, sort_keys=True),
    )


async def test_the_fold_books_the_same_facts_for_another_bot_that_holds_the_contract(tmp_path: Path) -> None:
    """The control: unchanged facts are booked. Every refusal below changes one thing from this."""
    repo, facts = await _booked_exercise(tmp_path)
    try:
        repo.append_transition(_as_for_bot_b(facts))

        assert held(repo, "bot-b") == {"AAPL": 200.0}
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("changes", "refused_for"),
    [
        pytest.param(
            {"contracts": 3, "shares": {"symbol": "AAPL", "quantity": 300, "price": "150", "cash_cents": -4_500_000,
                                        "order_ref": "learn-ai/bot-b/v1:os-again",
                                        "execution_id": "option-event:shares:again:bot-b"}},
            "cannot be ended", id="more contracts than the bot holds",
        ),
        pytest.param({"held": "sold"}, "only a bought option is exercised", id="the side an exercise cannot end"),
        pytest.param({"close_price": "0.40"}, "price of zero", id="an exercise that closes its lot at a price"),
        pytest.param({"multiplier": 10}, "not on the books with a multiplier", id="a multiplier the books do not hold"),
        pytest.param({"ended_by": "expired"}, "moves no shares", id="an expiry that moves shares"),
    ],
)
async def test_the_fold_refuses_a_fact_the_contracts_terms_do_not_give_and_writes_nothing(
    tmp_path: Path, changes: dict[str, Any], refused_for: str
) -> None:
    repo, facts = await _booked_exercise(tmp_path)
    try:
        with pytest.raises(ValueError, match=refused_for):
            repo.append_transition(_as_for_bot_b(facts, **changes))

        assert held(repo, "bot-b") == {CALL: 2.0} and len(bookings(repo)) == 1
    finally:
        repo.close()


@pytest.mark.parametrize(
    "wrong",
    [
        pytest.param({"quantity": 100}, id="half the shares"),
        pytest.param({"quantity": -200}, id="shares sold where a call's holder buys"),
        pytest.param({"price": "90"}, id="a price that is not the strike"),
        pytest.param({"cash_cents": -3_000_001}, id="cash a cent out"),
        pytest.param({"symbol": "MSFT"}, id="shares in a symbol the bot is not registered under"),
    ],
)
async def test_the_fold_refuses_shares_or_cash_that_are_not_the_contracts(tmp_path: Path, wrong: dict[str, Any]) -> None:
    repo, facts = await _booked_exercise(tmp_path)
    try:
        with pytest.raises(ValueError):
            repo.append_transition(_as_for_bot_b(facts, shares={
                **facts["shares"], "order_ref": "learn-ai/bot-b/v1:os-again",
                "execution_id": "option-event:shares:again:bot-b", **wrong,
            }))

        assert held(repo, "bot-b") == {CALL: 2.0} and len(bookings(repo)) == 1
    finally:
        repo.close()


async def test_the_fold_refuses_a_booking_stamped_with_another_time_than_its_facts_state(tmp_path: Path) -> None:
    """When an event is counted decides the session its money falls in and the lot FIFO closes with it."""
    repo, facts = await _booked_exercise(tmp_path)
    try:
        booking = _as_for_bot_b(facts)
        with pytest.raises(ValueError, match="booking's time"):
            repo.append_transition(replace(booking, source_event_at_ms=facts["booked_as_of_ms"] + 1))

        assert held(repo, "bot-b") == {CALL: 2.0} and len(bookings(repo)) == 1
    finally:
        repo.close()


async def test_the_fold_refuses_an_exercise_that_names_an_outside_order(tmp_path: Path) -> None:
    """Naming an outside order takes that order out of the money reads' count. Only its own sale may."""
    repo, facts = await _booked_exercise(tmp_path)
    try:
        with pytest.raises(ValueError, match="names the outside order"):
            repo.append_transition(replace(_as_for_bot_b(facts), broker_order_id="by-hand-1"))

        assert held(repo, "bot-b") == {CALL: 2.0} and len(bookings(repo)) == 1
    finally:
        repo.close()


def test_the_fold_refuses_a_sale_that_does_not_name_the_order_it_books(tmp_path: Path) -> None:
    """A sale booked with no order named would leave that order counted outside the books as well."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL", "bot-b": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1)
    try:
        observe_external_order(
            repo,
            order=_broker_order_fixture(
                "not-ours-1", order_id="alpaca-sells-1", symbol=CALL, status="filled", side="sell", quantity=1,
                filled_quantity=1, filled_avg_price=0.40,
            ),
        )
        book_option_events(repo, None)
        [booked] = bookings(repo)
        holds_option(repo, "bot-b", CALL, contracts=1)
        # The same sale handed to the fold again for the other bot. A sale has no date of its own.
        facts = json.loads(booked["facts_json"])
        key = facts["idempotency_key"].removesuffix("bot-a") + "bot-b"
        again = replace(
            _as_for_bot_b({**facts, "event_date_ms": 0}, observed_at_ms=repo.clock()),
            facts_json=json.dumps(
                {**facts, "idempotency_key": key, "option_order_ref": "learn-ai/bot-b/v1:oc-again",
                 "close_execution_id": "option-outside-sale:fill:again:bot-b"},
                sort_keys=True,
            ),
        )

        for named in (None, "another-order"):
            with pytest.raises(ValueError, match="names the outside order"):
                repo.append_transition(replace(again, broker_order_id=named))
        assert held(repo, "bot-b") == {CALL: 1.0} and len(bookings(repo)) == 1

        repo.append_transition(replace(again, broker_order_id="alpaca-sells-1"))
        assert held(repo, "bot-b") == {}
    finally:
        repo.close()


async def test_no_fee_is_predicted_for_a_lot_an_event_closed_and_its_share_trade_is_priced_as_shares(
    tmp_path: Path,
) -> None:
    """An expiry, an exercise and an assignment trade nothing in the option: the fee model is never asked to
    price that fill, which it would refuse at a price of zero. The shares are a fill in shares."""
    spy_call = "SPY260918C00585000"
    repo = books_at(tmp_path, date(2026, 9, 17), {"bot-a": "SPY"})
    holds_option(repo, "bot-a", spy_call, contracts=1)
    later(repo, 2 * 86_400_000)
    account = _FakeReadPort()
    account.option_events.rows = [
        documented_option_activity("trading_api", "option_exercise", id="exc-1", symbol=spy_call, date="2026-09-18", qty="-1"),
        documented_option_activity(
            "trading_api", "option_exercise", 1, id="trd-1", symbol="SPY", date="2026-09-18", qty="100", price="585",
            net_amount="-58500",
        ),
    ]
    try:
        await poll(repo, account)
        complete_fee_evidence(repo)
        with repo.write_fence() as conn:
            fees = custody_fee_attribution(conn, now_ms=repo.clock(), evidence_checked_at_ms=repo.clock())
            population, _orders, _complete = fee_evidence._effective_fills(conn)

        priced = [fill.fill_id for fills in population.values() for fill in fills]
        assert not any(fill_id.startswith("option-event:close:") for fill_id in priced)
        assert sum(fill_id.startswith("option-event:shares:") for fill_id in priced) == 1
        # The model priced what is left, the entry and the shares, and refused nothing.
        assert fees.predicted_total is not None and fees.unresolved == ()
    finally:
        repo.close()


async def test_two_exercises_of_one_day_are_each_paired_with_the_share_trade_that_meets_their_terms(
    tmp_path: Path,
) -> None:
    """Two contracts on one underlying exercised the same day. Each share trade is paired with the event whose
    terms it meets, whichever comes first in the read, so neither is named as a difference."""
    other = "AAPL230721C00160000"
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL", "bot-b": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1)
    holds_option(repo, "bot-b", other, contracts=2)
    account = _FakeReadPort()
    account.option_events.rows = [
        documented_option_activity("trading_api", "option_exercise", id="exc-a", qty="-1"),
        documented_option_activity("trading_api", "option_exercise", id="exc-b", qty="-2", symbol=other),
        # In the read, the trade of 200 at 160 sorts before the trade of 100 at 150.
        documented_option_activity("trading_api", "option_exercise", 1, id="trd-1", qty="200", price="160", net_amount="-32000"),
        documented_option_activity("trading_api", "option_exercise", 1, id="trd-2", qty="100", price="150", net_amount="-15000"),
    ]
    try:
        await poll(repo, account)

        assert held(repo, "bot-a") == {"AAPL": 100.0} and held(repo, "bot-b") == {"AAPL": 200.0}
        paired = {row["strategy_instance_id"]: json.loads(row["facts_json"])["share_trade"] for row in bookings(repo)}
        assert paired["bot-a"] == {"activity_id": "trd-2", "agrees": True, "differences": []}
        assert paired["bot-b"] == {"activity_id": "trd-1", "agrees": True, "differences": []}
        assert {reason for _bot, reason in option_events_holds(repo)} == {"OPTION_SHARES_HELD"}
    finally:
        repo.close()


def option_events_holds(repo: ClerkSqliteRepository) -> set[tuple[str | None, str]]:
    from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import option_holds

    return set(option_holds(repo))


def test_no_hold_of_the_family_is_named_twice() -> None:
    """That every hold has words of its own is held where the module is loaded: it would not import without."""
    from app.broker.alpaca.clerk.sqlite.uncertainty_causes import OPTION_EVENT_HOLD_REASON_CODES

    assert len(set(OPTION_EVENT_HOLD_REASON_CODES)) == len(OPTION_EVENT_HOLD_REASON_CODES)


# ── a simulation's booking: its own record, or nothing ───────────────────


async def _booked_expiry(tmp_path: Path) -> tuple[ClerkSqliteRepository, dict[str, Any]]:
    """Books on which a real expiry of bot-a's two calls was booked, and its facts. Bot-b still holds two."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL", "bot-b": "AAPL"})
    holds_option(repo, "bot-b", CALL, contracts=2)
    holds_option(repo, "bot-a", CALL, contracts=2)
    account = _FakeReadPort()
    account.option_events.rows = [documented_option_activity("trading_api", "otm_option_expiry")]
    later(repo, 2 * 86_400_000)
    await poll(repo, account)
    [booked] = bookings(repo)
    assert (booked["strategy_instance_id"], booked["summary_code"]) == ("bot-a", "OPTION_EXPIRED")
    return repo, json.loads(booked["facts_json"])


def _a_simulations_record(close: str, **changes: Any) -> dict[str, Any]:
    """A simulation's record of two AAPL 150 calls settled from ``close``: at 155.25, 5.25 a share and $1,050.00."""
    price = Decimal(close)
    value = max(price - 150, Decimal(0))
    booked = Decimal(int(value * 100)).scaleb(-2)
    record = SynthesizedOptionSettlement(
        contract=contract(CALL), contracts=2, multiplier=100, last_session_close_ms=1_689_969_600_000,
        settled_at_ms=1_690_000_000_000, rule=SIMULATED_SETTLEMENT_RULE,
        outcome=SimulatedOutcome.SETTLED_IN_CASH if booked else SimulatedOutcome.EXPIRED,
        price=SettlementPrice(
            underlying_price=price, bar_ref="bar-1", bar_provider="ibkr", bar_end_ms=1_689_969_600_000,
            bar_before_close_ms=0,
        ),
        exercise_value=value, booked_price=booked, cash_cents=int(booked * 100) * 2 * 100,
        fee_cents=13 if booked else None,
    )
    return {**record.note(), **changes}


_SETTLED_IN_CASH = {
    "ended_by": "settled_in_cash", "close_price": "5.25", "simulated": True,
    "close_execution_id": "option-event:cash:again:bot-b",
}


async def test_a_simulations_cash_settlement_is_booked_as_a_sale_at_the_price_its_own_record_gives(
    tmp_path: Path,
) -> None:
    """Two calls bought at 1.94 and settled at 5.25 a share: 2 x 100 x (5.25 - 1.94) = $662.00 made. No shares."""
    repo, facts = await _booked_expiry(tmp_path)
    try:
        repo.append_transition(_as_for_bot_b(facts, **_SETTLED_IN_CASH, simulation=_a_simulations_record("155.25")))

        assert held(repo, "bot-b") == {}
        assert fifo(repo, "bot-b").exact_realized_pnl == Decimal("662")
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("changes", "refused_for"),
    [
        pytest.param({"simulated": True}, "carries the simulation's own record", id="a simulated expiry with no record"),
        pytest.param(
            {**_SETTLED_IN_CASH, "simulation": None}, "carries the simulation's own record",
            id="a simulated cash settlement with no record",
        ),
        pytest.param(
            {"simulation": _a_simulations_record("149")}, "carries no simulation's record",
            id="a real account's expiry that carries a simulation's record",
        ),
        pytest.param(
            {**_SETTLED_IN_CASH, "simulated": False}, "only a simulation settles",
            id="a cash settlement no simulation made",
        ),
        pytest.param(
            {**_SETTLED_IN_CASH, "close_price": "5.26", "simulation": _a_simulations_record("155.25")}, "is worth",
            id="a cash settlement a cent above what its record gives",
        ),
        pytest.param(
            {**_SETTLED_IN_CASH, "simulation": _a_simulations_record("149")}, "is worth",
            id="a cash settlement whose record says the contract was worth nothing",
        ),
        pytest.param(
            {"simulated": True, "simulation": _a_simulations_record("155.25")}, "is worth",
            id="an expiry at nothing whose record says the contract was worth 5.25",
        ),
        pytest.param(
            {**_SETTLED_IN_CASH, "close_price": "0.40", "simulation": _a_simulations_record("155.25")}, "is worth",
            id="a cash settlement at a price no record gives",
        ),
        pytest.param(
            {**_SETTLED_IN_CASH, "simulation": _a_simulations_record("155.25"),
             "close_execution_id": "option-event:close:again:bot-b"},
            "is named", id="a sale filed as a close at nothing, which is charged no fee",
        ),
        pytest.param(
            {"ended_by": "expired", "close_execution_id": "option-event:cash:again:bot-b"}, "is named",
            id="a close at nothing filed as a sale",
        ),
        pytest.param(
            {**_SETTLED_IN_CASH, "simulation": _a_simulations_record("155.25"), "held": "sold"},
            "only contracts it bought", id="a cash settlement of a sold lot",
        ),
        pytest.param(
            {**_SETTLED_IN_CASH, "simulation": _a_simulations_record("155.25", contracts=1, cash_cents=52_500)},
            "from a settlement of 1", id="more contracts than the simulation settled",
        ),
        pytest.param(
            {"ended_by": "exercised", "simulated": True, "simulation": _a_simulations_record("155.25"),
             "shares": {"symbol": "AAPL", "quantity": 200, "price": "150", "cash_cents": -3_000_000,
                        "order_ref": "learn-ai/bot-b/v1:os-again", "execution_id": "option-event:shares:again:bot-b"}},
            "never as exercised", id="a simulation that exercises into shares",
        ),
        pytest.param(
            {**_SETTLED_IN_CASH, "simulation": _a_simulations_record("155.25"),
             "shares": {"symbol": "AAPL", "quantity": 200, "price": "150", "cash_cents": -3_000_000,
                        "order_ref": "learn-ai/bot-b/v1:os-again", "execution_id": "option-event:shares:again:bot-b"}},
            "moves no shares", id="a cash settlement that moves shares",
        ),
    ],
)
async def test_the_fold_refuses_a_simulated_booking_its_own_record_does_not_bear_out_and_writes_nothing(
    tmp_path: Path, changes: dict[str, Any], refused_for: str
) -> None:
    """A price above zero reaches the books only with the record that says where it came from."""
    repo, facts = await _booked_expiry(tmp_path)
    try:
        with pytest.raises(ValueError, match=refused_for):
            repo.append_transition(_as_for_bot_b(facts, **changes))

        assert held(repo, "bot-b") == {CALL: 2.0} and len(bookings(repo)) == 1
    finally:
        repo.close()


# ── the owner's ending: who, when and why, or nothing ────────────────────

# AAPL230721C00150000 stops trading at 16:00 New York time on Friday 2023-07-21.
_LAST_SESSION_CLOSE = 1_689_969_600_000
_THE_OWNERS_WORD = {
    "operator": "the-owner", "reason": "nothing will settle it", "said_at_ms": _LAST_SESSION_CLOSE + 86_400_000,
    "why_no_settlement": "The broker reported no activity for the contract and holds none of it.",
}
_ENDED_BY_OWNER = {"ended_by": "ended_by_owner", "owner_word": _THE_OWNERS_WORD}
# A row of the broker's the rule refused by name, as an owner's ending made beside it names it.
_A_REFUSED_ROW = {
    "ref": "option-activity:exp-1", "refused_as": "OPTION_EVENT_MORE_CONTRACTS_THAN_HELD",
    "detail": "3 of AAPL230721C00150000 were expired on 2023-07-21, and the lots open then hold 2.",
}


# A row of the broker's that is booked whole and left contracts on the books, as an owner's ending names it.
_A_BOOKED_ROW = {
    "ref": "option-activity:exp-2",
    "detail": "2 of AAPL230721C00150000 were expired on 2023-07-21, and that is booked: 2 for bot-a.",
}


def _beside(rows: Any) -> dict[str, Any]:
    """An owner's ending whose word names ``rows`` as the broker's rows that could not be booked."""
    return {**_ENDED_BY_OWNER, "owner_word": {**_THE_OWNERS_WORD, "rows_not_booked": rows}}


def _naming(key: str, named: Any) -> dict[str, Any]:
    """An owner's ending whose word lists ``named`` under ``key``: rows that were booked, or outside orders."""
    return {**_ENDED_BY_OWNER, "owner_word": {**_THE_OWNERS_WORD, key: named}}


def _an_owner_endings_record(**changes: Any) -> dict[str, Any]:
    """A simulation's record of two AAPL 150 calls it had no price for and let go of at the owner's word."""
    from app.broker.alpaca.clerk.simulated_option_settlement import SIMULATED_OWNER_ENDING_RULE, OwnersWord

    record = SynthesizedOptionSettlement(
        contract=contract(CALL), outcome=SimulatedOutcome.ENDED_BY_OWNER, contracts=2, multiplier=100,
        last_session_close_ms=_LAST_SESSION_CLOSE, settled_at_ms=_LAST_SESSION_CLOSE + 86_400_000,
        rule=SIMULATED_OWNER_ENDING_RULE, booked_price=Decimal(0), cash_cents=0,
        owner=OwnersWord(operator="the-owner", reason=None, said_at_ms=_LAST_SESSION_CLOSE + 86_400_000),
        why_no_settlement="This simulation kept no price of AAPL from the contract's last session.",
    )
    return {**record.note(), **changes}


async def test_an_owners_ending_closes_the_lot_at_nothing_so_what_it_cost_is_the_loss(tmp_path: Path) -> None:
    """Two calls bought at 1.94, ended at the owner's word: 2 x 100 x 1.94 = $388.00 lost. No shares, no cash."""
    repo, facts = await _booked_expiry(tmp_path)
    try:
        repo.append_transition(_as_for_bot_b(facts, observed_at_ms=_LAST_SESSION_CLOSE, **_ENDED_BY_OWNER))

        assert held(repo, "bot-b") == {}
        assert fifo(repo, "bot-b").exact_realized_pnl == Decimal("-388")
        assert fifo(repo, "bot-b").open_lots == []
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("changes", "observed_at_ms", "refused_for"),
    [
        pytest.param({"ended_by": "ended_by_owner"}, _LAST_SESSION_CLOSE, "records who said so, when", id="no word at all"),
        pytest.param(
            {**_ENDED_BY_OWNER, "owner_word": {**_THE_OWNERS_WORD, "operator": " "}}, _LAST_SESSION_CLOSE,
            "records who said so", id="a word that names nobody",
        ),
        pytest.param(
            {**_ENDED_BY_OWNER, "owner_word": {**_THE_OWNERS_WORD, "said_at_ms": None}}, _LAST_SESSION_CLOSE,
            "records when it was said", id="a word with no time",
        ),
        pytest.param(
            {**_ENDED_BY_OWNER, "owner_word": {**_THE_OWNERS_WORD, "why_no_settlement": ""}}, _LAST_SESSION_CLOSE,
            "records why no settlement", id="a word that does not say why nothing settled it",
        ),
        pytest.param(_ENDED_BY_OWNER, _LAST_SESSION_CLOSE - 1, "still trades", id="a contract that still trades"),
        pytest.param({**_ENDED_BY_OWNER, "held": "sold"}, _LAST_SESSION_CLOSE, "only a bought contract", id="a sold contract"),
        pytest.param(
            {**_ENDED_BY_OWNER, "close_price": "0.40"}, _LAST_SESSION_CLOSE, "price of zero",
            id="an ending at a price, which no word can name",
        ),
        pytest.param(
            {**_ENDED_BY_OWNER,
             "shares": {"symbol": "AAPL", "quantity": 200, "price": "150", "cash_cents": -3_000_000,
                        "order_ref": "learn-ai/bot-b/v1:os-again", "execution_id": "option-event:shares:again:bot-b"}},
            _LAST_SESSION_CLOSE, "moves no shares", id="an ending that moves shares",
        ),
        pytest.param(
            {"owner_word": _THE_OWNERS_WORD}, _LAST_SESSION_CLOSE, "at nobody's word",
            id="an expiry that claims the owner's word",
        ),
        pytest.param(
            {**_ENDED_BY_OWNER, "simulated": True}, _LAST_SESSION_CLOSE, "carries the simulation's own record",
            id="a simulation's ending with no record of the simulation",
        ),
        pytest.param(
            {**_ENDED_BY_OWNER, "simulated": True,
             "simulation": _an_owner_endings_record(underlying_price="155.25")},
            _LAST_SESSION_CLOSE, "had no price", id="a simulation's ending whose record names a price after all",
        ),
        pytest.param(
            {**_ENDED_BY_OWNER, "simulated": True, "simulation": _an_owner_endings_record(cash_cents=52_500)},
            _LAST_SESSION_CLOSE, "counted as worth nothing", id="a simulation's ending whose record paid cash",
        ),
        pytest.param(
            {**_ENDED_BY_OWNER, "simulated": True, "simulation": _a_simulations_record("155.25")},
            _LAST_SESSION_CLOSE, "counted as worth nothing",
            id="a simulation's ending whose record is of a settlement in cash",
        ),
        pytest.param(_beside([]), _LAST_SESSION_CLOSE, "names at least one", id="a list of refused rows that names none"),
        pytest.param(
            _beside([{**_A_REFUSED_ROW, "ref": " "}]), _LAST_SESSION_CLOSE, "carries its reference",
            id="a refused row with no reference",
        ),
        pytest.param(
            _beside([{**_A_REFUSED_ROW, "refused_as": "OPTION_EVENT_UNREADABLE"}]), _LAST_SESSION_CLOSE,
            "the name the rule refused it by", id="a row the rule did not refuse by name",
        ),
        pytest.param(
            _beside([{**_A_REFUSED_ROW, "refused_as": "OPTION_EVENT_CASH_NOT_WHOLE_CENTS"}]), _LAST_SESSION_CLOSE,
            "the name the rule refused it by", id="a row the rule refused under a name no ending is made beside",
        ),
        pytest.param(
            {**_beside([_A_REFUSED_ROW]), "simulated": True, "simulation": _an_owner_endings_record()},
            _LAST_SESSION_CLOSE, "names no row of a broker's", id="a simulation's ending that names a broker's row",
        ),
        pytest.param(
            _naming("rows_booked", []), _LAST_SESSION_CLOSE, "names at least one", id="a list of booked rows that names none"
        ),
        pytest.param(
            _naming("rows_booked", [{**_A_BOOKED_ROW, "ref": " "}]), _LAST_SESSION_CLOSE, "carries its reference",
            id="a booked row with no reference",
        ),
        pytest.param(
            _naming("rows_booked", [{"ref": _A_BOOKED_ROW["ref"]}]), _LAST_SESSION_CLOSE, "what is known of it",
            id="a booked row that does not say how it was booked",
        ),
        pytest.param(
            {**_naming("rows_booked", [_A_BOOKED_ROW]), "simulated": True, "simulation": _an_owner_endings_record()},
            _LAST_SESSION_CLOSE, "a simulated account has no broker",
            id="a simulation's ending that names a row a broker's booking came from",
        ),
    ],
)
async def test_the_fold_refuses_an_owners_ending_that_is_not_whole_and_writes_nothing(
    tmp_path: Path, changes: dict[str, Any], observed_at_ms: int, refused_for: str
) -> None:
    repo, facts = await _booked_expiry(tmp_path)
    try:
        with pytest.raises(ValueError, match=refused_for):
            repo.append_transition(_as_for_bot_b(facts, observed_at_ms=observed_at_ms, **changes))

        assert held(repo, "bot-b") == {CALL: 2.0} and len(bookings(repo)) == 1
    finally:
        repo.close()


async def test_an_owners_ending_made_beside_rows_the_rule_refused_is_booked_with_those_rows_on_its_record(
    tmp_path: Path,
) -> None:
    """The control for the four refusals above: one thing differs from each, and this is booked."""
    repo, facts = await _booked_expiry(tmp_path)
    try:
        repo.append_transition(_as_for_bot_b(facts, observed_at_ms=_LAST_SESSION_CLOSE, **_beside([_A_REFUSED_ROW])))

        assert held(repo, "bot-b") == {} and fifo(repo, "bot-b").exact_realized_pnl == Decimal("-388")
        [ended] = [json.loads(row["facts_json"]) for row in bookings(repo) if row["strategy_instance_id"] == "bot-b"]
        assert ended["owner_word"]["rows_not_booked"] == [_A_REFUSED_ROW]
    finally:
        repo.close()


async def test_an_owners_ending_made_beside_a_row_that_was_booked_is_booked_with_that_row_on_its_record(
    tmp_path: Path,
) -> None:
    """The control for the refusals of a list of booked rows above: one thing differs from each, and this is booked."""
    repo, facts = await _booked_expiry(tmp_path)
    try:
        repo.append_transition(
            _as_for_bot_b(facts, observed_at_ms=_LAST_SESSION_CLOSE, **_naming("rows_booked", [_A_BOOKED_ROW]))
        )

        assert held(repo, "bot-b") == {} and fifo(repo, "bot-b").exact_realized_pnl == Decimal("-388")
        [ended] = [json.loads(row["facts_json"]) for row in bookings(repo) if row["strategy_instance_id"] == "bot-b"]
        assert ended["owner_word"]["rows_booked"] == [_A_BOOKED_ROW]
    finally:
        repo.close()


async def test_a_simulations_ending_at_the_owners_word_is_booked_with_the_simulations_own_record(
    tmp_path: Path,
) -> None:
    repo, facts = await _booked_expiry(tmp_path)
    try:
        repo.append_transition(_as_for_bot_b(
            facts, observed_at_ms=_LAST_SESSION_CLOSE, **_ENDED_BY_OWNER, simulated=True,
            simulation=_an_owner_endings_record(),
        ))

        assert held(repo, "bot-b") == {} and fifo(repo, "bot-b").exact_realized_pnl == Decimal("-388")
    finally:
        repo.close()
