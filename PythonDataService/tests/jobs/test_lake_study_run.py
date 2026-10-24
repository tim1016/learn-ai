"""The run every lake-sourced study shares, driven with a toy study (#3521 review; slices 32-34 adopt it).

The Feature study's own tests (``test_feature_study_job.py``) pin what Feature does with the run. These pin what the
run does for *any* study: the data-source block, the refusal payload, the cache identity and the cancel check, with a
study whose math is a stand-in. Lake, vendor, catalog and the job store are the real seams, as in the Feature tests.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx

from app.config import settings
from app.jobs import cache as result_cache
from app.jobs.progress import CancellationCheck, JobCancelled, ProgressEmitter
from app.jobs.runner import run_in_thread
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.research import lake_study
from app.research.lake_study import FoldedStudyIdentity, LakeStudy, StudyRun, run_lake_study
from app.research.study_inputs import MinuteWindowIdentity, StudyInputRefusal
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import commit_bars, corrected_day
from tests._helpers.pinned_vendor import mock_vendor, vendor_history
from tests.jobs.conftest import _FakeRedis

WINDOW = expected_sessions(date(2024, 3, 4), date(2024, 3, 8))
NOW_MS = 1_735_689_600_000  # 2025-01-01


class Toy:
    """A study whose math is a stand-in: it counts its runs and reports what it was handed."""

    def __init__(self) -> None:
        self.runs: list[StudyRun] = []

    def study(
        self, *, knob: int = 3, subject: dict[str, Any] | None = None, cache_extra: dict[str, Any] | None = None
    ) -> LakeStudy:
        def compute(run: StudyRun) -> dict[str, Any]:
            self.runs.append(run)
            return {"success": True, "bars": len(run.rows), "first_bar_ms": run.rows[0]["timestamp"]}

        return LakeStudy(
            job_type="feature_research",
            cache_namespace="toy:lake:v1",
            calculation_version="toy/1",
            requester="toy",
            subject=subject if subject is not None else {"ticker": "SPY"},
            admit_message="Checking the lake",
            load_message="Loading bars",
            compute=compute,
            cache_extra=cache_extra if cache_extra is not None else {"knob": knob},
        )

    @staticmethod
    def identity(*, bar_minutes: int = 5, days: list[date] = WINDOW) -> FoldedStudyIdentity:
        return FoldedStudyIdentity(MinuteWindowIdentity("SPY", "raw", "extended", days[0], days[-1], 0), bar_minutes)


@pytest.fixture
def toy() -> Toy:
    return Toy()


@pytest.fixture
def lake_world(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    catalog = install_fake_catalog(monkeypatch)
    point_lake_writer_at_tmp(tmp_path, monkeypatch)
    tape = vendor_history(WINDOW, seed=3, start_price=50.0, regular_keep=0.995, extended_keep=0.9)
    with respx.mock:
        mock_launcher()
        mock_vendor("SPY", tape)
        yield catalog, tape


def run_job(fake: _FakeRedis, study: LakeStudy, make_identity, job_id: str, *, force: bool = False) -> dict[str, str]:
    def work(emit: ProgressEmitter, cancel: Any) -> dict | None:
        return run_lake_study(study, make_identity, job_id=job_id, force=force, emit=emit, cancel=cancel, now_ms=NOW_MS)

    run_in_thread(job_id, work, thread_name=f"test-{job_id}").join(timeout=120)
    return fake.hashes[f"job:{job_id}:state"]


def result_of(fake: _FakeRedis, job_id: str) -> dict:
    return json.loads(fake.strings[f"job:{job_id}:result"])


# ---------------------------------------------------------------------------
# A completed study
# ---------------------------------------------------------------------------


def test_a_completed_study_gets_folded_bars_and_a_data_source_naming_what_was_read(fake, lake_world, toy):
    state = run_job(fake, toy.study(), lambda: toy.identity(bar_minutes=5), "done")

    result, source = result_of(fake, "done"), result_of(fake, "done")["data_source"]
    assert state["status"] == "completed" and result["success"] is True
    assert result["first_bar_ms"] % 300_000 == 0, "the study is handed 5-minute bars on a wall-clock boundary"
    assert source["kind"] == "lake" and source["symbol"] == "SPY" and source["bar_minutes"] == 5
    assert source["aggregation"] == "minute_fold_et_wall_clock_no_fill"
    assert source["calculation_version"] == "toy/1" and len(source["snapshot_digest"]) == 64
    assert source["coverage"]["window"]["added"] == len(WINDOW) and source["coverage"]["window"]["failed"] == 0
    assert source["bars_used"] == result["bars"]
    assert source["cache"] == {"hit": False, "cached_at_ms": None}
    # What was admitted, as ET-midnight dates; the requested bounds are in the identity beside it.
    assert source["effective_window"] == {"start_ms": et_midnight_ms(WINDOW[0]), "end_ms": et_midnight_ms(WINDOW[-1])}
    assert (source["window_start_ms"], source["window_end_ms"]) == (et_midnight_ms(WINDOW[0]), et_midnight_ms(WINDOW[-1]))


def test_the_effective_window_is_the_sessions_admitted_not_the_dates_asked_for(fake, lake_world, toy):
    saturday_to_sunday_padding = [date(2024, 3, 2), *WINDOW, date(2024, 3, 10)]
    identity = FoldedStudyIdentity(
        MinuteWindowIdentity("SPY", "raw", "extended", saturday_to_sunday_padding[0], saturday_to_sunday_padding[-1], 0), 5
    )

    run_job(fake, toy.study(), lambda: identity, "padded")

    source = result_of(fake, "padded")["data_source"]
    assert source["window_start_ms"] == et_midnight_ms(date(2024, 3, 2)), "the identity keeps the request as asked"
    assert source["effective_window"]["start_ms"] == et_midnight_ms(WINDOW[0]), "the first session admitted is Monday"
    assert source["effective_window"]["end_ms"] == et_midnight_ms(WINDOW[-1])


# ---------------------------------------------------------------------------
# T05: a failed admission refuses
# ---------------------------------------------------------------------------


def test_a_refused_admission_parks_a_payload_with_the_reason_the_counts_and_the_study_s_own_fields(fake, lake_world, toy):
    _, tape = lake_world
    del tape[WINDOW[2]]

    state = run_job(fake, toy.study(), lambda: toy.identity(), "refused")

    payload = result_of(fake, "refused")
    assert state["status"] == "failed" and state["error_code"] == "data_unavailable_from_provider"
    assert payload["success"] is False and payload["status"] == "refused" and payload["ticker"] == "SPY"
    assert payload["error"] == state["error_message"]
    assert payload["refusal"]["sessions_total"] == 1 and payload["refusal"]["sessions"][0]["outcome"] == "unavailable_from_provider"
    assert payload["refusal"]["sessions"][0]["trading_date_ms"] == session_open_ms_utc(WINDOW[2])
    source = payload["data_source"]
    assert source["coverage"]["window"]["failed"] == 1 and source["coverage"]["window"]["added"] == len(WINDOW) - 1
    assert source["snapshot_digest"] is None and source["effective_window"] is None, "nothing was admitted"
    assert toy.runs == [], "a refused study computes nothing"
    assert [k for k in fake.strings if k.startswith("result:")] == [], "and a refusal is never cached"


def test_a_study_refused_before_anything_was_read_names_no_data_source(fake, lake_world, toy):
    def refuse() -> FoldedStudyIdentity:
        raise StudyInputRefusal("resolution_unsupported", "60-minute bars cannot form the target")

    state = run_job(fake, toy.study(), refuse, "no-source")

    payload = result_of(fake, "no-source")
    assert state["error_code"] == "resolution_unsupported"
    assert payload["data_source"] is None, "there is no source to name when nothing was read"
    assert payload["refusal"]["coverage"] is None and payload["ticker"] == "SPY"


def test_a_subject_named_like_a_log_record_field_is_echoed_and_does_not_break_the_refusal(fake, lake_world, toy):
    """The refusal is logged with the subject beside it; a subject key such as ``name`` once raised inside the log call
    and the job ended in an internal error instead of the typed refusal."""

    def refuse() -> FoldedStudyIdentity:
        raise StudyInputRefusal("resolution_unsupported", "60-minute bars cannot form the target")

    state = run_job(fake, toy.study(subject={"name": "momentum", "module": "m", "args": 5}), refuse, "log-fields")

    payload = result_of(fake, "log-fields")
    assert state["error_code"] == "resolution_unsupported", "the refusal, not an internal error"
    assert (payload["name"], payload["module"], payload["args"]) == ("momentum", "m", 5), "the echo is unchanged"
    assert payload["status"] == "refused"


@pytest.mark.parametrize("key", sorted(lake_study.RESULT_KEYS))
def test_a_subject_cannot_name_a_key_the_run_sets_in_its_own_payload(toy, key):
    with pytest.raises(ValueError, match=key):
        toy.study(subject={"ticker": "SPY", key: "x"})


# ---------------------------------------------------------------------------
# T09: cache identity
# ---------------------------------------------------------------------------


def test_the_cache_key_carries_the_study_s_own_inputs_and_the_admitted_bytes(fake, lake_world, toy):
    run_job(fake, toy.study(knob=3), lambda: toy.identity(), "first")

    assert run_job(fake, toy.study(knob=3), lambda: toy.identity(), "same").get("cached") == "1"
    assert run_job(fake, toy.study(knob=4), lambda: toy.identity(), "knob").get("cached") != "1", "cache_extra decides"
    assert run_job(fake, toy.study(), lambda: toy.identity(bar_minutes=15), "bars").get("cached") != "1"
    assert len(toy.runs) == 3
    assert all(key.startswith("result:toy:lake:v1:") for key in fake.strings if key.startswith("result:")), (
        "every entry is under the study's own namespace"
    )


def test_a_study_s_inputs_named_like_the_cache_identity_cannot_hide_a_repair_of_the_lake(fake, lake_world, toy):
    """``cache_extra`` is the study's; the digest of the admitted bytes is the run's. A study input that happens to be
    named ``snapshot_digest`` once replaced it, and a repaired day then hit the entry computed on the old bytes."""
    greedy = {"snapshot_digest": "same", "identity": "same", "data_root_id": "same", "calculation_version": "same"}
    catalog, tape = lake_world
    run_job(fake, toy.study(cache_extra=greedy), lambda: toy.identity(), "before")
    day = WINDOW[2]
    commit_bars(catalog, Path(settings.LEAN_DATA_WRITE_ROOT), day, corrected_day(day, tape[day]))

    state = run_job(fake, toy.study(cache_extra=greedy), lambda: toy.identity(), "after-repair")

    assert state.get("cached") != "1", "different bytes are a different revision, whatever the study calls its inputs"
    assert len(toy.runs) == 2


def test_a_cache_entry_that_never_recorded_its_time_reports_it_as_unknown_not_as_zero(fake, lake_world, toy):
    run_job(fake, toy.study(), lambda: toy.identity(), "first")
    (key,) = (k for k in fake.strings if k.startswith("result:toy:lake:v1:"))
    entry = json.loads(fake.strings[key])
    del entry["cached_at"]
    fake.strings[key] = json.dumps(entry)

    run_job(fake, toy.study(), lambda: toy.identity(), "second")

    assert result_of(fake, "second")["data_source"]["cache"] == {"hit": True, "cached_at_ms": None}


def test_force_skips_the_lookup_but_still_stores(fake, lake_world, toy):
    run_job(fake, toy.study(), lambda: toy.identity(), "first")

    state = run_job(fake, toy.study(), lambda: toy.identity(), "forced", force=True)

    assert state.get("cached") != "1" and len(toy.runs) == 2


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_admission_consults_the_cancel_flag_at_every_step_though_the_jobs_own_check_is_throttled(
    fake, monkeypatch, toy
):
    """The Feature job asks for a throttled check (100): without its own, a Cancel during a cold capture is not seen."""
    handed: dict[str, Any] = {}

    def stop_at_admission(inputs, *, requester, now_ms, check_cancelled):
        handed["check"] = check_cancelled
        raise StudyInputRefusal("lake_unavailable", "the test stops here")

    monkeypatch.setattr(lake_study, "admit_minute_inputs", stop_at_admission)
    emit, cancel = ProgressEmitter("cancelme"), CancellationCheck("cancelme", check_every_n=100)

    run_lake_study(toy.study(), lambda: toy.identity(), job_id="cancelme", force=False, emit=emit, cancel=cancel)
    cancel.raise_if_cancelled()  # a job's first check always reads the flag; nothing is requested yet
    fake.hset("job:cancelme:state", mapping={"cancel_requested": "1"})

    cancel.raise_if_cancelled()  # throttled: it does not read Redis again until its 100th call, so the request goes unseen
    with pytest.raises(JobCancelled):
        handed["check"]()


def test_a_cancel_requested_during_admission_stops_the_study_before_it_computes(fake, lake_world, toy, monkeypatch):
    real_admit = lake_study.admit_minute_inputs

    def admit_then_the_user_cancels(*args: Any, **kwargs: Any):
        admitted = real_admit(*args, **kwargs)
        fake.hset("job:cancel2:state", mapping={"cancel_requested": "1"})
        return admitted

    monkeypatch.setattr(lake_study, "admit_minute_inputs", admit_then_the_user_cancels)
    emit, cancel = ProgressEmitter("cancel2"), CancellationCheck("cancel2", check_every_n=100)

    with pytest.raises(JobCancelled):
        run_lake_study(toy.study(), lambda: toy.identity(), job_id="cancel2", force=False, emit=emit, cancel=cancel, now_ms=NOW_MS)

    assert toy.runs == [], "the math never started"
    assert [k for k in fake.strings if k.startswith("result:")] == []


def test_the_run_leaves_a_provider_era_entry_where_it_was(fake, lake_world, toy):
    """T09 for any study: an entry under another namespace is neither returned nor rewritten."""
    old = result_cache.store("feature_research", {"ticker": "SPY"}, {"success": True, "bars": -1})
    key = f"result:feature_research:{old}"
    seeded = fake.strings[key]

    run_job(fake, toy.study(), lambda: toy.identity(), "fresh")

    assert result_of(fake, "fresh")["bars"] != -1 and fake.strings[key] == seeded
