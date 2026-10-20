"""Unit tests for the proactive Polygon rate-limit throttle.

Uses a fake monotonic clock + captured sleeps so each test runs in
milliseconds regardless of the real rate limit. The throttle class is the
subject — we don't hit Polygon's SDK at all.
"""

from __future__ import annotations

import pytest

from app.services.polygon_client import _PolygonThrottle


class FakeClock:
    """Monotonic clock + sleep that bookkeeps simulated time for tests."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        # Advance the fake clock by the sleep duration so the next
        # monotonic() call reflects the elapsed wait.
        self.now += seconds


def _install_fake_clock(monkeypatch, clock: FakeClock) -> None:
    monkeypatch.setattr("app.services.polygon_client.time.monotonic", clock.monotonic)
    monkeypatch.setattr("app.services.polygon_client.time.sleep", clock.sleep)


@pytest.mark.parametrize(
    ("max_per_min", "requests_at_once", "wait_s", "requests_after", "expected_sleeps"),
    [
        pytest.param(0, 50, 0.0, 0, [], id="disabled_when_max_is_zero"),
        pytest.param(5, 5, 0.0, 0, [], id="allows_first_n_requests_without_sleeping"),
        # Burn the 5-req budget at t=1000. Advance 10 seconds -- still inside the 60s window. The 6th
        # acquire must sleep until the first hit ages out (i.e., 50 more seconds).
        pytest.param(5, 5, 10.0, 1, [50.0], id="paces_sixth_request_to_next_window"),
        # Advance just past the 60-second window so every old hit ages out. The next 5 requests should
        # fly through with zero sleeps because the window is empty again.
        pytest.param(5, 5, 61.0, 5, [], id="evicts_old_hits_and_resets_budget"),
        # Fire 12 back-to-back requests. With instantaneous requests (fake clock only advances during
        # sleeps), the throttle pauses 60 s whenever the window is saturated: 5 free, sleep 60 s, 5 free,
        # sleep 60 s, 2 free. In production each Polygon call takes ~hundreds of ms, so the pauses
        # distribute more finely -- but what we care about here is that the throttle caps throughput at
        # ``max_per_min`` and that requests 6 and 11 block rather than race through.
        pytest.param(5, 12, 0.0, 0, [60.0, 60.0], id="paces_steady_state_burst"),
    ],
)
def test_throttle_paces_requests_to_its_per_minute_allowance(
    monkeypatch, max_per_min, requests_at_once, wait_s, requests_after, expected_sleeps
):
    clock = FakeClock()
    _install_fake_clock(monkeypatch, clock)
    throttle = _PolygonThrottle(max_per_min=max_per_min)

    for _ in range(requests_at_once):
        throttle.acquire()
    clock.now += wait_s
    for _ in range(requests_after):
        throttle.acquire()

    assert clock.sleeps == expected_sleeps, "the throttle sleeps only to hold throughput at max_per_min (max=0 is unlimited)"
    # Two full minute-windows elapsed in the steady-state burst → wall-clock moved 120 s forward.
    assert clock.now - 1000.0 == wait_s + sum(expected_sleeps), "wall-clock moved by the wait and the sleeps alone"
