"""The bot end's rules: its default, what it accepts, when it may change, and its words (#2607).

Every clock below is ``int64 ms UTC`` built from an ET wall clock through the
NY zone; the session times the rules use come from the canonical calendar.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.schemas.bot_end import EXPLICIT_NULL_END, BotEnd, BotEndInput, BotEndPreviewRequest, RecordedEnd
from app.services import bot_end as bot_end_service
from app.services.bot_end import (
    BotEndRefused,
    bot_end_view,
    default_bot_end,
    end_edit_refusal,
    resolve_bot_end,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS
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


def _resolve(choice: BotEndInput | None, *, now_ms: int, dry_run: bool = False, use_rth: bool = True):
    return resolve_bot_end(choice, now_ms=now_ms, dry_run=dry_run, use_rth=use_rth)


def _scheduled(end_at_ms: int, action: str = "SELL") -> RecordedEnd:
    return RecordedEnd(end_at_ms=end_at_ms, end_action=action)


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
    resolved = _resolve(None, now_ms=_at(_WEDNESDAY, 10))

    assert resolved.end == BotEnd(end_at_ms=_at(_WEDNESDAY, 15, 59), end_action="SELL")
    assert resolved.notice is None


# ── what is accepted ─────────────────────────────────────────────────────────


def test_resolve_bot_end_accepts_no_end() -> None:
    resolved = _resolve(_choose(None), now_ms=_at(_WEDNESDAY, 10))

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

    resolved = _resolve(_choose(end_at_ms, "KEEP"), now_ms=now)

    assert resolved.end == BotEnd(end_at_ms=end_at_ms, end_action="KEEP")
    assert resolved.notice is None


def test_resolve_bot_end_clamps_a_clock_time_after_an_early_close_and_says_so() -> None:
    resolved = _resolve(_choose(_at(_HALF_DAY, 15, 59)), now_ms=_at(_HALF_DAY, 9))

    assert resolved.end == BotEnd(end_at_ms=_at(_HALF_DAY, 12, 59), end_action="SELL")
    assert resolved.notice == "Fri Nov 27 closes early at 13:00 ET, so this bot ends at 12:59 ET."


# ── what is refused, in plain words ──────────────────────────────────────────


def _refusal(
    choice: BotEndInput | None, *, now_ms: int, dry_run: bool = False, use_rth: bool = True,
) -> BotEndRefused:
    with pytest.raises(BotEndRefused) as refused:
        _resolve(choice, now_ms=now_ms, dry_run=dry_run, use_rth=use_rth)
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
    assert refused.detail.startswith("On Thu Oct 1 the market is open from 09:30 to 16:00 ET")


def test_resolve_bot_end_refuses_an_end_after_an_early_close_that_no_full_session_would_allow() -> None:
    refused = _refusal(_choose(_at(_HALF_DAY, 17)), now_ms=_at(_HALF_DAY, 9))

    assert str(refused) == "The end must fall within regular hours."
    assert refused.detail == (
        "On Fri Nov 27 the market is open from 09:30 to 13:00 ET, so the latest end is 12:59 ET."
    )


def test_resolve_bot_end_refuses_a_day_the_market_is_closed() -> None:
    refused = _refusal(_choose(_at(_SATURDAY, 12)), now_ms=_at(_WEDNESDAY, 10))

    assert str(refused) == "The market is closed on Sat Oct 3."
    assert refused.next_action == "Choose a trading day."


def test_resolve_bot_end_refuses_an_end_already_passed() -> None:
    refused = _refusal(_choose(_at(_WEDNESDAY, 11)), now_ms=_at(_WEDNESDAY, 12))

    assert str(refused) == "That end time has already passed."
    assert refused.next_action == "Choose a time later than now."


def test_resolve_bot_end_refuses_a_clamped_end_already_passed() -> None:
    refused = _refusal(_choose(_at(_HALF_DAY, 15, 59)), now_ms=_at(_HALF_DAY, 13, 30))

    assert str(refused) == "That end time has already passed."


@pytest.mark.parametrize(
    "end_at_ms",
    [
        pytest.param(to_ms_utc(datetime(2263, 6, 17, 15, 0, tzinfo=_ET)), id="year-2263-past-the-calendar"),
        pytest.param(MAX_TIMESTAMP_MS, id="the-last-admissible-instant"),
    ],
)
def test_resolve_bot_end_refuses_a_date_the_calendar_does_not_cover(end_at_ms: int) -> None:
    """An in-contract instant past the calendar's range is refused in words, never a 500 (#2607 review)."""
    refused = _refusal(_choose(end_at_ms), now_ms=_at(_WEDNESDAY, 10))

    assert str(refused) == "The market calendar doesn't cover that date."
    assert refused.next_action == "Choose a date the market calendar covers."
    assert refused.http_status == 400


