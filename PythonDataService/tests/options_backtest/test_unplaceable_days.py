"""A stored day that cannot be used, at every place the run service reads a day (#3030).

The lake can hold a day whose file is well formed and whose bars cannot be put in a session: a bar that starts off a
whole minute, or one that closes on another New York date. The reader refuses such a day whole, as it refuses a damaged
file (``UnderlyingDayUnreadable`` for the underlying, ``OptionBarsFileError`` for a contract; the refusal of a bar that
cannot be placed is a subclass of each). These tests seed a real lake through the lake's own capture functions, put such
a file in place of a captured one, and ask what each place does with it: Check data, the download phase and the replay.
None may end in a code the page cannot read (``INTERNAL_ERROR``) or in an exception nobody handles; a run that meets
the day ends ``DATA_UNREADABLE`` and names the session it was on by its open.

Hand-worked figures. The sessions are Wednesday 2026-10-07 and Thursday 2026-10-08, each opening at 09:30 New York (EDT,
UTC-4) = 13:30 UTC. 2026-10-07 is epoch day 20733 (2026-01-01 is day 20454, and 2026-10-07 is the 280th day of the year:
273 days to the end of September and 7 more, so 279 days after 2026-01-01), and 20733 * 86 400 000 = 1 791 331 200 000
ms is its UTC midnight; 13:30 is 48 600 000 ms after it, so Wednesday opens at 1 791 379 800 000. Thursday is one day
(86 400 000 ms) later: 1 791 466 200 000. A bar is stamped by its start. The underlying's odd bar starts 1 ms after the
open, Thursday 1 791 466 200 001; the contract's starts 30 000 ms after it, 1 791 466 230 000. Each closes a minute
after its start, which is not on a whole minute.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.main import app
from app.options.contract import OptionContract
from app.options_backtest.data_check import DataCheck, ProblemCode, SessionVerdict, check_data
from app.options_backtest.fetching import ensure_run_data
from app.options_backtest.lake_check import LakeCheckData
from app.options_backtest.lake_data import UnderlyingBarsCannotBePlaced
from app.options_backtest.service import RunFailedError, RunHooks, execute_run
from app.options_backtest.setup import OptionsBacktestSetup
from app.routers import options_backtest_runs as runs_router
from app.utils.session_anchors import et_day_end_ms, et_midnight_ms
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import (
    NOW_2026_10_12_MS,
    option_bars_path,
    option_day_zip,
    raw_root,
    replace_the_stored_file,
    stock_bar,
    stock_day_zip,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import THU, WED
from tests.options_backtest._run_lake import Vendor, ladder_day
from tests.options_backtest._words import assert_plain
from tests.options_backtest.setup_fixtures import leg, setup, setup_body
from tests.options_backtest.test_service import FakeStore, Job

OPEN_MS = {WED: 1791379800000, THU: 1791466200000}
SESSIONS = [WED, THU]
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def the_setup() -> OptionsBacktestSetup:
    """One same-day call at the money: on the ladder's 500.20 underlying, the 500 call, a session's only contract."""
    return setup(legs=[leg(expiry={"kind": "same_day"})])


def the_500_call(day: date) -> OptionContract:
    return OptionContract.of(root="SPY", expiry_date=day, right="call", strike=500)


async def a_lake_that_holds_both_sessions() -> Vendor:
    """Everything the two sessions need is captured through the lake's own functions, by the download phase."""
    vendor = Vendor({WED: ladder_day(), THU: ladder_day()})
    vendor.mount()
    await asyncio.to_thread(ensure_run_data, the_setup(), SESSIONS, now_ms=NOW_2026_10_12_MS, should_stop=lambda: False)
    return vendor


@dataclass(frozen=True)
class Spoil:
    """A stored file replaced by another: where it lives, and the bytes that take its place."""

    path: Callable[[date], str]
    payload: Callable[[date], bytes]
    # What a check or a failure says it is about, in words that name no date.
    named: Callable[[date], str]

    def apply(self, fake_catalog: FakeCatalog, tmp_lake: Path, day: date) -> None:
        # The receipt is made to match, so the catalog admits the file: it is well formed, or at least admitted.
        replace_the_stored_file(fake_catalog, raw_root(tmp_lake), self.path(day), self.payload(day), admit=True)


