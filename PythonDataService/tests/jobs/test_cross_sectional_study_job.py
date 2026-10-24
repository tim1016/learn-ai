"""The cross-sectional job over admitted lake stock bars: refusals, partial universes, cache identity, saved results (#3524, #3527; T05, T07, T09, T18).

The job runs through the real runner, emitter and result cache over the job-store fake; the lake under it is the real
pipeline (vendor HTTP, ``ensure_data``, the lake writer, admission) as in ``tests/research/test_cross_sectional_inputs.py``.

The one stand-in is ``build_iv_history``, the provider-side IV build. It is real and tested against a priced option chain
in ``tests/research/options/test_iv_builder_provider_inputs.py``; here it is replaced because, on master since #3484, no
chain can pass the IV diagnostics over a window long enough to compute an IC (``contract_finder`` caches each month's
expiry bracket and ``_interpolate_iv`` raises once 30 days is no longer inside it; see ``docs/known-gaps.md``). The stand-in
derives its IV from the stock rows it is handed, so these tests also prove which bars reach the IV build, and it records
into the same ``OptionInputLedger`` the real builder does.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import respx
from pydantic import ValidationError

from app.jobs import cache as result_cache
from app.jobs.progress import ProgressEmitter
from app.jobs.runner import run_in_thread
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research import batch_runner
from app.research.options.option_input_ledger import OptionInputLedger
from app.routers.jobs import CrossSectionalJobRequest, start_cross_sectional_job
from app.services import cross_sectional_study_service as service
from app.services.cross_sectional_study_service import CACHE_NAMESPACE, CrossSectionalParams, run_cross_sectional_job
from app.services.polygon_client import PolygonClientService
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import commit_bars, corrected_day
from tests._helpers.pinned_vendor import VendorBar, mock_vendor, vendor_history
from tests.jobs.conftest import _FakeRedis

#: 37 sessions: past the 30 aligned IV rows the options pipeline needs, and no longer than that, since every test admits it.
WINDOW = expected_sessions(date(2024, 1, 2), date(2024, 2, 23))
NOW_MS = 1_735_689_600_000
SPARSE = {"regular_keep": 0.3, "extended_keep": 0.05}


class _Harness:
    def __init__(self, fake: _FakeRedis, catalog: FakeCatalog, lake: Path, tapes: dict[str, dict[date, list[VendorBar]]]) -> None:
        self.fake, self.catalog, self.lake, self.tapes = fake, catalog, lake, tapes
        self.computed = 0
        self.iv_received: dict[str, list[dict]] = {}
        self.iv_behaviour: dict[str, Callable[[OptionInputLedger], pd.DataFrame | None]] = {}

    def run(self, params: CrossSectionalParams, *, job_id: str, force: bool = False) -> dict[str, str]:
        """One job through the real runner: its state hash after the worker thread has finished."""

        def work(emit: ProgressEmitter, cancel: Any) -> dict | None:
            return run_cross_sectional_job(
                params, job_id=job_id, force=force, emit=emit, cancel=cancel, polygon_client=PolygonClientService(), now_ms=NOW_MS
            )

        run_in_thread(job_id, work, thread_name=f"test-{job_id}").join(timeout=120)
        return self.fake.hashes[f"job:{job_id}:state"]

    def result(self, job_id: str) -> dict:
        return json.loads(self.fake.strings[f"job:{job_id}:result"])

    def result_text(self, job_id: str) -> str:
        return self.fake.strings[f"job:{job_id}:result"]

    def cache_keys(self) -> list[str]:
        return [key for key in self.fake.strings if key.startswith("result:")]


def stand_in_iv(harness: _Harness):
    """The provider-side IV build, derived from the stock rows it is handed, recording like the real one."""

    def build_iv_history(*, underlying, start_date, end_date, polygon_client, stock_bars, ledger) -> pd.DataFrame:
        harness.iv_received[underlying] = stock_bars
        ledger.answered("option_expiries", f"{underlying}:2024-01", ["expiry-a", "expiry-b"])
        ledger.answered("option_contracts", f"{underlying}:expiry-a", {"atm_call": "c", "atm_put": "p"})
        ledger.answered("option_bars", f"{underlying}:c", {"bars": len(stock_bars)})
        ledger.risk_free_rate("default")
        behaviour = harness.iv_behaviour.get(underlying)
        if behaviour is not None:
            outcome = behaviour(ledger)
            if outcome is not None:
                return outcome
        close = np.array([row["close"] for row in stock_bars])
        move = np.abs(np.diff(np.log(close), prepend=np.log(close[0])))
        iv = 0.2 + 2.0 * move + 0.003 * (np.arange(len(close)) % 5)
        return pd.DataFrame(
            {
                "date": [pd.Timestamp(row["timestamp"], unit="ms", tz="UTC").strftime("%Y-%m-%d") for row in stock_bars],
                "iv_30d_atm": iv,
                "iv_30d_put": iv + 0.01,
                "iv_30d_call": iv - 0.01,
                "stock_close": close,
                "dte_low": 25,
                "dte_high": 35,
                "price_source": "vwap",
                "iv_quality": "medium",
            }
        )

    return build_iv_history


SYMBOLS = ("SPY", "QQQ")


def params(**overrides: Any) -> CrossSectionalParams:
    base: dict[str, Any] = {
        "feature_name": "iv_30d",
        "symbols": SYMBOLS,
        "from_date": WINDOW[0],
        "to_date": WINDOW[-1],
        "timespan": "day",
        "multiplier": 1,
    }
    return CrossSectionalParams(**{**base, **overrides})


@pytest.fixture
def harness(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    catalog = install_fake_catalog(monkeypatch)
    lake = point_lake_writer_at_tmp(tmp_path, monkeypatch)
    tapes = {
        symbol: vendor_history(WINDOW, seed=seed, start_price=price, **SPARSE)
        for symbol, seed, price in (("SPY", 11, 150.0), ("QQQ", 12, 300.0), ("IWM", 13, 90.0))
    }
    harness_ = _Harness(fake, catalog, lake, tapes)
    real = batch_runner.run_options_feature_research

    def counting(*args: Any, **kwargs: Any):
        harness_.computed += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(batch_runner, "run_options_feature_research", counting)
    monkeypatch.setattr(batch_runner, "build_iv_history", stand_in_iv(harness_))
    with respx.mock:
        mock_launcher()
        for symbol, tape in tapes.items():
            mock_vendor(symbol, tape)
        yield harness_


def repair_day(harness: _Harness, symbol: str = "SPY") -> None:
    """The lake repairs one session in place: the same request now reads different, committed bytes."""
    commit_bars(harness.catalog, harness.lake, WINDOW[3], corrected_day(WINDOW[3], harness.tapes[symbol][WINDOW[3]]), mode="polygon_split_adjusted")


# ---------------------------------------------------------------------------
# A completed study names every input and its source
# ---------------------------------------------------------------------------


def test_a_completed_study_names_the_lake_for_its_stock_bars_and_the_provider_for_its_option_inputs(harness):
    state = harness.run(params(), job_id="done")

    result = harness.result("done")
    source = result["data_source"]
    assert state["status"] == "completed" and result["success"] is True and result["status"] == "complete"
    assert source["kind"] == "composite", "the result as a whole is not labelled lake"
    stock = source["stock_bars"]
    assert stock["kind"] == "lake" and stock["session_policy"] == "regular" and stock["bar_resolution"] == "day"
    assert stock["price_adjustment_mode"] == "polygon_split_adjusted" and stock["aggregation"] == "minute_fold_per_et_session_no_fill"
    assert stock["coverage"]["window"] == {
        "expected_sessions": 2 * len(WINDOW), "reused": 0, "added": 2 * len(WINDOW), "missing": 0, "failed": 0,
        "building": 0, "corrupt": 0, "unavailable_from_provider": 0,
    }
    assert [(m["ticker"], m["status"], m["daily_bars"]) for m in stock["members"]] == [(s, "admitted", len(WINDOW)) for s in SYMBOLS]
    assert len(stock["universe_snapshot_digest"]) == 64 and len(stock["identity_digest"]) == 64
    options = source["option_inputs"]
    assert {name: entry["kind"] for name, entry in options["inputs"].items()} == dict.fromkeys(
        ("option_expiries", "option_contracts", "option_bars"), "provider"
    )
    assert all(entry["reproducible"] is False and len(entry["observation_digest"]) == 64 for entry in options["inputs"].values())
    assert options["risk_free_rate"]["provider"] == "fred" and options["policy"] == service.OPTION_INPUT_POLICY
    assert source["cache"] == {"hit": False, "cached_at_ms": None}


def test_the_iv_build_and_the_target_are_computed_from_the_same_admitted_rows(harness):
    """Before: the underlying was fetched twice, once inside the IV build and once for the target. Now once, from the lake."""
    harness.run(params(), job_id="done")

    assert set(harness.iv_received) == set(SYMBOLS)
    for symbol in SYMBOLS:
        rows = harness.iv_received[symbol]
        assert len(rows) == len(WINDOW) and rows[0]["timestamp"] < rows[-1]["timestamp"]
        assert all(set(r) == {"timestamp", "open", "high", "low", "close", "volume"} for r in rows)
    assert harness.computed == len(SYMBOLS)


# ---------------------------------------------------------------------------
# T05: a failed admission refuses; it never completes with a result
# ---------------------------------------------------------------------------


def test_a_universe_the_lake_cannot_admit_fails_the_job_with_a_typed_refusal_and_each_members_reason(harness):
    harness.tapes["SPY"].clear()
    harness.tapes["QQQ"].clear()

    state = harness.run(params(), job_id="refused")

    refusal = harness.result("refused")
    assert state["status"] == "failed" and state["error_code"] == "data_unavailable_from_provider"
    assert refusal["success"] is False and refusal["status"] == "refused"
    assert "any of the 2 requested members" in state["error_message"] and "SPY: " in state["error_message"]
    assert refusal["refusal"]["code"] == "data_unavailable_from_provider"
    assert [m["ticker"] for m in refusal["refusal"]["members"]] == ["SPY", "QQQ"]
    assert refusal["universe"]["requested_count"] == 2 and refusal["universe"]["evaluated_count"] == 0
    assert all(m["status"] == "excluded" for m in refusal["data_source"]["stock_bars"]["members"])
    assert harness.computed == 0, "no number is computed on an empty universe"
    assert harness.cache_keys() == [], "a refusal is never cached"


@pytest.mark.parametrize(("timespan", "multiplier"), [("minute", 1), ("hour", 1), ("day", 5)])
def test_a_resolution_other_than_daily_is_refused_before_the_lake_is_touched(harness, timespan, multiplier):
    """#3527: the resolution control did nothing. A request for minute or hour bars used to run on daily bars without a word."""
    state = harness.run(params(timespan=timespan, multiplier=multiplier), job_id="bad-resolution")

    refusal = harness.result("bad-resolution")
    assert state["status"] == "failed" and state["error_code"] == "resolution_unsupported"
    assert "daily bars only" in state["error_message"]
    assert refusal["data_source"]["stock_bars"] is None
    assert harness.catalog.rows == {}, "a request that can never run costs the lake nothing"
    assert harness.computed == 0


