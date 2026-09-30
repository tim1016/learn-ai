"""The rules of a bot's owner-set end, and its words (#2607).

Canonical implementation of three owner decisions (grill 2026-09-29):

* **The default** is today's session close minus one minute -- 15:59 ET, or
  12:59 ET on a half-day -- taken from the canonical calendar; once today's
  last minute has begun, the next session's.
* **An end falls within the bot's trading hours.** Every Deploy is regular
  hours only (``use_rth=True``), so the end must be in the future, on a
  trading day, no earlier than the open and no later than the close minus one
  minute. A clock time after an *early* close that a full session would allow
  is moved to one minute before that early close, and the owner is told; any
  other time outside regular hours is refused in plain words.
* **A Dry Run never ends holding** (owner decision 2026-09-29, #2641): its
  simulation sells at the last price it saw on every ending, so asking it to
  keep its shares is refused rather than silently ignored.

Session times come from ``app.lean_sidecar.trading_calendar``; this module
holds no session literal. Instants are ``int64 ms UTC``; ET wall clocks
appear only in the words it authors.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.lean_sidecar.trading_calendar import (
    is_early_close,
    is_trading_day,
    next_trading_day,
    session_close_minute_et,
    session_window_for_date,
)
from app.schemas.bot_end import BotEnd, BotEndAction, BotEndInput, BotEndStatus, BotEndView
from app.services.session_authority import et_minute_of_day_ms
from app.utils.session_anchors import et_date_at_ms

_ET = ZoneInfo("America/New_York")

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


def resolve_bot_end(choice: BotEndInput | None, *, now_ms: int, dry_run: bool) -> ResolvedBotEnd:
    """The end a Deploy or an edit records, or :class:`BotEndRefused`.

    ``choice`` ``None`` is the default end. ``dry_run`` refuses KEEP.
    """
    if choice is None:
        return ResolvedBotEnd(end=default_bot_end(now_ms))
    if dry_run and choice.end_action == "KEEP":
        raise BotEndRefused(
            "A Dry Run can't keep its shares at its end.",
            detail="A Dry Run never ends holding; it always sells at the last price it saw.",
            next_action="Choose Sell for this Dry Run.",
        )
    if choice.end_at_ms is None:
        return ResolvedBotEnd(end=None)
    day = et_date_at_ms(choice.end_at_ms)
    if not is_trading_day(day):
        raise BotEndRefused(
            f"The market is closed on {_day_label(day)}.",
            detail="A bot can only end while the market is open.",
            next_action="Choose a trading day.",
        )
    window = session_window_for_date(day)
    last_ms = window.close_ms_utc - BOT_END_LEAD_MS
    end_at_ms, notice = choice.end_at_ms, None
    if last_ms < end_at_ms <= _full_session_last_end_ms(day) and is_early_close(day):
        end_at_ms = last_ms
        notice = (
            f"{_day_label(day)} closes early at {_clock(window.close_ms_utc)} ET, "
            f"so this bot ends at {_clock(last_ms)} ET."
        )
    if not window.open_ms_utc <= end_at_ms <= last_ms:
        raise BotEndRefused(
            "The end must fall within regular hours.",
            detail=(
                f"On {_day_label(day)} the market is open from {_clock(window.open_ms_utc)} to "
                f"{_clock(window.close_ms_utc)} ET, so the latest end is {_clock(last_ms)} ET."
            ),
            next_action=f"Choose a time from {_clock(window.open_ms_utc)} to {_clock(last_ms)} ET.",
        )
    if end_at_ms <= now_ms:
        raise BotEndRefused(
            "That end time has already passed.",
            detail="A bot's end must be later than now.",
            next_action="Choose a time later than now.",
        )
    return ResolvedBotEnd(end=BotEnd(end_at_ms=end_at_ms, end_action=choice.end_action), notice=notice)


def bot_end_view(
    end: BotEnd | None,
    *,
    carried_out: bool,
    now_ms: int,
    dry_run: bool,
    running: bool,
    editable: bool,
    notice: str | None = None,
) -> BotEndView:
    """A bot's end in the owner's words, at ``now_ms``."""
    if end is None:
        return BotEndView(
            end_at_ms=None, end_action="SELL", status="no_end",
            headline="No end · runs until you stop it" if running else "No end scheduled",
            explanation=(
                "This bot has no end time. It runs until you stop it."
                if running
                else "This bot is not running, and no end is scheduled for it."
            ),
            notice=notice, editable=editable,
        )
    status: BotEndStatus = "ended" if carried_out else "ending" if end.end_at_ms <= now_ms else "scheduled"
    at, prose_at = _when(end.end_at_ms, now_ms)
    action = _ACTION_WORDS[end.end_action]
    headline = {
        "scheduled": f"Ends {at} · {action}",
        "ending": f"Ending now · {action}",
        "ended": f"Ended {at} · {action}",
    }[status]
    return BotEndView(
        end_at_ms=end.end_at_ms, end_action=end.end_action, status=status, headline=headline,
        explanation=_explanation(status, end.end_action, prose_at, dry_run=dry_run),
        notice=notice, editable=editable,
    )


def resolved_bot_end_view(resolved: ResolvedBotEnd, *, now_ms: int, dry_run: bool) -> BotEndView:
    """An end the Deploy form chose, before its bot exists, with the notice when its time was moved."""
    return bot_end_view(
        resolved.end, carried_out=False, now_ms=now_ms, dry_run=dry_run, running=True, editable=True,
        notice=resolved.notice,
    )


_ACTION_WORDS: dict[BotEndAction, str] = {"SELL": "sells", "KEEP": "keeps its shares"}


def _explanation(status: BotEndStatus, action: BotEndAction, at: str, *, dry_run: bool) -> str:
    if dry_run:
        return {
            "scheduled": f"At {at} the bot stops, and its simulation sells what it holds at the last price it saw.",
            "ending": (
                f"Its end, {at}, has come. The bot is stopping, and its simulation sells what it holds "
                "at the last price it saw."
            ),
            "ended": "The bot stopped at its end, and its simulation sold what it held at the last price it saw.",
        }[status]
    if action == "KEEP":
        return {
            "scheduled": f"At {at} the Clerk stops the bot and cancels its working orders. It keeps its shares.",
            "ending": f"Its end, {at}, has come. The Clerk is stopping the bot; it keeps its shares.",
            "ended": "The Clerk stopped the bot at its end. It kept its shares.",
        }[status]
    return {
        "scheduled": (
            f"At {at} the Clerk stops the bot, cancels its working orders and sells its shares at market."
        ),
        "ending": f"Its end, {at}, has come. The Clerk is stopping the bot and selling its shares.",
        "ended": (
            "The Clerk stopped the bot at its end and put in the sale of its shares at market. "
            "A sale that meets a closed market waits for the next open; it never goes out after hours."
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


def _when(end_at_ms: int, now_ms: int) -> tuple[str, str]:
    """``end_at_ms`` relative to the ET date of ``now_ms``: a headline form and a prose form."""
    day, today = et_date_at_ms(end_at_ms), et_date_at_ms(now_ms)
    clock = f"{_clock(end_at_ms)} ET"
    if day == today:
        return f"today {clock}", f"{clock} today"
    if day == today + timedelta(days=1):
        return f"tomorrow {clock}", f"{clock} tomorrow"
    return f"{_day_label(day)} {clock}", f"{clock} on {_day_label(day)}"


def _clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(_ET).strftime("%H:%M")


def _day_label(day: date) -> str:
    return day.strftime("%a %b %d")


__all__ = [
    "BOT_END_LEAD_MS",
    "SCHEDULED_END_REASON_CODE",
    "BotEndRefused",
    "ResolvedBotEnd",
    "bot_end_view",
    "default_bot_end",
    "resolve_bot_end",
    "resolved_bot_end_view",
]
