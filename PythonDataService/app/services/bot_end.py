"""The rules of a bot's owner-set end, and its words (#2607).

Canonical implementation of three owner decisions (grill 2026-09-29):

* **The default** is today's session close minus one minute -- 15:59 ET, or
  12:59 ET on a half-day -- taken from the canonical calendar; once today's
  last minute has begun, the next session's.
* **An end falls within the bot's trading hours.** Every Deploy is regular
  hours only (``use_rth=True``), so the end must be in the future, on a
  trading day the calendar covers, no earlier than the open and no later
  than the close minus one minute. A clock time after an *early* close that a
  full session would allow is moved to one minute before that early close,
  and the owner is told; any other time outside regular hours is refused in
  plain words. A bot that trades outside regular hours has no end rule yet,
  so it is refused one.
* **A Dry Run never ends holding** (owner decision 2026-09-29, #2641): its
  simulation sells at the last price it saw on every ending, so asking it to
  keep its shares is refused rather than silently ignored.

Whether an end may still be changed is decided here too
(:func:`end_edit_refusal`), once, for the edit and for the view's
``editable``.

Session times come from ``app.lean_sidecar.trading_calendar``; this module
holds no session literal. Instants are ``int64 ms UTC``; ET wall clocks
appear only in the words it authors.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from app.lean_sidecar.trading_calendar import (
    is_early_close,
    is_trading_day,
    next_trading_day,
    session_close_minute_et,
    session_window_for_date,
)
from app.schemas.bot_end import (
    BotEnd,
    BotEndAction,
    BotEndInput,
    BotEndStatus,
    BotEndView,
    RecordedEnd,
)
from app.services.session_authority import et_minute_of_day_ms
from app.utils.et_words import et_clock_words, et_day_words, et_when_words
from app.utils.session_anchors import et_date_at_ms

#: The end is one minute before the close: the bot is gone before the bar that
#: ends at the close is decided.
BOT_END_LEAD_MS = 60_000

#: The duty-outcome reason a run stopped at its owner-set end records.
SCHEDULED_END_REASON_CODE = "SCHEDULED_END"


class BotEndRefused(ValueError):
    """An end refused, with what went wrong, why, and what to do.

    ``http_status`` 400 is an end the rules refuse; 409 is an end that cannot
    be changed in the bot's current state.
    """

    def __init__(self, message: str, *, detail: str, next_action: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.detail = detail
        self.next_action = next_action
        self.http_status = http_status


@dataclass(frozen=True)
class ResolvedBotEnd:
    """An accepted end (``None``: no end), and the notice when its time was moved."""

    end: BotEnd | None
    notice: str | None = None


def default_bot_end(now_ms: int) -> BotEnd:
    """One minute before the close of today's session, or of the next one once that has passed."""
    day = et_date_at_ms(now_ms)
    if not is_trading_day(day) or _last_end_ms(day) <= now_ms:
        day = next_trading_day(day)
    return BotEnd(end_at_ms=_last_end_ms(day))


def resolve_bot_end(choice: BotEndInput | None, *, now_ms: int, dry_run: bool, use_rth: bool) -> ResolvedBotEnd:
    """The end a Deploy or an edit records, or :class:`BotEndRefused`.

    ``choice`` ``None`` is the default end. ``dry_run`` refuses KEEP;
    ``use_rth`` ``False`` -- a bot that also trades in extended hours --
    refuses every end, since its hours have no end rule yet.
    """
    if choice is not None and choice.end_at_ms is None:
        if choice.end_action == "KEEP":
            raise BotEndRefused(
                "A bot with no end has no shares to keep at it.",
                detail="Keep says what happens to the shares at the end time, and no end time was chosen.",
                next_action="Choose an end time, or choose no end with Sell.",
            )
        return ResolvedBotEnd(end=None)
    hours_refusal = _hours_refusal(use_rth=use_rth)
    if hours_refusal is not None:
        raise hours_refusal
    if choice is None:
        return ResolvedBotEnd(end=default_bot_end(now_ms))
    if dry_run and choice.end_action == "KEEP":
        raise BotEndRefused(
            "A Dry Run can't keep its shares at its end.",
            detail="A Dry Run never ends holding; it always sells at the last price it saw.",
            next_action="Choose Sell for this Dry Run.",
        )
    return _checked_end(choice.end_at_ms, choice.end_action, now_ms=now_ms)


