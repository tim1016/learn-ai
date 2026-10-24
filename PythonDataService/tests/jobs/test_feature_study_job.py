"""The Feature study job over admitted lake inputs: refusals, cache identity and saved results (#3521; T05, T07, T09).

The job runs through the real runner, emitter and result cache over the job-store fake; the lake under it is the
real pipeline (vendor HTTP, ``ensure_data``, the lake writer, admission) as in ``tests/research/test_study_inputs.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx

from app.jobs import cache as result_cache
from app.jobs.progress import ProgressEmitter
from app.jobs.runner import run_in_thread
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.lake_study import SESSION_POLICY_FOR_REQUEST
from app.routers.jobs import FeatureResearchJobRequest, start_feature_research_job
from app.services import feature_study_service
from app.services.feature_study_service import CACHE_NAMESPACE, FeatureStudyParams, run_feature_study
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import commit_bars, corrected_day
from tests._helpers.pinned_vendor import VendorBar, mock_vendor, vendor_history
from tests.jobs.conftest import _FakeRedis

WINDOW = expected_sessions(date(2024, 3, 4), date(2024, 3, 15))


class _Harness:
    def __init__(self, fake: _FakeRedis, catalog: FakeCatalog, lake: Path, tape: dict[date, list[VendorBar]]) -> None:
        self.fake, self.catalog, self.lake, self.tape = fake, catalog, lake, tape
        self.computed = 0

    def run(self, params: FeatureStudyParams, *, job_id: str, force: bool = False) -> dict[str, str]:
        """One job through the real runner: its state hash after the worker thread has finished."""

        def work(emit: ProgressEmitter, cancel: Any) -> dict | None:
            return run_feature_study(params, job_id=job_id, force=force, emit=emit, cancel=cancel, now_ms=1_735_689_600_000)

        run_in_thread(job_id, work, thread_name=f"test-{job_id}").join(timeout=120)
        return self.fake.hashes[f"job:{job_id}:state"]

    def result(self, job_id: str) -> dict:
        return json.loads(self.fake.strings[f"job:{job_id}:result"])

    def cache_keys(self) -> list[str]:
        return [key for key in self.fake.strings if key.startswith("result:")]


def params(**overrides: Any) -> FeatureStudyParams:
    base: dict[str, Any] = {
        "symbol": "SPY",
        "feature_name": "rsi_14",
        "from_date": WINDOW[0],
        "to_date": WINDOW[-1],
        "timespan": "minute",
        "multiplier": 5,
    }
    return FeatureStudyParams(**{**base, **overrides})


@pytest.fixture
def harness(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    catalog = install_fake_catalog(monkeypatch)
    lake = point_lake_writer_at_tmp(tmp_path, monkeypatch)
    h_tape = vendor_history(WINDOW, seed=11, start_price=150.0, regular_keep=0.995, extended_keep=0.92)
    harness_ = _Harness(fake, catalog, lake, h_tape)
    real = feature_study_service.run_feature_research

    def counting(*args: Any, **kwargs: Any):
        harness_.computed += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(feature_study_service, "run_feature_research", counting)
    with respx.mock:
        mock_launcher()
        mock_vendor("SPY", h_tape)
        yield harness_


def repair_day(harness: _Harness) -> None:
    """The lake repairs one session in place: the same request now reads different, committed bytes."""
    commit_bars(
        harness.catalog, harness.lake, WINDOW[3], corrected_day(WINDOW[3], harness.tape[WINDOW[3]]),
        mode="polygon_split_adjusted",
    )


def status_of(state: dict[str, str]) -> str:
    return state["status"]


# ---------------------------------------------------------------------------
# A completed study reports where its data came from
# ---------------------------------------------------------------------------


def test_a_completed_study_carries_a_lake_data_source_with_its_coverage_counts(harness):
    state = harness.run(params(), job_id="done")

    result = harness.result("done")
    source = result["data_source"]
    assert status_of(state) == "completed" and result["success"] is True
    assert source["kind"] == "lake"
    assert source["session_policy"] == "extended", "the default is the session the study has always computed on"
    assert (source["bar_minutes"], source["look_forward_minutes"], source["warmup_sessions"]) == (5, 15, 0)
    assert source["price_adjustment_mode"] == "polygon_split_adjusted"
    assert source["coverage"]["window"] | {} == {
        "expected_sessions": len(WINDOW), "reused": 0, "added": len(WINDOW), "missing": 0, "failed": 0,
        "building": 0, "corrupt": 0, "captured_before_final": 0, "unavailable_from_provider": 0,
    }
    assert source["coverage"]["warmup"]["expected_sessions"] == 0
    assert len(source["snapshot_digest"]) == 64 and len(source["identity_digest"]) == 64
    assert source["bars_used"] == result["bars_used"]
    assert source["cache"] == {"hit": False, "cached_at_ms": None}
    assert source["window_start_ms"] < source["window_end_ms"]


# ---------------------------------------------------------------------------
# T05: a failed admission refuses; it never completes with a result
# ---------------------------------------------------------------------------


def test_a_window_the_lake_cannot_admit_fails_the_job_with_a_typed_refusal_and_the_counts(harness, monkeypatch):
    missing_day = WINDOW[4]
    del harness.tape[missing_day]

    state = harness.run(params(), job_id="refused")

    refusal = harness.result("refused")
    assert status_of(state) == "failed"
    assert state["error_code"] == "data_unavailable_from_provider"
    assert missing_day.isoformat() in state["error_message"]
    assert refusal["success"] is False and refusal["status"] == "refused"
    assert refusal["refusal"]["code"] == "data_unavailable_from_provider"
    assert [(s["outcome"], s["group"]) for s in refusal["refusal"]["sessions"]] == [("unavailable_from_provider", "window")]
    window = refusal["data_source"]["coverage"]["window"]
    assert (window["expected_sessions"], window["added"], window["failed"]) == (len(WINDOW), len(WINDOW) - 1, 1)
    assert refusal["data_source"]["snapshot_digest"] is None, "nothing was admitted, so nothing is fingerprinted"
    assert harness.computed == 0, "no number is computed on a shorter series"
    assert harness.cache_keys() == [], "a refusal is never cached"


@pytest.mark.parametrize(("timespan", "multiplier"), [("hour", 1), ("day", 1), ("minute", 60), ("minute", 240)])
def test_a_resolution_the_study_always_failed_on_is_refused_before_the_lake_is_touched(harness, timespan, multiplier):
    state = harness.run(params(timespan=timespan, multiplier=multiplier), job_id="bad-resolution")

    refusal = harness.result("bad-resolution")
    assert status_of(state) == "failed" and state["error_code"] == "resolution_unsupported"
    assert "15-minute forward target" in state["error_message"]
    assert refusal["refusal"]["coverage"] is None
    assert harness.catalog.rows == {}, "a request that can never run costs the lake nothing"
    assert harness.computed == 0


# ---------------------------------------------------------------------------
# T09: cache identity
# ---------------------------------------------------------------------------


def _provider_era_key(p: FeatureStudyParams) -> dict[str, Any]:
    """The key the feature job used when the provider was its source (before #3521)."""
    return {
        "ticker": p.symbol,
        "feature_name": p.feature_name,
        "from_date": p.from_date.isoformat(),
        "to_date": p.to_date.isoformat(),
        "multiplier": p.multiplier,
        "timespan": p.timespan,
    }


def test_a_provider_era_cache_entry_is_neither_returned_nor_relabelled(harness):
    p = params()
    poisoned = {"success": True, "ticker": "SPY", "mean_ic": 99.0, "from": "the provider"}
    old_hash = result_cache.store("feature_research", _provider_era_key(p), poisoned)
    old_key = f"result:feature_research:{old_hash}"
    seeded = harness.fake.strings[old_key]

    state = harness.run(p, job_id="fresh")

    result = harness.result("fresh")
    assert status_of(state) == "completed" and state.get("cached") != "1"
    assert result["mean_ic"] != 99.0 and "from" not in result
    assert result["data_source"]["cache"]["hit"] is False
    assert harness.fake.strings[old_key] == seeded, "the old entry is left exactly as it was written"
    assert harness.computed == 1
    assert any(key.startswith(f"result:{CACHE_NAMESPACE}:") for key in harness.cache_keys())


def test_an_identical_repeat_is_served_from_the_cache_without_recomputing(harness):
    harness.run(params(), job_id="first")

    state = harness.run(params(), job_id="second")

    first, second = harness.result("first"), harness.result("second")
    assert state["cached"] == "1" and status_of(state) == "completed"
    assert harness.computed == 1
    events = harness.fake.events("second")
    assert [e["type"] for e in events].count("job.started") == 1, "a hit found by the worker does not announce a second start"
    assert events[-1]["type"] == "job.completed" and events[-1]["cached"] is True
    assert {k: v for k, v in second.items() if k != "data_source"} == {k: v for k, v in first.items() if k != "data_source"}
    assert second["data_source"]["cache"]["hit"] is True
    assert second["data_source"]["cache"]["cached_at_ms"] > 0
    # The repeat reports what admission did this time (every session reused), not the first run's counts.
    assert second["data_source"]["coverage"]["window"]["reused"] == len(WINDOW)
    assert second["data_source"]["coverage"]["window"]["added"] == 0
    assert second["data_source"]["snapshot_digest"] == first["data_source"]["snapshot_digest"]


def test_force_recomputes_even_when_the_identity_matches(harness):
    harness.run(params(), job_id="first")

    state = harness.run(params(), job_id="forced", force=True)

    assert state.get("cached") != "1" and harness.computed == 2


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"session_policy": "regular"}, id="session_policy"),
        pytest.param({"multiplier": 3}, id="bar_length"),
        pytest.param({"feature_name": "momentum_5m"}, id="feature"),
        pytest.param({"to_date": WINDOW[-2]}, id="window_end"),
    ],
)
def test_a_changed_policy_or_scientific_input_is_a_new_identity_not_a_cache_hit(harness, change):
    harness.run(params(), job_id="first")

    state = harness.run(params(**change), job_id="changed")

    assert state.get("cached") != "1"
    assert harness.computed == 2


