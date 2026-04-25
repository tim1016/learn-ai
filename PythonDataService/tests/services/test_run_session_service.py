"""Unit tests for the in-memory run-session registry."""

from __future__ import annotations

from app.services.run_session_service import RunSessionRegistry


def test_create_returns_unique_session_with_uncancelled_event():
    reg = RunSessionRegistry()

    a = reg.create()
    b = reg.create()

    assert a.id != b.id
    assert a.cancelled.is_set() is False
    assert b.cancelled.is_set() is False


def test_get_returns_stored_session_until_popped():
    reg = RunSessionRegistry()
    s = reg.create()

    assert reg.get(s.id) is s
    popped = reg.pop(s.id)
    assert popped is s
    assert reg.get(s.id) is None
    # Popping a missing id returns None, not raises.
    assert reg.pop(s.id) is None


def test_cancel_flips_event_and_returns_true():
    reg = RunSessionRegistry()
    s = reg.create()

    assert reg.cancel(s.id) is True
    assert s.cancelled.is_set()


def test_cancel_unknown_id_returns_false():
    reg = RunSessionRegistry()

    assert reg.cancel("nonexistent") is False


def test_create_reaps_expired_sessions(monkeypatch):
    reg = RunSessionRegistry()

    # Fix a fake monotonic clock; create() reaps anything older than MAX_AGE_S.
    fake_time = [1000.0]
    monkeypatch.setattr("app.services.run_session_service.time.monotonic", lambda: fake_time[0])

    old = reg.create()
    fake_time[0] += 10_000.0  # advance well past MAX_AGE_S

    # Creating a new session triggers the sweep.
    fresh = reg.create()

    assert reg.get(old.id) is None
    assert reg.get(fresh.id) is fresh
