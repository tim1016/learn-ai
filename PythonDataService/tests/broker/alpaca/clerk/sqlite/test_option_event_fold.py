"""The fold of ``OPTION_POSITION_ENDED`` works the money out again and refuses what the contract does not give (#2913).

The batch function never hands it a wrong fact. A replay of a damaged log,
or a later caller's mistake, could: so the fold is its own fence.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite import fee_evidence, option_events
from app.broker.alpaca.clerk.sqlite.fee_evidence import custody_fee_attribution
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.option_event_folds import OPTION_POSITION_ENDED
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, complete_fee_evidence, documented_option_activity
from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import bookings, books_at, held, holds_option, later, poll

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


def _as_for_bot_b(facts: dict[str, Any], **changes: Any) -> TransitionInput:
    """The same booking handed to the fold again for the other bot, with ``changes`` laid over its facts."""
    key = facts["idempotency_key"].removesuffix("bot-a") + "bot-b"
    altered = {
        **facts, "idempotency_key": key, "option_order_ref": "learn-ai/bot-b/v1:oc-again",
        "close_execution_id": "option-event:close:again:bot-b",
        "shares": {**facts["shares"], "order_ref": "learn-ai/bot-b/v1:os-again", "execution_id": "option-event:shares:again:bot-b"},
        **changes,
    }
    return TransitionInput(
        strategy_instance_id="bot-b", command_id=f"cmd:{key}", effect_operation_id=f"effect:{key}",
        order_ref=altered["option_order_ref"], transition_kind=OPTION_POSITION_ENDED, custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK", operation_state="succeeded",
        source_event_at_ms=altered["booked_as_of_ms"], clerk_observed_at_ms=0, summary_code="OPTION_EXERCISED",
        facts_json=json.dumps(altered, sort_keys=True),
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


def test_every_hold_of_the_family_has_words_of_its_own() -> None:
    from app.broker.alpaca.clerk.sqlite.uncertainty_causes import OPTION_EVENT_HOLD_REASON_CODES

    assert set(option_events._HOLD_COPY) == set(OPTION_EVENT_HOLD_REASON_CODES)
    assert len(set(OPTION_EVENT_HOLD_REASON_CODES)) == len(OPTION_EVENT_HOLD_REASON_CODES)
