"""Bounded off-loop receipt publication keeps its actual I/O start deadline."""
from __future__ import annotations

import json
import threading

import pytest

from app.jobs import external_work, progress
from app.jobs.external_work import WorkReceipts
from app.utils.timestamps import now_ms_utc
from tests.jobs.test_facade import job_store as job_store


def test_delayed_publication_keeps_original_wait_deadline_and_polling_renews_nothing(
    job_store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    clock = [now_ms_utc()]
    monkeypatch.setattr(external_work, "now_ms_utc", lambda: clock[0])

    def check() -> None:
        entered.set()
        assert release.wait(2)

    sink = WorkReceipts(progress.ProgressEmitter("bounded", renews_lease=False), check)
    sink.phase("downloading")
    try:
        assert entered.wait(2)
        started_ms = clock[0]
        sink.work({"kind": "external_wait", "bound_ms": 2_000})
        sink.check()
        clock[0] += 100
        assert not job_store.expires
    finally:
        release.set()
        sink.deactivate()
    events = [json.loads(fields["event"]) for _, fields in job_store.streams[progress.events_key("bounded")]]
    wait = next(event for event in events if event["type"] == "job.external_wait")
    assert wait["deadline_ms"] == started_ms + 2_000
    assert len([key for key, _ in job_store.expires if key == progress.lease_key("bounded")]) == 2


def test_receipt_overflow_fails_explicitly_and_close_stops_admission(job_store, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(external_work, "MAX_PENDING_WORK_RECEIPTS", 2)

    def check() -> None:
        entered.set()
        assert release.wait(2)

    sink = WorkReceipts(progress.ProgressEmitter("bounded", renews_lease=False), check)
    sink.phase("downloading")
    try:
        assert entered.wait(2)
        sink.progress(1, 3)
        sink.progress(2, 3)
        with pytest.raises(ValueError, match="queue bound"):
            sink.progress(3, 3)
    finally:
        release.set()
        sink.deactivate()
    published = list(job_store.streams[progress.events_key("bounded")])
    sink.progress(3, 3)
    assert job_store.streams[progress.events_key("bounded")] == published
    assert not any(thread.name.startswith("job-receipts-") for thread in threading.enumerate())