def _hours_refusal(*, use_rth: bool) -> BotEndRefused | None:
    """Why a bot's hours allow it no end, or ``None`` when they allow one.

    The one answer for :func:`resolve_bot_end` and the default
    :func:`bot_end_view` offers: an end is set within regular hours, so a bot
    that also trades outside them (``use_rth`` ``False``) has no end rule yet.
    """
    if use_rth:
        return None
    return BotEndRefused(
        "This bot can't have an end.",
        detail="An end is set within regular hours, and this bot also trades outside them.",
        next_action="Remove the end, or deploy the bot for regular hours only.",
    )


def end_edit_refusal(pending: BotEnd | None, *, running: bool, now_ms: int) -> BotEndRefused | None:
    """Why a bot's end cannot be changed now, or ``None`` when it can.

    It can while the bot runs with no end still to be carried out, and while
    an end is still to come -- a bot whose run died keeps its end, which the
    Clerk still carries out. It cannot once that end has come (the Clerk is
    carrying it out), nor for a stopped bot with no end pending.
    """
    if pending is not None and pending.end_at_ms <= now_ms:
        return BotEndRefused(
            "This bot's end has come; the Clerk is carrying it out.",
            detail="An end can be changed only before its time.",
            next_action="The bot page shows how it ended once the Clerk is done.",
            http_status=409,
        )
    if pending is None and not running:
        return BotEndRefused(
            "This bot has stopped, so it has no end to change.",
            detail="A bot's end can be changed while it runs, or while an end is still to come.",
            next_action="Deploy the bot again to give it a new end.",
            http_status=409,
        )
    return None


def bot_end_view(
    recorded: RecordedEnd | None,
    *,
    now_ms: int,
    dry_run: bool,
    running: bool,
    use_rth: bool,
    notice: str | None = None,
) -> BotEndView:
    """A bot's end in the owner's words, at ``now_ms``.

    A bot with no end whose end may change now is offered the default end
    (:func:`default_bot_end`, Deploy's rule) for the owner adding one --
    unless its hours allow it no end (:func:`_hours_refusal`).
    """
    pending = None if recorded is None else recorded.pending()
    refusal = end_edit_refusal(pending, running=running, now_ms=now_ms)
    editable = refusal is None
    edit_refusal = None if refusal is None else str(refusal)
    if recorded is None:
        offers_default = editable and _hours_refusal(use_rth=use_rth) is None
        return BotEndView(
            end_at_ms=None, end_action="SELL", status="no_end",
            headline="No end · runs until you stop it" if running else "No end scheduled",
            explanation=(
                "This bot has no end time. It runs until you stop it."
                if running
                else "This bot is stopped, so it has no end: a Stop cancels any end, and nothing is sold at it."
            ),
            notice=notice, editable=editable, edit_refusal=edit_refusal,
            default_end_at_ms=default_bot_end(now_ms).end_at_ms if offers_default else None,
        )
    if recorded.carried_out_at_ms is not None:
        status: BotEndStatus = "ended"
        done = "sold at its last price" if dry_run else _DONE_WORDS[recorded.end_action]
        headline = f"Ended {et_when_words(recorded.carried_out_at_ms, now_ms=now_ms)} · {done}"
    else:
        status = "ending" if recorded.end_at_ms <= now_ms else "scheduled"
        when = et_when_words(recorded.end_at_ms, now_ms=now_ms)
        action = _ACTION_WORDS[recorded.end_action]
        headline = f"Ends {when} · {action}" if status == "scheduled" else f"End reached {when} · {action}"
    return BotEndView(
        end_at_ms=recorded.end_at_ms, end_action=recorded.end_action, status=status, headline=headline,
        explanation=_explanation(
            status, recorded.end_action, et_when_words(recorded.end_at_ms, now_ms=now_ms),
            dry_run=dry_run, running=running,
        ),
        notice=notice, editable=editable, edit_refusal=edit_refusal, default_end_at_ms=None,
    )


def resolved_bot_end_view(resolved: ResolvedBotEnd, *, now_ms: int, dry_run: bool) -> BotEndView:
    """An end the Deploy form chose, before its bot exists, with the notice when its time was moved.

    Every Deploy is regular hours only.
    """
    return bot_end_view(
        None if resolved.end is None else RecordedEnd.scheduled(resolved.end),
        now_ms=now_ms, dry_run=dry_run, running=True, use_rth=True, notice=resolved.notice,
    )


_ACTION_WORDS: dict[BotEndAction, str] = {"SELL": "sells", "KEEP": "keeps its shares"}
_DONE_WORDS: dict[BotEndAction, str] = {"SELL": "sale put in", "KEEP": "kept its shares"}


