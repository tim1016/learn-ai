"""Lift the assignment, exercise and expiry activities out of Alpaca's published documents, verbatim.

These fixtures stand in for recordings from an Alpaca account until one is
made (#2913): an assignment, an exercise and an expiry cannot be made to
happen on demand. Nothing in them is typed by hand: every activity, every
list of activity codes and every field name is cut from the page that
publishes it and written as it stands.

Run from ``PythonDataService/``::

    python -m scripts.fixture_generators.alpaca_documented_option_activities
    python -m scripts.fixture_generators.alpaca_documented_option_activities --pages-dir <dir>

Without ``--pages-dir`` the pages are fetched from ``docs.alpaca.markets``,
which serves each one as Markdown at its own address plus ``.md``. With it,
the pages are read from files named as ``PAGES`` names them, so a run can be
repeated against the same reading. No Alpaca account, key or API is touched.

The same run writes golden fixture ``OEV-001``
(``tests/fixtures/golden/option-events/OEV-001/v1``): each documented event
as an input, and the share trade Alpaca prints beside it as the output. It
is written from the pages too, and this never imports
``app.options.event_allocation``, the rule the fixture holds.

The attribution files beside the fixtures are written by hand and carry the
date the pages were read; re-running this does not change them.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from app.utils.session_anchors import et_midnight_ms
from scripts.fixture_generators.alpaca_documented_option_examples import DESTINATION, DOCS_ROOT, FETCH_TIMEOUT_S

logger = logging.getLogger(__name__)

GOLDEN_DESTINATION = Path(__file__).parents[2] / "tests/fixtures/golden/option-events/OEV-001/v1"

# Page path → the file name a saved copy is read from.
PAGES: dict[str, str] = {
    "docs/non-trade-activities-for-option-events": "docs_non-trade-activities-for-option-events.md",
    "docs/options-trading-overview": "docs_options-trading-overview.md",
    "docs/account-activities": "docs_account-activities.md",
    "reference/getaccountactivities-2": "reference_getaccountactivities-2.md",
    "reference/getaccountactivitiesbyactivitytype-1": "reference_getaccountactivitiesbyactivitytype-1.md",
}

# The headings whose examples are taken, on each page that has them.
_TRADING_API_HEADINGS = ("option_exercise", "option_assignment", "otm_option_expiry")
_BROKER_API_HEADINGS = (
    "exercising_call",
    "exercising_put",
    "call_assignment",
    "put_assignment",
    "call_expiration",
    "put_expiration",
)

# Every option activity code Alpaca's pages use begins with these two letters.
_OPTION_CODE = re.compile(r"^OP[A-Z]+$")

# The codes of the three events, as the words this repo knows each by, and of the share trade one creates.
_EVENT_KIND = {"OPEXC": "exercised", "OPASN": "assigned", "OPEXP": "expired"}
_SHARE_TRADE_CODE = "OPTRD"
# The shares a standard contract delivers. No page states it as a rule; every
# documented pair is held to it below before a file is written.
_STANDARD_MULTIPLIER = 100
# The sentence the Trading API page puts under its exercise and its assignment.
_STATED_TRADE = re.compile(
    r"applicable to (?P<contracts>\d+) contracts, and the corresponding trade \(OPTRD\) represents "
    r"(?P<shares>\d+) of the underlying shares being (?P<way>purchased|sold) "
    r"at a per-share amount of \$(?P<price>\d+(?:\.\d+)?) \(strike price\)"
)

_HEADING = re.compile(r"^#{1,4}\s+(?P<title>.+?)\s*$", re.MULTILINE)
# A fence may carry a label after its language ("```json JSON").
_FENCE = re.compile(r"^```[^\n]*\n(?P<body>.*?)\n```", re.MULTILINE | re.DOTALL)
_TABLE_CODE = re.compile(r"^\|\s*`(?P<code>[A-Z]+)`\s*\|", re.MULTILINE)


def _page(path: str, pages_dir: Path | None) -> str:
    if pages_dir is not None:
        return (pages_dir / PAGES[path]).read_text(encoding="utf-8")
    response = httpx.get(f"{DOCS_ROOT}/{path}.md", timeout=FETCH_TIMEOUT_S, follow_redirects=True)
    response.raise_for_status()
    return response.text


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def _json_objects(text: str) -> list[dict[str, Any]]:
    """Every JSON object in ``text``, in order, whether the page wrote them as a list or one after another."""
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    at = text.find("{")
    while at != -1:
        parsed, end = decoder.raw_decode(text[at:])
        if not isinstance(parsed, dict):
            raise SystemExit("an activity example was not a JSON object")
        found.append(parsed)
        at = text.find("{", at + end)
    return found


def _activities_by_heading(page: str, wanted: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    """The activities in the one code block under each wanted heading."""
    headings = [(match.start(), _slug(match["title"])) for match in _HEADING.finditer(page)]
    examples: dict[str, list[dict[str, Any]]] = {}
    for fence in _FENCE.finditer(page):
        under = [title for start, title in headings if start < fence.start()]
        if not under or under[-1] not in wanted:
            continue
        if under[-1] in examples:
            raise SystemExit(f"two examples sit under the heading {under[-1]!r}")
        examples[under[-1]] = _json_objects(fence["body"])
    missing = [name for name in wanted if not examples.get(name)]
    if missing:
        raise SystemExit(f"the page no longer carries the examples under {missing}")
    return {name: examples[name] for name in wanted}


def _stated_trades(page: str, wanted: tuple[str, ...]) -> dict[str, re.Match[str]]:
    """The sentence under each wanted heading that states its share trade in words, where the page has one."""
    headings = [(match.start(), _slug(match["title"])) for match in _HEADING.finditer(page)]
    stated: dict[str, re.Match[str]] = {}
    for (start, title), (end, _next_title) in zip(headings, [*headings[1:], (len(page), "")], strict=True):
        sentence = _STATED_TRADE.search(page, start, end)
        if title in wanted and sentence is not None:
            stated[title] = sentence
    return stated


def _whole(text: str, *, what: str) -> int:
    number = Decimal(text)
    if number != number.to_integral_value():
        raise SystemExit(f"{what} is not a whole number on the page: {text!r}")
    return int(number)


def _golden_case(
    name: str, rows: list[dict[str, Any]], stated: re.Match[str] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One documented example as a golden case: the event as input, the share trade printed beside it as output.

    The share trade is copied as printed. Beside it go the price its own
    net amount comes to and the price the page's sentence states, so a page
    that contradicts itself is recorded and not smoothed over. Whatever else
    the page says about the trade must agree with the row, or nothing is
    written: the shares a contract delivers, which way they go, and that
    cash is paid for shares bought.
    """
    events = [row for row in rows if row["activity_type"] in _EVENT_KIND]
    share_trades = [row for row in rows if row["activity_type"] == _SHARE_TRADE_CODE]
    if len(events) != 1 or len(share_trades) > 1 or len(events) + len(share_trades) != len(rows):
        raise SystemExit(f"{name}: expected one event and at most one share trade, got {len(rows)} rows")
    (event,) = events
    kind = _EVENT_KIND[event["activity_type"]]
    position_change = _whole(event["qty"], what=f"{name}: the event's qty")
    case = {
        "case": name,
        "activity_type": event["activity_type"],
        "kind": kind,
        "contract": event["symbol"],
        "position_change": position_change,
        # The row's date at 00:00 New York time, int64 ms UTC.
        "event_date_ms": et_midnight_ms(date.fromisoformat(event["date"])),
    }
    if (kind == "expired") != (not share_trades):
        raise SystemExit(f"{name}: an expiry comes with no share trade, and an exercise or an assignment with one")
    if not share_trades:
        return case, {"case": name, "share_trade": None}
    (share_trade,) = share_trades
    shares = _whole(share_trade["qty"], what=f"{name}: the share trade's qty")
    net_amount = Decimal(share_trade["net_amount"])
    if abs(shares) != abs(position_change) * _STANDARD_MULTIPLIER:
        raise SystemExit(f"{name}: {shares} shares for {position_change} contracts is not {_STANDARD_MULTIPLIER} a contract")
    if (net_amount < 0) != (shares > 0) or net_amount == 0:
        raise SystemExit(f"{name}: cash of {net_amount} for {shares} shares is not paid for shares bought")
    price_from_net_amount = abs(net_amount) / abs(shares)
    if price_from_net_amount * abs(shares) != abs(net_amount):
        raise SystemExit(f"{name}: {net_amount} over {shares} shares is not an exact price")
    if stated is not None and (
        int(stated["contracts"]) != abs(position_change)
        or int(stated["shares"]) != abs(shares)
        or (stated["way"] == "purchased") != (shares > 0)
    ):
        raise SystemExit(f"{name}: the sentence under the example no longer describes its rows")
    return case, {
        "case": name,
        "share_trade": {field: share_trade[field] for field in ("symbol", "qty", "price", "net_amount")},
        "price_from_net_amount": str(price_from_net_amount),
        "price_in_sentence": None if stated is None else stated["price"],
    }


