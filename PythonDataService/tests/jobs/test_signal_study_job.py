"""The Signal study job over admitted lake inputs: refusals, cache identity and saved results (#3522; T05, T07, T09).

The job runs through the real runner, emitter and result cache over the job-store fake; the lake under it is the
real pipeline (vendor HTTP, ``ensure_data``, the lake writer, admission) as in ``tests/research/test_study_inputs.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import respx

from app.data_lake import run_materialization
from app.data_lake.adjustment_versions import companion_path
from app.jobs import cache as result_cache
from app.jobs.progress import ProgressEmitter
from app.jobs.runner import run_in_thread
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.lake_study import SESSION_POLICY_FOR_REQUEST
from app.research.study_records.models import StudyRecordConfig, StudyRecordResult
from app.research.study_records.recorder import keeps_job_record
from app.research.study_records.store import StudyRecordStore
from app.routers.jobs import SignalEngineJobRequest, start_signal_engine_job
from app.services import signal_study_service
from app.services.signal_study_service import CACHE_NAMESPACE, SignalStudyParams, run_signal_study
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import commit_bars, corrected_day, lake_file
from tests._helpers.pinned_vendor import VendorBar, VendorCalls, mock_vendor, vendor_history
from tests.jobs.conftest import _FakeRedis

ET = ZoneInfo("America/New_York")
NOW_MS = 1_735_689_600_000  # 2025-01-01: every session below is complete and inside the provider's history
WINDOW = expected_sessions(date(2024, 3, 4), date(2024, 3, 15))  # ten sessions across the spring-forward Sunday


class _Harness:
    def __init__(
        self, fake: _FakeRedis, catalog: FakeCatalog, lake: Path, tape: dict[date, list[VendorBar]], calls: VendorCalls
    ) -> None:
        self.fake, self.catalog, self.lake, self.tape, self.calls = fake, catalog, lake, tape, calls
        self.computed = 0

    def run(self, params: SignalStudyParams, *, job_id: str, force: bool = False, now_ms: int = NOW_MS) -> dict[str, str]:
        """One job through the real runner: its state hash after the worker thread has finished."""

        def work(emit: ProgressEmitter, cancel: Any) -> dict | None:
            return run_signal_study(params, job_id=job_id, force=force, emit=emit, cancel=cancel, now_ms=now_ms)

        run_in_thread(job_id, work, thread_name=f"test-{job_id}").join(timeout=120)
        return self.fake.hashes[f"job:{job_id}:state"]

    def run_recorded(self, params: SignalStudyParams, *, job_id: str, root: Path, force: bool = False) -> dict[str, str]:
        """One job as the route starts it: the study's work wrapped to keep a History record under ``root``."""
        request = SignalEngineJobRequest(
            job_id=job_id, symbol=params.symbol, from_date=params.from_date.isoformat(), to_date=params.to_date.isoformat(),
            timespan=params.timespan, multiplier=params.multiplier, session="extended", feature_name=params.feature_name,
            flip_sign=params.flip_sign, regime_gate_enabled=params.regime_gate_enabled, force=force,
        )

        def work(emit: ProgressEmitter, cancel: Any) -> dict | None:
            return run_signal_study(params, job_id=job_id, force=force, emit=emit, cancel=cancel, now_ms=NOW_MS)

        run_in_thread(job_id, keeps_job_record("signal-study", request, work, root=root), thread_name=f"test-{job_id}").join(
            timeout=120
        )
        return self.fake.hashes[f"job:{job_id}:state"]

    def result(self, job_id: str) -> dict:
        return json.loads(self.fake.strings[f"job:{job_id}:result"])

    def cache_keys(self) -> list[str]:
        return [key for key in self.fake.strings if key.startswith("result:")]

    def phases(self, job_id: str) -> list[str]:
        return [e["phase"] for e in self.fake.events(job_id) if e["type"] == "job.phase"]


def params(**overrides: Any) -> SignalStudyParams:
    base: dict[str, Any] = {
        "symbol": "SPY",
        "feature_name": "momentum_5m",
        "from_date": WINDOW[0],
        "to_date": WINDOW[-1],
        "timespan": "minute",
        "multiplier": 5,
    }
    return SignalStudyParams(**{**base, **overrides})


