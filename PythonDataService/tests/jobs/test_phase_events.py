"""The ``job.phase`` event: the same bytes for every job that does not give a label, and the label for one that does.

A page shows a phase by the event's ``friendly`` field when there is one and by the id made readable when there is
none, so the field is added to the event only when a job gives it. Every other job's event must stay byte for byte what
it was; the stored text is compared whole.
"""

from __future__ import annotations

import json

from app.jobs.progress import ProgressEmitter
from tests.jobs.conftest import _FakeRedis

EVENTS = "job:phase-1:events"


def test_a_phase_entered_with_no_label_is_the_event_every_job_has_always_sent(fake: _FakeRedis) -> None:
    ProgressEmitter("phase-1").phase("running")

    # json.dumps writes its keys in the order they were added, separated by ", " and ": ".
    assert [entry["event"] for entry in fake.streams[EVENTS]] == ['{"type": "job.phase", "phase": "running"}']
    assert fake.hashes["job:phase-1:state"]["phase"] == "running"


def test_a_phase_entered_with_a_label_carries_it_beside_the_id_and_the_state_keeps_the_id(fake: _FakeRedis) -> None:
    ProgressEmitter("phase-1").phase("running", friendly="Running backtests")

    assert [json.loads(entry["event"]) for entry in fake.streams[EVENTS]] == [
        {"type": "job.phase", "phase": "running", "friendly": "Running backtests"}
    ]
    assert fake.hashes["job:phase-1:state"]["phase"] == "running"
