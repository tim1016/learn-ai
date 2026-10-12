"""The scene the closer-look tests read: three sessions of SPY and a setup with every kind of leg (#3030).

Minute ``k`` is the bar closing ``k`` minutes after the open, as in ``_run_data.py``. The setup buys at minute 5 and is
out at minute 60, with no slippage, a price step of 0.01 and a fee of 0.65 a contract on every fill; square-off is
partial; 10,000.00 to start. The underlying closes at 500.20 all day, so the money is the 500 strike and the strike
one place below it is 495. Three legs, in this order:

* ``call``: the 500 call, 25 percent up on momentum, a 20 percent stop, and one re-entry on momentum;
* ``put``: the 500 put, bought at once, no rule;
* ``lag``: the 495 put, waits for a 10 percent fall in its premium (momentum down).

Wednesday 2026-10-07 is the busy day. Hand-worked, minute by minute (prices are bar closes unless a fill):

    call 500   m5 close 4.00 is the starting price, so the momentum level is 4.00 x 1.25 = 5.00.
               m10 closes at 5.00: momentum reached, the buy is decided; m11 opens at 5.10, so the buy fills at 5.10
               (no slippage, already on the step). Stop 5.10 x 0.8 = 4.08. m20 closes at 4.00, at or under it: the
               stop is hit, a re-entry is earned; m21 opens at 3.90 and the sale fills at 3.90.
               The re-entry starts at m21: m21 closes at 3.80, the new starting price, level 3.80 x 1.25 = 4.75.
               m30 closes at 4.80: reached; m31 opens at 4.85, the second buy fills at 4.85. Stop 4.85 x 0.8 = 3.88.
               No bar at m40 and m41: nothing traded, nothing is filled in. m60 is the exit time; m61 opens at 4.55
               and the sale fills at 4.55 (exit group 2).
    put 500    decided at m5, fills at the open of m6, 2.00. Sold at the open of m61, 2.10 (exit group 2).
    put 495    m5 closes at 1.00, the starting price, level 1.00 x 0.90 = 0.90. It never closes at or under it, so the
               leg is cancelled at the exit time and never trades.

    Trades, in the order the replay records them (by entry fill, then leg order):
      0 put   2.00 -> 2.10:                      (2.10 - 2.00) x 100 - 0.65 - 0.65 =    10.00 - 1.30 =    8.70
      1 call  5.10 -> 3.90, stop:                (3.90 - 5.10) x 100 - 1.30        =  -120.00 - 1.30 = -121.30
      2 call  4.85 -> 4.55, exit time:           (4.55 - 4.85) x 100 - 1.30        =   -30.00 - 1.30 =  -31.30
    The session's profit is 8.70 - 121.30 - 31.30 = -143.90 and the cash goes 10,000.00 -> 9,856.10.

Thursday 2026-10-08: the underlying has no bar at minute 5, so the session is skipped and nothing is read of options.
Friday 2026-10-09: the 500 put has no bar at minute 6, so its buy is dropped. The call stays at 4.10, under 5.00, and
the put 495 at 1.00: both wait until the exit time and are cancelled. No trade; 0.00.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest.run import RunResult
from app.options_backtest.saved_runs import RunFailure
from app.options_backtest.service import RunHooks, execute_run
from app.options_backtest.setup import MS_PER_MINUTE, OptionsBacktestSetup
from tests.options_backtest._run_data import FRI, THU, WED, Day, flat, steady
from tests.options_backtest._run_lake import Vendor
from tests.options_backtest.setup_fixtures import leg, setup_body

# 2026-10-12 14:00 New York, a Monday: more than a day after the last of the three sessions.
NOW_MS = 1_791_828_000_000
SESSIONS = [WED, THU, FRI]
# 2026-10-07 09:30 New York = 13:30 UTC. The 9th opens at 1_791_552_600_000 (``_market.py``); two days earlier.
WED_OPEN_MS = 1_791_379_800_000
THU_OPEN_MS = WED_OPEN_MS + 86_400_000
FRI_OPEN_MS = THU_OPEN_MS + 86_400_000

for _day, _open_ms in ((WED, WED_OPEN_MS), (THU, THU_OPEN_MS), (FRI, FRI_OPEN_MS)):
    # The hand-worked opens above are checked against the calendar when the module loads.
    assert session_window_for_date(_day).open_ms_utc == _open_ms, _day

CALL_500 = "SPY261007C00500000"
PUT_500 = "SPY261007P00500000"
PUT_495 = "SPY261007P00495000"
FRI_CALL_500 = "SPY261009C00500000"
FRI_PUT_500 = "SPY261009P00500000"
FRI_PUT_495 = "SPY261009P00495000"


def minute(open_ms: int, k: int) -> int:
    """The instant of the bar closing ``k`` minutes after a session that opened at ``open_ms``."""
    return open_ms + k * MS_PER_MINUTE


def the_setup() -> OptionsBacktestSetup:
    return OptionsBacktestSetup.model_validate(
        setup_body(
            entry={"anchor": "open", "offset_ms": 5 * MS_PER_MINUTE},
            exit={"anchor": "open", "offset_ms": 60 * MS_PER_MINUTE},
            square_off="partial",
            slippage_percent="0",
            fees={"kind": "per_contract", "per_contract_usd": "0.65"},
            legs=[
                leg(
                    leg_id="call",
                    expiry={"kind": "same_day"},
                    stop_loss={"kind": "percent", "value": "20"},
                    momentum={"kind": "percent_up", "value": "25"},
                    reentry_on_stop={"kind": "on_momentum", "count": 1},
                ),
                leg(leg_id="put", right="put", expiry={"kind": "same_day"}),
                leg(
                    leg_id="lag",
                    right="put",
                    expiry={"kind": "same_day"},
                    strike={"kind": "atm_offset", "steps": -1},
                    momentum={"kind": "percent_down", "value": "10"},
                ),
            ],
        )
    )


def the_scene() -> dict[date, Day]:
    wednesday_call = {
        **flat("4.00", 5, 5),
        **flat("4.10", 6, 9),
        10: ("4.90", "5.00"),
        11: ("5.10", "5.05"),
        **flat("5.00", 12, 19),
        20: ("5.00", "4.00"),
        21: ("3.90", "3.80"),
        **flat("3.70", 22, 29),
        30: ("4.60", "4.80"),
        31: ("4.85", "4.70"),
        **flat("4.60", 32, 39),
        # No bar at 40 and 41.
        **flat("4.60", 42, 60),
        61: ("4.55", "4.50"),
    }
    wednesday_put = {5: ("2.00", "2.00"), 6: ("2.00", "2.02"), **flat("2.02", 7, 60), 61: ("2.10", "2.12")}
    # The 495 put also has a bar closing at the open itself (it started at 09:29) and two closing after the close (it
    # printed after 16:00). The replay never reads them and a timeline does not draw them.
    wednesday_put_495 = {0: ("0.70", "0.70"), **flat("1.00", 5, 60), 391: ("0.50", "0.50"), 392: ("0.50", "0.50")}
    friday_call = {**flat("4.00", 5, 5), **flat("4.10", 6, 60)}
    underlying_without_minute_5 = {k: price for k, price in steady("500.20").items() if k != 5}
    return {
        WED: Day(
            underlying=steady("500.20"),
            options={("call", 500): wednesday_call, ("put", 500): wednesday_put, ("put", 495): wednesday_put_495},
        ),
        THU: Day(underlying=underlying_without_minute_5, options={}),
        # The 500 put is listed and never traded on Friday: captured, with no bar.
        FRI: Day(
            underlying=steady("500.20"),
            options={("call", 500): friday_call, ("put", 500): {}, ("put", 495): wednesday_put_495},
        ),
    }


@dataclass
class CollectingStore:
    """The store a run is handed, which keeps how it ended."""

    completed: list[tuple[int, RunResult, int]] = field(default_factory=list)
    failed: list[tuple[int, RunFailure, int]] = field(default_factory=list)

    def complete(self, run_id: int, result: RunResult, *, finished_at_ms: int) -> None:
        self.completed.append((run_id, result, finished_at_ms))

    def fail(self, run_id: int, failure: RunFailure, *, finished_at_ms: int) -> None:
        self.failed.append((run_id, failure, finished_at_ms))

    def cancel(self, run_id: int, *, finished_at_ms: int) -> None:
        raise AssertionError("the scene is never cancelled")


async def run_the_scene() -> RunResult:
    """Run the scene over a lake in a temporary folder with a faked vendor, and return the run it made.

    The test is decorated with ``@respx.mock`` and asks for ``fake_catalog`` and ``tmp_lake``. The run's pins are the
    hashes of the files it really read, so a test can then change a file and see the pin refuse it.
    """
    Vendor(the_scene()).mount()
    store = CollectingStore()
    hooks = RunHooks(
        on_phase=lambda _: None, on_progress=lambda *_: None, on_log=lambda _: None, should_stop=lambda: False
    )
    await asyncio.to_thread(
        execute_run,
        run_id=1,
        setup=the_setup(),
        sessions=SESSIONS,
        store=store,
        hooks=hooks,
        clock=lambda: NOW_MS,
    )
    assert store.failed == [], store.failed
    [(_, result, _)] = store.completed
    return result
