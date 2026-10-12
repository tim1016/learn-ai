"""Setups an owner saves by name, and the two that ship (#3030).

The tests that need Postgres run only against a database attested as
disposable (``POSTGRES_URL_IS_EPHEMERAL=1``), like the rest of the research
suites, and skip without one. The name rule and the guards on the examples
need no database.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.options_backtest.saved_setups import (
    EXAMPLE_SETUPS,
    ExampleSetupError,
    InvalidSetupName,
    SavedSetup,
    delete_setup,
    get_setup,
    is_valid_setup_name,
    list_setups,
    save_setup,
)
from app.options_backtest.setup import MS_PER_MINUTE
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import leg as _leg
from tests.options_backtest.setup_fixtures import setup as _setup
from tests.options_backtest.setup_fixtures import tag as tag
from tests.options_backtest.setup_fixtures import unique as unique

_EXAMPLE_NAMES = ("SPY premium momentum, no re-entry", "SPY premium momentum, one re-entry")


class _NoDatabase:
    """A connection that fails the test if anything reaches SQL."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"SQL ({name}) was reached before the refusal")


# ── The shipped examples ──────────────────────────────────────────────────


def test_two_examples_ship_and_both_are_the_strategy_the_page_started_from() -> None:
    assert tuple(example.name for example in EXAMPLE_SETUPS) == _EXAMPLE_NAMES
    assert all(
        example.example and example.saved_at_ms is None and example.cannot_open_reason is None
        for example in EXAMPLE_SETUPS
    )

    for example in EXAMPLE_SETUPS:
        setup = example.setup
        assert setup is not None
        assert setup.underlying == "SPY"
        assert (setup.entry.anchor, setup.entry.offset_ms) == ("open", 5 * MS_PER_MINUTE)
        assert (setup.exit.anchor, setup.exit.offset_ms) == ("close", 46 * MS_PER_MINUTE)
        assert setup.square_off == "complete"
        assert (setup.slippage_percent, setup.fees.kind, setup.starting_cash_usd) == (
            Decimal("0.3"),
            "not_charged",
            Decimal("10000"),
        )
        assert [(leg.leg_id, leg.right, leg.contracts) for leg in setup.legs] == [
            ("call", "call", 1),
            ("put", "put", 1),
        ]
        for leg in setup.legs:
            assert leg.expiry.kind == "this_week" and leg.strike.kind == "atm"
            assert (leg.momentum.kind, leg.momentum.value) == ("percent_up", Decimal(25))
            assert (leg.stop_loss.kind, leg.stop_loss.value) == ("percent", Decimal(25))


def test_the_example_named_no_re_entry_has_no_re_entry_rule_and_no_cutoff() -> None:
    no_re_entry = EXAMPLE_SETUPS[0].setup

    assert no_re_entry.no_reentry_after is None
    assert all(leg.reentry_on_stop is None for leg in no_re_entry.legs)


def test_the_example_named_one_re_entry_re_enters_once_at_once_until_an_hour_before_the_close() -> None:
    one_re_entry = EXAMPLE_SETUPS[1].setup

    assert (one_re_entry.no_reentry_after.anchor, one_re_entry.no_reentry_after.offset_ms) == (
        "close",
        60 * MS_PER_MINUTE,
    )
    for leg in one_re_entry.legs:
        assert (leg.reentry_on_stop.kind, leg.reentry_on_stop.count) == ("at_once", 1)


def test_the_two_examples_differ_only_in_their_re_entry_rules() -> None:
    no_re_entry, one_re_entry = (example.setup for example in EXAMPLE_SETUPS)

    came_back = no_re_entry.model_copy(
        update={
            "no_reentry_after": one_re_entry.no_reentry_after,
            "legs": tuple(
                leg.model_copy(update={"reentry_on_stop": other.reentry_on_stop})
                for leg, other in zip(no_re_entry.legs, one_re_entry.legs, strict=True)
            ),
        }
    )
    assert came_back == one_re_entry


async def test_an_example_cannot_be_saved_over_or_deleted() -> None:
    for name in _EXAMPLE_NAMES:
        with pytest.raises(ExampleSetupError, match="example"):
            await save_setup(_NoDatabase(), name, _setup(), saved_at_ms=1)
        with pytest.raises(ExampleSetupError, match="example"):
            await delete_setup(_NoDatabase(), name)


async def test_an_example_is_read_without_the_database() -> None:
    found = await get_setup(_NoDatabase(), _EXAMPLE_NAMES[0])

    assert found == EXAMPLE_SETUPS[0]


# ── The name rule ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["a", "SPY (v2), 0.3-slippage_a.b", "x" * 80, "two  spaces inside", "7"],
)
def test_a_name_of_letters_digits_spaces_and_the_few_marks_is_taken(name: str) -> None:
    assert is_valid_setup_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "x" * 81,
        " leading",
        "trailing ",
        " ",
        "slash/inside",
        "semi;colon",
        "quote'd",
        "naïve",
        "tab\there",
        "line\nbreak",
        "end\n",
    ],
)
def test_a_name_outside_the_rule_is_refused(name: str) -> None:
    assert not is_valid_setup_name(name)


async def test_saving_under_a_refused_name_stops_before_the_database() -> None:
    with pytest.raises(InvalidSetupName):
        await save_setup(_NoDatabase(), "no/good", _setup(), saved_at_ms=1)