def _underlying_path(day: date) -> str:
    return f"equity/usa/minute/spy/{day:%Y%m%d}_trade.zip"


UNDERLYING_WITH_A_BAR_OFF_THE_MINUTE = Spoil(
    path=_underlying_path,
    payload=lambda day: stock_day_zip([stock_bar(OPEN_MS[day] + 1, "500.20", "500.20", "500.20", "500.20")], day=day),
    named=lambda _day: "the underlying",
)
CONTRACT_WITH_A_BAR_OFF_THE_MINUTE = Spoil(
    path=lambda day: option_bars_path(the_500_call(day), day),
    payload=lambda day: option_day_zip(the_500_call(day), [OPEN_MS[day] + 30_000], day=day),
    named=lambda day: the_500_call(day).occ_symbol,
)
UNDERLYING_THAT_IS_DAMAGED = Spoil(
    path=_underlying_path, payload=lambda _day: b"admitted, but not a zip", named=lambda _day: "the underlying"
)
CONTRACT_THAT_IS_DAMAGED = Spoil(
    path=lambda day: option_bars_path(the_500_call(day), day),
    payload=lambda _day: b"admitted, but not a zip",
    named=lambda day: the_500_call(day).occ_symbol,
)

# ── Check data ───────────────────────────────────────────────────────────


def _check(days: list[date]) -> DataCheck:
    return check_data(the_setup(), days, now_ms=NOW_2026_10_12_MS, data=LakeCheckData("SPY"))