def test_repaired_lake_data_under_the_same_request_is_a_new_identity(harness):
    harness.run(params(), job_id="before")
    repair_day(harness)

    state = harness.run(params(), job_id="after")

    before, after = harness.result("before"), harness.result("after")
    assert state.get("cached") != "1" and harness.computed == 2
    assert after["data_source"]["snapshot_digest"] != before["data_source"]["snapshot_digest"]
    assert after["data_source"]["identity_digest"] == before["data_source"]["identity_digest"], "same request, new bytes"
    assert after["data_source"]["coverage"]["window"]["reused"] == len(WINDOW)


# ---------------------------------------------------------------------------
# T07: this study has no saved or locked result of its own
# ---------------------------------------------------------------------------
#
# A job's result key is written once by the worker that computed it and nothing rewrites it, so "a saved result is
# unchanged after the lake is repaired" is true of any job and no test of it can fail. The Feature study has no saved
# or locked-study surface (``research_experiments`` is legacy and read-only). What can change is the result cache: a
# repaired lake must add a new entry and leave the old one exactly as it was.


def test_a_repaired_lake_adds_a_cache_entry_and_leaves_the_earlier_one_byte_for_byte(harness):
    harness.run(params(), job_id="saved")
    saved_source = harness.result("saved")["data_source"]
    cached_before = {key: harness.fake.strings[key] for key in harness.cache_keys()}

    repair_day(harness)
    harness.run(params(), job_id="rerun")

    assert harness.result("rerun")["data_source"]["snapshot_digest"] != saved_source["snapshot_digest"]
    # The old identity keeps its entry, byte for byte, beside the new one: nothing was relabelled or replaced.
    assert {key: harness.fake.strings[key] for key in cached_before} == cached_before
    assert len(harness.cache_keys()) == len(cached_before) + 1
    assert harness.computed == 2