# ── Saved setups in Postgres ──────────────────────────────────────────────


async def test_a_setup_reads_back_exactly_as_it_was_saved(conn, tag: str) -> None:
    setup = _setup(
        no_reentry_after={"anchor": "close", "offset_ms": 60 * MS_PER_MINUTE},
        legs=[
            _leg(
                expiry={"kind": "min_days", "days": 3},
                strike={"kind": "atm_offset", "steps": -2},
                contracts=4,
                momentum={"kind": "points_down", "value": "0.35"},
                stop_loss={"kind": "points", "value": "1.25"},
                reentry_on_stop={"kind": "on_momentum", "count": 2},
            ),
            _leg(leg_id="put", right="put", stop_loss={"kind": "percent", "value": "12.5"}),
        ],
        slippage_percent="0.3500",
        price_step_usd="0.05",
        fees={"kind": "per_contract", "per_contract_usd": "0.65"},
        starting_cash_usd="25000.50",
    )
    name = f"Everything {tag}"

    saved = await save_setup(conn, name, setup, saved_at_ms=1_700_000_000_000)
    found = await get_setup(conn, name)

    assert saved == SavedSetup(
        name=name, example=False, saved_at_ms=1_700_000_000_000, setup=setup, cannot_open_reason=None
    )
    assert found == saved


async def test_saving_again_under_the_same_name_replaces_the_setup_and_the_stamp(conn, tag: str) -> None:
    name = f"Straddle {tag}"
    await save_setup(conn, name, _setup(underlying="SPY"), saved_at_ms=1_000)

    await save_setup(conn, name, _setup(underlying="QQQ"), saved_at_ms=2_000)

    found = await get_setup(conn, name)
    assert found is not None and found.setup is not None
    assert (found.setup.underlying, found.saved_at_ms) == ("QQQ", 2_000)
    assert [s.name for s in await list_setups(conn)].count(name) == 1


async def test_the_list_has_the_examples_first_then_saved_setups_newest_first(conn, tag: str) -> None:
    await save_setup(conn, f"Oldest {tag}", _setup(), saved_at_ms=1_000)
    await save_setup(conn, f"Newest {tag}", _setup(), saved_at_ms=3_000)
    await save_setup(conn, f"Middle {tag}", _setup(), saved_at_ms=2_000)
    await save_setup(conn, f"B same stamp {tag}", _setup(), saved_at_ms=2_000)
    await save_setup(conn, f"A same stamp {tag}", _setup(), saved_at_ms=2_000)

    listed = await list_setups(conn)

    assert tuple(s.name for s in listed[:2]) == _EXAMPLE_NAMES
    assert all(s.example for s in listed[:2]) and not any(s.example for s in listed[2:])
    mine = [s.name for s in listed if s.name.endswith(tag)]
    assert mine == [f"Newest {tag}", f"A same stamp {tag}", f"B same stamp {tag}", f"Middle {tag}", f"Oldest {tag}"]


async def test_a_stored_setup_that_no_longer_validates_is_listed_as_one_that_cannot_be_opened(conn, tag: str) -> None:
    good = f"Fine {tag}"
    stale_stop = f"Stale stop {tag}"
    newer_version = f"Newer version {tag}"
    not_a_setup = f"Not a setup {tag}"
    await save_setup(conn, good, _setup(), saved_at_ms=5_000)
    insert = "INSERT INTO research_options_backtest_setups (name, setup_json, saved_at_ms) VALUES ($1, $2::jsonb, $3)"
    # A stop of 150% passed an older rule; the current one refuses it.
    stored = _setup(legs=[_leg(stop_loss={"kind": "percent", "value": "25"})]).model_dump_json()
    stale = stored.replace('"value":"25"', '"value":"150"')
    newer = _setup().model_dump_json().replace('"version":1', '"version":2')
    assert stale != stored and newer != _setup().model_dump_json()
    await conn.execute(insert, stale_stop, stale, 4_000)
    await conn.execute(insert, newer_version, newer, 3_000)
    await conn.execute(insert, not_a_setup, "[1, 2]", 2_000)

    listed = {s.name: s for s in await list_setups(conn) if s.name.endswith(tag)}

    assert list(listed) == [good, stale_stop, newer_version, not_a_setup]
    assert listed[good].setup is not None and listed[good].cannot_open_reason is None
    for broken in (stale_stop, newer_version, not_a_setup):
        assert listed[broken].setup is None
        assert listed[broken].saved_at_ms is not None and not listed[broken].example
    assert "stop_loss" in listed[stale_stop].cannot_open_reason and "under 100" in listed[stale_stop].cannot_open_reason
    assert "version" in listed[newer_version].cannot_open_reason
    assert listed[not_a_setup].cannot_open_reason
    # The same answer from a direct read, and the setup is still there to be seen, not dropped.
    assert await get_setup(conn, stale_stop) == listed[stale_stop]


async def test_deleting_removes_the_setup_and_says_whether_there_was_one(conn, tag: str) -> None:
    name = f"Gone {tag}"
    await save_setup(conn, name, _setup(), saved_at_ms=1_000)

    assert await delete_setup(conn, name) is True
    assert await get_setup(conn, name) is None
    assert await delete_setup(conn, name) is False