@pytest.fixture
def harness(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    catalog = install_fake_catalog(monkeypatch)
    lake = point_lake_writer_at_tmp(tmp_path, monkeypatch)
    tape = vendor_history(WINDOW, seed=11, start_price=150.0, regular_keep=0.995, extended_keep=0.92)
    calls = VendorCalls()
    harness_ = _Harness(fake, catalog, lake, tape, calls)
    real = signal_study_service.run_signal_engine

    def counting(*args: Any, **kwargs: Any):
        harness_.computed += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(signal_study_service, "run_signal_engine", counting)
    with respx.mock:
        mock_launcher()
        mock_vendor("SPY", tape, calls=calls)
        yield harness_


def repair_day(harness: _Harness) -> None:
    """The lake repairs one session in place: the same request now reads different, committed bytes."""
    commit_bars(
        harness.catalog, harness.lake, WINDOW[3], corrected_day(WINDOW[3], harness.tape[WINDOW[3]]),
        mode="polygon_split_adjusted",
    )


def status_of(state: dict[str, str]) -> str:
    return state["status"]


def window_counts(source: dict) -> tuple[int, int, int, int]:
    """(reused, added, missing, failed) of the window."""
    window = source["coverage"]["window"]
    return window["reused"], window["added"], window["missing"], window["failed"]


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
    assert (source["bar_minutes"], source["look_forward_bars"], source["warmup_sessions"]) == (5, 15, 0)
    assert source["price_adjustment_mode"] == "polygon_split_adjusted"
    assert source["coverage"]["window"] == {
        "expected_sessions": len(WINDOW), "reused": 0, "added": len(WINDOW), "missing": 0, "failed": 0,
        "building": 0, "corrupt": 0, "captured_before_final": 0, "unavailable_from_provider": 0,
    }
    assert source["coverage"]["warmup"]["expected_sessions"] == 0
    assert len(source["snapshot_digest"]) == 64 and len(source["identity_digest"]) == 64
    assert source["bars_used"] == result["bars_used"]
    assert source["cache"] == {"hit": False, "cached_at_ms": None}
    assert source["window_start_ms"] < source["window_end_ms"]
    assert source["aggregation"] == "minute_fold_et_wall_clock_no_fill"
    assert source["effective_window"] == {"start_ms": source["window_start_ms"], "end_ms": source["window_end_ms"]}
    assert harness.phases("done")[:2] == ["admitting_data", "loading_bars"], "the lake check is the first thing it does"


# ---------------------------------------------------------------------------
# T05: held, one gap, fully cold, corrupt adjustment, daily rollup; a failed admission refuses
# ---------------------------------------------------------------------------


def test_a_cold_window_adds_every_session_and_a_held_one_reuses_them_without_asking_the_vendor(harness):
    harness.run(params(), job_id="cold")
    asked_when_cold = harness.calls.count

    harness.run(params(), job_id="held", force=True)

    assert window_counts(harness.result("cold")["data_source"]) == (0, len(WINDOW), 0, 0)
    assert asked_when_cold >= len(WINDOW)
    assert window_counts(harness.result("held")["data_source"]) == (len(WINDOW), 0, 0, 0)
    assert harness.calls.count == asked_when_cold, "a held window costs the vendor nothing"


def test_one_gap_is_filled_and_counted_apart_from_the_sessions_already_held(harness):
    harness.run(params(to_date=WINDOW[1]), job_id="front")
    harness.run(params(from_date=WINDOW[3]), job_id="back")
    harness.calls.requests.clear()

    harness.run(params(), job_id="whole")

    assert window_counts(harness.result("whole")["data_source"]) == (len(WINDOW) - 1, 1, 0, 0)
    assert harness.calls.days() == [WINDOW[2]], "only the gap is fetched"


def test_an_adjusted_day_with_a_bad_adjustment_record_is_rebuilt_and_counted_as_added(harness):
    harness.run(params(), job_id="first")
    record = companion_path(lake_file(harness.lake, "polygon_split_adjusted", WINDOW[2]))
    record.write_text(record.read_text().replace('"corporate_action_version": "', '"corporate_action_version": "0'))
    harness.calls.requests.clear()

    state = harness.run(params(), job_id="repaired", force=True)

    assert status_of(state) == "completed"
    assert window_counts(harness.result("repaired")["data_source"]) == (len(WINDOW) - 1, 1, 0, 0)
    assert harness.calls.days() == [WINDOW[2]]


def test_a_daily_rollup_built_for_a_narrower_run_neither_blocks_nor_is_touched_by_the_study(harness):
    narrow = run_materialization._build_engine_run_spec(
        symbol="SPY", start=WINDOW[0], end=WINDOW[1], price_adjustment_mode="polygon_split_adjusted", requester="test"
    )
    asyncio.run(run_materialization._materialize_run_data(narrow, resolution="minute"))
    rollups = lambda: [dict(r) for r in harness.catalog.rows.values() if r["resolution"] == "daily"]  # noqa: E731
    before = rollups()
    assert len(before) == 1

    state = harness.run(params(), job_id="wide")

    assert status_of(state) == "completed"
    assert window_counts(harness.result("wide")["data_source"]) == (2, len(WINDOW) - 2, 0, 0)
    assert rollups() == before, "a minute study leaves the daily rollup exactly as it found it"


def test_a_window_the_lake_cannot_admit_fails_the_job_with_a_typed_refusal_and_the_counts(harness):
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


def test_a_window_too_short_for_the_engine_fails_loudly_and_is_never_cached(harness):
    """Admission succeeds; the engine's own minimum (500 bars) does not. That stays a failed job, not a thin result."""
    state = harness.run(params(to_date=WINDOW[1]), job_id="thin")

    assert status_of(state) == "failed" and state["error_code"] == "ValueError"
    assert "Not enough bars" in state["error_message"]
    assert harness.cache_keys() == [], "a failed run is never cached"
    assert "job:thin:result" not in harness.fake.strings


@pytest.mark.parametrize("fault", ["duplicate", "out_of_order"])
def test_vendor_bars_with_a_duplicate_or_out_of_order_timestamp_refuse_the_job_and_never_reach_the_lake(harness, fault):
    day = WINDOW[1]
    bars = harness.tape[day]
    harness.tape[day] = [*bars[:5], bars[4], *bars[5:]] if fault == "duplicate" else [*bars[:5], bars[6], bars[5], *bars[7:]]

    state = harness.run(params(), job_id="bad-tape")

    refusal = harness.result("bad-tape")
    assert status_of(state) == "failed" and state["error_code"] == "data_unavailable_from_provider"
    assert "validation_failed" in state["error_message"] and day.isoformat() in state["error_message"]
    assert [(s["outcome"], s["trading_date_ms"] > 0) for s in refusal["refusal"]["sessions"]] == [("unavailable_from_provider", True)]
    assert not lake_file(harness.lake, "polygon_split_adjusted", day).exists(), "a corrupt tape never reaches the lake"
    assert harness.computed == 0


def test_a_window_ending_on_a_session_that_has_not_finished_refuses_instead_of_computing_on_a_partial_day(harness):
    mid_session = int(datetime(2024, 3, 15, 12, 0, tzinfo=ET).timestamp() * 1000)

    state = harness.run(params(), job_id="forming", now_ms=mid_session)

    refusal = harness.result("forming")
    assert status_of(state) == "failed" and state["error_code"] == "window_not_complete"
    assert "2024-03-15" in refusal["refusal"]["reason"]
    assert harness.computed == 0 and harness.calls.count == 0, "nothing is fetched for a window that cannot complete"


# ---------------------------------------------------------------------------
# #3527: bar sizes the study can never run on are refused before the lake is touched
# ---------------------------------------------------------------------------


def _assert_refused_before_the_lake_is_touched(harness, state: dict[str, str], job_id: str) -> dict:
    refusal = harness.result(job_id)
    assert status_of(state) == "failed" and state["error_code"] == "resolution_unsupported"
    assert refusal["refusal"]["coverage"] is None
    assert refusal["data_source"] is None, "nothing was read, so there is no source to name"
    assert harness.catalog.rows == {}, "a request that can never run costs the lake nothing"
    assert harness.calls.count == 0, "and the vendor nothing"
    assert harness.computed == 0
    return refusal


@pytest.mark.parametrize(
    ("timespan", "multiplier"), [("hour", 1), ("day", 1), ("minute", 30), ("minute", 60), ("minute", 240)]
)
def test_a_bar_size_that_can_never_reach_the_validity_gate_is_refused_before_the_lake_is_touched(
    harness, timespan, multiplier
):
    state = harness.run(params(timespan=timespan, multiplier=multiplier), job_id="bad-resolution")

    _assert_refused_before_the_lake_is_touched(harness, state, "bad-resolution")
    assert "15-bar forward target" in state["error_message"] and "needs 70%" in state["error_message"]


@pytest.mark.parametrize("multiplier", [7, 29])
def test_a_bar_size_that_does_not_divide_the_hour_is_refused_before_the_lake_is_touched(harness, multiplier):
    state = harness.run(params(multiplier=multiplier), job_id="off-the-hour")

    _assert_refused_before_the_lake_is_touched(harness, state, "off-the-hour")
    assert f"{multiplier}-minute bars do not divide the hour" in state["error_message"]


@pytest.mark.parametrize(("session_policy", "multiplier"), [("regular", 15), ("extended", 20)])
def test_a_bar_size_the_sessions_of_the_policy_cannot_fill_is_refused_before_the_lake_is_touched(
    harness, session_policy, multiplier
):
    """The default 15-minute run on regular hours (26 bars a day, 42% with a target) used to be fetched, written to the
    lake and then stopped by the engine with an untyped error."""
    state = harness.run(params(multiplier=multiplier, session_policy=session_policy), job_id="thin-sessions")

    _assert_refused_before_the_lake_is_touched(harness, state, "thin-sessions")
    assert f"under {session_policy}-hours sessions" in state["error_message"]
    assert "the longest session in this window holds at most" in state["error_message"]


# ---------------------------------------------------------------------------
# The History record: a lake run is kept with the data source it read, a refused run with its refusal
# ---------------------------------------------------------------------------


def saved_records(root: Path) -> list[tuple[StudyRecordConfig, StudyRecordResult]]:
    store = StudyRecordStore("signal-study", root=root)
    return [store.load(record_id) for record_id, _ in store.config_paths()]


def test_a_completed_lake_run_is_recorded_with_the_data_source_block_and_the_options_it_was_given(harness, tmp_path):
    state = harness.run_recorded(params(flip_sign=False, regime_gate_enabled=False), job_id="kept", root=tmp_path / "records")

    result = harness.result("kept")
    [(config, stored)] = saved_records(tmp_path / "records")
    assert status_of(state) == "completed" and config.status == "completed"
    assert config.data_source == result["data_source"], "the lake block is the record's provenance, field for field"
    assert config.data_source["kind"] == "lake" and len(config.data_source["snapshot_digest"]) == 64
    assert config.headline is not None and config.headline.endswith(f"{result['bars_used']:,} bars")
    assert stored.result["feature_name"] == result["feature_name"] == "momentum_5m", "what is stored is what the job delivered"
    assert stored.result["bars_used"] == result["bars_used"]
    assert config.inputs["flip_sign"] is False and config.inputs["regime_gate_enabled"] is False
    assert config.inputs["session"] == "extended" and config.inputs["multiplier"] == 5
    assert config.job_id == "kept"


def test_a_window_the_lake_refused_is_recorded_as_a_failed_run_with_its_refusal_and_counts(harness, tmp_path):
    del harness.tape[WINDOW[4]]

    state = harness.run_recorded(params(), job_id="refused-kept", root=tmp_path / "records")

    parked = harness.result("refused-kept")
    [(config, stored)] = saved_records(tmp_path / "records")
    assert status_of(state) == "failed" and state["error_code"] == "data_unavailable_from_provider"
    assert config.status == "failed" and config.status_detail == parked["error"]
    assert config.headline is None
    assert stored.result == parked, "the record holds the refusal the page reads, as the job parked it"
    assert stored.result["refusal"]["code"] == "data_unavailable_from_provider"
    assert config.data_source == parked["data_source"]
    assert config.data_source["coverage"]["window"]["failed"] == 1 and config.data_source["snapshot_digest"] is None
    assert harness.computed == 0


def test_a_window_too_short_for_the_engine_is_recorded_as_a_failed_run_with_the_engines_reason(harness, tmp_path):
    state = harness.run_recorded(params(to_date=WINDOW[1]), job_id="thin-kept", root=tmp_path / "records")

    [(config, stored)] = saved_records(tmp_path / "records")
    assert status_of(state) == "failed" and state["error_code"] == "ValueError"
    assert config.status == "failed" and "Not enough bars" in (config.status_detail or "")
    assert stored.result is None, "the engine raised; there is no refusal payload to keep"


def test_a_repeat_served_from_the_cache_is_not_recorded_a_second_time(harness, tmp_path):
    harness.run_recorded(params(), job_id="first-kept", root=tmp_path / "records")

    state = harness.run_recorded(params(), job_id="repeat-kept", root=tmp_path / "records")

    assert state["cached"] == "1"
    assert [config.job_id for config, _ in saved_records(tmp_path / "records")] == ["first-kept"]


# ---------------------------------------------------------------------------
# T09: cache identity
# ---------------------------------------------------------------------------


def _provider_era_key(p: SignalStudyParams) -> dict[str, Any]:
    """The key the signal job used when the provider was its source (before #3522)."""
    return {
        "ticker": p.symbol,
        "feature_name": p.feature_name,
        "from_date": p.from_date.isoformat(),
        "to_date": p.to_date.isoformat(),
        "multiplier": p.multiplier,
        "timespan": p.timespan,
        "flip_sign": p.flip_sign,
        "regime_gate_enabled": p.regime_gate_enabled,
    }


def test_a_provider_era_cache_entry_is_neither_returned_nor_relabelled(harness):
    p = params()
    poisoned = {"success": True, "ticker": "SPY", "best_threshold": 99.0, "from": "the provider"}
    old_hash = result_cache.store("signal_engine", _provider_era_key(p), poisoned)
    old_key = f"result:signal_engine:{old_hash}"
    seeded = harness.fake.strings[old_key]

    state = harness.run(p, job_id="fresh")

    result = harness.result("fresh")
    assert status_of(state) == "completed" and state.get("cached") != "1"
    assert result["best_threshold"] != 99.0 and "from" not in result
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
    assert window_counts(second["data_source"]) == (len(WINDOW), 0, 0, 0)
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
        pytest.param({"feature_name": "rsi_14"}, id="feature"),
        pytest.param({"flip_sign": False}, id="flip_sign"),
        pytest.param({"regime_gate_enabled": False}, id="regime_gate"),
        pytest.param({"to_date": WINDOW[-2]}, id="window_end"),
    ],
)
def test_a_changed_policy_or_scientific_input_is_a_new_identity_not_a_cache_hit(harness, change):
    harness.run(params(), job_id="first")

    state = harness.run(params(**change), job_id="changed")

    assert status_of(state) == "completed" and state.get("cached") != "1"
    assert harness.computed == 2