# ---------------------------------------------------------------------------
# The route: wiring and the one default that must not change
# ---------------------------------------------------------------------------


def test_the_one_session_field_is_honoured_and_defaults_to_the_session_the_study_has_always_read():
    # The body the browser sends (tickerRangeToWire plus the job fields), with the session it now names explicitly.
    browser = {
        "job_id": "j", "symbol": "SPY", "from_date": "2024-03-04", "to_date": "2024-03-15",
        "timespan": "minute", "multiplier": 5, "session": "extended", "feature_name": "rsi_14", "force": False,
    }

    asked_for_extended = FeatureResearchJobRequest.model_validate(browser)
    omitted = FeatureResearchJobRequest.model_validate({k: v for k, v in browser.items() if k != "session"})
    asked_for_rth = FeatureResearchJobRequest.model_validate({**browser, "session": "rth"})

    assert asked_for_extended.session == "extended"
    assert omitted.session == "extended", "a caller that omits it keeps the sample the study has always computed on (D02)"
    assert asked_for_rth.session == "rth"
    assert [SESSION_POLICY_FOR_REQUEST[r.session] for r in (asked_for_extended, omitted, asked_for_rth)] == [
        "extended", "extended", "regular",
    ]
    with pytest.raises(ValueError, match=r"session"):
        FeatureResearchJobRequest.model_validate({**browser, "session": "regular"})
    with pytest.raises(ValueError, match=r"session_policy"):
        FeatureResearchJobRequest.model_validate({**browser, "session_policy": "regular"}), "the second field is gone"


@pytest.mark.asyncio
async def test_the_route_hands_the_worker_the_study_it_was_asked_for(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_run_in_thread(job_id: str, work: Callable, **kwargs: Any) -> None:
        captured["work"] = work

    def fake_study(study_params: FeatureStudyParams, **kwargs: Any) -> None:
        captured["params"], captured["kwargs"] = study_params, kwargs

    monkeypatch.setattr("app.routers.jobs.run_in_thread", fake_run_in_thread)
    monkeypatch.setattr("app.routers.jobs.run_feature_study", fake_study)
    request = FeatureResearchJobRequest.model_validate(
        {"jobId": "j1", "symbol": "spy", "featureName": "rsi_14", "fromDate": "2024-03-04", "toDate": "2024-03-15",
         "multiplier": 15, "force": True}
    )

    response = await start_feature_research_job(request)
    captured["work"]("emit", "cancel")

    assert response == {"job_id": "j1", "status": "queued"}, "the route no longer answers 'cached' before any work"
    assert captured["params"] == FeatureStudyParams(
        "SPY", "rsi_14", date(2024, 3, 4), date(2024, 3, 15), "minute", 15, "extended", "polygon_split_adjusted"
    )
    assert captured["kwargs"]["force"] is True and captured["kwargs"]["job_id"] == "j1"

    regular = FeatureResearchJobRequest.model_validate(
        {"jobId": "j2", "symbol": "SPY", "featureName": "rsi_14", "fromDate": "2024-03-04", "toDate": "2024-03-15",
         "session": "rth"}
    )
    await start_feature_research_job(regular)
    captured["work"]("emit", "cancel")
    assert captured["params"].session_policy == "regular", "rth is the scheduled session, honoured, not ignored"