# ---------------------------------------------------------------------------
# T18: partial universes
# ---------------------------------------------------------------------------


def test_one_unavailable_member_is_named_with_its_reason_and_the_statistics_divide_by_the_members_that_remain(harness):
    del harness.tapes["IWM"][WINDOW[10]]

    state = harness.run(params(symbols=("SPY", "QQQ", "IWM")), job_id="partial")

    result = harness.result("partial")
    universe = result["universe"]
    assert state["status"] == "completed" and result["status"] == "partial", "a three-symbol study that evaluates two says so"
    assert (universe["requested_count"], universe["evaluated_count"], universe["denominator"]) == (3, 2, 2)
    assert universe["requested"] == ["SPY", "QQQ", "IWM"] and universe["evaluated"] == ["SPY", "QQQ"]
    (excluded,) = universe["excluded"]
    assert (excluded["ticker"], excluded["stage"], excluded["code"]) == ("IWM", "stock_bars", "data_unavailable_from_provider")
    assert WINDOW[10].isoformat() in excluded["reason"] and excluded["retryable"] is True
    assert universe["inclusion_policy"]["name"] == "valid_members_only" and universe["complete"] is False
    assert (result["tickers_tested_raw"], result["tickers_valid"]) == (3, 2)
    assert result["binomial_test"]["n_valid"] == 2, "the denominator of every statistic is the evaluated count"
    rows = {r["ticker"]: r for r in result["ticker_results"]}
    assert rows["IWM"]["validity"] == "invalid_data" and rows["IWM"]["exclusion"]["code"] == "data_unavailable_from_provider"
    assert "IWM" not in harness.iv_received, "an excluded member never reaches the IV build"
    iwm = next(m for m in result["data_source"]["stock_bars"]["members"] if m["ticker"] == "IWM")
    assert iwm["status"] == "excluded" and iwm["coverage"]["window"]["failed"] == 1


