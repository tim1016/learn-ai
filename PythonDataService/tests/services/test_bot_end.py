"""The bot end's rules: its default, what it accepts, and its words (#2607).

Every clock below is ``int64 ms UTC`` built from an ET wall clock through the
NY zone; the session times the rules use come from the canonical calendar.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.schemas.bot_end import BotEnd, BotEndInput
from app.services.bot_end import (
    BotEndRefused,
    bot_end_view,
    default_bot_end,
    resolve_bot_end,
)
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")
_WEDNESDAY = date(2026, 9, 30)
_THURSDAY = date(2026, 10, 1)
_SATURDAY = date(2026, 10, 3)
_MONDAY = date(2026, 10, 5)
_HALF_DAY = date(2026, 11, 27)  # the day after Thanksgiving: the close is 13:00


def _at(day: date, hour: int, minute: int = 0, second: int = 0) -> int:
    return to_ms_utc(datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=_ET))


def _choose(end_at_ms: int | None, action: str = "SELL") -> BotEndInput:
    return BotEndInput(end_at_ms=end_at_ms, end_action=action)


# ── the default ──────────────────────────────────────────────────────────────


def test_default_bot_end_is_one_minute_before_todays_close() -> None:
    assert default_bot_end(_at(_WEDNESDAY, 10)) == BotEnd(end_at_ms=_at(_WEDNESDAY, 15, 59), end_action="SELL")


def test_default_bot_end_on_a_half_day_is_one_minute_before_the_early_close() -> None:
    assert default_bot_end(_at(_HALF_DAY, 10)).end_at_ms == _at(_HALF_DAY, 12, 59)


def test_default_bot_end_after_todays_last_minute_is_the_next_sessions() -> None:
    assert default_bot_end(_at(_WEDNESDAY, 15, 59, 30)).end_at_ms == _at(_THURSDAY, 15, 59)


def test_default_bot_end_on_a_weekend_is_the_next_sessions() -> None:
    assert default_bot_end(_at(_SATURDAY, 10)).end_at_ms == _at(_MONDAY, 15, 59)


def test_resolve_bot_end_with_no_choice_is_the_default() -> None:
    resolved = resolve_bot_end(None, now_ms=_at(_WEDNESDAY, 10), dry_run=False)

    assert resolved.end == BotEnd(end_at_ms=_at(_WEDNESDAY, 15, 59), end_action="SELL")
    assert resolved.notice is None


# ── what is accepted ─────────────────────────────────────────────────────────


def test_resolve_bot_end_accepts_no_end() -> None:
    resolved = resolve_bot_end(_choose(None), now_ms=_at(_WEDNESDAY, 10), dry_run=False)

    assert resolved.end is None


@pytest.mark.parametrize(
    "end_at_ms",
    [
        pytest.param(_at(_WEDNESDAY, 15, 59), id="close-minus-one-minute"),
        pytest.param(_at(_WEDNESDAY, 11, 30), id="midday"),
        pytest.param(_at(_WEDNESDAY, 9, 30), id="at-the-open-of-a-later-session"),
        pytest.param(_at(_MONDAY, 15, 59), id="a-later-day"),
    ],
)
def test_resolve_bot_end_accepts_an_end_inside_regular_hours(end_at_ms: int) -> None:
    now = _at(_WEDNESDAY, 9) if end_at_ms == _at(_WEDNESDAY, 9, 30) else _at(_WEDNESDAY, 10)

    resolved = resolve_bot_end(_choose(end_at_ms, "KEEP"), now_ms=now, dry_run=False)

    assert resolved.end == BotEnd(end_at_ms=end_at_ms, end_action="KEEP")
    assert resolved.notice is None


def test_resolve_bot_end_clamps_a_clock_time_after_an_early_close_and_says_so() -> None:
    resolved = resolve_bot_end(_choose(_at(_HALF_DAY, 15, 59)), now_ms=_at(_HALF_DAY, 9), dry_run=False)

    assert resolved.end == BotEnd(end_at_ms=_at(_HALF_DAY, 12, 59), end_action="SELL")
    assert resolved.notice == "Fri Nov 27 closes early at 13:00 ET, so this bot ends at 12:59 ET."


# ── what is refused, in plain words ──────────────────────────────────────────


def _refusal(choice: BotEndInput, *, now_ms: int, dry_run: bool = False) -> BotEndRefused:
    with pytest.raises(BotEndRefused) as refused:
        resolve_bot_end(choice, now_ms=now_ms, dry_run=dry_run)
    return refused.value


def test_resolve_bot_end_refuses_an_end_after_the_close() -> None:
    refused = _refusal(_choose(_at(_WEDNESDAY, 16, 30)), now_ms=_at(_WEDNESDAY, 10))

    assert str(refused) == "The end must fall within regular hours."
    assert refused.detail == (
        "On Wed Sep 30 the market is open from 09:30 to 16:00 ET, so the latest end is 15:59 ET."
    )
    assert refused.next_action == "Choose a time from 09:30 to 15:59 ET."


def test_resolve_bot_end_refuses_the_close_itself() -> None:
    refused = _refusal(_choose(_at(_WEDNESDAY, 15, 59) + 1), now_ms=_at(_WEDNESDAY, 10))

    assert str(refused) == "The end must fall within regular hours."


def test_resolve_bot_end_refuses_an_end_before_the_open() -> None:
    refused = _refusal(_choose(_at(_THURSDAY, 8)), now_ms=_at(_WEDNESDAY, 10))

    assert str(refused) == "The end must fall within regular hours."
    assert refused.detail.startswith("On Thu Oct 01 the market is open from 09:30 to 16:00 ET")


def test_resolve_bot_end_refuses_an_end_after_an_early_close_that_no_full_session_would_allow() -> None:
    refused = _refusal(_choose(_at(_HALF_DAY, 17)), now_ms=_at(_HALF_DAY, 9))

    assert str(refused) == "The end must fall within regular hours."
    assert refused.detail == (
        "On Fri Nov 27 the market is open from 09:30 to 13:00 ET, so the latest end is 12:59 ET."
    )


def test_resolve_bot_end_refuses_a_day_the_market_is_closed() -> None:
    refused = _refusal(_choose(_at(_SATURDAY, 12)), now_ms=_at(_WEDNESDAY, 10))

    assert str(refused) == "The market is closed on Sat Oct 03."
    assert refused.next_action == "Choose a trading day."


def test_resolve_bot_end_refuses_an_end_already_passed() -> None:
    refused = _refusal(_choose(_at(_WEDNESDAY, 11)), now_ms=_at(_WEDNESDAY, 12))

    assert str(refused) == "That end time has already passed."
    assert refused.next_action == "Choose a time later than now."


def test_resolve_bot_end_refuses_a_clamped_end_already_passed() -> None:
    refused = _refusal(_choose(_at(_HALF_DAY, 15, 59)), now_ms=_at(_HALF_DAY, 13, 30))

    assert str(refused) == "That end time has already passed."


@pytest.mark.parametrize("end_at_ms", [None, _at(_WEDNESDAY, 15, 59)])
def test_resolve_bot_end_refuses_keep_on_a_dry_run(end_at_ms: int | None) -> None:
    """A Dry Run never ends holding (owner decision 2026-09-29, #2641): KEEP is refused, never ignored."""
    refused = _refusal(_choose(end_at_ms, "KEEP"), now_ms=_at(_WEDNESDAY, 10), dry_run=True)

    assert str(refused) == "A Dry Run can't keep its shares at its end."
    assert refused.detail == "A Dry Run never ends holding; it always sells at the last price it saw."
    assert refused.next_action == "Choose Sell for this Dry Run."


def test_resolve_bot_end_sells_on_a_dry_run() -> None:
    resolved = resolve_bot_end(_choose(_at(_WEDNESDAY, 15, 59)), now_ms=_at(_WEDNESDAY, 10), dry_run=True)

    assert resolved.end == BotEnd(end_at_ms=_at(_WEDNESDAY, 15, 59), end_action="SELL")


# ── the owner's words ────────────────────────────────────────────────────────


def test_bot_end_view_of_a_scheduled_sale_today() -> None:
    view = bot_end_view(
        BotEnd(end_at_ms=_at(_WEDNESDAY, 15, 59)), carried_out=False,
        now_ms=_at(_WEDNESDAY, 10), dry_run=False, running=True, editable=True,
    )

    assert view.status == "scheduled"
    assert view.headline == "Ends today 15:59 ET · sells"
    assert view.explanation == (
        "At 15:59 ET today the Clerk stops the bot, cancels its working orders and sells its shares at market."
    )
    assert view.end_at_ms == _at(_WEDNESDAY, 15, 59)
    assert view.end_action == "SELL"
    assert view.editable is True


def test_bot_end_view_of_a_later_day_that_keeps() -> None:
    view = bot_end_view(
        BotEnd(end_at_ms=_at(_MONDAY, 12), end_action="KEEP"), carried_out=False,
        now_ms=_at(_WEDNESDAY, 10), dry_run=False, running=True, editable=True,
    )

    assert view.headline == "Ends Mon Oct 05 12:00 ET · keeps its shares"
    assert view.explanation == (
        "At 12:00 ET on Mon Oct 05 the Clerk stops the bot and cancels its working orders. It keeps its shares."
    )


def test_bot_end_view_of_a_dry_run_names_the_last_price() -> None:
    view = bot_end_view(
        BotEnd(end_at_ms=_at(_THURSDAY, 15, 59)), carried_out=False,
        now_ms=_at(_WEDNESDAY, 10), dry_run=True, running=True, editable=True,
    )

    assert view.headline == "Ends tomorrow 15:59 ET · sells"
    assert view.explanation == (
        "At 15:59 ET tomorrow the bot stops, and its simulation sells what it holds at the last price it saw."
    )


def test_bot_end_view_of_no_end() -> None:
    view = bot_end_view(None, carried_out=False, now_ms=_at(_WEDNESDAY, 10), dry_run=False, running=True, editable=True)

    assert view.status == "no_end"
    assert view.end_at_ms is None
    assert view.headline == "No end · runs until you stop it"
    assert view.explanation == "This bot has no end time. It runs until you stop it."


def test_bot_end_view_of_a_stopped_bot_with_no_end_never_says_it_runs() -> None:
    view = bot_end_view(None, carried_out=False, now_ms=_at(_WEDNESDAY, 10), dry_run=False, running=False, editable=False)

    assert view.status == "no_end"
    assert view.headline == "No end scheduled"
    assert view.explanation == "This bot is not running, and no end is scheduled for it."


def test_bot_end_view_once_the_end_has_come() -> None:
    view = bot_end_view(
        BotEnd(end_at_ms=_at(_WEDNESDAY, 15, 59)), carried_out=False,
        now_ms=_at(_WEDNESDAY, 15, 59, 3), dry_run=False, running=True, editable=False,
    )

    assert view.status == "ending"
    assert view.headline == "Ending now · sells"
    assert view.explanation == (
        "Its end, 15:59 ET today, has come. The Clerk is stopping the bot and selling its shares."
    )


def test_bot_end_view_once_the_end_was_carried_out() -> None:
    view = bot_end_view(
        BotEnd(end_at_ms=_at(_WEDNESDAY, 15, 59)), carried_out=True,
        now_ms=_at(_WEDNESDAY, 17), dry_run=False, running=True, editable=False,
    )

    assert view.status == "ended"
    assert view.headline == "Ended today 15:59 ET · sells"
    assert view.explanation == (
        "The Clerk stopped the bot at its end and put in the sale of its shares at market. "
        "A sale that meets a closed market waits for the next open; it never goes out after hours."
    )


def test_bot_end_view_carries_the_clamp_notice() -> None:
    view = bot_end_view(
        BotEnd(end_at_ms=_at(_HALF_DAY, 12, 59)), carried_out=False, now_ms=_at(_HALF_DAY, 9),
        dry_run=False, running=True, editable=True, notice="moved",
    )

    assert view.notice == "moved"
