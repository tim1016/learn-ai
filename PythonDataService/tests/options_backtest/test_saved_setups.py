"""Setups an owner saves by name, and the two that ship (#3030).

The tests that need Postgres run only against a database attested as
disposable (``POSTGRES_URL_IS_EPHEMERAL=1``), like the rest of the research
suites, and skip without one. The name rule and the guards on the examples
need no database.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import asyncpg
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
    read_stored_setup,
    save_setup,
)
from app.options_backtest.setup import MS_PER_MINUTE
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import leg as _leg
from tests.options_backtest.setup_fixtures import setup as _setup
from tests.options_backtest.setup_fixtures import setup_body as _setup_body
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
    ["a", "SPY (v2), 0.3-slippage_a.b", "x" * 80, "two  spaces inside", "7", "ends with a dot.", "a.."],
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
        # A name that starts with a mark: "." and ".." never reach a route from a browser, and a leading dot hides.
        ".",
        "..",
        ".hidden",
        "-dash",
        "_under",
        "(paren",
        ",comma",
    ],
)
def test_a_name_outside_the_rule_is_refused(name: str) -> None:
    assert not is_valid_setup_name(name)


async def test_saving_under_a_refused_name_stops_before_the_database() -> None:
    with pytest.raises(InvalidSetupName):
        await save_setup(_NoDatabase(), "no/good", _setup(), saved_at_ms=1)


# ── A stored row that cannot be opened ────────────────────────────────────


def _reason_for(**changes: Any) -> str:
    reason = read_stored_setup("Stored", json.dumps(_setup_body(**changes)), 1_000).cannot_open_reason
    assert reason is not None
    return reason


def test_an_unknown_rule_kind_is_named_where_it_sits() -> None:
    reason = _reason_for(legs=[_leg(expiry={"kind": "SECRET_KIND"})])

    assert "legs.0.expiry" in reason and "SECRET_KIND" in reason


def test_an_extra_field_is_named_where_it_sits() -> None:
    top = _reason_for(password_SECRETKEY="x")
    in_a_leg = _reason_for(legs=[_leg(lots_SECRETKEY=3)])

    assert "password_SECRETKEY" in top
    assert "legs.0.lots_SECRETKEY" in in_a_leg


def test_a_stored_value_that_fails_a_rule_is_not_printed() -> None:
    reason = _reason_for(underlying="SECRETVALUE", starting_cash_usd="-5.123456")

    assert "underlying" in reason and "starting_cash_usd" in reason
    assert "SECRETVALUE" not in reason and "5.123456" not in reason


def test_the_reason_spells_out_five_problems_and_counts_the_rest() -> None:
    reason = _reason_for(legs=[_leg(leg_id=f"leg{n}", extra=1) for n in range(8)])

    assert all(f"legs.{n}.extra" in reason for n in range(5))
    assert "legs.5.extra" not in reason
    assert reason.endswith("and 3 more")


# ── A setup stored before a leg had a side, and one that sells (#3105) ────

# A saved setup as master wrote it at 90937b3c, before a leg had a side: no leg carries the key.
_STORED_BEFORE_SIDES = (
    '{"version":1,"underlying":"SPY","entry":{"anchor":"open","offset_ms":300000},'
    '"exit":{"anchor":"close","offset_ms":2760000},"no_reentry_after":null,"square_off":"complete","legs":['
    '{"leg_id":"call","right":"call","contracts":1,"expiry":{"kind":"this_week"},"strike":{"kind":"atm"},'
    '"stop_loss":{"kind":"percent","value":"25"},"momentum":null,"reentry_on_stop":null}],'
    '"slippage_percent":"0.3","price_step_usd":"0.01","fees":{"kind":"not_charged","per_contract_usd":null},'
    '"starting_cash_usd":"10000"}'
)


class _ReadOnlyConnection:
    """A connection that answers the two reads a listing makes with stored rows, and fails the test on any write."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[str] = []

    async def fetch(self, statement: str, *_args: Any) -> list[dict[str, Any]]:
        self.statements.append(statement)
        return self.rows

    async def fetchrow(self, statement: str, *_args: Any) -> dict[str, Any] | None:
        self.statements.append(statement)
        return self.rows[0] if self.rows else None

    async def execute(self, statement: str, *_args: Any) -> str:
        raise AssertionError(f"a read wrote: {statement}")