def test_repaired_lake_data_under_the_same_request_is_a_new_identity(harness):
    harness.run(params(), job_id="before")
    repair_day(harness)

    state = harness.run(params(), job_id="after")

    before, after = harness.result("before"), harness.result("after")
    assert state.get("cached") != "1" and harness.computed == 2
    assert after["data_source"]["snapshot_digest"] != before["data_source"]["snapshot_digest"]
    assert after["data_source"]["identity_digest"] == before["data_source"]["identity_digest"], "same request, new bytes"
    assert window_counts(after["data_source"]) == (len(WINDOW), 0, 0, 0)


# ---------------------------------------------------------------------------
# T07: this study has no saved or locked result of its own
# ---------------------------------------------------------------------------
#
# A job's result key is written once by the worker that computed it and nothing rewrites it, so "a saved result is
# unchanged after the lake is repaired" is true of any job and no test of it can fail. The Signal study has no saved
# or locked-study surface (its history rows are legacy and read-only). What can change is the result cache: a repaired
# lake must add a new entry and leave the old one exactly as it was.


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
        "timespan": "minute", "multiplier": 15, "session": "extended", "feature_name": "momentum_5m",
        "flip_sign": True, "regime_gate_enabled": True, "force": False,
    }

    asked_for_extended = SignalEngineJobRequest.model_validate(browser)
    omitted = SignalEngineJobRequest.model_validate({k: v for k, v in browser.items() if k != "session"})
    asked_for_rth = SignalEngineJobRequest.model_validate({**browser, "session": "rth"})

    assert asked_for_extended.session == "extended"
    assert omitted.session == "extended", "a caller that omits it keeps the sample the study has always computed on (#3522)"
    assert asked_for_rth.session == "rth"
    assert [SESSION_POLICY_FOR_REQUEST[r.session] for r in (asked_for_extended, omitted, asked_for_rth)] == [
        "extended", "extended", "regular",
    ]
    with pytest.raises(ValueError, match=r"session"):
        SignalEngineJobRequest.model_validate({**browser, "session": "regular"})
    # The second field is gone: the model refuses an unknown one rather than ignoring it.
    with pytest.raises(ValueError, match=r"session_policy"):
        SignalEngineJobRequest.model_validate({**browser, "session_policy": "regular"})