def test_resolve_bot_end_reports_a_bug_as_a_bug_not_as_a_date_the_calendar_does_not_cover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2607 review: only the calendar's reads are refused as an uncovered date; a programming
    error past them is not turned into a 400 blaming the owner's date."""

    def broken(**_kwargs: object) -> None:
        raise ValueError("a bug after the calendar answered")

    monkeypatch.setattr(bot_end_service, "ResolvedBotEnd", broken)

    with pytest.raises(ValueError, match="a bug after the calendar answered") as raised:
        _resolve(_choose(_at(_WEDNESDAY, 14)), now_ms=_at(_WEDNESDAY, 10))
    assert not isinstance(raised.value, BotEndRefused)


def test_resolve_bot_end_names_the_year_of_a_day_in_another_year() -> None:
    """#2607 review: a refusal about next year names its year, as ``et_when_words`` does."""
    refused = _refusal(_choose(_at(date(2027, 1, 1), 12)), now_ms=_at(_WEDNESDAY, 10))

    assert str(refused) == "The market is closed on Fri Jan 1 2027."


@pytest.mark.parametrize("dry_run", [False, True], ids=["paper", "dry-run"])
def test_resolve_bot_end_refuses_keep_with_no_end_in_every_mode(dry_run: bool) -> None:
    """A bot with no end has no shares to keep at it: one answer for every mode (#2607 review)."""
    refused = _refusal(_choose(None, "KEEP"), now_ms=_at(_WEDNESDAY, 10), dry_run=dry_run)

    assert str(refused) == "A bot with no end has no shares to keep at it."
    assert refused.next_action == "Choose an end time, or choose no end with Sell."


def test_resolve_bot_end_refuses_keep_on_a_dry_run() -> None:
    """A Dry Run never ends holding (owner decision 2026-09-29, #2641): KEEP is refused, never ignored."""
    refused = _refusal(_choose(_at(_WEDNESDAY, 15, 59), "KEEP"), now_ms=_at(_WEDNESDAY, 10), dry_run=True)

    assert str(refused) == "A Dry Run can't keep its shares at its end."
    assert refused.detail == "A Dry Run never ends holding; it always sells at the last price it saw."
    assert refused.next_action == "Choose Sell for this Dry Run."


def test_resolve_bot_end_sells_on_a_dry_run() -> None:
    resolved = _resolve(_choose(_at(_WEDNESDAY, 15, 59)), now_ms=_at(_WEDNESDAY, 10), dry_run=True)

    assert resolved.end == BotEnd(end_at_ms=_at(_WEDNESDAY, 15, 59), end_action="SELL")


@pytest.mark.parametrize("choice", [None, _choose(_at(_WEDNESDAY, 15, 59))], ids=["the-default", "a-chosen-time"])
def test_resolve_bot_end_refuses_an_end_for_a_bot_that_trades_outside_regular_hours(
    choice: BotEndInput | None,
) -> None:
    """An end is checked against regular hours: a bot that trades outside them gets none (#2607 review)."""
    refused = _refusal(choice, now_ms=_at(_WEDNESDAY, 10), use_rth=False)

    assert str(refused) == "This bot can't have an end."
    assert refused.next_action == "Remove the end, or deploy the bot for regular hours only."