def test_a_member_whose_iv_source_did_not_answer_is_excluded_with_the_failure_named_and_the_run_is_not_cached(harness):
    def unanswered(ledger: OptionInputLedger) -> pd.DataFrame:
        ledger.failed("option_bars", "O:QQQ240315C00300000", TimeoutError("read timed out"))
        return pd.DataFrame()

    harness.iv_behaviour["QQQ"] = unanswered

    harness.run(params(), job_id="iv-down")

    result = harness.result("iv-down")
    (excluded,) = result["universe"]["excluded"]
    assert (excluded["ticker"], excluded["stage"], excluded["code"]) == ("QQQ", "iv_inputs", "iv_source_unavailable")
    assert "read timed out" in excluded["reason"] and excluded["retryable"] is True
    options = result["data_source"]["option_inputs"]
    assert options["failures_total"] == 1 and options["failures"][0]["ticker"] == "QQQ"
    assert options["inputs"]["option_bars"]["failed"] == 1
    assert result["status"] == "partial" and result["universe"]["evaluated"] == ["SPY"]
    assert harness.cache_keys() == [], "#3527: a result holding a failed provider request is not cached"


def test_a_member_built_on_unanswered_option_requests_is_counted_but_flagged_degraded(harness):
    def one_request_failed(ledger: OptionInputLedger) -> None:
        ledger.failed("option_bars", "O:QQQ240315C00300000", ConnectionError("reset by peer"))

    harness.iv_behaviour["QQQ"] = one_request_failed

    harness.run(params(), job_id="degraded")

    universe = harness.result("degraded")["universe"]
    assert universe["evaluated"] == ["SPY", "QQQ"] and universe["denominator"] == 2
    assert universe["degraded"] == [{"ticker": "QQQ", "failures_total": 1}]
    assert universe["complete"] is False and harness.result("degraded")["status"] == "partial"
    assert harness.cache_keys() == []