@pytest.mark.parametrize(
    "spoil", [UNDERLYING_WITH_A_BAR_OFF_THE_MINUTE, CONTRACT_WITH_A_BAR_OFF_THE_MINUTE], ids=["underlying", "contract"]
)
@respx.mock
async def test_check_data_cannot_test_a_session_whose_stored_day_holds_a_bar_no_session_can_place(
    spoil: Spoil, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await a_lake_that_holds_both_sessions()
    assert [s.verdict for s in (await asyncio.to_thread(_check, SESSIONS)).sessions] == [SessionVerdict.READY] * 2
    spoil.apply(fake_catalog, tmp_lake, THU)

    wednesday, thursday = (await asyncio.to_thread(_check, SESSIONS)).sessions

    assert wednesday.verdict is SessionVerdict.READY
    assert thursday.verdict is SessionVerdict.CANNOT_BE_TESTED
    [problem] = thursday.problems
    assert problem.code is ProblemCode.STORED_DATA_IS_MALFORMED
    assert problem.code.value == "stored_data_is_malformed"
    assert spoil.named(THU) in problem.message
    assert_plain(problem.message)
    assert not any("fetched again" in note for note in thursday.notes), "the lake would not fetch such a day again"


@pytest.mark.parametrize(
    "spoil", [UNDERLYING_WITH_A_BAR_OFF_THE_MINUTE, CONTRACT_WITH_A_BAR_OFF_THE_MINUTE], ids=["underlying", "contract"]
)
@respx.mock
async def test_check_data_over_http_answers_a_stored_day_no_session_can_place_as_a_session_that_cannot_be_tested(
    spoil: Spoil, fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await a_lake_that_holds_both_sessions()
    spoil.apply(fake_catalog, tmp_lake, THU)
    monkeypatch.setattr(runs_router, "now_ms_utc", lambda: NOW_2026_10_12_MS)
    body = {
        "setup": setup_body(legs=[leg(expiry={"kind": "same_day"})]),
        "start_ms": et_midnight_ms(THU),
        "end_ms": et_day_end_ms(THU),
    }

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/research/options-backtest/check-data", json=body)

    assert response.status_code == 200, response.text
    [session] = response.json()["sessions"]
    assert (session["session_open_ms"], session["verdict"]) == (OPEN_MS[THU], "cannot_be_tested")
    assert [problem["code"] for problem in session["problems"]] == ["stored_data_is_malformed"]
    assert not DATE.search(response.text) and str(tmp_lake) not in response.text and ".zip" not in response.text


# ── The download phase ───────────────────────────────────────────────────


@respx.mock
async def test_the_download_phase_stops_at_an_underlying_day_no_session_can_place_and_asks_the_vendor_for_nothing(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = await a_lake_that_holds_both_sessions()
    asked = vendor.vendor_requests
    UNDERLYING_WITH_A_BAR_OFF_THE_MINUTE.apply(fake_catalog, tmp_lake, THU)

    with pytest.raises(UnderlyingBarsCannotBePlaced):
        await asyncio.to_thread(
            ensure_run_data, the_setup(), SESSIONS, now_ms=NOW_2026_10_12_MS, should_stop=lambda: False
        )

    assert vendor.vendor_requests == asked


@respx.mock
async def test_the_download_phase_asks_the_lake_again_for_a_contract_day_no_session_can_place_and_the_lake_holds_it(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The phase's guard calls a captured day that does not read "not stored" and captures it again. The lake holds this
    # one as good, so it is reused and nothing is asked of the vendor; the replay is where such a day ends a run.
    vendor = await a_lake_that_holds_both_sessions()
    asked = vendor.vendor_requests
    CONTRACT_WITH_A_BAR_OFF_THE_MINUTE.apply(fake_catalog, tmp_lake, THU)

    report = await asyncio.to_thread(
        ensure_run_data, the_setup(), SESSIONS, now_ms=NOW_2026_10_12_MS, should_stop=lambda: False
    )

    assert (report.option_days_fetched, report.option_days_reused) == (0, 2)
    assert vendor.vendor_requests == asked


# ── A run that meets one ─────────────────────────────────────────────────

# (what is put in place of Thursday's file, the phase the run has just entered when it is put there)
MEETS = [
    pytest.param(
        UNDERLYING_WITH_A_BAR_OFF_THE_MINUTE, "downloading_data", id="underlying, bar off the minute, at the download"
    ),
    pytest.param(UNDERLYING_WITH_A_BAR_OFF_THE_MINUTE, "replaying", id="underlying, bar off the minute, at the replay"),
    pytest.param(
        CONTRACT_WITH_A_BAR_OFF_THE_MINUTE, "downloading_data", id="contract, bar off the minute, at the download"
    ),
    pytest.param(CONTRACT_WITH_A_BAR_OFF_THE_MINUTE, "replaying", id="contract, bar off the minute, at the replay"),
    pytest.param(UNDERLYING_THAT_IS_DAMAGED, "replaying", id="underlying, damaged, at the replay"),
    pytest.param(CONTRACT_THAT_IS_DAMAGED, "replaying", id="contract, damaged, at the replay"),
]


def _hooks_that_spoil_at(job: Job, phase: str, spoil: Callable[[], None]) -> RunHooks:
    """The job's hooks, which also put a stored file in place the moment the run enters ``phase``."""
    hooks = job.hooks

    def on_phase(reported: str) -> None:
        hooks.on_phase(reported)
        if reported == phase:
            spoil()

    return RunHooks(on_phase, hooks.on_progress, hooks.on_log, hooks.should_stop)


@pytest.mark.parametrize(("spoil", "phase"), MEETS)
@respx.mock
async def test_a_run_that_meets_a_stored_day_it_cannot_use_fails_as_unreadable_and_names_the_session_by_its_open(
    spoil: Spoil, phase: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # Check data passes (both sessions are stored whole), and Thursday's file is replaced as the phase begins. The run
    # has finished Wednesday when it meets it, so the session it names is Thursday's, not the first.
    vendor = await a_lake_that_holds_both_sessions()
    asked = vendor.vendor_requests
    store, job = FakeStore(), Job()
    hooks = _hooks_that_spoil_at(job, phase, lambda: spoil.apply(fake_catalog, tmp_lake, THU))

    with pytest.raises(RunFailedError):
        await asyncio.to_thread(
            execute_run,
            run_id=7,
            setup=the_setup(),
            sessions=SESSIONS,
            store=store,
            hooks=hooks,
            clock=lambda: NOW_2026_10_12_MS,
        )

    [(_, failure, _)] = store.failed
    assert store.completed == [] and store.cancelled == []
    assert failure.code == "DATA_UNREADABLE"
    [item] = failure.items
    assert (item.session_open_ms, item.code) == (OPEN_MS[THU], "stored_data_unreadable")
    assert_plain(failure.reason)
    assert_plain(item.message)
    assert vendor.vendor_requests == asked, "a day the run cannot use is never a reason to ask the vendor again"
