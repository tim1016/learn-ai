"""Lift the option examples out of Alpaca's published Trading API documents, verbatim.

These fixtures stand in for recordings from Alpaca Paper until one is made
(#2913). Nothing in them is typed by hand: every body, answer and example
object is cut from the page that publishes it and written as it stands, so a
fixture can only say what the documentation says.

Run from ``PythonDataService/``::

    python -m scripts.fixture_generators.alpaca_documented_option_examples
    python -m scripts.fixture_generators.alpaca_documented_option_examples --pages-dir <dir>

Without ``--pages-dir`` the pages are fetched from ``docs.alpaca.markets``,
which serves each one as Markdown at its own address plus ``.md``. With it,
the pages are read from files named as ``PAGES`` names them, so a run can be
repeated against the same reading. No Alpaca account, key or API is touched.

The attribution files beside the fixtures are written by hand and carry the
date the pages were read; re-running this does not change them.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DOCS_ROOT = "https://docs.alpaca.markets"
FETCH_TIMEOUT_S = 30.0

# Page path → the file name a saved copy is read from.
PAGES: dict[str, str] = {
    "reference/postorder": "reference_postorder.md",
    "docs/options-orders": "docs_options-orders.md",
    "docs/options-level-3-trading": "docs_options-level-3-trading.md",
    "docs/options-trading": "docs_options-trading.md",
    "reference/get-options-contracts": "reference_get-options-contracts.md",
    "reference/get-option-contract-symbol_or_id": "reference_get-option-contract-symbol_or_id.md",
    "reference/getaccount-1": "reference_getaccount-1.md",
}

DESTINATION = Path(__file__).parents[2] / "tests/fixtures/alpaca_documented"

_HEADING = re.compile(r"^#{1,4}\s+(?P<title>.+?)\s*$", re.MULTILINE)
_FENCE = re.compile(r"^```[a-z]*\n(?P<body>.*?)\n```", re.MULTILINE | re.DOTALL)


def _page(path: str, pages_dir: Path | None) -> str:
    if pages_dir is not None:
        return (pages_dir / PAGES[path]).read_text(encoding="utf-8")
    response = httpx.get(f"{DOCS_ROOT}/{path}.md", timeout=FETCH_TIMEOUT_S, follow_redirects=True)
    response.raise_for_status()
    return response.text


def _openapi(page: str) -> dict[str, Any]:
    """The OpenAPI definition a reference page carries in its one JSON block."""
    match = re.search(r"```json\n(?P<body>.*?)\n```", page, re.DOTALL)
    if match is None:
        raise SystemExit("a reference page carried no OpenAPI definition")
    return json.loads(match["body"])


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def _first_json_object(text: str) -> dict[str, Any]:
    """The first complete JSON object in ``text``, as written."""
    start = text.index("{")
    parsed, _end = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(parsed, dict):
        raise SystemExit("an example was not a JSON object")
    return parsed


def _bodies_by_heading(page: str) -> dict[str, dict[str, Any]]:
    """Each code block's first JSON object, named for the heading it sits under."""
    headings = [(match.start(), _slug(match["title"])) for match in _HEADING.finditer(page)]
    bodies: dict[str, dict[str, Any]] = {}
    for fence in _FENCE.finditer(page):
        if "{" not in fence["body"]:
            continue
        under = [title for start, title in headings if start < fence.start()]
        if not under:
            continue
        try:
            body = _first_json_object(fence["body"])
        except ValueError:
            # A block the page abridges with "..." is not a whole example.
            continue
        if under[-1] in bodies:
            raise SystemExit(f"two examples sit under the heading {under[-1]!r}")
        bodies[under[-1]] = body
    return bodies


def _order_requests(pages_dir: Path | None) -> dict[str, dict[str, dict[str, Any]]]:
    single = _bodies_by_heading(_page("docs/options-orders", pages_dir))
    multi = {
        name: body
        for name, body in _bodies_by_heading(_page("docs/options-level-3-trading", pages_dir)).items()
        if body.get("order_class") == "mleg"
    }
    if not single or not multi:
        raise SystemExit("the order pages no longer carry the examples this reads")
    return {"single_leg": single, "multi_leg": multi}


def _order_responses(pages_dir: Path | None) -> dict[str, dict[str, Any]]:
    examples = _openapi(_page("reference/postorder", pages_dir))["components"]["examples"]
    return {name: examples[name]["value"] for name in ("OptionOrderResponse", "MultilegOptionsOrderResponse")}


def _contracts(pages_dir: Path | None) -> dict[str, dict[str, Any]]:
    listing = _openapi(_page("reference/get-options-contracts", pages_dir))
    (reference_example,) = listing["components"]["schemas"]["OptionContractsResponse"]["properties"][
        "option_contracts"
    ]["example"]
    lookup = _openapi(_page("reference/get-option-contract-symbol_or_id", pages_dir))
    not_found = lookup["paths"]["/v2/options/contracts/{symbol_or_id}"]["get"]["responses"]["404"]["content"][
        "application/json"
    ]["examples"]["Contract Not Found"]["value"]
    # The Options Trading page abridges its list with "...", so its one whole contract is cut out of it.
    page = _page("docs/options-trading", pages_dir)
    page_example = _first_json_object(page[page.index('"option_contracts"') + len('"option_contracts"') :])
    return {
        "reference_example": reference_example,
        "options_trading_page_example": page_example,
        "not_found": not_found,
    }


def _account(pages_dir: Path | None) -> dict[str, Any]:
    spec = _openapi(_page("reference/getaccount-1", pages_dir))
    return spec["paths"]["/v2/account"]["get"]["responses"]["200"]["content"]["application/json"]["example"]


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages-dir", type=Path, default=None, help="read saved pages instead of fetching them")
    pages_dir: Path | None = parser.parse_args().pages_dir
    fixtures: dict[str, Any] = {
        "option_orders/documented_requests.json": _order_requests(pages_dir),
        "option_orders/documented_responses.json": _order_responses(pages_dir),
        "option_contracts/documented_contracts.json": _contracts(pages_dir),
        "account/documented_account.json": _account(pages_dir),
    }
    for relative, payload in fixtures.items():
        path = DESTINATION / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        logger.info("documented example written", extra={"path": str(path.relative_to(DESTINATION.parents[2]))})


if __name__ == "__main__":
    main()
