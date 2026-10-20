"""Why a stored setup cannot be opened, for every sentence the setup's own rules can give (#3030).

A saved setup, or the setup of a saved run, that no longer validates is listed with a reason that spells out what is
wrong (``read_stored_setup``). Each problem in it is a line of at most ``_REASON_PROBLEM_CHARS`` characters. Every
sentence the setup's rules can write has to reach the owner whole, so each is triggered here, through the same function
the lists use, and the reason must end with the sentence's own last word.

Hand-worked figures. A problem is "<where>: <what pydantic says>", where a place is the dotted path to the field, or
``setup`` for the whole. The longest sentence the setup's own rules write is the re-entry rule's, about a sold call
whose id is 32 letters (the longest the id's pattern allows): ``setup: Value error, leg '`` is 25 characters, the id
32, and ``' has a re-entry rule and nothing can stop it out: give it, or a bought call, a stop loss, or (when the
square-off is complete) give another leg one, or take the re-entry rule off`` is 179, so 25 + 32 + 179 = 236 (a bought
leg's has ``give it a stop loss`` in its place, 160, so 217). The window rule's is 203, the repeated ids of four legs'
192, the expiry rule's of a setup that sells, with two ids of 32 letters, 227, and the cover rule's 186. The cap is
240, so none is cut.

A problem that names something the owner stored of any size (an extra field, an unknown rule kind) is cut at the last
space inside 240 characters (the 241st counts, since a space just past the end is a word boundary), or at the 240th when
there is none, and ends " …". For an extra field of N letters in a leg the problem is ``legs.0.`` (7 characters), the
name, then ``: Extra inputs are not permitted``, whose colon is character 7 + N + 1 and whose space is the next:

* N = 231: the colon is the 239th character and the space the 240th, so the text kept is those 239 characters.
* N = 232: the colon is the 240th and the space the 241st, which counts: the text kept is the 240 characters.
* N = 233: the colon is the 241st, nothing in the first 241 characters is a space, and the text kept is the first 240.

The problem is ``legs.0.``, the name and ``: Extra inputs are not permitted`` (32 characters), so N = 201 makes exactly
240 characters, which fit, and N = 202 makes 241. For N = 202 the colon is the 210th character and the spaces after it
are the 211th, 217th, 224th, 228th and 232nd; the last inside the first 241 characters is the 232nd, the one before
"permitted", so the text kept is the 231 characters before it, ending "are not".

A name with a space in it, ten ``a``, a space, 222 ``b``, a space and 100 ``c``, puts its second space at the 241st
character of the problem (7 + 10 + 1 + 222 = 240 characters come first). That space is a word boundary, so the text kept
is the 240 characters before it, and not the shorter text before the first space.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.options_backtest.saved_setups import read_stored_setup
from tests.options_backtest.setup_fixtures import leg, setup_body

A_STOP = {"kind": "points", "value": "1"}
LONG_ID = "a" * 31 + "b"  # 32 letters, the longest the id's pattern allows


def _without(key: str) -> dict[str, Any]:
    body = setup_body()
    del body[key]
    return body


def _legs_named(*ids: str) -> dict[str, Any]:
    return setup_body(legs=[leg(leg_id=each) for each in ids])


# (the stored setup, where the problem is, the last word of its sentence)
SENTENCES = [
    # The whole setup.
    pytest.param(setup_body(version=2), "version", "1", id="version"),
    pytest.param(setup_body(underlying="TOOLONGX"), "underlying", "'^[A-Z]{1,5}$'", id="underlying pattern"),
    pytest.param(setup_body(underlying=5), "underlying", "string", id="underlying not a string"),
    pytest.param(setup_body(square_off="half"), "square_off", "'complete'", id="square-off kind"),
    pytest.param(setup_body(legs=[]), "legs", "0", id="no legs"),
    pytest.param(setup_body(legs=[leg(leg_id=f"l{n}") for n in range(9)]), "legs", "9", id="nine legs"),
    pytest.param(setup_body(legs=3), "legs", "array", id="legs not a list"),
    pytest.param(setup_body(lots=1), "lots", "permitted", id="an extra field"),
    pytest.param(_without("fees"), "fees", "required", id="a field missing"),
    # A time of the session.
    pytest.param(setup_body(entry={"anchor": "middle", "offset_ms": 300000}), "entry.anchor", "'close'", id="anchor"),
    pytest.param(
        setup_body(entry={"anchor": "open", "offset_ms": "5"}), "entry.offset_ms", "integer", id="offset type"
    ),
    pytest.param(setup_body(entry={"anchor": "open", "offset_ms": -60000}), "entry.offset_ms", "0", id="offset below"),
    pytest.param(
        setup_body(entry={"anchor": "open", "offset_ms": 86400000}), "entry.offset_ms", "86400000", id="offset a day"
    ),
    pytest.param(
        setup_body(entry={"anchor": "open", "offset_ms": 60001}), "entry.offset_ms", "60000", id="offset off a minute"
    ),
    pytest.param(
        setup_body(no_reentry_after={"anchor": "middle", "offset_ms": 60000}),
        "no_reentry_after.anchor",
        "'close'",
        id="the re-entry cut-off's anchor",
    ),
    # Costs.
    pytest.param(setup_body(slippage_percent="-1"), "slippage_percent", "0", id="slippage below"),
    pytest.param(setup_body(slippage_percent="100"), "slippage_percent", "100", id="slippage a hundred"),
    pytest.param(setup_body(slippage_percent="12345.678"), "slippage_percent", "total", id="slippage digits"),
    pytest.param(setup_body(slippage_percent="0.12345"), "slippage_percent", "places", id="slippage places"),
    pytest.param(setup_body(slippage_percent="abc"), "slippage_percent", "decimal", id="slippage not a number"),
    pytest.param(setup_body(price_step_usd="0"), "price_step_usd", "0", id="price step zero"),
    pytest.param(setup_body(price_step_usd="1234567890123"), "price_step_usd", "total", id="price step digits"),
    pytest.param(setup_body(price_step_usd="0.00001"), "price_step_usd", "places", id="price step places"),
    pytest.param(setup_body(fees={"kind": "free"}), "fees.kind", "'not_charged'", id="fee kind"),
    pytest.param(
        setup_body(fees={"kind": "per_contract", "per_contract_usd": "-1"}),
        "fees.per_contract_usd",
        "0",
        id="fee below",
    ),
    pytest.param(
        setup_body(fees={"kind": "per_contract", "per_contract_usd": "123456789"}),
        "fees.per_contract_usd",
        "total",
        id="fee digits",
    ),
    pytest.param(
        setup_body(fees={"kind": "per_contract", "per_contract_usd": "0.00001"}),
        "fees.per_contract_usd",
        "places",
        id="fee places",
    ),
    pytest.param(setup_body(fees={"kind": "per_contract"}), "fees", "other", id="a per-contract fee with no amount"),
    pytest.param(
        setup_body(fees={"kind": "not_charged", "per_contract_usd": "1"}), "fees", "other", id="an amount with no fee"
    ),
    pytest.param(setup_body(starting_cash_usd="0"), "starting_cash_usd", "0", id="cash zero"),
    pytest.param(setup_body(starting_cash_usd="123456789012345"), "starting_cash_usd", "total", id="cash digits"),
    pytest.param(setup_body(starting_cash_usd="1.234"), "starting_cash_usd", "places", id="cash places"),
    # A leg and its rules.
    pytest.param(setup_body(legs=[leg(leg_id="Bad Id")]), "legs.0.leg_id", "'^[a-z0-9_]{1,32}$'", id="leg id pattern"),
    pytest.param(setup_body(legs=[leg(right="straddle")]), "legs.0.right", "'put'", id="right"),
    pytest.param(setup_body(legs=[leg(contracts=0)]), "legs.0.contracts", "1", id="contracts below"),
    pytest.param(setup_body(legs=[leg(contracts=1001)]), "legs.0.contracts", "1000", id="contracts above"),
    pytest.param(setup_body(legs=[leg(contracts="1")]), "legs.0.contracts", "integer", id="contracts type"),
    pytest.param(
        setup_body(legs=[leg(expiry={"kind": "SECRET_KIND"})]), "legs.0.expiry", "'min_days'", id="unknown expiry rule"
    ),
    pytest.param(setup_body(legs=[leg(expiry={})]), "legs.0.expiry", "'kind'", id="expiry rule with no kind"),
    pytest.param(
        setup_body(legs=[leg(expiry={"kind": "min_days", "days": 731})]),
        "legs.0.expiry.min_days.days",
        "730",
        id="days above",
    ),
    pytest.param(
        setup_body(legs=[leg(expiry={"kind": "min_days", "days": -1})]),
        "legs.0.expiry.min_days.days",
        "0",
        id="days below",
    ),
    pytest.param(setup_body(legs=[leg(expiry="same_day")]), "legs.0.expiry", "object", id="expiry rule not an object"),
    pytest.param(
        setup_body(legs=[leg(strike={"kind": "SECRET_KIND"})]),
        "legs.0.strike",
        "'atm_offset'",
        id="unknown strike rule",
    ),
    pytest.param(
        setup_body(legs=[leg(strike={"kind": "atm_offset", "steps": 51})]),
        "legs.0.strike.atm_offset.steps",
        "50",
        id="steps above",
    ),
    pytest.param(
        setup_body(legs=[leg(strike={"kind": "atm_offset", "steps": -51})]),
        "legs.0.strike.atm_offset.steps",
        "-50",
        id="steps below",
    ),
    pytest.param(
        setup_body(legs=[leg(strike={"kind": "atm_offset", "steps": 0})]),
        "legs.0.strike.atm_offset",
        "rule",
        id="no steps from the money",
    ),
    pytest.param(
        setup_body(legs=[leg(stop_loss={"kind": "ticks", "value": "1"})]),
        "legs.0.stop_loss.kind",
        "'points'",
        id="stop loss kind",
    ),
    pytest.param(
        setup_body(legs=[leg(stop_loss={"kind": "points", "value": "0"})]),
        "legs.0.stop_loss.value",
        "0",
        id="stop loss zero",
    ),
    pytest.param(
        setup_body(legs=[leg(stop_loss={"kind": "points", "value": "12345678901"})]),
        "legs.0.stop_loss.value",
        "point",
        id="stop loss digits before the point",
    ),
    pytest.param(
        setup_body(legs=[leg(stop_loss={"kind": "points", "value": "0.00001"})]),
        "legs.0.stop_loss.value",
        "places",
        id="stop loss places",
    ),
    pytest.param(
        setup_body(legs=[leg(stop_loss={"kind": "percent", "value": "100"})]),
        "legs.0.stop_loss",
        "nothing",
        id="stop loss of a hundred percent",
    ),
    pytest.param(
        setup_body(legs=[leg(momentum={"kind": "sideways", "value": "1"})]),
        "legs.0.momentum.kind",
        "'points_down'",
        id="momentum kind",
    ),
    pytest.param(
        setup_body(legs=[leg(momentum={"kind": "percent_down", "value": "100"})]),
        "legs.0.momentum",
        "below",
        id="momentum fall of a hundred percent",
    ),
    pytest.param(
        setup_body(legs=[leg(reentry_on_stop={"kind": "never", "count": 1})]),
        "legs.0.reentry_on_stop.kind",
        "'on_momentum'",
        id="re-entry kind",
    ),
    pytest.param(
        setup_body(legs=[leg(reentry_on_stop={"kind": "at_once", "count": 0})]),
        "legs.0.reentry_on_stop.count",
        "1",
        id="re-entries below",
    ),
    pytest.param(
        setup_body(legs=[leg(reentry_on_stop={"kind": "at_once", "count": 21})]),
        "legs.0.reentry_on_stop.count",
        "20",
        id="re-entries above",
    ),
    pytest.param(setup_body(legs=[5]), "legs.0", "object", id="a leg that is not an object"),
    pytest.param(setup_body(entry=5), "entry", "object", id="a time that is not an object"),
    # The rules that look at more than one thing.
    pytest.param(
        setup_body(legs=[leg(stop_loss=A_STOP, reentry_on_stop={"kind": "on_momentum", "count": 1})]),
        "legs.0",
        "once",
        id="re-entry on momentum with no momentum rule",
    ),
    pytest.param(
        setup_body(legs=[leg(leg_id=LONG_ID, stop_loss=A_STOP, reentry_on_stop={"kind": "on_momentum", "count": 1})]),
        "legs.0",
        "once",
        id="the same on a leg with the longest id",
    ),
    pytest.param(
        setup_body(legs=[leg(reentry_on_stop={"kind": "at_once", "count": 1})]),
        "setup",
        "off",
        id="a re-entry rule no stop can reach",
    ),
    pytest.param(
        setup_body(legs=[leg(leg_id=LONG_ID, reentry_on_stop={"kind": "at_once", "count": 1})]),
        "setup",
        "off",
        id="the same on a leg with the longest id: the longest sentence",
    ),
    pytest.param(
        setup_body(legs=[leg(leg_id="p", right="put", side="sell", contracts=1000), leg(leg_id="c", contracts=1000)]),
        "setup",
        "sold",
        id="a sold put with nothing bought of its kind: the cover rule",
    ),
    pytest.param(
        setup_body(
            legs=[
                leg(leg_id="c"),
                leg(leg_id=LONG_ID, side="sell", reentry_on_stop={"kind": "at_once", "count": 1}),
            ],
            square_off="partial",
        ),
        "setup",
        "off",
        id="a sold call's re-entry rule no stop can reach, the longest id: the longest sentence",
    ),
    pytest.param(
        setup_body(legs=[leg(leg_id="b" * 32), leg(leg_id=LONG_ID, side="sell", expiry={"kind": "same_day"})]),
        "setup",
        f"'{'b' * 32}'",
        id="a sold leg and a bought one on different expiry rules, two ids of 32 letters",
    ),
    pytest.param(setup_body(legs=[leg(side="short")]), "legs.0.side", "'sell'", id="a side that is neither"),
    pytest.param(_legs_named("x", "x"), "setup", "x", id="a repeated leg id"),
    pytest.param(
        setup_body(legs=[leg(leg_id=each * 32) for each in "abcd" for _ in range(2)]),
        "setup",
        "d" * 32,
        id="four repeated leg ids of 32 letters",
    ),
    pytest.param(
        setup_body(entry={"anchor": "open", "offset_ms": 0}), "setup", "open", id="an entry before the first minute"
    ),
    pytest.param(
        setup_body(entry={"anchor": "close", "offset_ms": 60000}), "setup", "exit", id="an entry too near the close"
    ),
    pytest.param(
        setup_body(exit={"anchor": "close", "offset_ms": 0}), "setup", "close", id="an exit after the last minute"
    ),
    pytest.param(
        setup_body(exit={"anchor": "open", "offset_ms": 60000}), "setup", "entry", id="an exit too near the open"
    ),
    pytest.param(
        setup_body(entry={"anchor": "open", "offset_ms": 600000}, exit={"anchor": "open", "offset_ms": 600000}),
        "setup",
        "time",
        id="an exit not after the entry, counted from the open",
    ),
    pytest.param(
        setup_body(entry={"anchor": "close", "offset_ms": 600000}, exit={"anchor": "close", "offset_ms": 600000}),
        "setup",
        "time",
        id="an exit not after the entry, counted from the close",
    ),
    pytest.param(
        setup_body(entry={"anchor": "open", "offset_ms": 86340000}, exit={"anchor": "close", "offset_ms": 600000}),
        "setup",
        "ends",
        id="a window of a day or more: the window rule",
    ),
]


def _reason_of(stored: str) -> str:
    reason = read_stored_setup("Stored", stored, 1_000).cannot_open_reason
    assert reason is not None, "the setup was meant not to validate"
    return reason


@pytest.mark.parametrize(("body", "where", "last_word"), SENTENCES)
def test_every_sentence_the_setups_rules_can_give_reaches_the_owner_whole(
    body: dict[str, Any], where: str, last_word: str
) -> None:
    reason = _reason_of(json.dumps(body))

    assert reason.startswith(f"{where}: ")
    assert reason.endswith(last_word), reason
    assert not reason.endswith(" …"), "a sentence of the setup's own was cut"


@pytest.mark.parametrize(
    ("stored", "where", "last_word"),
    [
        pytest.param("{", "setup", "1", id="not JSON"),
        pytest.param("", "setup", "0", id="nothing at all"),
        pytest.param("[]", "setup", "object", id="a list"),
    ],
)
def test_a_stored_text_that_is_not_a_setup_says_so_in_whole_words(stored: str, where: str, last_word: str) -> None:
    reason = _reason_of(stored)

    assert reason.startswith(f"{where}: ") and reason.endswith(last_word), reason


# ── What the owner stored of any size ────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("x" * 231, "legs.0." + "x" * 231 + ": …", id="the space is the 240th character"),
        pytest.param("x" * 232, "legs.0." + "x" * 232 + ": …", id="the space is the 241st, one past the line"),
        pytest.param("x" * 233, "legs.0." + "x" * 233 + " …", id="no space in the line: cut at its end"),
        pytest.param("x" * 5000, "legs.0." + "x" * 233 + " …", id="a name of any size"),
        pytest.param(
            "x" * 201,
            "legs.0." + "x" * 201 + ": Extra inputs are not permitted",
            id="a problem of exactly a line's length is whole",
        ),
        pytest.param(
            "x" * 202,
            "legs.0." + "x" * 202 + ": Extra inputs are not …",
            id="one character more than a line's length is cut at a word",
        ),
        pytest.param(
            "a" * 10 + " " + "b" * 222 + " " + "c" * 100,
            "legs.0." + "a" * 10 + " " + "b" * 222 + " …",
            id="a space just past the end of a line is a word boundary",
        ),
    ],
)
def test_an_extra_field_with_a_name_of_any_size_is_cut_at_a_word_and_marked(name: str, expected: str) -> None:
    reason = _reason_of(json.dumps(setup_body(legs=[leg(**{name: 1})])))

    assert reason == expected


def test_an_unknown_rule_kind_of_any_size_is_cut_at_a_word_and_marked_and_says_where() -> None:
    # "legs.0.expiry: Input tag 'kkk...": the only spaces in the first 241 characters are the 15th, 21st and 25th.
    reason = _reason_of(json.dumps(setup_body(legs=[leg(expiry={"kind": "k" * 5000})])))

    assert reason == "legs.0.expiry: Input tag …"


def test_a_name_of_ordinary_size_is_still_named_whole() -> None:
    reason = _reason_of(json.dumps(setup_body(legs=[leg(expiry={"kind": "SECRET_KIND"}, lots_SECRETKEY=3)])))

    assert "'SECRET_KIND'" in reason and "legs.0.lots_SECRETKEY" in reason and "…" not in reason
