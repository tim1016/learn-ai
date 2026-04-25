"""Unit tests for the proactive Polygon rate-limit throttle.

Uses a fake monotonic clock + captured sleeps so each test runs in
milliseconds regardless of the real rate limit. The throttle class is the
subject — we don't hit Polygon's SDK at all.
"""

from __future__ import annotations

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


def test_throttle_disabled_when_max_is_zero(monkeypatch):
    clock = FakeClock()
    _install_fake_clock(monkeypatch, clock)
    throttle = _PolygonThrottle(max_per_min=0)

    for _ in range(50):
        throttle.acquire()

    assert clock.sleeps == [], "max=0 means no pacing, no sleeps"


def test_throttle_allows_first_n_requests_without_sleeping(monkeypatch):
    clock = FakeClock()
    _install_fake_clock(monkeypatch, clock)
    throttle = _PolygonThrottle(max_per_min=5)

    for _ in range(5):
        throttle.acquire()

    assert clock.sleeps == [], "first 5 requests are the allowance — no pacing"


def test_throttle_paces_sixth_request_to_next_window(monkeypatch):
    clock = FakeClock()
    _install_fake_clock(monkeypatch, clock)
    throttle = _PolygonThrottle(max_per_min=5)

    # Burn the 5-req budget at t=1000.
    for _ in range(5):
        throttle.acquire()
    # Advance 10 seconds — still inside the 60s window. The 6th acquire
    # must sleep until the first hit ages out (i.e., 50 more seconds).
    clock.now += 10.0
    throttle.acquire()

    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] == 50.0


def test_throttle_evicts_old_hits_and_resets_budget(monkeypatch):
    clock = FakeClock()
    _install_fake_clock(monkeypatch, clock)
    throttle = _PolygonThrottle(max_per_min=5)

    for _ in range(5):
        throttle.acquire()
    # Advance just past the 60-second window so every old hit ages out.
    clock.now += 61.0

    # The next 5 requests should fly through with zero sleeps because the
    # window is empty again.
    for _ in range(5):
        throttle.acquire()

    assert clock.sleeps == []


def test_throttle_paces_steady_state_burst(monkeypatch):
    clock = FakeClock()
    _install_fake_clock(monkeypatch, clock)
    throttle = _PolygonThrottle(max_per_min=5)

    # Fire 12 back-to-back requests. With instantaneous requests (fake clock
    # only advances during sleeps), the throttle pauses 60 s whenever the
    # window is saturated. The pattern is: 5 free, sleep 60 s, 5 free,
    # sleep 60 s, 2 free. In production each Polygon call takes ~hundreds of
    # ms, so the pauses distribute more finely — but what we care about
    # here is that the throttle caps throughput at ``max_per_min`` and that
    # requests 6 and 11 block rather than race through.
    for _ in range(12):
        throttle.acquire()

    assert len(clock.sleeps) == 2
    assert all(s == 60.0 for s in clock.sleeps)
    # Two full minute-windows elapsed → wall-clock moved 120 s forward.
    assert clock.now - 1000.0 == 120.0