def test_a_no_iv_member_with_no_failed_request_is_excluded_as_a_deterministic_outcome_and_the_run_is_cached(harness):
    harness.iv_behaviour["QQQ"] = lambda ledger: pd.DataFrame()

    harness.run(params(), job_id="no-iv")

    (excluded,) = harness.result("no-iv")["universe"]["excluded"]
    assert (excluded["code"], excluded["retryable"]) == ("no_iv_derived", False)
    assert len(harness.cache_keys()) == 1, "nothing in it could answer differently, so it may be reused"


# ---------------------------------------------------------------------------
# #3527: a result with a failure in it is never served from cache
# ---------------------------------------------------------------------------


def test_a_ticker_that_errors_on_a_provider_failure_is_not_cached_and_the_next_run_recomputes(harness):
    """Before: the whole result, error row included, was cached for 24 hours and the UI could not bypass it."""
    attempts = {"n": 0}

    def flaky(ledger: OptionInputLedger) -> pd.DataFrame | None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("provider reset the connection")
        return None

    harness.iv_behaviour["QQQ"] = flaky

    harness.run(params(), job_id="first")
    second_state = harness.run(params(), job_id="second")

    first, second = harness.result("first"), harness.result("second")
    rows = {r["ticker"]: r for r in first["ticker_results"]}
    assert rows["QQQ"]["validity"] == "error" and rows["QQQ"]["exclusion"]["retryable"] is True
    assert first["status"] == "partial" and "provider reset the connection" in rows["QQQ"]["error"]
    assert second_state.get("cached") != "1", "the failure was not served back"
    assert second["status"] == "complete" and second["universe"]["evaluated"] == ["SPY", "QQQ"]
    assert len(harness.cache_keys()) == 1, "only the clean run is stored"


# ---------------------------------------------------------------------------
# T09: cache identity
# ---------------------------------------------------------------------------


def _provider_era_key(p: CrossSectionalParams) -> dict[str, Any]:
    """The key the cross-sectional job used when the provider was its stock-bar source (before #3524)."""
    return {
        "feature_name": p.feature_name,
        "tickers": list(p.symbols),
        "from_date": p.from_date.isoformat(),
        "to_date": p.to_date.isoformat(),
        "target_type": p.target_type,
    }


