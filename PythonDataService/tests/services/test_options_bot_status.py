"""What the bot page says about an Options bot's legs."""

from __future__ import annotations

from app.options_bots.session import OptionsSession
from app.services.options_bot_status import leg_views
from tests.options_backtest._market import TableMarket, at, contract, flat, leg, picked, setup, steady

CALL = contract("call", "500")


def test_legs_read_not_started_until_the_entry_minute_starts_the_session() -> None:
    """A session's legs default to ``done``; before the entry minute that read as if the day were over."""
    market = TableMarket(
        underlying=steady("500"), picks=picked(("call", "500", CALL)), options={CALL: flat("2.00", 5, 60)},
    )
    rules = OptionsSession(setup(leg()), market)

    before = [view.phase for view in leg_views(rules.state)]
    rules.advance(at(4))
    still_before = [view.phase for view in leg_views(rules.state)]
    rules.advance(at(5))
    at_entry = [view.phase for view in leg_views(rules.state)]

    assert before == still_before == ["not_started"]
    assert at_entry == ["ordered"]
