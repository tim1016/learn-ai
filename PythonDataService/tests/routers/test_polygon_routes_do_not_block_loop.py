"""Slow Polygon reads must leave the API event loop able to serve other work (#3213)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable

import pytest

from app.models.requests import OptionsChainSnapshotRequest, OptionsExpirationsRequest, StockSnapshotRequest
from app.routers import iv30, iv_recorder, options, snapshot


async def _assert_loop_can_release_vendor(
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    method: str,
    call: Callable[[], Awaitable[object]],
) -> None:
    entered = threading.Event()
    released = threading.Event()
    vendor_was_released_by_loop: list[bool] = []

    def stalled_vendor(*_args: object, **_kwargs: object) -> None:
        entered.set()
        vendor_was_released_by_loop.append(released.wait(timeout=5.0))
        raise RuntimeError("stop after the responsiveness probe")

    monkeypatch.setattr(target, method, stalled_vendor)

    async def release_from_loop() -> None:
        assert await asyncio.to_thread(entered.wait, 10.0), "the route reached the vendor call"
        released.set()

    results = await asyncio.gather(call(), release_from_loop(), return_exceptions=True)

    assert vendor_was_released_by_loop == [True], "the event loop ran while the vendor call was pending"
    assert isinstance(results[0], Exception)


@pytest.mark.parametrize(
    ("target", "method", "call"),
    [
        (iv30, "snapshot_around_target", lambda: iv30.iv30_vix_style(iv30.Iv30LiveRequest(symbol="SPY"))),
        (iv30, "snapshot_around_target", lambda: iv30.iv30_parametric(iv30.Iv30LiveRequest(symbol="SPY"))),
        (iv_recorder, "record_iv_snapshot", lambda: iv_recorder.take_snapshot(iv_recorder.SnapshotRequest(ticker="SPY", slot="09:35"))),
        (options.polygon_client, "list_options_expirations", lambda: options.list_options_expirations(OptionsExpirationsRequest(underlying_ticker="SPY"))),
        (snapshot.polygon_client, "list_snapshot_options_chain", lambda: snapshot.get_options_chain_snapshot(OptionsChainSnapshotRequest(underlying_ticker="SPY"))),
        (snapshot.polygon_client, "get_stock_snapshot", lambda: snapshot.get_stock_snapshot(StockSnapshotRequest(ticker="SPY"))),
    ],
    ids=["vix-style", "parametric", "recorder", "expirations", "options-chain", "stock-snapshot"],
)
async def test_vendor_wait_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    method: str,
    call: Callable[[], Awaitable[object]],
) -> None:
    await _assert_loop_can_release_vendor(monkeypatch, target, method, call)


async def test_snapshot_rate_lookup_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        snapshot.polygon_client,
        "list_snapshot_options_chain",
        lambda **_kwargs: {"underlying": {"ticker": "SPY", "price": 100.0}, "contracts": []},
    )
    await _assert_loop_can_release_vendor(
        monkeypatch,
        snapshot,
        "get_rate_and_dividend",
        lambda: snapshot.get_options_chain_snapshot(OptionsChainSnapshotRequest(underlying_ticker="SPY")),
    )


async def test_iv30_rate_lookup_does_not_block_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        iv30,
        "snapshot_around_target",
        lambda *_args: {"underlying": {"price": 100.0}, "contracts": [{}]},
    )
    await _assert_loop_can_release_vendor(
        monkeypatch,
        iv30,
        "_rate_and_dividend",
        lambda: iv30.iv30_vix_style(iv30.Iv30LiveRequest(symbol="SPY")),
    )