def _checked_end(end_at_ms: int, end_action: BotEndAction, *, now_ms: int) -> ResolvedBotEnd:
    """``end_at_ms`` held to the regular session of its ET date."""
    with _calendar_covers_the_date():
        day = et_date_at_ms(end_at_ms)
        trading_day = is_trading_day(day)
    day_words = et_day_words(end_at_ms, now_ms=now_ms)
    if not trading_day:
        raise BotEndRefused(
            f"The market is closed on {day_words}.",
            detail="A bot can only end while the market is open.",
            next_action="Choose a trading day.",
        )
    with _calendar_covers_the_date():
        window = session_window_for_date(day)
        last_ms = window.close_ms_utc - BOT_END_LEAD_MS
        moved_to_early_close = last_ms < end_at_ms <= _full_session_last_end_ms(day) and is_early_close(day)
    notice = None
    if moved_to_early_close:
        end_at_ms = last_ms
        notice = (
            f"{day_words} closes early at {et_clock_words(window.close_ms_utc)} ET, "
            f"so this bot ends at {et_clock_words(last_ms)} ET."
        )
    if not window.open_ms_utc <= end_at_ms <= last_ms:
        raise BotEndRefused(
            "The end must fall within regular hours.",
            detail=(
                f"On {day_words} the market is open from {et_clock_words(window.open_ms_utc)} to "
                f"{et_clock_words(window.close_ms_utc)} ET, so the latest end is {et_clock_words(last_ms)} ET."
            ),
            next_action=f"Choose a time from {et_clock_words(window.open_ms_utc)} to {et_clock_words(last_ms)} ET.",
        )
    if end_at_ms <= now_ms:
        raise BotEndRefused(
            "That end time has already passed.",
            detail="A bot's end must be later than now.",
            next_action="Choose a time later than now.",
        )
    return ResolvedBotEnd(end=BotEnd(end_at_ms=end_at_ms, end_action=end_action), notice=notice)


def _explanation(status: BotEndStatus, action: BotEndAction, at: str, *, dry_run: bool, running: bool) -> str:
    stopped = "" if running else "The bot is not running, but its end still stands. "
    if dry_run:
        return {
            "scheduled": f"{stopped}At {at} the bot stops, and its simulation sells what it holds at the last price it saw.",
            "ending": (
                f"Its end, {at}, has come. The bot stops, and its simulation sells what it holds at the "
                "last price it saw."
            ),
            "ended": "The bot has stopped, and its simulation sold what it held at the last price it saw.",
        }[status]
    if action == "KEEP":
        return {
            "scheduled": f"{stopped}At {at} the Clerk stops the bot and cancels its working orders. It keeps its shares.",
            "ending": (
                f"Its end, {at}, has come. On its next pass the Clerk stops the bot and cancels its working "
                "orders; it keeps its shares."
            ),
            "ended": "The Clerk stopped the bot at its end. It kept its shares.",
        }[status]
    return {
        "scheduled": (
            f"{stopped}At {at} the Clerk stops the bot, cancels its working orders and sells its shares at market."
        ),
        "ending": (
            f"Its end, {at}, has come. On its next pass the Clerk stops the bot, cancels its working orders "
            "and sells its shares at market; a sale that meets a closed market waits for the next open."
        ),
        "ended": (
            "The Clerk stopped the bot at its end and put in the sale of its shares at market. "
            "A sale that meets a closed market waits for the next regular open; it never goes out after hours."
        ),
    }[status]


def _last_end_ms(day: date) -> int:
    return session_window_for_date(day).close_ms_utc - BOT_END_LEAD_MS


def _full_session_last_end_ms(day: date) -> int:
    """The latest end a full session on ``day`` would allow.

    The full session's close is read from the calendar: the close minute of
    the next session that does not close early.
    """
    full_day = next_trading_day(day)
    while is_early_close(full_day):
        full_day = next_trading_day(full_day)
    return et_minute_of_day_ms(day, session_close_minute_et(full_day)) - BOT_END_LEAD_MS


@contextmanager
def _calendar_covers_the_date() -> Iterator[None]:
    """Refuse, in plain words, a date the market calendar cannot answer for.

    Wraps only the calendar's reads: a pandas timestamp past 2262 overflows,
    and a year past 9999 is no date. Any other error is a bug, not a date.
    """
    try:
        yield
    except (LookupError, OverflowError, ValueError) as exc:
        raise BotEndRefused(
            "The market calendar doesn't cover that date.",
            detail="An end is checked against the market's calendar, which has no sessions for that date.",
            next_action="Choose a date the market calendar covers.",
        ) from exc


__all__ = [
    "BOT_END_LEAD_MS",
    "SCHEDULED_END_REASON_CODE",
    "BotEndRefused",
    "ResolvedBotEnd",
    "bot_end_view",
    "default_bot_end",
    "end_edit_refusal",
    "resolve_bot_end",
    "resolved_bot_end_view",
]
