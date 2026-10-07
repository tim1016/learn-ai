"""What History and Activity are handed about an option position that ended with no order of the bot's (#2913).

A closed set of four endings and one sentence each, written by the server.
The screens themselves are another slice's.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import get_args

from app.broker.alpaca.clerk.sqlite.option_event_folds import OptionEnding
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.schemas.bot_history import BotHistoryOrders, BotHistoryRun
from app.schemas.option_endings import OptionEndingKind
from app.services.option_ending_views import option_ending_view
from app.services.sqlite_clerk_transaction_projection import _option_event_operations, _summary_row
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, documented_option_activity
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import ACCOUNT_ID
from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import books_at, holds_option, poll

CALL = "AAPL230721C00150000"


def test_the_endings_a_screen_may_be_handed_are_the_ones_the_books_record() -> None:
    assert set(get_args(OptionEndingKind)) == {ending.value for ending in OptionEnding}


async def test_history_is_told_on_the_run_that_held_the_contract_how_it_ended(tmp_path: Path) -> None:
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL", "stock-bot": "MSFT"})
    holds_option(repo, "bot-a", CALL, contracts=2)
    account = _FakeReadPort()
    account.option_events.rows = [documented_option_activity("trading_api", "option_exercise", index) for index in (0, 1)]
    try:
        await poll(repo, account)

        history = {bot.strategy_instance_id: bot for bot in repo.bot_history().bots}
        [run] = history["bot-a"].runs
        [ending] = run.option_endings
        shown = option_ending_view(ending)
        assert (shown.kind, shown.contract, shown.contracts, shown.shares, shown.simulated) == (
            "exercised", CALL, 2, 200, False
        )
        # One sentence, the server's: the contract, what happened, and the shares it left.
        assert CALL in shown.headline and "200" in shown.headline and "150" in shown.headline
        assert [run.option_endings for run in history["stock-bot"].runs] == [()]
    finally:
        repo.close()


def test_a_run_with_no_such_ending_is_served_exactly_as_it_was_before_the_field_existed() -> None:
    """The coordinator reads each lane's answer strictly: a new key on every run would be refused by an older one."""
    run = BotHistoryRun(
        run_id="run-1", started_at_ms=1, stopped_at_ms=None, running=True, outcome=None, transaction_count=0,
        orders=BotHistoryOrders(sent=0, filled=0, cancelled=0, rejected=0),
    )

    assert "option_endings" not in run.model_dump()
    assert "option_endings" not in run.model_dump_json()


async def test_activity_is_told_which_bot_held_the_contract_and_how_its_position_ended(tmp_path: Path) -> None:
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=2)
    account = _FakeReadPort()
    account.option_events.rows = [documented_option_activity("trading_api", "otm_option_expiry")]
    try:
        await poll(repo, account)
        reader = SqliteClerkProjectionReader.from_repository(repo)
        try:
            operations = reader.operation_page(page_size=10).operations
            endings = reader.option_endings(_option_event_operations(operations))
            rows = [_summary_row(operation, account_id=ACCOUNT_ID, option_endings=endings) for operation in operations]
        finally:
            reader.close()

        [event] = [row for row in rows if row.option_event is not None]
        assert event.strategy_instance_id == "bot-a" and event.subject_id == "bot:bot-a"
        assert (event.option_event.kind, event.option_event.contract, event.option_event.contracts) == ("expired", CALL, 2)
        assert CALL in event.option_event.headline
        # The entry's own row carries no such thing, and is served without the key.
        [entry] = [row for row in rows if row.option_event is None]
        assert "option_event" not in entry.model_dump()
    finally:
        repo.close()
