"""A closer look at a saved run reads its rows as the store does, with no database (#3030).

``test_saved_runs.py`` builds the rows the store writes, turns every number into the form asyncpg hands back (a whole
number with zeros at its end as ``1E+4``), and reads them. This does the same for the rows the closer look reads on its
own: a session's profit (one session, and the lines two runs are compared by) and a run's header. A pull request has no
database, so these are what holds that a profit of ten thousand is read as ``10000`` and a stored setup that no longer
validates opens a header without a setup, not a crash.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from app.options_backtest import saved_runs
from app.options_backtest.results import SessionStatus
from app.options_backtest.saved_runs import RunHeader
from tests.options_backtest._a_run import a_busy_run
from tests.options_backtest.test_saved_runs import _as_asyncpg_returns_it, _columns_of

D = Decimal


def _session_row(profit: str) -> dict[str, Any]:
    """The first session of the busy run as the store keeps it, every number as asyncpg returns it, with this profit."""
    session = a_busy_run().sessions[0]
    row = dict(
        zip(
            _columns_of(saved_runs._INSERT_SESSION),
            (_as_asyncpg_returns_it(each) for each in saved_runs._session_row(1, 0, session)),
            strict=True,
        )
    )
    row["profit_usd"] = _as_asyncpg_returns_it(D(profit))
    return row


@pytest.mark.parametrize(
    "profit",
    # Whole thousands and tens of thousands are the numbers asyncpg gives in exponent form (1000 as 1E+3, 10000 as 1E+4,
    # 1200000 as 1.2E+6). The text of the number read is the text written, in plain digits.
    ["1000", "10000", "20000", "100000", "1200000", "99990000", "-10000", "5000.50", "0.10", "0"],
)
def test_a_sessions_stored_profit_is_read_in_plain_digits(profit: str) -> None:
    row = _session_row(profit)
    assert profit in ("0", "5000.50", "0.10") or str(row["profit_usd"]) != profit, "the row must hold the awkward form"

    stored = saved_runs._stored_session(row, ())

    assert str(stored.profit_usd) == profit


@pytest.mark.parametrize("profit", ["1000", "10000", "20000", "100000", "1200000", "99990000", "-10000", "5000.50"])
def test_the_line_a_session_gives_a_comparison_has_its_profit_in_plain_digits(profit: str) -> None:
    row = {
        "session_open_ms": 1_791_379_800_000,
        "status": "traded",
        "profit_usd": _as_asyncpg_returns_it(D(profit)),
        "trades": 3,
    }

    line = saved_runs._session_line(row)

    assert (line.session_open_ms, line.status, line.trades) == (1_791_379_800_000, SessionStatus.TRADED, 3)
    assert str(line.profit_usd) == profit


def _run_row(setup_json: str) -> dict[str, Any]:
    """The columns ``get_run_header`` selects, for a complete run of SPY."""
    return {
        "id": 41, "created_at_ms": 1_791_800_000_000, "finished_at_ms": 1_791_800_100_000, "job_id": "j",
        "status": "complete", "setup_name": "the setup", "underlying": "SPY", "first_session_open_ms": 1,
        "last_session_open_ms": 2, "sessions_requested": 2, "fee_policy_text": "x", "calendar_identity": "NYSE",
        "calendar_package_version": "1", "failure_code": None, "failure_reason": None, "failure_detail_json": None,
        "sessions_tested": 2, "sessions_traded": 1, "trades": 3, "trades_won": 1,
        "net_profit_usd": _as_asyncpg_returns_it(D(10000)), "ending_cash_usd": _as_asyncpg_returns_it(D(20000)),
        "setup_json": setup_json,
    }  # fmt: skip


def _header_of(setup_json: str) -> RunHeader:
    return saved_runs._header(_run_row(setup_json), lambda _job_id: None)


def test_a_header_has_the_setup_the_run_stored() -> None:
    setup = a_busy_run().setup

    header = _header_of(setup.model_dump_json())

    assert header.setup == setup and header.setup_cannot_open_reason is None
    assert (header.record.id, header.record.underlying, header.record.status) == (41, "SPY", "complete")
    # The record is read as the history reads it, money in plain digits.
    assert header.record.headline is not None and str(header.record.headline.net_profit_usd) == "10000"


def test_a_run_whose_stored_setup_no_longer_validates_still_has_a_header_with_its_record_and_the_reason() -> None:
    # Edited as a later rule would leave it: a field the setup no longer has.
    stored = json.loads(a_busy_run().setup.model_dump_json()) | {"lots": 1}

    header = _header_of(json.dumps(stored))

    assert header.setup is None and header.setup_cannot_open_reason is not None
    assert header.setup_cannot_open_reason.startswith("lots: ")
    # Its record is stored apart from the setup: the underlying, the status and the money read all the same.
    assert (header.record.id, header.record.underlying, header.record.status) == (41, "SPY", "complete")
    assert header.record.headline is not None and str(header.record.headline.ending_cash_usd) == "20000"


def test_a_header_has_a_setup_or_the_reason_it_has_none_and_never_both_nor_neither() -> None:
    record = _header_of(a_busy_run().setup.model_dump_json()).record
    setup = a_busy_run().setup

    for setup_given, reason in ((setup, "why"), (None, None)):
        with pytest.raises(ValueError, match="not both"):
            RunHeader(record=record, setup=setup_given, setup_cannot_open_reason=reason)
