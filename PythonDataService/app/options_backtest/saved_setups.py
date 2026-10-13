"""Setups an owner saves by name, and the two that ship (#3030).

Python owns the table (``research/persistence/schema.py`` version 19). The
functions take a connection, the way ``research/backtest_runs/repository.py``
does. A setup is stored as the JSON the page sent; reading it back validates
it again, so a row that no longer validates under a later rule is still
listed, marked as one that cannot be opened and why, and never dropped.

The two examples are defined here, not stored: they are listed with the saved
ones and can be neither overwritten nor deleted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

import asyncpg
from pydantic import ValidationError

from app.options.contract import OptionRight
from app.options_backtest.setup import (
    MS_PER_MINUTE,
    AtTheMoney,
    FeePolicy,
    LegSetup,
    Momentum,
    OptionsBacktestSetup,
    ReentryOnStop,
    SessionTime,
    StopLoss,
    ThisWeekExpiryRule,
)

# 1 to 80 characters of ASCII letters, digits, spaces and - _ . , ( ). A name starts with a letter or a digit, so
# "." and ".." (which a browser's URL parser removes from a path) and a leading dot or dash cannot be saved. A space
# never ends a name, so "a" and "a " cannot be two rows that read alike. ASCII only, so Python's re and pydantic's
# engine agree.
SETUP_NAME_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9 _.,()-]{0,78}[A-Za-z0-9_.,()-])?$"
SETUP_NAME_RULE = (
    "A name is 1 to 80 characters: letters, digits, spaces and - _ . , ( ), "
    "starting with a letter or a digit and not ending with a space."
)
_NAME = re.compile(SETUP_NAME_PATTERN)

# How many validation errors a "cannot be opened" reason spells out.
_REASON_ERRORS = 5
# The longest sentence the setup's own rules can give is 236 characters: its place, then the re-entry rule's sentence
# about a sold call whose id is 32 letters long. So a sentence that is the setup's own is never cut. One that is longer
# names something the owner stored of any size (an extra field, an unknown rule kind), and is cut at a word and marked.
_REASON_PROBLEM_CHARS = 240
_CUT = " …"


class InvalidSetupName(ValueError):
    """A setup name outside the rule."""


class ExampleSetupError(ValueError):
    """A save or delete named one of the shipped examples."""


@dataclass(frozen=True, slots=True)
class SavedSetup:
    """One listed setup: a saved one, or a shipped example.

    Exactly one of ``setup`` and ``cannot_open_reason`` is set. ``saved_at_ms`` is ``None`` for an example.
    """

    name: str
    example: bool
    saved_at_ms: int | None
    setup: OptionsBacktestSetup | None
    cannot_open_reason: str | None


def is_valid_setup_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None


# ── The shipped examples ──────────────────────────────────────────────────


def _premium_momentum_leg(leg_id: str, right: OptionRight, *, re_enters: bool) -> LegSetup:
    return LegSetup(
        leg_id=leg_id,
        right=right,
        contracts=1,
        expiry=ThisWeekExpiryRule(kind="this_week"),
        strike=AtTheMoney(kind="atm"),
        stop_loss=StopLoss(kind="percent", value=Decimal(25)),
        momentum=Momentum(kind="percent_up", value=Decimal(25)),
        reentry_on_stop=ReentryOnStop(kind="at_once", count=1) if re_enters else None,
    )


def _spy_premium_momentum(*, no_reentry_after: SessionTime | None, re_enters: bool) -> OptionsBacktestSetup:
    return OptionsBacktestSetup(
        underlying="SPY",
        entry=SessionTime(anchor="open", offset_ms=5 * MS_PER_MINUTE),
        exit=SessionTime(anchor="close", offset_ms=46 * MS_PER_MINUTE),
        no_reentry_after=no_reentry_after,
        square_off="complete",
        legs=(
            _premium_momentum_leg("call", "call", re_enters=re_enters),
            _premium_momentum_leg("put", "put", re_enters=re_enters),
        ),
        slippage_percent=Decimal("0.3"),
        fees=FeePolicy(kind="not_charged"),
        starting_cash_usd=Decimal(10000),
    )


def _example(name: str, setup: OptionsBacktestSetup) -> SavedSetup:
    return SavedSetup(name=name, example=True, saved_at_ms=None, setup=setup, cannot_open_reason=None)


# The strategy #3030 started from, twice: with no re-entry rule on its legs, and with each leg coming back once
# after a stop until an hour before the close.
EXAMPLE_SETUPS: tuple[SavedSetup, ...] = (
    _example("SPY premium momentum, no re-entry", _spy_premium_momentum(no_reentry_after=None, re_enters=False)),
    _example(
        "SPY premium momentum, one re-entry",
        _spy_premium_momentum(
            no_reentry_after=SessionTime(anchor="close", offset_ms=60 * MS_PER_MINUTE), re_enters=True
        ),
    ),
)
_EXAMPLE_BY_NAME = {example.name: example for example in EXAMPLE_SETUPS}


# ── Reads and writes ──────────────────────────────────────────────────────

_SELECT = "SELECT name, setup_json::text AS setup_json, saved_at_ms FROM research_options_backtest_setups"


async def list_setups(conn: asyncpg.Connection) -> tuple[SavedSetup, ...]:
    """The examples, then the saved setups newest first (a tie goes by name)."""
    rows = await conn.fetch(f"{_SELECT} ORDER BY saved_at_ms DESC, name ASC")
    return EXAMPLE_SETUPS + tuple(_saved(row) for row in rows)


async def get_setup(conn: asyncpg.Connection, name: str) -> SavedSetup | None:
    """The setup of that name, an example or a saved one, or ``None`` when there is none."""
    example = _EXAMPLE_BY_NAME.get(name)
    if example is not None:
        return example
    row = await conn.fetchrow(f"{_SELECT} WHERE name = $1", name)
    return None if row is None else _saved(row)


async def save_setup(
    conn: asyncpg.Connection, name: str, setup: OptionsBacktestSetup, *, saved_at_ms: int
) -> SavedSetup:
    """Save ``setup`` under ``name``, replacing whatever was saved under it."""
    if not is_valid_setup_name(name):
        raise InvalidSetupName(SETUP_NAME_RULE)
    _refuse_an_example(name, "overwritten")
    await conn.execute(
        """
        INSERT INTO research_options_backtest_setups (name, setup_json, saved_at_ms)
        VALUES ($1, $2::jsonb, $3)
        ON CONFLICT (name) DO UPDATE SET setup_json = EXCLUDED.setup_json, saved_at_ms = EXCLUDED.saved_at_ms
        """,
        name,
        setup.model_dump_json(),
        saved_at_ms,
    )
    return SavedSetup(name=name, example=False, saved_at_ms=saved_at_ms, setup=setup, cannot_open_reason=None)


async def delete_setup(conn: asyncpg.Connection, name: str) -> bool:
    """Delete the setup of that name; ``False`` when there was none."""
    _refuse_an_example(name, "deleted")
    return await conn.execute("DELETE FROM research_options_backtest_setups WHERE name = $1", name) == "DELETE 1"


def _refuse_an_example(name: str, what_cannot_happen: str) -> None:
    if name in _EXAMPLE_BY_NAME:
        raise ExampleSetupError(f"'{name}' is one of the shipped examples, which cannot be {what_cannot_happen}.")


def _saved(row: asyncpg.Record) -> SavedSetup:
    return read_stored_setup(row["name"], row["setup_json"], row["saved_at_ms"])


def read_stored_setup(name: str, setup_json: str, saved_at_ms: int) -> SavedSetup:
    """A stored row as a listed setup: opened, or marked as one that cannot be opened and why."""
    try:
        setup = OptionsBacktestSetup.model_validate_json(setup_json)
    except ValidationError as exc:
        return SavedSetup(
            name=name, example=False, saved_at_ms=saved_at_ms, setup=None, cannot_open_reason=_reason(exc)
        )
    return SavedSetup(name=name, example=False, saved_at_ms=saved_at_ms, setup=setup, cannot_open_reason=None)


def _reason(error: ValidationError) -> str:
    """Where each problem is and what it says, for the owner whose setup it is.

    The value stored for a field the setup has is never printed. The name of an unknown rule kind or of an extra
    field is: pydantic puts it in the location or in the message, and it is what tells the owner which part of
    the setup to look at. A problem longer than a line is cut at a word and marked, so a stored name of any size
    stays readable and no sentence of the setup's own loses its end.
    """
    problems = error.errors(include_url=False, include_input=False, include_context=False)
    shown = [
        _within_a_line(f"{'.'.join(str(part) for part in problem['loc']) or 'setup'}: {problem['msg']}")
        for problem in problems[:_REASON_ERRORS]
    ]
    if len(problems) > _REASON_ERRORS:
        shown.append(f"and {len(problems) - _REASON_ERRORS} more")
    return "; ".join(shown)


def _within_a_line(problem: str) -> str:
    """``problem`` whole if it fits a line, else cut at the last space that fits (the line's end if none) and marked."""
    if len(problem) <= _REASON_PROBLEM_CHARS:
        return problem
    # One character more than fits, so that a space just past the line's end counts as a word boundary.
    kept = problem[: _REASON_PROBLEM_CHARS + 1].rpartition(" ")[0] or problem[:_REASON_PROBLEM_CHARS]
    return kept + _CUT
