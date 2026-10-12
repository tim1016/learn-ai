"""Ordinary-progress coalescing and the research waiting state (#3052).

At most one ``job.progress`` event per interval per job, independent of the
internal checkpoint/lease processing; terminal events, phases and logs are
never coalesced, and a pending update lands BEFORE any terminal event so an
older progress can never follow one. The waiting state is a
transition-gated additive event read from accepted scheduling facts.
"""

from __future__ import annotations

import json

import pytest

import app.jobs.progress as progress_module
from app.jobs.progress import ProgressEmitter
from tests.jobs.conftest import _FakeRedis


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    redis = _FakeRedis()
    monkeypatch.setattr(progress_module, "get_redis", lambda: redis)
    return redis


def _events(redis: _FakeRedis, job_id: str = "job-c") -> list[dict]:
    return [json.loads(entry["event"]) for entry in redis.streams.get(f"job:{job_id}:events", [])]


def _progress_events(redis: _FakeRedis) -> list[dict]:
    return [event for event in _events(redis) if event["type"] == "job.progress"]


def test_zero_interval_keeps_the_legacy_every_tick_contract(fake: _FakeRedis) -> None:
    emit = ProgressEmitter("job-c", progress_min_interval_s=0.0)
    for tick in range(5):
        emit.progress(tick, 5, unit="cells")
    assert [event["current"] for event in _progress_events(fake)] == [0, 1, 2, 3, 4]


def test_one_interval_coalesces_a_burst_into_a_single_event(fake: _FakeRedis) -> None:
    emit = ProgressEmitter("job-c", progress_min_interval_s=60.0)
    for tick in range(1, 9):  # eight workers' checkpoint traffic, one logical tick each
        emit.progress(tick, 8, unit="cells")
    landed = _progress_events(fake)
    assert len(landed) == 1, "a burst inside one interval is one event"
    assert landed[0]["current"] == 1, "the interval's first tick lands immediately"


def test_a_pending_update_lands_before_the_terminal_event_never_after(fake: _FakeRedis) -> None:
    emit = ProgressEmitter("job-c", progress_min_interval_s=60.0)
    emit.progress(1, 4, unit="cells")
    emit.progress(2, 4, unit="cells")
    emit.progress(3, 4, unit="cells")  # suppressed, pending
    emit.completed({"done": True})
    types = [event["type"] for event in _events(fake)]
    assert types.index("job.progress") < types.index("job.completed"), "flush-before-terminal ordering"
    assert _progress_events(fake)[-1]["current"] == 3, "the newest suppressed update is the one that lands"
    assert not any(event["type"] == "job.progress" for event in _events(fake)[types.index("job.completed"):])


def test_the_newest_pending_payload_wins_and_detail_fields_ride(fake: _FakeRedis) -> None:
    emit = ProgressEmitter("job-c", progress_min_interval_s=1.0)
    emit.progress(1, 4, unit="cells")
    emit.progress(2, 4, unit="cells", detail={"completed": 2, "reused": 1})
    emit.failed(code="Boom", message="no")
    last = _progress_events(fake)[-1]
    assert last["current"] == 2 and last["completed"] == 2 and last["reused"] == 1


def test_the_waiting_state_is_a_transition_gated_additive_event(fake: _FakeRedis) -> None:
    from app.routers.grid_search import _waiting_emitter

    emit = ProgressEmitter("job-c")
    emit_waiting = _waiting_emitter(emit)
    emit_waiting("capacity", {"workers": 8, "busy_lanes": 8, "queued": 2})
    emit_waiting("capacity", {"workers": 8, "busy_lanes": 8, "queued": 3})  # same state: no event
    emit_waiting("adaptive", {"workers": 8, "busy_lanes": 1, "queued": 0})
    emit_waiting(None, {})
    notes = [event for event in _events(fake) if event["type"] == "job.research_waiting"]
    assert [note["waiting"] for note in notes] == ["capacity", "adaptive", None]
    assert notes[0]["queued"] == 2 and notes[0]["workers"] == 8


# ── The state reader ──────────────────────────────────────────────────────


def test_capacity_and_adaptive_are_distinct_backend_states() -> None:
    from app.research.golden_search.shared_pool import waiting_state_of

    assert waiting_state_of({"ready_width": 4, "busy_lanes": 8, "workers": 8}) == "capacity"
    assert waiting_state_of({"ready_width": 0, "busy_lanes": 3, "workers": 8}) == "adaptive"
    assert waiting_state_of({"ready_width": 4, "busy_lanes": 2, "workers": 8}) is None
    assert waiting_state_of({"ready_width": 0, "busy_lanes": 0, "workers": 8}) is None
    # Golden's DispatchFacts vocabulary (active, not busy_lanes) reads the same.
    assert waiting_state_of({"ready_width": 3, "active": 8, "workers": 8}) == "capacity"
