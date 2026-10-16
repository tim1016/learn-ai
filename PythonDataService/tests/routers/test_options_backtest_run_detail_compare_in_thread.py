"""Two runs are compared in a worker thread and not on the event loop (#3030).

``compare`` and the answer made of it walk every setting, every session and every trade line of both runs. They are pure
(``_compared`` says why), so they are run with ``asyncio.to_thread``, as the export's CSV was. No database: the real
route, ``run_detail`` and ``saved_runs`` run over a fake connection (``_fake_connection.py``).

Hand-worked. Run 41 is the base setup (enters 5 minutes after the open, 0.3 percent slippage) and run 42 enters 10
minutes after the open with 0.5 percent slippage: two settings differ, entry time and slippage, in that order (the
window first, the costs last). Sessions S1 to S4 are a day apart. Run 41 tested S1, S2 and S3 and run 42 tested S1, S2 and
S4, so the ranges differ (S3 against S4 as the last), S1 and S2 are shared, S3 is only in 41 and S4 only in 42.

* S1: run 41 traded and made 125.50 over 2 trades; run 42 traded and lost 30.25 over 1 trade.
* S2: both skipped, 0 and 0 trades.
* Over the shared sessions: run 41 made 125.50 + 0 = 125.50 in 2 + 0 = 2 trades with 1 winning session (S1 only); run 42
  made -30.25 + 0 = -30.25 in 1 + 0 = 1 trade with 0 winning sessions.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse

from app.options_backtest import run_detail
from app.options_backtest.comparison import compare
from app.options_backtest.run_detail import read_comparison_inputs
from app.options_backtest.setup import MS_PER_MINUTE
from app.routers import options_backtest_run_detail as router
from app.schemas.options_backtest_run_detail import OptionsBacktestComparisonResponse
from tests.options_backtest._fake_connection import (
    CREATED_AT_MS,
    FIRST_OPEN_MS,
    FakeConnection,
    Pool,
    a_line_row,
    a_run_row,
)
from tests.options_backtest._stored_runs import PREFIX
from tests.options_backtest._stored_runs import client as client
from tests.options_backtest.setup_fixtures import setup

DAY_MS = 86_400_000
S1, S2, S3, S4 = (FIRST_OPEN_MS + day * DAY_MS for day in range(4))


def _two_runs() -> FakeConnection:
    runs = [
        a_run_row(41, setup_json=setup().model_dump_json(), first_open_ms=S1, last_open_ms=S3, sessions_requested=3),
        a_run_row(
            42,
            setup_json=setup(
                entry={"anchor": "open", "offset_ms": 10 * MS_PER_MINUTE}, slippage_percent="0.5"
            ).model_dump_json(),
            first_open_ms=S1,
            last_open_ms=S4,
            sessions_requested=3,
        ),
    ]
    lines = {
        41: [
            a_line_row(S1, "traded", "125.50", 2),
            a_line_row(S2, "skipped", "0", 0),
            a_line_row(S3, "no_trade", "0", 0),
        ],
        42: [
            a_line_row(S1, "traded", "-30.25", 1),
            a_line_row(S2, "skipped", "0", 0),
            a_line_row(S4, "traded", "10.00", 1),
        ],
    }
    return FakeConnection(runs, lines=lines)


THE_ANSWER = {
    "underlying": "SPY",
    "a": {
        "id": 41,
        "setup_name": None,
        "created_at_ms": CREATED_AT_MS + 41,
    },
    "b": {
        "id": 42,
        "setup_name": None,
        "created_at_ms": CREATED_AT_MS + 42,
    },
    "setting_differences": [
        {"label": "entry time", "a": "5 minutes after the open", "b": "10 minutes after the open"},
        {"label": "slippage", "a": "0.3%", "b": "0.5%"},
    ],
    "range_difference": {
        "a": {"first_session_open_ms": S1, "last_session_open_ms": S3, "sessions_requested": 3},
        "b": {"first_session_open_ms": S1, "last_session_open_ms": S4, "sessions_requested": 3},
    },
    "overlap": {
        "shared_sessions": 2,
        "only_in_a": 1,
        "only_in_b": 1,
        "a": {"net_profit_usd": "125.50", "trades": 2, "winning_sessions": 1},
        "b": {"net_profit_usd": "-30.25", "trades": 1, "winning_sessions": 0},
        "sessions": [
            {"session_open_ms": S1, "a_status": "traded", "b_status": "traded", "a_profit_usd": "125.50", "b_profit_usd": "-30.25"},
            {"session_open_ms": S2, "a_status": "skipped", "b_status": "skipped", "a_profit_usd": "0", "b_profit_usd": "0"},
        ],
    },
}  # fmt: skip


async def test_the_comparison_and_its_answer_are_made_off_the_event_loop_and_are_what_the_loop_thread_would_make(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What the loop's own thread makes of the same rows, which is what the route did before this was moved.
    inputs = await read_comparison_inputs(_two_runs(), 41, 42)
    inline = OptionsBacktestComparisonResponse.from_comparison(
        inputs.a, inputs.b, compare(inputs.a, inputs.a_lines, inputs.b, inputs.b_lines)
    )
    monkeypatch.setattr(run_detail, "with_connection", Pool(_two_runs()))
    loop_thread = threading.get_ident()
    ran_in: dict[str, int] = {}
    real_compare, real_from_comparison = router.compare, OptionsBacktestComparisonResponse.from_comparison.__func__  # type: ignore[attr-defined]

    def compare_noting_its_thread(*args: Any) -> Any:
        ran_in["compare"] = threading.get_ident()
        return real_compare(*args)

    def from_comparison_noting_its_thread(cls: Any, *args: Any) -> Any:
        ran_in["answer"] = threading.get_ident()
        return real_from_comparison(cls, *args)

    monkeypatch.setattr(router, "compare", compare_noting_its_thread)
    monkeypatch.setattr(
        OptionsBacktestComparisonResponse, "from_comparison", classmethod(from_comparison_noting_its_thread)
    )

    async with client as c:
        response = await c.get(f"{PREFIX}/runs/compare", params={"a": 41, "b": 42})

    assert response.status_code == 200, response.text
    # Both the comparison and the answer built from it ran, and neither on the thread of the event loop.
    assert set(ran_in) == {"compare", "answer"}
    assert ran_in["compare"] != loop_thread and ran_in["answer"] != loop_thread
    assert response.json() == THE_ANSWER
    # The same bytes on the wire as the loop's own thread would have sent.
    assert response.content == JSONResponse(jsonable_encoder(inline)).body


async def test_many_comparisons_at_once_in_many_threads_all_give_the_same_answer() -> None:
    inputs = await read_comparison_inputs(_two_runs(), 41, 42)

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda _: router._compared(inputs).model_dump_json(), range(64)))

    assert len(set(answers)) == 1
    assert OptionsBacktestComparisonResponse.model_validate_json(answers[0]).model_dump(mode="json") == THE_ANSWER
