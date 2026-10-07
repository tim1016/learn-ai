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

The attribution file beside the fixtures is written by hand and carries the
date the pages were read; re-running this does not change it.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from scripts.fixture_generators.alpaca_documented_option_examples import DESTINATION, DOCS_ROOT, FETCH_TIMEOUT_S

logger = logging.getLogger(__name__)

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
    fixtures: dict[str, Any] = {
        "option_activities/documented_activities.json": {
            "trading_api": _activities_by_heading(
                _page("docs/non-trade-activities-for-option-events", pages_dir), _TRADING_API_HEADINGS
            ),
            "broker_api": _activities_by_heading(_page("docs/options-trading-overview", pages_dir), _BROKER_API_HEADINGS),
        },
        "option_activities/documented_activity_types.json": _what_the_pages_list(pages_dir),
    }
    for relative, payload in fixtures.items():
        path = DESTINATION / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        logger.info("documented example written", extra={"path": str(path.relative_to(DESTINATION.parents[2]))})


if __name__ == "__main__":
    main()
