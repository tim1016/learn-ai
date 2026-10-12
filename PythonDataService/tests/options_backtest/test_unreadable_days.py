"""A stored day the run cannot use: what a download does with it, and what Check data and a run say (#3030).

A stored file can be there and not be one this feature can use: damaged, in a form the lake never writes, or well formed
with a bar no session can place. Whether a download fetches such a day again is the lake's to decide, by the gate it
runs on a stored day before it reuses it, and Check data must say the same: "will be fetched again" only of a day the
lake replaces, "cannot be tested" only of one it keeps. The table is in ``data_check``'s module docstring; every row of
it is a case here, put through a real download so that what the words promise is what the lake does.

These tests seed a real lake through the lake's own capture functions (the vendor faked at the HTTP layer), put another
file in place of Thursday's captured one, and ask the same place four things: Check data (also over HTTP), a download
alone, and a whole run that meets the file as its download begins and as its replay begins. A day the lake replaces is
repaired by one vendor request and then reads, and a run over it completes. A day it keeps costs no request and ends a
run ``DATA_UNREADABLE``, naming the session by its open. None ends in a code the page cannot read (``INTERNAL_ERROR``).

Hand-worked figures. The sessions are Wednesday 2026-10-07 and Thursday 2026-10-08, each opening at 09:30 New York (EDT,
UTC-4) = 13:30 UTC. 2026-10-07 is epoch day 20733 (2026-01-01 is day 20454, and 2026-10-07 is the 280th day of the year:
273 days to the end of September and 7 more, so 279 days after 2026-01-01), and 20733 * 86 400 000 = 1 791 331 200 000
ms is its UTC midnight; 13:30 is 48 600 000 ms after it, so Wednesday opens at 1 791 379 800 000. Thursday is one day
(86 400 000 ms) later: 1 791 466 200 000, and its New York midnight, four hours after its UTC midnight
(1 791 417 600 000 + 14 400 000), is 1 791 432 000 000.

A stock file's row is ``start,open,high,low,close,volume``: the start in ms after New York midnight, the prices in
ten-thousandths of a dollar. 09:30 is 34 200 000 ms (9.5 hours of 3 600 000), 09:31 is 34 260 000, and 500.20 dollars
is 5 002 000. The underlying's odd bar starts 1 ms after the open, Thursday 1 791 466 200 001, and closes 60 000 later:
not on a whole minute. A contract's last-minute bar starts at 23:59 New York, 23 * 3 600 000 + 59 * 60 000 = 86 340 000
ms after the midnight, 1 791 432 000 000 + 86 340 000 = 1 791 518 340 000, and closes at 1 791 518 400 000, which is
Friday's New York midnight: the lake's parser accepts the bar (it starts inside Thursday) and the close falls on Friday.
A contract's off-minute bar starts 30 000 ms after the open, 1 791 466 230 000.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import re
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.data_lake import catalog_client
from app.main import app
from app.options.contract import OptionContract
from app.options_backtest.data_check import DataCheck, DataKind, ProblemCode, SessionVerdict, check_data
from app.options_backtest.fetching import ensure_run_data
from app.options_backtest.lake_check import LakeCheckData
from app.options_backtest.lake_data import UnderlyingBarsCannotBePlaced, UnderlyingDayUnreadable
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
NEW_YORK_MIDNIGHT_MS = {WED: 1791345600000, THU: 1791432000000}
SESSIONS = [WED, THU]
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
THE_ROW_AT_THE_OPEN = "34200000,5002000,5002000,5002000,5002000,100"
THE_ROW_AT_0931 = "34260000,5002000,5002000,5002000,5002000,100"
THE_UNDERLYING_MEMBER = "20261008_spy_minute_trade.csv"


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


def _stock_zip(*rows: str, member: str = THE_UNDERLYING_MEMBER) -> bytes:
    """A zip with one member holding these rows as the lake's writer lays them out: joined by a newline, one at the end."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, "".join(f"{row}\n" for row in rows))
    return buffer.getvalue()