def test_a_provider_era_cache_entry_is_neither_returned_nor_relabelled(harness):
    p = params()
    poisoned = {"success": True, "feature_name": "iv_30d", "aggregate_ic": 99.0, "from": "the provider"}
    old_hash = result_cache.store("cross_sectional", _provider_era_key(p), poisoned)
    old_key = f"result:cross_sectional:{old_hash}"
    seeded = harness.fake.strings[old_key]

    state = harness.run(p, job_id="fresh")

    result = harness.result("fresh")
    assert state["status"] == "completed" and state.get("cached") != "1"
    assert result["aggregate_ic"] != 99.0 and "from" not in result
    assert result["data_source"]["cache"]["hit"] is False
    assert harness.fake.strings[old_key] == seeded, "the old entry is left exactly as it was written"
    assert harness.computed == len(SYMBOLS)
    new_keys = [key for key in harness.cache_keys() if key != old_key]
    assert new_keys and all(key.startswith(f"result:{CACHE_NAMESPACE}:") for key in new_keys)
    assert CACHE_NAMESPACE != "cross_sectional" and not any(key.startswith("result:cross_sectional:") and ":lake:" not in key for key in new_keys)


def test_an_identical_repeat_is_served_from_the_cache_without_recomputing(harness):
    harness.run(params(), job_id="first")
    computed_after_first = harness.computed

    state = harness.run(params(), job_id="second")

    first, second = harness.result("first"), harness.result("second")
    assert state["cached"] == "1" and state["status"] == "completed"
    assert harness.computed == computed_after_first
    events = harness.fake.events("second")
    assert [e["type"] for e in events].count("job.started") == 1, "a hit found by the worker does not announce a second start"
    assert events[-1]["type"] == "job.completed" and events[-1]["cached"] is True
    scientific = lambda r: {k: v for k, v in r.items() if k != "data_source"}  # noqa: E731
    assert scientific(second) == scientific(first)
    assert second["data_source"]["cache"]["hit"] is True and second["data_source"]["cache"]["cached_at_ms"] > 0
    # The repeat reports what admission did this time (every member session reused); the option inputs stay the ones the
    # stored run saw, because that is what its numbers were computed from.
    assert second["data_source"]["stock_bars"]["coverage"]["window"]["reused"] == 2 * len(WINDOW)
    assert second["data_source"]["stock_bars"]["coverage"]["window"]["added"] == 0
    assert second["data_source"]["stock_bars"]["universe_snapshot_digest"] == first["data_source"]["stock_bars"]["universe_snapshot_digest"]
    assert second["data_source"]["option_inputs"] == first["data_source"]["option_inputs"]


def test_force_recomputes_even_when_the_identity_matches(harness):
    harness.run(params(), job_id="first")
    computed_after_first = harness.computed

    state = harness.run(params(), job_id="forced", force=True)

    assert state.get("cached") != "1" and harness.computed == 2 * computed_after_first


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"session_policy": "extended"}, id="session_policy"),
        pytest.param({"feature_name": "log_skew"}, id="feature"),
        pytest.param({"target_type": "abs_return"}, id="target"),
        pytest.param({"symbols": ("SPY",)}, id="universe"),
    ],
)
def test_a_changed_policy_or_scientific_input_is_a_new_identity_not_a_cache_hit(harness, change):
    harness.run(params(), job_id="first")
    computed_after_first = harness.computed

    state = harness.run(params(**change), job_id="changed")

    assert state.get("cached") != "1"
    assert harness.computed > computed_after_first


def test_repaired_lake_data_under_the_same_request_is_a_new_identity(harness):
    harness.run(params(), job_id="before")
    repair_day(harness)
    computed_before = harness.computed

    state = harness.run(params(), job_id="after")

    before, after = harness.result("before")["data_source"]["stock_bars"], harness.result("after")["data_source"]["stock_bars"]
    assert state.get("cached") != "1" and harness.computed == computed_before + len(SYMBOLS)
    assert after["universe_snapshot_digest"] != before["universe_snapshot_digest"]
    assert after["identity_digest"] == before["identity_digest"], "same request, new bytes"
    changed = {m["ticker"] for m, b in zip(after["members"], before["members"], strict=True) if m["snapshot_digest"] != b["snapshot_digest"]}
    assert changed == {"SPY"}, "only the member whose bytes were repaired has a new snapshot"