async def test_a_setup_stored_before_legs_had_a_side_opens_as_bought_and_reading_it_writes_nothing() -> None:
    connection = _ReadOnlyConnection([{"name": "Old", "setup_json": _STORED_BEFORE_SIDES, "saved_at_ms": 5_000}])

    listed = [each for each in await list_setups(connection) if each.name == "Old"]  # type: ignore[arg-type]
    found = await get_setup(connection, "Old")  # type: ignore[arg-type]

    assert len(listed) == 1 and found is not None
    for opened in (listed[0], found):
        assert opened.setup is not None and opened.cannot_open_reason is None
        assert [leg.side for leg in opened.setup.legs] == ["buy"]
        # What opening it holds writes back, were it ever saved again, is the bytes it was stored as.
        assert opened.setup.model_dump_json() == _STORED_BEFORE_SIDES


def test_a_stored_setup_that_sells_opens_as_sold_and_one_whose_sold_leg_is_uncovered_cannot_be_opened() -> None:
    covered = _setup(legs=[_leg(leg_id="long_call"), _leg(leg_id="short_call", side="sell")])
    stored = covered.model_dump_json()
    uncovered = json.dumps(
        _setup_body(legs=[_leg(leg_id="long_put", right="put"), _leg(leg_id="short_call", side="sell")])
    )

    opened = read_stored_setup("Selling", stored, 1_000)
    refused = read_stored_setup("Uncovered", uncovered, 1_000)

    assert '"side":"sell"' in stored
    assert opened.setup is not None and [leg.side for leg in opened.setup.legs] == ["buy", "sell"]
    assert opened.setup == covered
    assert refused.setup is None and refused.cannot_open_reason is not None
    assert "sell more call contracts than they buy" in refused.cannot_open_reason


# ── Saved setups in Postgres ──────────────────────────────────────────────


async def test_a_setup_that_sells_is_saved_with_its_side_and_a_setup_that_buys_with_none(conn, tag: str) -> None:
    selling = _setup(legs=[_leg(leg_id="long_call"), _leg(leg_id="short_call", side="sell")])
    buying = _setup()

    await save_setup(conn, f"Selling {tag}", selling, saved_at_ms=1_000)
    await save_setup(conn, f"Buying {tag}", buying, saved_at_ms=2_000)

    stored = {
        row["name"]: json.loads(row["setup_json"])
        for row in await conn.fetch(
            "SELECT name, setup_json::text AS setup_json FROM research_options_backtest_setups WHERE name LIKE $1",
            f"%{tag}",
        )
    }
    assert [leg.get("side") for leg in stored[f"Selling {tag}"]["legs"]] == [None, "sell"]
    assert all("side" not in leg for leg in stored[f"Buying {tag}"]["legs"])
    found = await get_setup(conn, f"Selling {tag}")
    assert found is not None and found.setup == selling


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


@pytest.mark.parametrize("stamp", [-1, MAX_TIMESTAMP_MS + 1, 2**62])
async def test_a_stamp_the_list_could_not_serve_is_refused_by_the_table(conn, tag: str, stamp: int) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await save_setup(conn, f"Odd stamp {tag}", _setup(), saved_at_ms=stamp)


@pytest.mark.parametrize("stamp", [0, MAX_TIMESTAMP_MS])
async def test_the_earliest_and_latest_stamp_the_list_can_serve_are_stored(conn, tag: str, stamp: int) -> None:
    name = f"Edge stamp {tag}"

    saved = await save_setup(conn, name, _setup(), saved_at_ms=stamp)

    assert saved.saved_at_ms == stamp
    assert (await get_setup(conn, name)) == saved


async def test_a_name_that_differs_from_an_example_only_by_case_is_an_ordinary_setup(conn) -> None:
    # Names are compared exactly, so the lookalike is its own row: saved, listed beside the example, and deletable.
    name = _EXAMPLE_NAMES[0].upper()
    assert name != _EXAMPLE_NAMES[0]
    try:
        saved = await save_setup(conn, name, _setup(underlying="QQQ"), saved_at_ms=1_000)
        listed = {s.name: s for s in await list_setups(conn)}

        assert saved.example is False and saved.saved_at_ms == 1_000
        assert listed[name] == saved and listed[_EXAMPLE_NAMES[0]].example is True
        assert await delete_setup(conn, name) is True
    finally:
        await conn.execute("DELETE FROM research_options_backtest_setups WHERE name = $1", name)
    assert [example.name for example in EXAMPLE_SETUPS] == list(_EXAMPLE_NAMES)


async def test_deleting_removes_the_setup_and_says_whether_there_was_one(conn, tag: str) -> None:
    name = f"Gone {tag}"
    await save_setup(conn, name, _setup(), saved_at_ms=1_000)

    assert await delete_setup(conn, name) is True
    assert await get_setup(conn, name) is None
    assert await delete_setup(conn, name) is False