def _golden(
    activities: dict[str, dict[str, list[dict[str, Any]]]], stated: dict[str, dict[str, re.Match[str]]]
) -> dict[str, Any]:
    """``input.json`` and ``output.json`` of OEV-001, a case for every documented example, in the pages' order."""
    cases = [
        _golden_case(f"{product}/{heading}", rows, stated.get(product, {}).get(heading))
        for product, examples in activities.items()
        for heading, rows in examples.items()
    ]
    return {
        "input.json": {"multiplier": _STANDARD_MULTIPLIER, "cases": [case for case, _expected in cases]},
        "output.json": {"cases": [expected for _case, expected in cases]},
    }


def _openapi(page: str) -> dict[str, Any]:
    """The OpenAPI definition a reference page carries in its one JSON block."""
    match = re.search(r"```json\n(?P<body>.*?)\n```", page, re.DOTALL)
    if match is None:
        raise SystemExit("a reference page carried no OpenAPI definition")
    return json.loads(match["body"])


def _what_the_pages_list(pages_dir: Path | None) -> dict[str, Any]:
    """The option activity codes each page lists, and the fields and statuses the reference gives an activity."""
    listing = _openapi(_page("reference/getaccountactivities-2", pages_dir))["components"]["schemas"]
    by_type = _openapi(_page("reference/getaccountactivitiesbyactivitytype-1", pages_dir))["components"]["schemas"]
    if listing["ActivityType"] != by_type["ActivityType"] or listing["NonTradeActivities"] != by_type["NonTradeActivities"]:
        raise SystemExit("the two activity reference pages no longer describe an activity alike")
    table = _TABLE_CODE.findall(_page("docs/account-activities", pages_dir))
    non_trade = listing["NonTradeActivities"]["properties"]
    return {
        "reference_option_activity_types": [code for code in listing["ActivityType"]["enum"] if _OPTION_CODE.match(code)],
        "account_activities_page_option_activity_types": [code for code in table if _OPTION_CODE.match(code)],
        "reference_non_trade_activity_fields": sorted(non_trade),
        "reference_non_trade_activity_statuses": non_trade["status"]["enum"],
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages-dir", type=Path, default=None, help="read saved pages instead of fetching them")
    pages_dir: Path | None = parser.parse_args().pages_dir
    trading_page = _page("docs/non-trade-activities-for-option-events", pages_dir)
    activities = {
        "trading_api": _activities_by_heading(trading_page, _TRADING_API_HEADINGS),
        "broker_api": _activities_by_heading(_page("docs/options-trading-overview", pages_dir), _BROKER_API_HEADINGS),
    }
    fixtures: dict[str, Any] = {
        "option_activities/documented_activities.json": activities,
        "option_activities/documented_activity_types.json": _what_the_pages_list(pages_dir),
    }
    for relative, payload in fixtures.items():
        path = DESTINATION / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        logger.info("documented example written", extra={"path": str(path.relative_to(DESTINATION.parents[2]))})
    # Only the Trading API page states a share trade's price in a sentence; the Broker API page says "the strike price".
    golden = _golden(activities, {"trading_api": _stated_trades(trading_page, _TRADING_API_HEADINGS)})
    GOLDEN_DESTINATION.mkdir(parents=True, exist_ok=True)
    for filename, payload in golden.items():
        path = GOLDEN_DESTINATION / filename
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        logger.info("golden fixture written", extra={"path": str(path.relative_to(DESTINATION.parents[2]))})


if __name__ == "__main__":
    main()