# ---------------------------------------------------------------------------
# T07: a saved result reads the same after the lake is repaired
# ---------------------------------------------------------------------------


def test_a_saved_result_is_byte_for_byte_unchanged_after_the_lake_is_repaired(harness):
    harness.run(params(), job_id="saved")
    saved = harness.result_text("saved")
    saved_digest = harness.result("saved")["data_source"]["stock_bars"]["universe_snapshot_digest"]
    cached_before = {key: harness.fake.strings[key] for key in harness.cache_keys()}

    repair_day(harness)
    harness.run(params(), job_id="rerun")

    assert harness.result_text("saved") == saved, "a stored result is never rewritten from mutable lake data"
    assert harness.result("rerun")["data_source"]["stock_bars"]["universe_snapshot_digest"] != saved_digest
    assert {key: harness.fake.strings[key] for key in cached_before} == cached_before, "the old identity keeps its entry"
    assert len(harness.cache_keys()) == len(cached_before) + 1


# ---------------------------------------------------------------------------
# The route: wiring and the defaults that must not change
# ---------------------------------------------------------------------------

_BROWSER_BODY = {
    "job_id": "j", "symbols": ["SPY", "QQQ"], "from_date": "2024-01-02", "to_date": "2024-02-23",
    "timespan": "day", "multiplier": 1, "session": "rth", "feature_name": "iv_30d", "target_type": "directional",
}


def test_the_request_defaults_to_the_study_it_has_always_been_and_the_legacy_session_label_stays_inert():
    request = CrossSectionalJobRequest.model_validate(_BROWSER_BODY)

    assert request.session == "rth" and request.session_policy == "regular" and request.force is False
    bare = {k: v for k, v in _BROWSER_BODY.items() if k not in ("timespan", "multiplier", "session")}
    assert CrossSectionalJobRequest.model_validate(bare).timespan == "day", "a caller that names no resolution gets daily bars"
    camel = {"jobId": "j", "symbols": ["SPY"], "featureName": "iv_30d", "fromDate": "2024-01-02", "toDate": "2024-02-23", "sessionPolicy": "extended"}
    assert CrossSectionalJobRequest.model_validate(camel).session_policy == "extended"
    with pytest.raises(ValueError, match=r"session_policy"):
        CrossSectionalJobRequest.model_validate({**_BROWSER_BODY, "session_policy": "rth"})


@pytest.mark.parametrize("symbols", [["SPY", "spy"], ["SPY", "QQQ", "SPY"]])
def test_a_repeated_symbol_is_refused_rather_than_counted_twice(symbols):
    with pytest.raises(ValidationError, match="must not repeat"):
        CrossSectionalJobRequest.model_validate({**_BROWSER_BODY, "symbols": symbols})


@pytest.mark.asyncio
async def test_the_route_hands_the_worker_the_study_it_was_asked_for(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_run_in_thread(job_id: str, work: Callable, **kwargs: Any) -> None:
        captured["work"], captured["thread"] = work, kwargs

    def fake_study(study_params: CrossSectionalParams, **kwargs: Any) -> None:
        captured["params"], captured["kwargs"] = study_params, kwargs

    monkeypatch.setattr("app.routers.jobs.run_in_thread", fake_run_in_thread)
    monkeypatch.setattr("app.routers.jobs.run_cross_sectional_job", fake_study)
    request = CrossSectionalJobRequest.model_validate({**_BROWSER_BODY, "symbols": ["spy", "qqq"], "force": True, "job_id": "j1"})

    response = await start_cross_sectional_job(request)
    captured["work"]("emit", "cancel")

    assert response == {"job_id": "j1", "status": "queued"}, "the route no longer answers 'cached' before any work"
    assert captured["params"] == CrossSectionalParams(
        "iv_30d", ("SPY", "QQQ"), date(2024, 1, 2), date(2024, 2, 23), "day", 1, "directional", "regular", "polygon_split_adjusted"
    )
    assert captured["kwargs"]["force"] is True and captured["kwargs"]["job_id"] == "j1"
    assert captured["thread"]["cancel_check_every_n"] == 1