def test_resolve_bot_end_lets_a_bot_outside_regular_hours_have_no_end() -> None:
    assert _resolve(_choose(None), now_ms=_at(_WEDNESDAY, 10), use_rth=False).end is None


# ── the choice as sent ───────────────────────────────────────────────────────


def test_an_end_choice_must_say_sell_or_keep() -> None:
    """``end_action`` has no default: a request naming only a time never flips KEEP to SELL (#2607 review)."""
    with pytest.raises(ValidationError, match="end_action"):
        BotEndInput.model_validate({"end_at_ms": _at(_WEDNESDAY, 15, 59)})


def test_the_end_preview_refuses_an_explicit_null_end() -> None:
    with pytest.raises(ValidationError, match="end may not be null"):
        BotEndPreviewRequest.model_validate({"execution_mode": "paper", "end": None})


def test_the_end_preview_with_no_end_field_previews_the_default() -> None:
    assert BotEndPreviewRequest.model_validate({"execution_mode": "paper"}).end is None
    assert "omit it for the default end" in EXPLICIT_NULL_END


# ── when an end may still change ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pending_at", "running", "refused"),
    [
        pytest.param(None, True, None, id="running-with-no-end"),
        pytest.param(_at(_WEDNESDAY, 15, 59), True, None, id="running-before-its-end"),
        pytest.param(_at(_WEDNESDAY, 15, 59), False, None, id="died-before-its-end"),
        pytest.param(_at(_WEDNESDAY, 11), True, "This bot's end has come; the Clerk is carrying it out.", id="end-has-come"),
        pytest.param(None, False, "This bot has stopped, so it has no end to change.", id="stopped-with-no-end"),
    ],
)
def test_end_edit_refusal_is_the_one_answer_for_the_edit_and_the_view(
    pending_at: int | None, running: bool, refused: str | None,
) -> None:
    now = _at(_WEDNESDAY, 12)
    pending = None if pending_at is None else BotEnd(end_at_ms=pending_at)

    refusal = end_edit_refusal(pending, running=running, now_ms=now)
    view = bot_end_view(
        None if pending is None else RecordedEnd.scheduled(pending), now_ms=now, dry_run=False, running=running,
    )

    assert (None if refusal is None else str(refusal)) == refused
    assert refusal is None or refusal.http_status == 409
    assert view.editable is (refused is None)


# ── the owner's words ────────────────────────────────────────────────────────


def test_bot_end_view_of_a_scheduled_sale() -> None:
    view = bot_end_view(_scheduled(_at(_WEDNESDAY, 15, 59)), now_ms=_at(_WEDNESDAY, 10), dry_run=False, running=True)

    assert view.status == "scheduled"
    assert view.headline == "Ends Wed Sep 30, 15:59 ET · sells"
    assert view.explanation == (
        "At Wed Sep 30, 15:59 ET the Clerk stops the bot, cancels its working orders and sells its shares at market."
    )
    assert view.end_at_ms == _at(_WEDNESDAY, 15, 59)
    assert view.end_action == "SELL"
    assert view.editable is True


def test_bot_end_view_of_a_later_day_that_keeps() -> None:
    view = bot_end_view(
        _scheduled(_at(_MONDAY, 12), "KEEP"), now_ms=_at(_WEDNESDAY, 10), dry_run=False, running=True,
    )

    assert view.headline == "Ends Mon Oct 5, 12:00 ET · keeps its shares"
    assert view.explanation == (
        "At Mon Oct 5, 12:00 ET the Clerk stops the bot and cancels its working orders. It keeps its shares."
    )