def _zip_of(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def _zip_whose_compressed_stream_is_corrupt() -> bytes:
    """A zip whose one member says it is deflated and holds bytes no deflate decoder accepts.

    The member is first written stored, holding bytes whose first byte is 0x07: its low bit says "last block" and the next
    two, 0b11, are the one block type deflate reserves, so the decoder refuses it at once ("invalid block type") on any
    version of zlib. The method field of the member's local header (at offset 8) and of its central directory entry (at
    offset 10 of the entry) is then changed from 0, stored, to 8, deflated.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(THE_UNDERLYING_MEMBER, b"\x07" + b"\x00" * 40)
    raw = bytearray(buffer.getvalue())
    raw[8:10] = (8).to_bytes(2, "little")
    central = raw.index(b"PK\x01\x02")
    raw[central + 10 : central + 12] = (8).to_bytes(2, "little")
    return bytes(raw)


@dataclass(frozen=True)
class Spoil:
    """A stored file replaced by another, and what is true of the day it leaves.

    ``fetched_again`` is what the lake does on a download: True when it fetches the day again (the vendor is asked
    once and the day then reads), False when it keeps the file. ``replay_says`` is the phrase of a run's failure when
    the replay meets the file.
    """

    path: Callable[[date], str]
    payload: Callable[[date], bytes]
    # What a check or a failure says it is about, in words that name no date: "the underlying" or the contract's symbol.
    named: Callable[[date], str]
    missing: DataKind
    fetched_again: bool
    replay_says: str
    # False leaves the catalog without a receipt for the bytes, so it does not admit them.
    admit: bool = True
    # True when the stock capture, asked for the day, stops with the file's own error: it neither rebuilds nor reuses it.
    stops_the_stock_capture: bool = False

    def apply(self, fake_catalog: FakeCatalog, tmp_lake: Path, day: date) -> None:
        replace_the_stored_file(fake_catalog, raw_root(tmp_lake), self.path(day), self.payload(day), admit=self.admit)


def _underlying_path(day: date) -> str:
    return f"equity/usa/minute/spy/{day:%Y%m%d}_trade.zip"


def _underlying(
    payload: Callable[[date], bytes],
    *,
    fetched_again: bool,
    replay_says: str,
    admit: bool = True,
    stops_the_stock_capture: bool = False,
) -> Spoil:
    return Spoil(
        path=_underlying_path,
        payload=payload,
        named=lambda _day: "the underlying",
        missing=DataKind.UNDERLYING_BARS,
        fetched_again=fetched_again,
        replay_says=replay_says,
        admit=admit,
        stops_the_stock_capture=stops_the_stock_capture,
    )


def _contract(payload: Callable[[date], bytes], *, fetched_again: bool, replay_says: str) -> Spoil:
    return Spoil(
        path=lambda day: option_bars_path(the_500_call(day), day),
        payload=payload,
        named=lambda day: the_500_call(day).occ_symbol,
        missing=DataKind.CONTRACTS,
        fetched_again=fetched_again,
        replay_says=replay_says,
    )


RULES = "rules of a bar"
CANNOT_BE_READ = "cannot be read"

# ── The underlying: the stock capture's gate decides ─────────────────────
# Fetched again: the stock capture's gate rejects the file (or the catalog does not admit it).
UNDERLYING_NOT_A_ZIP = _underlying(lambda _day: b"admitted, but not a zip", fetched_again=True, replay_says=CANNOT_BE_READ)
UNDERLYING_NOT_ADMITTED = _underlying(
    lambda _day: b"bytes the catalog holds no receipt for", fetched_again=True, replay_says=CANNOT_BE_READ, admit=False
)
UNDERLYING_ROW_OF_FIVE_COLUMNS = _underlying(
    lambda _day: _stock_zip(THE_ROW_AT_THE_OPEN, "34260000,5002000,5002000,5002000,100"),
    fetched_again=True,
    replay_says=CANNOT_BE_READ,
)
UNDERLYING_LETTER_IN_A_PRICE = _underlying(
    lambda _day: _stock_zip(THE_ROW_AT_THE_OPEN, "34260000,50x2000,5002000,5002000,5002000,100"),
    fetched_again=True,
    replay_says=CANNOT_BE_READ,
)
UNDERLYING_NAN_PRICE = _underlying(
    lambda _day: _stock_zip(THE_ROW_AT_THE_OPEN, "34260000,NaN,5002000,5002000,5002000,100"),
    fetched_again=True,
    replay_says=CANNOT_BE_READ,
)
UNDERLYING_MEMBER_WITHOUT_A_DATE = _underlying(
    lambda _day: _stock_zip(THE_ROW_AT_THE_OPEN, member="prices.csv"), fetched_again=True, replay_says=CANNOT_BE_READ
)
# The stock capture's contract (no minute twice, every price above zero) rejects these, though the stock reader reads
# them: the replay is what meets them, and its market refuses them as bars that break the rules of a bar.
UNDERLYING_WITH_A_MINUTE_TWICE = _underlying(
    lambda day: stock_day_zip([stock_bar(OPEN_MS[day], "500.20", "500.20", "500.20", "500.20")] * 2, day=day),
    fetched_again=True,
    replay_says=RULES,
)
UNDERLYING_WITH_A_PRICE_OF_ZERO = _underlying(
    lambda _day: _stock_zip("34200000,0,0,0,0,100"), fetched_again=True, replay_says=RULES
)
# Kept: the stock capture's gate passes the file and this feature's reader still refuses it.
UNDERLYING_BAR_OFF_THE_MINUTE = _underlying(
    lambda day: stock_day_zip([stock_bar(OPEN_MS[day] + 1, "500.20", "500.20", "500.20", "500.20")], day=day),
    fetched_again=False,
    replay_says=RULES,
)
UNDERLYING_ROW_OF_SEVEN_COLUMNS = _underlying(
    lambda _day: _stock_zip(THE_ROW_AT_THE_OPEN, THE_ROW_AT_0931 + ",extra"), fetched_again=False, replay_says=CANNOT_BE_READ
)
UNDERLYING_EMPTY_LINE = _underlying(
    lambda _day: _stock_zip(THE_ROW_AT_THE_OPEN, "", THE_ROW_AT_0931), fetched_again=False, replay_says=CANNOT_BE_READ
)
UNDERLYING_PRICE_WRITTEN_WITH_A_PLUS = _underlying(
    lambda _day: _stock_zip(THE_ROW_AT_THE_OPEN, "34260000,+5002000,5002000,5002000,5002000,100"),
    fetched_again=False,
    replay_says=CANNOT_BE_READ,
)
UNDERLYING_ZIP_WITH_NO_MEMBER = _underlying(
    lambda _day: _zip_of({}), fetched_again=False, replay_says=CANNOT_BE_READ
)
UNDERLYING_ZIP_WITH_A_SECOND_MEMBER = _underlying(
    lambda _day: _zip_of({THE_UNDERLYING_MEMBER: THE_ROW_AT_THE_OPEN + "\n", "notes.txt": "x\n"}),
    fetched_again=False,
    replay_says=CANNOT_BE_READ,
)
# The gate does not handle the error a corrupt compressed stream raises, so the capture stops with it: a download does
# not replace such a day, and Check data must not promise it (nor fail itself on the error).
UNDERLYING_CORRUPT_COMPRESSED_STREAM = _underlying(
    lambda _day: _zip_whose_compressed_stream_is_corrupt(),
    fetched_again=False,
    replay_says=CANNOT_BE_READ,
    stops_the_stock_capture=True,
)
UNDERLYING_MEMBER_OF_ANOTHER_SYMBOL = _underlying(
    lambda _day: _stock_zip(THE_ROW_AT_THE_OPEN, member="20261008_qqq_minute_trade.csv"),
    fetched_again=False,
    replay_says=CANNOT_BE_READ,
)

# ── A contract's day: the lake's parser decides, and the reuse gate and the reader both run it ─────
CONTRACT_BAR_OFF_THE_MINUTE = _contract(
    lambda day: option_day_zip(the_500_call(day), [OPEN_MS[day] + 30_000], day=day),
    fetched_again=True,
    replay_says=CANNOT_BE_READ,
)
CONTRACT_THAT_IS_DAMAGED = _contract(
    lambda _day: b"admitted, but not a zip", fetched_again=True, replay_says=CANNOT_BE_READ
)
CONTRACT_BAR_CLOSING_ON_ANOTHER_DAY = _contract(
    lambda day: option_day_zip(the_500_call(day), [NEW_YORK_MIDNIGHT_MS[day] + 86_340_000], day=day),
    fetched_again=False,
    replay_says=RULES,
)

THE_UNDERLYING_SPOILS = {
    "not a zip": UNDERLYING_NOT_A_ZIP,
    "not admitted": UNDERLYING_NOT_ADMITTED,
    "a row of five columns": UNDERLYING_ROW_OF_FIVE_COLUMNS,
    "a letter in a price": UNDERLYING_LETTER_IN_A_PRICE,
    "NaN as a price": UNDERLYING_NAN_PRICE,
    "a member with no date": UNDERLYING_MEMBER_WITHOUT_A_DATE,
    "a minute twice": UNDERLYING_WITH_A_MINUTE_TWICE,
    "a price of zero": UNDERLYING_WITH_A_PRICE_OF_ZERO,
    "a bar off the minute": UNDERLYING_BAR_OFF_THE_MINUTE,
    "a row of seven columns": UNDERLYING_ROW_OF_SEVEN_COLUMNS,
    "an empty line": UNDERLYING_EMPTY_LINE,
    "a price written with a plus": UNDERLYING_PRICE_WRITTEN_WITH_A_PLUS,
    "a zip with no member": UNDERLYING_ZIP_WITH_NO_MEMBER,
    "a zip with a second member": UNDERLYING_ZIP_WITH_A_SECOND_MEMBER,
    "a member of another symbol": UNDERLYING_MEMBER_OF_ANOTHER_SYMBOL,
    "a corrupt compressed stream": UNDERLYING_CORRUPT_COMPRESSED_STREAM,
}
THE_CONTRACT_SPOILS = {
    "contract, a bar off the minute": CONTRACT_BAR_OFF_THE_MINUTE,
    "contract, damaged": CONTRACT_THAT_IS_DAMAGED,
    "contract, a bar closing on another day": CONTRACT_BAR_CLOSING_ON_ANOTHER_DAY,
}
EVERY_SPOIL = {**{f"underlying, {name}": spoil for name, spoil in THE_UNDERLYING_SPOILS.items()}, **THE_CONTRACT_SPOILS}


def _cases(*, fetched_again: bool | None = None, stops_the_stock_capture: bool | None = None) -> list[pytest.ParameterSet]:
    return [
        pytest.param(spoil, id=name)
        for name, spoil in EVERY_SPOIL.items()
        if (fetched_again is None or spoil.fetched_again is fetched_again)
        and (stops_the_stock_capture is None or spoil.stops_the_stock_capture is stops_the_stock_capture)
    ]


def _the_words_of_a_check_problem(spoil: Spoil) -> str:
    """The sentence Check data gives for a day it cannot test, by what is wrong with it."""
    return RULES if spoil.replay_says == RULES else "form the lake writes"


# ── Check data ───────────────────────────────────────────────────────────


def _check(days: list[date]) -> DataCheck:
    return check_data(the_setup(), days, now_ms=NOW_2026_10_12_MS, data=LakeCheckData("SPY"))


@pytest.mark.parametrize("spoil", _cases())
@respx.mock
async def test_check_data_promises_a_fetch_only_of_a_day_the_lake_replaces_and_refuses_one_it_keeps(
    spoil: Spoil, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await a_lake_that_holds_both_sessions()
    assert [s.verdict for s in (await asyncio.to_thread(_check, SESSIONS)).sessions] == [SessionVerdict.READY] * 2
    spoil.apply(fake_catalog, tmp_lake, THU)

    wednesday, thursday = (await asyncio.to_thread(_check, SESSIONS)).sessions

    assert wednesday.verdict is SessionVerdict.READY
    if spoil.fetched_again:
        assert thursday.verdict is SessionVerdict.NEEDS_DOWNLOAD
        assert thursday.problems == ()
        assert thursday.missing == (spoil.missing,)
        [note] = [note for note in thursday.notes if "will be fetched again" in note]
        assert_plain(note)
    else:
        assert thursday.verdict is SessionVerdict.CANNOT_BE_TESTED
        [problem] = thursday.problems
        assert problem.code is ProblemCode.STORED_DATA_IS_MALFORMED
        assert problem.code.value == "stored_data_is_malformed"
        assert spoil.named(THU) in problem.message
        assert _the_words_of_a_check_problem(spoil) in problem.message
        assert_plain(problem.message)
        assert not any("fetched again" in note for note in thursday.notes), "the lake would not fetch such a day again"


@pytest.mark.parametrize(
    ("spoil", "verdict", "codes"),
    [
        pytest.param(UNDERLYING_BAR_OFF_THE_MINUTE, "cannot_be_tested", ["stored_data_is_malformed"], id="underlying kept"),
        pytest.param(
            CONTRACT_BAR_CLOSING_ON_ANOTHER_DAY, "cannot_be_tested", ["stored_data_is_malformed"], id="contract kept"
        ),
        pytest.param(CONTRACT_BAR_OFF_THE_MINUTE, "needs_download", [], id="contract replaced"),
        pytest.param(UNDERLYING_WITH_A_MINUTE_TWICE, "needs_download", [], id="underlying replaced"),
    ],
)
@respx.mock
async def test_check_data_over_http_answers_a_stored_day_the_run_cannot_use_as_the_lake_will_treat_it(
    spoil: Spoil,
    verdict: str,
    codes: list[str],
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    assert (session["session_open_ms"], session["verdict"]) == (OPEN_MS[THU], verdict)
    assert [problem["code"] for problem in session["problems"]] == codes
    assert not DATE.search(response.text) and str(tmp_lake) not in response.text and ".zip" not in response.text


# ── The download phase alone ─────────────────────────────────────────────


@pytest.mark.parametrize("spoil", _cases(fetched_again=True))
@respx.mock
async def test_a_download_fetches_a_day_the_check_promised_to_fetch_again_with_one_request_and_the_day_then_reads(
    spoil: Spoil, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = await a_lake_that_holds_both_sessions()
    spoil.apply(fake_catalog, tmp_lake, THU)
    asked = vendor.vendor_requests

    await asyncio.to_thread(ensure_run_data, the_setup(), SESSIONS, now_ms=NOW_2026_10_12_MS, should_stop=lambda: False)

    # One request: the one file that was spoilt, whichever of the underlying's minute bars or the contract's day it was.
    assert vendor.vendor_requests == asked + 1
    assert [s.verdict for s in (await asyncio.to_thread(_check, SESSIONS)).sessions] == [SessionVerdict.READY] * 2


@pytest.mark.parametrize(
    ("spoil", "error"),
    [
        pytest.param(UNDERLYING_BAR_OFF_THE_MINUTE, UnderlyingBarsCannotBePlaced, id="a bar off the minute"),
        pytest.param(UNDERLYING_ROW_OF_SEVEN_COLUMNS, UnderlyingDayUnreadable, id="a row of seven columns"),
        pytest.param(UNDERLYING_EMPTY_LINE, UnderlyingDayUnreadable, id="an empty line"),
        pytest.param(UNDERLYING_ZIP_WITH_NO_MEMBER, UnderlyingDayUnreadable, id="a zip with no member"),
    ],
)
@respx.mock
async def test_the_download_phase_stops_at_an_underlying_day_the_stock_capture_keeps_and_asks_the_vendor_for_nothing(
    spoil: Spoil, error: type[Exception], fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = await a_lake_that_holds_both_sessions()
    asked = vendor.vendor_requests
    spoil.apply(fake_catalog, tmp_lake, THU)

    with pytest.raises(error):
        await asyncio.to_thread(
            ensure_run_data, the_setup(), SESSIONS, now_ms=NOW_2026_10_12_MS, should_stop=lambda: False
        )

    assert vendor.vendor_requests == asked
    assert (await asyncio.to_thread(LakeCheckData("SPY").download_replaces_the_underlying, THU)) is False


@respx.mock
async def test_a_download_does_not_replace_an_underlying_day_whose_compressed_stream_is_corrupt_it_stops_with_the_error(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The stock capture's gate handles the errors a bad row or a bad zip raise and rebuilds the day; a corrupt compressed
    # stream raises another (zlib's), which it does not handle, so the capture stops with it and fetches nothing. Check
    # data's answer for the day ("cannot be tested", above) rests on this; if the stock side learns to rebuild such a
    # day, this test, that answer and the table in data_check move together.
    vendor = await a_lake_that_holds_both_sessions()
    asked = vendor.vendor_requests
    UNDERLYING_CORRUPT_COMPRESSED_STREAM.apply(fake_catalog, tmp_lake, THU)

    with pytest.raises(zlib.error):
        await asyncio.to_thread(
            ensure_run_data, the_setup(), SESSIONS, now_ms=NOW_2026_10_12_MS, should_stop=lambda: False
        )

    assert vendor.vendor_requests == asked
    assert (await asyncio.to_thread(LakeCheckData("SPY").download_replaces_the_underlying, THU)) is False


@respx.mock
async def test_a_catalog_outage_while_asking_what_a_download_would_do_with_the_underlying_is_raised_and_is_no_answer(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stock capture's gate reads the stored file through the lake's admission, which asks the catalog for the file's
    # receipt. With the catalog gone at that read the lake's error is a RuntimeError, which a damaged zip can raise too;
    # it must not be taken for a fault of the file (the day would be called malformed because the catalog went away).
    await a_lake_that_holds_both_sessions()

    async def down(*args: object, **kwargs: object) -> bool:
        raise catalog_client.CatalogUnavailableError("the catalog is down")

    monkeypatch.setattr(catalog_client, "has_committed_file_receipt", down)

    with pytest.raises(catalog_client.CatalogUnavailableError):
        await asyncio.to_thread(LakeCheckData("SPY").download_replaces_the_underlying, THU)


@respx.mock
async def test_the_download_phase_reuses_a_contract_day_whose_only_fault_is_a_close_on_another_day_and_asks_for_nothing(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The phase's guard calls a captured day that does not read "not stored" and captures it again. The lake's parser
    # accepts this one, so the lake holds it as good: it is reused and nothing is asked of the vendor; the replay is
    # where such a day ends a run.
    vendor = await a_lake_that_holds_both_sessions()
    asked = vendor.vendor_requests
    CONTRACT_BAR_CLOSING_ON_ANOTHER_DAY.apply(fake_catalog, tmp_lake, THU)

    report = await asyncio.to_thread(
        ensure_run_data, the_setup(), SESSIONS, now_ms=NOW_2026_10_12_MS, should_stop=lambda: False
    )

    assert (report.option_days_fetched, report.option_days_reused) == (0, 2)
    assert vendor.vendor_requests == asked


# ── A run that meets one ─────────────────────────────────────────────────


def _hooks_that_spoil_at(job: Job, phase: str, spoil: Callable[[], None]) -> RunHooks:
    """The job's hooks, which also put a stored file in place the moment the run enters ``phase``."""
    hooks = job.hooks

    def on_phase(reported: str) -> None:
        hooks.on_phase(reported)
        if reported == phase:
            spoil()

    return RunHooks(on_phase, hooks.on_progress, hooks.on_log, hooks.should_stop)


async def _run_meeting(spoil: Spoil, phase: str, fake_catalog: FakeCatalog, tmp_lake: Path) -> tuple[FakeStore, Vendor, int]:
    """A run over both sessions whose Thursday file is replaced as ``phase`` begins (Check data has passed by then)."""
    vendor = await a_lake_that_holds_both_sessions()
    asked = vendor.vendor_requests
    store, job = FakeStore(), Job()
    hooks = _hooks_that_spoil_at(job, phase, lambda: spoil.apply(fake_catalog, tmp_lake, THU))
    # A run that fails is recorded in the store before it raises, and the tests read the failure there.
    with contextlib.suppress(RunFailedError):
        await asyncio.to_thread(
            execute_run,
            run_id=7,
            setup=the_setup(),
            sessions=SESSIONS,
            store=store,
            hooks=hooks,
            clock=lambda: NOW_2026_10_12_MS,
        )
    return store, vendor, asked


@pytest.mark.parametrize("spoil", _cases(fetched_again=True))
@respx.mock
async def test_a_run_whose_download_meets_a_day_the_lake_replaces_repairs_it_with_one_request_and_completes(
    spoil: Spoil, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    store, vendor, asked = await _run_meeting(spoil, "downloading_data", fake_catalog, tmp_lake)

    assert store.failed == [] and store.cancelled == []
    assert len(store.completed) == 1
    assert vendor.vendor_requests == asked + 1


@pytest.mark.parametrize("spoil", _cases(fetched_again=False, stops_the_stock_capture=False))
@respx.mock
async def test_a_run_whose_download_meets_a_day_the_lake_keeps_fails_as_unreadable_and_names_the_session_by_its_open(
    spoil: Spoil, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The run has finished Wednesday when it meets the file, so the session it names is Thursday's, not the first.
    store, vendor, asked = await _run_meeting(spoil, "downloading_data", fake_catalog, tmp_lake)

    _assert_failed_as_unreadable(store, spoil)
    assert vendor.vendor_requests == asked, "a day the run cannot use is never a reason to ask the vendor again"


@pytest.mark.parametrize("spoil", _cases())
@respx.mock
async def test_a_run_whose_replay_meets_a_stored_day_it_cannot_use_fails_as_unreadable_and_names_the_session(
    spoil: Spoil, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # Nothing repairs a file in the replay, whatever the lake would have done with it in a download.
    store, vendor, asked = await _run_meeting(spoil, "replaying", fake_catalog, tmp_lake)

    _assert_failed_as_unreadable(store, spoil)
    assert vendor.vendor_requests == asked


def _assert_failed_as_unreadable(store: FakeStore, spoil: Spoil) -> None:
    [(_, failure, _)] = store.failed
    assert store.completed == [] and store.cancelled == []
    assert failure.code == "DATA_UNREADABLE"
    [item] = failure.items
    assert (item.session_open_ms, item.code) == (OPEN_MS[THU], "stored_data_unreadable")
    assert spoil.replay_says in failure.reason, "bars that break a rule and a file that cannot be read are told apart"
    assert_plain(failure.reason)
    assert_plain(item.message)