@pytest.mark.asyncio
@pytest.mark.parametrize(("multiplier", "session", "said"), [
    (60, "rth", "15-bar forward target"),
    (240, "extended", "15-bar forward target"),
    (7, "extended", "do not divide the hour"),
    (15, "rth", "under regular-hours sessions"),
])
async def test_the_route_refuses_a_bar_size_the_study_can_never_run_on_without_asking_the_provider(
    fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch, multiplier: int, session: str, said: str
):
    """#3527 at the seam the browser uses: before, these runs fetched from Polygon and failed in the engine."""
    fetch = MagicMock(return_value=[])
    monkeypatch.setattr("app.routers.jobs.polygon_client.fetch_aggregates", fetch)
    job_id = f"route-{multiplier}m-{session}-0001"
    request = SignalEngineJobRequest.model_validate(
        {"job_id": job_id, "symbol": "SPY", "from_date": "2024-03-04", "to_date": "2024-03-15", "timespan": "minute",
         "multiplier": multiplier, "session": session, "feature_name": "momentum_5m"}
    )

    assert await start_signal_engine_job(request) == {"job_id": job_id, "status": "queued"}
    for worker in [t for t in threading.enumerate() if t.name == f"signal-{job_id[:8]}"]:
        worker.join(timeout=60)

    state = fake.hashes[f"job:{job_id}:state"]
    assert state["status"] == "failed" and state["error_code"] == "resolution_unsupported"
    assert said in state["error_message"]
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_the_route_hands_the_worker_the_study_it_was_asked_for(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_run_in_thread(job_id: str, work: Callable, **kwargs: Any) -> None:
        captured["work"] = work

    def fake_study(study_params: SignalStudyParams, **kwargs: Any) -> None:
        captured["params"], captured["kwargs"] = study_params, kwargs

    monkeypatch.setattr("app.routers.jobs.run_in_thread", fake_run_in_thread)
    monkeypatch.setattr("app.routers.jobs.run_signal_study", fake_study)
    request = SignalEngineJobRequest.model_validate(
        {"jobId": "j1", "symbol": "spy", "featureName": "momentum_5m", "fromDate": "2024-03-04", "toDate": "2024-03-15",
         "flipSign": False, "regimeGateEnabled": False, "force": True}
    )

    response = await start_signal_engine_job(request)
    captured["work"]("emit", "cancel")

    assert response == {"job_id": "j1", "status": "queued"}, "the route no longer answers 'cached' before any work"
    assert captured["params"] == SignalStudyParams(
        "SPY", "momentum_5m", date(2024, 3, 4), date(2024, 3, 15), "minute", 15,
        flip_sign=False, regime_gate_enabled=False, session_policy="extended",
        price_adjustment_mode="polygon_split_adjusted",
    )
    assert captured["kwargs"]["force"] is True and captured["kwargs"]["job_id"] == "j1"

    regular = SignalEngineJobRequest.model_validate(
        {"jobId": "j2", "symbol": "SPY", "featureName": "momentum_5m", "fromDate": "2024-03-04", "toDate": "2024-03-15",
         "session": "rth"}
    )
    await start_signal_engine_job(regular)
    captured["work"]("emit", "cancel")
    assert captured["params"].session_policy == "regular", "rth is the scheduled session, honoured, not ignored"