def test_bot_end_view_of_a_dry_run_names_the_last_price() -> None:
    view = bot_end_view(_scheduled(_at(_THURSDAY, 15, 59)), now_ms=_at(_WEDNESDAY, 10), dry_run=True, running=True)

    assert view.headline == "Ends Thu Oct 1, 15:59 ET · sells"
    assert view.explanation == (
        "At Thu Oct 1, 15:59 ET the bot stops, and its simulation sells what it holds at the last price it saw."
    )


def test_bot_end_view_of_a_bot_that_died_before_its_end_says_the_end_still_stands() -> None:
    view = bot_end_view(_scheduled(_at(_WEDNESDAY, 15, 59)), now_ms=_at(_WEDNESDAY, 10), dry_run=False, running=False)

    assert view.status == "scheduled"
    assert view.explanation.startswith("The bot is not running, but its end still stands. At Wed Sep 30, 15:59 ET")


def test_bot_end_view_of_no_end() -> None:
    view = bot_end_view(None, now_ms=_at(_WEDNESDAY, 10), dry_run=False, running=True)

    assert view.status == "no_end"
    assert view.end_at_ms is None
    assert view.headline == "No end · runs until you stop it"
    assert view.explanation == "This bot has no end time. It runs until you stop it."


def test_bot_end_view_of_a_stopped_bot_says_its_stop_cancelled_any_end() -> None:
    """The owner's Stop cancels a pending end: it keeps the shares and nothing is sold at the end time."""
    view = bot_end_view(None, now_ms=_at(_WEDNESDAY, 10), dry_run=False, running=False)

    assert view.status == "no_end"
    assert view.headline == "No end scheduled"
    assert view.explanation == (
        "This bot is stopped, so it has no end: a Stop cancels any end, and nothing is sold at it."
    )
    assert view.editable is False


def test_bot_end_view_once_the_end_has_come_says_what_is_pending_not_now() -> None:
    """Days late (the Clerk was down) the words still hold: no "Ending now" (#2607 review)."""
    view = bot_end_view(_scheduled(_at(_WEDNESDAY, 15, 59)), now_ms=_at(_MONDAY, 9), dry_run=False, running=False)

    assert view.status == "ending"
    assert view.headline == "End reached Wed Sep 30, 15:59 ET · sells"
    assert view.explanation == (
        "Its end, Wed Sep 30, 15:59 ET, has come. On its next pass the Clerk stops the bot, cancels its "
        "working orders and sells its shares at market; a sale that meets a closed market waits for the next open."
    )
    assert view.editable is False


def test_bot_end_view_once_the_end_was_carried_out_names_when() -> None:
    carried_out = RecordedEnd(end_at_ms=_at(_WEDNESDAY, 15, 59), carried_out_at_ms=_at(_WEDNESDAY, 15, 59, 4))

    view = bot_end_view(carried_out, now_ms=_at(_THURSDAY, 9), dry_run=False, running=False)

    assert view.status == "ended"
    assert view.headline == "Ended Wed Sep 30, 15:59 ET · sale put in"
    assert view.explanation == (
        "The Clerk stopped the bot at its end and put in the sale of its shares at market. "
        "A sale that meets a closed market waits for the next regular open; it never goes out after hours."
    )


def test_bot_end_view_of_an_ended_dry_run() -> None:
    carried_out = RecordedEnd(end_at_ms=_at(_WEDNESDAY, 15, 59), carried_out_at_ms=_at(_WEDNESDAY, 16, 2))

    view = bot_end_view(carried_out, now_ms=_at(_WEDNESDAY, 17), dry_run=True, running=False)

    assert view.headline == "Ended Wed Sep 30, 16:02 ET · sold at its last price"
    assert view.explanation == "The bot has stopped, and its simulation sold what it held at the last price it saw."


def test_bot_end_view_carries_the_clamp_notice() -> None:
    view = bot_end_view(
        _scheduled(_at(_HALF_DAY, 12, 59)), now_ms=_at(_HALF_DAY, 9), dry_run=False, running=True, notice="moved",
    )

    assert view.notice == "moved"
