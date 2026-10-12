"""The vendor's real answers for option minute bars, as probed on the wire on 2026-10-12.

The option tests fake the vendor with ``respx``. These builders give the answers
key for key as the vendor sends them (the probe recorded raw HTTP, not the SDK):

* a contract that traded: HTTP 200, ``status: "OK"``, the ticker echoed,
  ``adjusted``, ``request_id``, and ``count``, ``queryCount`` and ``resultsCount``
  all equal to the number of ``results``; a result has ``t`` (the bar's start, ms),
  ``o h l c`` as plain decimals, ``v``, ``vw`` and ``n``;
* a listed contract that did not trade, a contract that never existed, a contract
  after it expired and a Saturday: all one body, ``resultsCount: 0``,
  ``queryCount: 0`` and no ``results`` key and no ``count``;
* a session older than the plan reaches: HTTP 403, ``status: "NOT_AUTHORIZED"``, a
  ``message`` about "this data timeframe", and a ``request_id``.

``BAD_BARS_ANSWERS`` is every answer that is not a well-formed one. None of them may
be stored as a day, and none may escape as a raw exception.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

SESSION_OPEN_MS = 1749821400000  # 09:30 New York on 2025-06-13
MINUTE_MS = 60_000

OUTSIDE_PLAN_MESSAGE = "Your plan doesn't include this data timeframe. Please upgrade your plan at https://polygon.io/pricing"


def bar_row(index: int = 0, **overrides: Any) -> dict[str, Any]:
    """A result row as the vendor writes it (the first bar of the probed traded contract, then one a minute apart)."""
    row = {"v": 4245, "vw": 1.4054, "o": 1.27, "c": 1.56, "h": 1.56, "l": 1.1, "t": SESSION_OPEN_MS + index * MINUTE_MS, "n": 710}
    row.update(overrides)
    return row


def traded_body(symbol: str, results: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "adjusted": False,
        "count": len(results),
        "queryCount": len(results),
        "request_id": "req-1",
        "results": results,
        "resultsCount": len(results),
        "status": "OK",
        "ticker": f"O:{symbol}",
    }
    body.update(overrides)
    return body


def no_trades_body(symbol: str, **overrides: Any) -> dict[str, Any]:
    """What the vendor says for a listed contract that did not trade, and equally for one that never existed."""
    body: dict[str, Any] = {
        "adjusted": False,
        "queryCount": 0,
        "request_id": "req-2",
        "resultsCount": 0,
        "status": "OK",
        "ticker": f"O:{symbol}",
    }
    body.update(overrides)
    return body


def traded(symbol: str, results: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json=traded_body(symbol, results))


def no_trades(symbol: str) -> httpx.Response:
    return httpx.Response(200, json=no_trades_body(symbol))


def outside_plan() -> httpx.Response:
    return httpx.Response(
        403, json={"status": "NOT_AUTHORIZED", "request_id": "req-3", "message": OUTSIDE_PLAN_MESSAGE}
    )


def raw_json(text: str) -> httpx.Response:
    """A 200 whose body is exactly ``text``, for the digits and the literals a Python ``json=`` would change."""
    return httpx.Response(200, content=text.encode(), headers={"content-type": "application/json"})


def _row_json(**fields: str) -> str:
    row = {"v": "12", "o": "1.27", "h": "1.31", "l": "1.25", "c": "1.27", "t": str(SESSION_OPEN_MS)}
    row.update(fields)
    return "{" + ",".join(f'"{k}":{v}' for k, v in row.items() if v is not None) + "}"


def _answer(symbol: str, results: str) -> httpx.Response:
    """A one-bar answer whose ``results`` is the given JSON text."""
    fields = {"status": '"OK"', "ticker": f'"O:{symbol}"', "resultsCount": "1", "results": results}
    return raw_json("{" + ",".join(f'"{k}":{v}' for k, v in fields.items()) + "}")


def bad_bars_answers(symbol: str) -> dict[str, Callable[[], httpx.Response]]:
    """Every 200 answer for ``symbol`` that is not a well-formed one, by a name for the test id.

    Includes the bodies the write-path review drove into the lake (a body that is not
    JSON, an empty body, a JSON list or null, ``results`` a dict, a row without ``t``,
    a null row, a non-numeric price) and the bare answers that once pinned a day as
    "never traded" (``{}``, a status alone, a count the rows do not match).
    """
    ticker = f"O:{symbol}"
    good_row = bar_row(0)
    return {
        "not json": lambda: httpx.Response(200, text="<html>maintenance</html>"),
        "empty body": lambda: httpx.Response(200, text=""),
        "json list": lambda: httpx.Response(200, json=[]),
        "json null": lambda: httpx.Response(200, content=b"null"),
        "bare object": lambda: httpx.Response(200, json={}),
        "status alone": lambda: httpx.Response(200, json={"status": "OK"}),
        "status null": lambda: httpx.Response(200, json=no_trades_body(symbol, status=None)),
        "status absent": lambda: httpx.Response(
            200, json={k: v for k, v in no_trades_body(symbol).items() if k != "status"}
        ),
        "status ERROR": lambda: httpx.Response(200, json=no_trades_body(symbol, status="ERROR", error="boom")),
        "status NOT_AUTHORIZED on a 200": lambda: httpx.Response(200, json=no_trades_body(symbol, status="NOT_AUTHORIZED")),
        "status a list": lambda: httpx.Response(200, json=no_trades_body(symbol, status=["OK"])),
        "no ticker": lambda: httpx.Response(
            200, json={k: v for k, v in no_trades_body(symbol).items() if k != "ticker"}
        ),
        "another contract's ticker": lambda: httpx.Response(200, json=no_trades_body(symbol, ticker="O:SPY250613C00601000")),
        "ticker without its prefix": lambda: httpx.Response(200, json=no_trades_body(symbol, ticker=symbol)),
        "no resultsCount": lambda: httpx.Response(
            200, json={k: v for k, v in no_trades_body(symbol).items() if k != "resultsCount"}
        ),
        "resultsCount a string": lambda: httpx.Response(200, json=no_trades_body(symbol, resultsCount="0")),
        "resultsCount a float": lambda: httpx.Response(200, json=no_trades_body(symbol, resultsCount=0.0)),
        "resultsCount true": lambda: httpx.Response(200, json=no_trades_body(symbol, resultsCount=True)),
        "resultsCount negative": lambda: httpx.Response(200, json=no_trades_body(symbol, resultsCount=-1)),
        "count 5 and results empty": lambda: httpx.Response(200, json=no_trades_body(symbol, resultsCount=5, results=[])),
        "count 5 and no results key": lambda: httpx.Response(200, json=no_trades_body(symbol, resultsCount=5, queryCount=5)),
        "count 0 and one result": lambda: httpx.Response(200, json=no_trades_body(symbol, results=[good_row])),
        "count 2 and one result": lambda: httpx.Response(200, json=traded_body(symbol, [good_row], resultsCount=2)),
        "count 1 and two results": lambda: httpx.Response(200, json=traded_body(symbol, [good_row, bar_row(1)], resultsCount=1)),
        "results null": lambda: httpx.Response(200, json=no_trades_body(symbol, results=None)),
        "results a dict": lambda: httpx.Response(200, json=no_trades_body(symbol, resultsCount=1, results={"t": 1})),
        "results a string": lambda: httpx.Response(200, json=no_trades_body(symbol, resultsCount=1, results="x")),
        "a next page with bars": lambda: httpx.Response(
            200, json=traded_body(symbol, [good_row], next_url=f"https://api.polygon.io/v2/aggs/ticker/{ticker}?cursor=abc")
        ),
        "a next page on an empty answer": lambda: httpx.Response(
            200, json=no_trades_body(symbol, next_url=f"https://api.polygon.io/v2/aggs/ticker/{ticker}?cursor=abc")
        ),
        "a null row": lambda: httpx.Response(200, json=traded_body(symbol, [None])),
        "a row that is a list": lambda: httpx.Response(200, json=traded_body(symbol, [[1, 2]])),
        "a row without t": lambda: httpx.Response(
            200, json=traded_body(symbol, [{k: v for k, v in good_row.items() if k != "t"}])
        ),
        "t a string": lambda: httpx.Response(200, json=traded_body(symbol, [bar_row(0, t=str(SESSION_OPEN_MS))])),
        "t a fraction": lambda: httpx.Response(200, json=traded_body(symbol, [bar_row(0, t=SESSION_OPEN_MS + 0.5)])),
        "t true": lambda: httpx.Response(200, json=traded_body(symbol, [bar_row(0, t=True)])),
        "a price a string": lambda: httpx.Response(200, json=traded_body(symbol, [bar_row(0, o="abc")])),
        "a price null": lambda: httpx.Response(200, json=traded_body(symbol, [bar_row(0, c=None)])),
        "a price true": lambda: httpx.Response(200, json=traded_body(symbol, [bar_row(0, h=True)])),
        "a price missing": lambda: httpx.Response(
            200, json=traded_body(symbol, [{k: v for k, v in good_row.items() if k != "l"}])
        ),
        "a volume missing": lambda: httpx.Response(
            200, json=traded_body(symbol, [{k: v for k, v in good_row.items() if k != "v"}])
        ),
        "a volume a string": lambda: httpx.Response(200, json=traded_body(symbol, [bar_row(0, v="12")])),
        "NaN for a price": lambda: _answer(symbol, "[" + _row_json(o="NaN") + "]"),
        "Infinity for a price": lambda: _answer(symbol, "[" + _row_json(h="Infinity") + "]"),
        "minus Infinity for the volume": lambda: _answer(symbol, "[" + _row_json(v="-Infinity") + "]"),
    }
