"""The lane half of go-live: a real bar proves the feed; both halves gate a release (#2269).

IB Gateway is faked at the module seam the check reads (``get_client`` and
``fetch_historical_minute_bars``): nothing here reaches a gateway.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.ibkr.bars import IBKRBarStreamError
from app.broker.ibkr.client import NotConnectedError
from app.services import lane_go_live
from app.services.go_live_hold import (
    GO_LIVE_HOLD_MARKER,
    GoLiveHoldMarker,
    go_live_marker_bytes,
    read_go_live_hold,
)
from app.services.lane_go_live import (
    BAR_CHECK_FRESHNESS_MS,
    BAR_CHECK_SYMBOL,
    GoLiveBarCheckRequired,
    LaneBarCheckFailed,
    check_ibkr_historical_bars,
    forget_bar_checks,
    release_lane_go_live,
)

_T0 = 1_789_100_000_000


class _Client:
    def __init__(self, *, connected: bool = True, connection_lost: bool = False) -> None:
        self._connected = connected
        self.connection_lost = connection_lost

    def is_connected(self) -> bool:
        return self._connected


def _bar(start_ms: int) -> SimpleNamespace:
    return SimpleNamespace(start_ms=start_ms, end_ms=start_ms + 60_000)


@pytest.fixture(autouse=True)
def _no_memory() -> Iterator[None]:
    forget_bar_checks()
    yield
    forget_bar_checks()


def _gateway(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: _Client | Exception | None = None,
    bars: list[SimpleNamespace] | Exception | None = None,
    hang: bool = False,
) -> list[dict]:
    requests: list[dict] = []

    def get_client() -> _Client:
        if isinstance(client, Exception):
            raise client
        return client or _Client()

    async def fetch(client_arg: object, symbol: str, **kwargs: object) -> list[SimpleNamespace]:
        requests.append({"symbol": symbol, **kwargs})
        if hang:
            await asyncio.sleep(10)
        if isinstance(bars, Exception):
            raise bars
        return [_bar(_T0 - 120_000), _bar(_T0 - 60_000)] if bars is None else bars

    monkeypatch.setattr(lane_go_live, "get_client", get_client)
    monkeypatch.setattr(lane_go_live, "fetch_historical_minute_bars", fetch)
    return requests


def _hold(root: Path) -> None:
    marker = GoLiveHoldMarker(
        kind="learn-ai-go-live-hold",
        schema_version=1,
        written_at_ms=_T0,
        volume="learn-ai-alpaca-clerk-data",
        source_commit="a" * 40,
        registry_id="reg_1",
    )
    (root / GO_LIVE_HOLD_MARKER).write_bytes(go_live_marker_bytes(marker))


async def test_a_real_bar_passes_and_reports_what_came_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _gateway(monkeypatch)

    check = await check_ibkr_historical_bars(clock=lambda: _T0)

    assert (check.symbol, check.bar_count) == (BAR_CHECK_SYMBOL, 2)
    assert check.first_bar_start_ms == _T0 - 120_000
    assert check.last_bar_end_ms == _T0
    # Historical, not a live subscription: it answers off-hours too.
    assert requests == [{"symbol": "SPY", "duration": "5 D", "use_rth": True}]


@pytest.mark.parametrize(
    ("gateway", "reason"),
    [
        ({"client": NotConnectedError("IbkrClient is not initialised.")}, "ibkr_gateway_unreachable"),
        ({"client": _Client(connected=False)}, "ibkr_gateway_unreachable"),
        ({"client": _Client(connection_lost=True)}, "ibkr_gateway_unreachable"),
        ({"bars": []}, "ibkr_no_bars"),
        ({"bars": IBKRBarStreamError("pacing violation")}, "ibkr_bar_check_failed"),
        ({"bars": ValueError("could not qualify SPY")}, "ibkr_bar_check_failed"),
    ],
    ids=["no_client", "socket_down", "soft_loss_1100", "zero_bars", "refused", "unqualified"],
)
async def test_anything_short_of_a_real_bar_fails_loudly_by_name(
    monkeypatch: pytest.MonkeyPatch, gateway: dict, reason: str
) -> None:
    _gateway(monkeypatch, **gateway)

    with pytest.raises(LaneBarCheckFailed) as failed:
        await check_ibkr_historical_bars()

    assert failed.value.reason == reason


async def test_a_gateway_that_never_answers_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _gateway(monkeypatch, hang=True)
    monkeypatch.setattr(lane_go_live, "IBKR_BAR_CHECK_TIMEOUT_S", 0.01)

    with pytest.raises(LaneBarCheckFailed) as failed:
        await check_ibkr_historical_bars()

    assert failed.value.reason == "ibkr_bar_check_timed_out"


def test_a_release_without_a_bar_check_refuses_and_the_lane_stays_held(tmp_path: Path) -> None:
    """The confirmation alone is not enough, even to a caller that skips the CLI."""
    _hold(tmp_path)

    with pytest.raises(GoLiveBarCheckRequired):
        release_lane_go_live(tmp_path, operator="inkant", change_ref="go-live")

    assert read_go_live_hold(tmp_path).held is True


async def test_a_failed_check_forgets_an_earlier_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold(tmp_path)
    _gateway(monkeypatch)
    await check_ibkr_historical_bars(clock=lambda: _T0)
    _gateway(monkeypatch, bars=[])
    with pytest.raises(LaneBarCheckFailed):
        await check_ibkr_historical_bars(clock=lambda: _T0)

    with pytest.raises(GoLiveBarCheckRequired):
        release_lane_go_live(tmp_path, operator="inkant", change_ref="go-live", clock=lambda: _T0)

    assert read_go_live_hold(tmp_path).held is True


async def test_a_stale_pass_does_not_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold(tmp_path)
    _gateway(monkeypatch)
    await check_ibkr_historical_bars(clock=lambda: _T0)

    with pytest.raises(GoLiveBarCheckRequired):
        release_lane_go_live(
            tmp_path,
            operator="inkant",
            change_ref="go-live",
            clock=lambda: _T0 + BAR_CHECK_FRESHNESS_MS + 1,
        )

    assert read_go_live_hold(tmp_path).held is True


async def test_a_fresh_pass_releases_and_the_receipt_carries_its_bars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold(tmp_path)
    _gateway(monkeypatch)
    await check_ibkr_historical_bars(clock=lambda: _T0)

    receipt = release_lane_go_live(
        tmp_path, operator="inkant", change_ref="go-live", clock=lambda: _T0 + 60_000
    )

    assert read_go_live_hold(tmp_path).held is False
    assert receipt.was_held is True
    assert receipt.bar_check["bar_count"] == 2
    assert receipt.released_at_ms == _T0 + 60_000
