"""The cross-sectional job over admitted lake inputs: refusals, partial universes, cache identity, saved results (#3524, #3527, #3540; T05, T07, T09, T18).

The job runs through the real runner, emitter and result cache over the job-store fake; the stock-bar lake under it is the real
pipeline (vendor HTTP, ``ensure_data``, the lake writer, admission) as in ``tests/research/test_cross_sectional_inputs.py``.

Two stand-ins, both of code tested elsewhere against the real lake and a priced option chain. ``admit_member_option_inputs``, the
saving and reading back of a member's option inputs, is tested through the lake in
``tests/research/options/test_option_inputs_lake.py`` and against the old provider path in
``tests/research/test_cross_sectional_options_lake_vs_provider.py``; here it hands the job a member's inputs (and can refuse them), so this
module's subject stays the stock bars and option inputs the job admits, caches and records. ``build_iv_history`` is real and tested in
``tests/research/options/test_iv_builder_member_inputs.py``; here it is replaced by a function that derives its IV from the spot rows the
admission was handed, so these tests also prove which bars reach the option side, and it counts the risk-free rate into the same
``RateTally`` the real builder does.

``builder_raises`` stands for an IV build that raises for a member. ``test_a_study_in_which_no_member_is_evaluated...`` pins what the
job does when it does so for every member: it fails with ``no_member_evaluated`` and each member's reason, and completes nothing.
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

from app.config import settings
from app.jobs import cache as result_cache
from app.jobs.progress import ProgressEmitter
from app.jobs.runner import run_in_thread
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc, session_open_ms_utc
from app.research import batch_runner, cross_sectional_inputs, options_runner
from app.research.options.option_inputs import (
    MemberOptionInputs,
    OptionInputCoverage,
    OptionInputIssue,
    OptionInputRefusal,
    RateTally,
)
from app.research.study_records.models import StudyRecordConfig, StudyRecordResult
from app.research.study_records.recorder import keeps_job_record
from app.research.study_records.store import StudyRecordStore
from app.routers.jobs import CrossSectionalJobRequest, start_cross_sectional_job
from app.services import cross_sectional_study_service as service
from app.services.cross_sectional_study_service import CACHE_NAMESPACE, CrossSectionalParams, run_cross_sectional_job
from app.services.fred_service import RATE_SOURCE_DEFAULT, RateResolution
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import commit_bars, corrected_day
from tests._helpers.pinned_vendor import VendorBar, mock_vendor, scaled, vendor_history
from tests.jobs.conftest import _FakeRedis

#: 37 sessions: past the 30 aligned IV rows the options pipeline needs, and no longer than that, since every test admits it.
WINDOW = expected_sessions(date(2024, 1, 2), date(2024, 2, 23))
NOW_MS = 1_735_689_600_000
SPARSE = {"regular_keep": 0.3, "extended_keep": 0.05}


class _Harness:
    def __init__(self, fake: _FakeRedis, catalog: FakeCatalog, lake: Path, tapes: dict[str, dict[date, list[VendorBar]]]) -> None:
        self.fake, self.catalog, self.lake, self.tapes = fake, catalog, lake, tapes
        self.computed = 0
        self.spot_received: dict[str, list[dict]] = {}
        self.target_received: dict[str, list[dict]] = {}
        #: What the stand-in admission hands a member: ``None`` hands its inputs over, a refusal is raised instead.
        self.admission_behaviour: dict[str, Callable[[], None]] = {}
        #: The sha256 of the listing every member's inputs were chosen from: changing it is changing the stored bytes.
        self.option_listing_sha: dict[str, str] = {}
        self.iv_behaviour: dict[str, Callable[[RateTally], pd.DataFrame | None]] = {}

    def run(self, params: CrossSectionalParams, *, job_id: str, force: bool = False, now_ms: int = NOW_MS) -> dict[str, str]:
        """One job through the real runner: its state hash after the worker thread has finished."""

        def work(emit: ProgressEmitter, cancel: Any) -> dict | None:
            return run_cross_sectional_job(params, job_id=job_id, force=force, emit=emit, cancel=cancel, now_ms=now_ms)

        run_in_thread(job_id, work, thread_name=f"test-{job_id}").join(timeout=120)
        return self.fake.hashes[f"job:{job_id}:state"]

    def run_recorded(self, params: CrossSectionalParams, *, job_id: str, root: Path, force: bool = False) -> dict[str, str]:
        """One job as the route starts it: the study's work wrapped to keep a History record under ``root``."""
        request = CrossSectionalJobRequest(
            job_id=job_id, symbols=list(params.symbols), from_date=params.from_date.isoformat(), to_date=params.to_date.isoformat(),
            timespan=params.timespan, multiplier=params.multiplier, session="rth", feature_name=params.feature_name,
            target_type=params.target_type, force=force,
        )

        def work(emit: ProgressEmitter, cancel: Any) -> dict | None:
            return run_cross_sectional_job(params, job_id=job_id, force=force, emit=emit, cancel=cancel, now_ms=NOW_MS)

        run_in_thread(job_id, keeps_job_record("cross-sectional-study", request, work, root=root), thread_name=f"test-{job_id}").join(
            timeout=120
        )
        return self.fake.hashes[f"job:{job_id}:state"]

    def result(self, job_id: str) -> dict:
        return json.loads(self.fake.strings[f"job:{job_id}:result"])

    def result_text(self, job_id: str) -> str:
        return self.fake.strings[f"job:{job_id}:result"]

    def cache_keys(self) -> list[str]:
        return [key for key in self.fake.strings if key.startswith("result:")]


def session_date(row: dict) -> str:
    return pd.Timestamp(row["timestamp"], unit="ms", tz="UTC").strftime("%Y-%m-%d")


def iv_frame(bracket: pd.DataFrame) -> pd.DataFrame:
    """An IV history a gentle function of the spot closes: it passes the IV diagnostics and aligns with the rows' dates."""
    close = bracket["stock_close"].to_numpy(dtype=float)
    move = np.abs(np.diff(np.log(close), prepend=np.log(close[0])))
    iv = 0.2 + 2.0 * move + 0.003 * (np.arange(len(close)) % 5)
    return pd.DataFrame(
        {
            "date": bracket["date"].tolist(),
            "iv_30d_atm": iv,
            "iv_30d_put": iv + 0.01,
            "iv_30d_call": iv - 0.01,
            "stock_close": close,
            "dte_low": 25,
            "dte_high": 35,
            "price_source": "close_filtered",
            "iv_quality": "medium",
        }
    )


def stand_in_admission(harness: _Harness):
    """A member's option inputs, chosen from the raw spot rows it is handed, as the real admission does (or refused, on request)."""

    def admit_member_option_inputs(ticker, *, spot_rows, start_date, end_date, now_ms, capture, check_cancelled, on_progress):
        harness.spot_received[ticker] = list(spot_rows)
        refuse = harness.admission_behaviour.get(ticker)
        if refuse is not None:
            refuse()
        bracket = pd.DataFrame({"date": [session_date(r) for r in spot_rows], "stock_close": [r["close"] for r in spot_rows]})
        sha = harness.option_listing_sha.get(ticker, "a" * 64)
        return MemberOptionInputs(
            ticker, bracket, {}, {start_date: sha}, OptionInputCoverage(listings_expected=1, listings_added=1), "test-root"
        )

    return admit_member_option_inputs


def stand_in_iv(harness: _Harness):
    """The IV build, derived from the spot closes the admission was handed, counting the rate like the real one."""

    def build_iv_history(*, underlying, inputs, rates) -> pd.DataFrame:
        for day in inputs.bracket["date"]:
            rates.record(day, RateResolution(0.043, RATE_SOURCE_DEFAULT))
        behaviour = harness.iv_behaviour.get(underlying)
        if behaviour is not None:
            outcome = behaviour(rates)
            if outcome is not None:
                return outcome
        return iv_frame(inputs.bracket)

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
    monkeypatch.setattr(settings, "FRED_API_KEY", "", raising=False)
    real = batch_runner.run_options_feature_research

    def counting(*args: Any, **kwargs: Any):
        harness_.computed += 1
        harness_.target_received[kwargs["ticker"]] = kwargs["stock_daily_bars"]
        return real(*args, **kwargs)

    monkeypatch.setattr(batch_runner, "run_options_feature_research", counting)
    monkeypatch.setattr(batch_runner, "build_iv_history", stand_in_iv(harness_))
    monkeypatch.setattr(cross_sectional_inputs, "admit_member_option_inputs", stand_in_admission(harness_))
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


def test_a_completed_study_names_the_lake_for_its_stock_bars_and_option_inputs_and_fred_for_the_rate(harness):
    state = harness.run(params(), job_id="done")

    result = harness.result("done")
    source = result["data_source"]
    assert state["status"] == "completed" and result["success"] is True and result["status"] == "complete"
    assert source["kind"] == "lake", "every input but the live rate is the lake's"
    stock = source["stock_bars"]
    assert stock["kind"] == "lake" and stock["session_policy"] == "regular" and stock["bar_resolution"] == "day"
    assert stock["price_adjustment_mode"] == "polygon_split_adjusted" and stock["aggregation"] == "minute_fold_per_et_session_no_fill"
    assert stock["coverage"]["window"] == {
        "expected_sessions": 2 * len(WINDOW), "reused": 0, "added": 2 * len(WINDOW), "missing": 0, "failed": 0,
        "building": 0, "corrupt": 0, "captured_before_final": 0, "unavailable_from_provider": 0,
    }
    assert [(m["ticker"], m["status"], m["daily_bars"]) for m in stock["members"]] == [(s, "admitted", len(WINDOW)) for s in SYMBOLS]
    assert len(stock["snapshot_digest"]) == 64 and len(stock["raw_snapshot_digest"]) == 64 and len(stock["identity_digest"]) == 64
    assert stock["bar_minutes"] is None and stock["symbol"] == "SPY, QQQ" and stock["calculation_version"] == service.CROSS_SECTIONAL_CALCULATION_VERSION
    assert "option_inputs" not in stock, "the option inputs' policy is the option block's"
    options = source["option_inputs"]
    assert options["kind"] == "lake" and options["policy"] == service.OPTION_INPUT_POLICY
    assert options["price_rule"] == "last_regular_hours_trade" and options["spot"] == "raw_regular_close" and options["contracts"] == "standard_only"
    assert [(m["ticker"], m["status"], m["source"]) for m in options["members"]] == [(s, "admitted", "kept") for s in SYMBOLS]
    assert len(options["digest"]) == 64 and options["listings"] == {"expected": 2, "reused": 0, "added": 2}
    rate = source["risk_free_rate"]
    assert rate["kind"] == "provider" and rate["provider"] == "fred" and rate["days_from_default"] == 2 * len(WINDOW) and len(rate["values_digest"]) == 64
    assert stock["cache"] == {"hit": False, "cached_at_ms": None} and "cache" not in source
    assert "reproducible" not in harness.result_text("done") and "composite" not in harness.result_text("done")


def regular_closes(tape: dict[date, list[VendorBar]]) -> list[float]:
    """The close of each session's last regular minute, worked from the vendor's tape (no lake code)."""
    return [
        [bar for bar in tape[day] if session_open_ms_utc(day) <= bar["t"] < session_close_ms_utc(day)][-1]["c"] for day in WINDOW
    ]


def test_the_option_side_is_handed_the_raw_closes_and_the_target_is_measured_on_the_adjusted_ones(harness):
    """Before #3540 one admitted set of rows fed both. The IV spot and the strike picks are raw (ADR 0078 decision 6)."""
    respx.mock.reset()
    mock_launcher()
    for symbol, tape in harness.tapes.items():
        mock_vendor(symbol, tape, adjusted=scaled(tape, 0.5))

    harness.run(params(), job_id="done")

    assert harness.result("done")["status"] == "complete"
    for symbol in SYMBOLS:
        spot, target = harness.spot_received[symbol], harness.target_received[symbol]
        assert len(spot) == len(target) == len(WINDOW) and spot[0]["timestamp"] < spot[-1]["timestamp"]
        assert all(set(row) == {"timestamp", "open", "high", "low", "close", "volume"} for row in spot)
        assert [row["close"] for row in spot] == regular_closes(harness.tapes[symbol]), "the spot: the raw tape's last regular minute"
        assert [row["close"] for row in target] == regular_closes(scaled(harness.tapes[symbol], 0.5)), "the target: the adjusted tape's"
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
    assert "IWM" not in harness.spot_received, "an excluded member never reaches the option admission"
    iwm = next(m for m in result["data_source"]["stock_bars"]["members"] if m["ticker"] == "IWM")
    assert iwm["status"] == "excluded" and iwm["coverage"]["window"]["failed"] == 1


def an_option_refusal(*, failure_reason: str, outcome: str, retryable: bool) -> Callable[[], None]:
    """A member whose option inputs the lake could not supply: what the real admission raises, with one issue behind it."""
    issue = OptionInputIssue(
        "contract_day", WINDOW[5], "O:QQQ240315C00300000", outcome, "O:QQQ240315C00300000: the data vendor asked us to slow down",  # type: ignore[arg-type]
        failure_reason, retryable,
    )

    def refuse() -> None:
        raise OptionInputRefusal([issue], expected=1)

    return refuse


def test_a_member_whose_option_inputs_the_lake_could_not_supply_is_excluded_at_its_own_stage_with_the_issue_named_and_the_run_is_not_cached(harness):
    harness.admission_behaviour["QQQ"] = an_option_refusal(failure_reason="provider_rate_limited", outcome="unavailable_from_provider", retryable=True)

    harness.run(params(), job_id="options-down")

    result = harness.result("options-down")
    (excluded,) = result["universe"]["excluded"]
    assert (excluded["ticker"], excluded["stage"], excluded["code"]) == ("QQQ", "option_inputs", "data_unavailable_from_provider")
    assert "O:QQQ240315C00300000" in excluded["reason"] and excluded["retryable"] is True
    assert excluded["refusal"]["issues_total"] == 1 and excluded["refusal"]["issues"][0]["failure_reason"] == "provider_rate_limited"
    options = result["data_source"]["option_inputs"]
    assert [(m["ticker"], m["status"]) for m in options["members"]] == [("SPY", "admitted"), ("QQQ", "excluded")]
    qqq = options["members"][1]
    assert (qqq["code"], qqq["retryable"], qqq["issues_total"]) == ("data_unavailable_from_provider", True, 1)
    assert result["status"] == "partial" and result["universe"]["evaluated"] == ["SPY"]
    assert "QQQ" not in harness.target_received, "a member whose option inputs were refused is not computed on its stock bars alone"
    assert harness.cache_keys() == [], "#3527: a result with a retryable exclusion is not cached"
    stock = result["data_source"]["stock_bars"]["members"]
    assert [(m["ticker"], m["status"]) for m in stock] == [("SPY", "admitted"), ("QQQ", "admitted")], "its stock bars were admitted"


def test_a_member_whose_option_inputs_can_never_be_supplied_is_excluded_as_a_deterministic_outcome_and_the_run_is_cached(harness):
    harness.admission_behaviour["QQQ"] = an_option_refusal(failure_reason="outside_data_plan", outcome="unavailable_from_provider", retryable=False)

    harness.run(params(), job_id="outside-plan")

    (excluded,) = harness.result("outside-plan")["universe"]["excluded"]
    assert (excluded["stage"], excluded["retryable"]) == ("option_inputs", False)
    assert len(harness.cache_keys()) == 1, "asking again cannot change it, so the result may be reused"


def test_a_universe_whose_every_member_lost_its_option_inputs_is_refused_with_the_reasons_and_computes_nothing(harness):
    for ticker in SYMBOLS:
        harness.admission_behaviour[ticker] = an_option_refusal(failure_reason="lease_timeout", outcome="building", retryable=True)

    state = harness.run(params(), job_id="no-options")

    refusal = harness.result("no-options")
    assert state["status"] == "failed" and refusal["status"] == "refused"
    assert "stock bars or option inputs for any of the 2 requested members" in state["error_message"]
    assert [m["ticker"] for m in refusal["refusal"]["members"]] == ["SPY", "QQQ"]
    assert {m["stage"] for m in refusal["refusal"]["members"]} == {"option_inputs"}
    assert harness.computed == 0 and harness.cache_keys() == []


def test_a_member_priced_at_the_default_rate_because_fred_did_not_answer_is_counted_but_flagged_degraded(harness):
    def fred_down(rates: RateTally) -> None:
        rates.record(WINDOW[0].isoformat(), RateResolution(0.043, RATE_SOURCE_DEFAULT, fetch_failed=True))

    harness.iv_behaviour["QQQ"] = fred_down

    harness.run(params(), job_id="degraded")

    universe = harness.result("degraded")["universe"]
    assert universe["evaluated"] == ["SPY", "QQQ"] and universe["denominator"] == 2
    assert universe["degraded"] == [{"ticker": "QQQ", "rate_days_fetch_failed": 1}]
    assert universe["complete"] is False and harness.result("degraded")["status"] == "partial"
    assert harness.cache_keys() == []


def test_a_no_iv_member_with_no_failed_request_is_excluded_as_a_deterministic_outcome_and_the_run_is_cached(harness):
    harness.iv_behaviour["QQQ"] = lambda rates: pd.DataFrame()

    harness.run(params(), job_id="no-iv")

    (excluded,) = harness.result("no-iv")["universe"]["excluded"]
    assert (excluded["code"], excluded["retryable"]) == ("no_iv_derived", False)
    assert len(harness.cache_keys()) == 1, "nothing in it could answer differently, so it may be reused"


# ---------------------------------------------------------------------------
# The History record: a lake run is kept with the sources it read, a refused or empty run with its reason
# ---------------------------------------------------------------------------


def saved_records(root: Path) -> list[tuple[StudyRecordConfig, StudyRecordResult]]:
    store = StudyRecordStore("cross-sectional-study", root=root)
    return [store.load(record_id) for record_id, _ in store.config_paths()]


def test_a_completed_run_is_recorded_with_the_lake_data_source_block_it_returned(harness, tmp_path):
    state = harness.run_recorded(params(), job_id="kept", root=tmp_path / "records")

    result = harness.result("kept")
    [(config, stored)] = saved_records(tmp_path / "records")
    assert state["status"] == "completed" and config.status == "completed"
    assert config.data_source == result["data_source"], "the block is the record's provenance, field for field"
    assert config.data_source["kind"] == "lake" and config.data_source["option_inputs"]["kind"] == "lake"
    assert config.data_source["stock_bars"]["kind"] == "lake" and config.data_source["risk_free_rate"]["kind"] == "provider"
    assert config.headline is not None and config.headline.startswith("Aggregate IC ")
    assert stored.result["tickers_valid"] == result["tickers_valid"] == 2, "what is stored is what the job delivered"
    assert config.inputs["symbols"] == list(SYMBOLS) and config.inputs["session"] == "rth" and config.inputs["timespan"] == "day"
    assert config.job_id == "kept"


def test_a_universe_with_a_member_the_lake_could_not_supply_is_recorded_as_partial_with_the_member_named(harness, tmp_path):
    del harness.tapes["IWM"][WINDOW[10]]

    harness.run_recorded(params(symbols=("SPY", "QQQ", "IWM")), job_id="partial-kept", root=tmp_path / "records")

    [(config, stored)] = saved_records(tmp_path / "records")
    assert config.status == "partial" and config.status_detail == "2 of 3 members evaluated: 1 had no data"
    assert stored.result["universe"]["excluded"][0]["ticker"] == "IWM"


def test_a_universe_the_lake_refused_is_recorded_as_a_failed_run_with_its_refusal_and_members(harness, tmp_path):
    harness.tapes["SPY"].clear()
    harness.tapes["QQQ"].clear()

    state = harness.run_recorded(params(), job_id="refused-kept", root=tmp_path / "records")

    parked = harness.result("refused-kept")
    [(config, stored)] = saved_records(tmp_path / "records")
    assert state["status"] == "failed" and state["error_code"] == "data_unavailable_from_provider"
    assert config.status == "failed" and config.status_detail == state["error_message"]
    assert config.headline is None
    assert stored.result == parked, "the record holds the refusal the page reads, as the job parked it"
    assert [m["ticker"] for m in stored.result["refusal"]["members"]] == ["SPY", "QQQ"]
    assert config.data_source == parked["data_source"]


def test_a_study_in_which_no_member_is_evaluated_is_recorded_as_a_failed_run_with_its_universe(harness, tmp_path):
    harness.iv_behaviour["SPY"] = harness.iv_behaviour["QQQ"] = builder_raises

    state = harness.run_recorded(params(), job_id="none-kept", root=tmp_path / "records")

    parked = harness.result("none-kept")
    [(config, stored)] = saved_records(tmp_path / "records")
    assert state["error_code"] == "no_member_evaluated"
    assert config.status == "failed" and stored.result == parked
    assert stored.result["status"] == "not_evaluated" and stored.result["universe"]["evaluated_count"] == 0


def test_a_repeat_served_from_the_cache_is_not_recorded_a_second_time(harness, tmp_path):
    harness.run_recorded(params(), job_id="first-kept", root=tmp_path / "records")

    state = harness.run_recorded(params(), job_id="repeat-kept", root=tmp_path / "records")

    assert state["cached"] == "1"
    assert [config.job_id for config, _ in saved_records(tmp_path / "records")] == ["first-kept"]


# ---------------------------------------------------------------------------
# A study in which no member is evaluated completes nothing (#3524)
# ---------------------------------------------------------------------------


def builder_raises(rates: RateTally) -> pd.DataFrame:
    raise ValueError("the IV build raised for this member")


def test_a_study_in_which_no_member_is_evaluated_fails_with_a_typed_code_and_each_members_reason(harness):
    """Every member's IV build raises: nothing is evaluated, so the job fails instead of completing over zero members."""
    harness.iv_behaviour["SPY"] = harness.iv_behaviour["QQQ"] = builder_raises

    state = harness.run(params(), job_id="none")

    payload = harness.result("none")
    assert state["status"] == "failed" and state["error_code"] == "no_member_evaluated"
    message = state["error_message"]
    assert "None of the 2 requested members" in message and "SPY: " in message and "QQQ: " in message
    assert "the IV build raised for this member" in message
    assert payload["success"] is False and payload["status"] == "not_evaluated" and "refusal" not in payload
    universe = payload["universe"]
    assert (universe["requested_count"], universe["evaluated_count"], universe["excluded_count"], universe["complete"]) == (2, 0, 2, False)
    assert [(e["ticker"], e["stage"], e["code"], e["retryable"]) for e in universe["excluded"]] == [
        ("SPY", "iv_inputs", "iv_build_failed", True),
        ("QQQ", "iv_inputs", "iv_build_failed", True),
    ]
    assert payload["data_source"]["stock_bars"]["coverage"]["window"]["added"] == 2 * len(WINDOW), "the lake did admit the bars"
    assert payload["data_source"]["option_inputs"] is not None, "and the option inputs the run did admit are still named"
    assert harness.computed == 0 and harness.cache_keys() == []
    assert harness.fake.events("none")[-1]["type"] == "job.failed"


def test_members_left_out_at_admission_and_at_the_iv_build_together_still_leave_nothing_to_report(harness):
    harness.tapes["SPY"].clear()
    harness.iv_behaviour["QQQ"] = builder_raises

    state = harness.run(params(), job_id="mixed")

    excluded = harness.result("mixed")["universe"]["excluded"]
    assert state["error_code"] == "no_member_evaluated"
    assert [(e["ticker"], e["stage"]) for e in excluded] == [("SPY", "stock_bars"), ("QQQ", "iv_inputs")]


# ---------------------------------------------------------------------------
# A FRED outage is a degraded input, not a complete result (#3524)
# ---------------------------------------------------------------------------


def test_a_member_priced_at_the_default_rate_because_fred_did_not_answer_is_degraded_and_the_run_is_not_cached(harness):
    def fred_down(rates: RateTally) -> None:
        rates.record(WINDOW[0].isoformat(), RateResolution(0.043, RATE_SOURCE_DEFAULT, fetch_failed=True))

    harness.iv_behaviour["QQQ"] = fred_down

    harness.run(params(), job_id="fred-down")

    result = harness.result("fred-down")
    assert result["universe"]["evaluated"] == ["SPY", "QQQ"], "the member stays in the statistics"
    assert result["universe"]["degraded"] == [{"ticker": "QQQ", "rate_days_fetch_failed": 1}]
    assert result["status"] == "partial" and result["universe"]["complete"] is False
    assert result["data_source"]["risk_free_rate"]["days_fetch_failed"] == 1
    assert harness.cache_keys() == [], "IVs solved at a rate FRED did not give could answer differently next time"


def test_a_default_rate_with_no_fred_key_is_a_stable_answer_and_the_cache_key_says_no_key_was_configured(harness, monkeypatch):
    """The stand-in records the default for every day without a failed fetch, which is what a deployment with no key does."""
    harness.run(params(), job_id="no-key")
    computed_without_key = harness.computed
    assert harness.result("no-key")["status"] == "complete" and len(harness.cache_keys()) == 1

    monkeypatch.setattr(settings, "FRED_API_KEY", "a-configured-key", raising=False)
    state = harness.run(params(), job_id="keyed")

    assert state.get("cached") != "1" and harness.computed == 2 * computed_without_key, "a configured key is a different study"
    assert len(harness.cache_keys()) == 2


# ---------------------------------------------------------------------------
# The retry flag comes from a typed failure, never from the error text (#3524)
# ---------------------------------------------------------------------------


def failing_iv_gate(*, fred_down: bool = False):
    """An IV history 40 percent of whose days are missing: it fails the diagnostics gate for want of data."""

    def behaviour(rates: RateTally) -> pd.DataFrame:
        if fred_down:
            rates.record(WINDOW[0].isoformat(), RateResolution(0.043, RATE_SOURCE_DEFAULT, fetch_failed=True))
        frame = iv_frame(pd.DataFrame({"date": [d.isoformat() for d in WINDOW], "stock_close": np.linspace(100.0, 110.0, len(WINDOW))}))
        frame.loc[np.arange(len(frame)) % 5 < 2, "iv_30d_atm"] = np.nan
        return frame

    return behaviour


def test_an_iv_history_that_fails_its_diagnostics_is_a_typed_deterministic_exclusion_and_the_run_is_cached(harness):
    harness.iv_behaviour["QQQ"] = failing_iv_gate()

    harness.run(params(), job_id="gate")

    result = harness.result("gate")
    row = {r["ticker"]: r for r in result["ticker_results"]}["QQQ"]
    assert row["validity"] == "invalid_iv"
    assert (row["exclusion"]["stage"], row["exclusion"]["code"], row["exclusion"]["retryable"]) == ("iv_diagnostics", "iv_failed_diagnostics", False)
    assert result["status"] == "partial" and len(harness.cache_keys()) == 1, "nothing in it could answer differently"


def test_an_iv_history_that_fails_its_diagnostics_at_a_rate_fred_did_not_give_may_pass_next_time_and_is_not_cached(harness):
    harness.iv_behaviour["QQQ"] = failing_iv_gate(fred_down=True)

    harness.run(params(), job_id="gate-down")

    row = {r["ticker"]: r for r in harness.result("gate-down")["ticker_results"]}["QQQ"]
    assert (row["exclusion"]["code"], row["exclusion"]["retryable"]) == ("iv_failed_diagnostics", True)
    assert harness.cache_keys() == []


def test_a_computation_failure_whose_text_happens_to_contain_iv_is_not_an_iv_problem(harness, monkeypatch):
    """Before: ``"iv" in error`` made "division by zero" an IV problem, hence deterministic, hence cached."""
    real_ic = options_runner.compute_information_coefficient
    calls = {"n": 0}

    def second_member_divides_by_zero(*args: Any, **kwargs: Any):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ZeroDivisionError("division by zero")
        return real_ic(*args, **kwargs)

    monkeypatch.setattr(options_runner, "compute_information_coefficient", second_member_divides_by_zero)

    harness.run(params(), job_id="division")

    result = harness.result("division")
    row = {r["ticker"]: r for r in result["ticker_results"]}["QQQ"]
    assert row["validity"] == "error" and "division by zero" in row["error"]
    assert (row["exclusion"]["stage"], row["exclusion"]["code"], row["exclusion"]["retryable"]) == ("computation", "computation_error", True)
    assert harness.cache_keys() == [], "a failure of unknown cause is not evidence either way"


# ---------------------------------------------------------------------------
# One start, phases in order, and a unit on every count (#3524)
# ---------------------------------------------------------------------------


def test_the_run_announces_each_phase_once_in_order_and_does_not_say_it_builds_an_iv_history_for_an_excluded_member(harness):
    harness.tapes["IWM"].clear()

    harness.run(params(symbols=("SPY", "QQQ", "IWM")), job_id="phases")

    events = harness.fake.events("phases")
    phases = [e["phase"] for e in events if e["type"] == "job.phase"]
    assert phases == [
        "starting", "admitting_data", "admitting_options", "ticker_1_SPY", "ticker_2_QQQ", "ticker_3_IWM", "aggregating", "completed"
    ]
    logs = [e["message"] for e in events if e["type"] == "job.log"]
    assert any("SPY: building IV history" in m for m in logs) and not any("IWM: building IV history" in m for m in logs)
    assert any("IWM: excluded (data_unavailable_from_provider)" in m for m in logs)
    assert any(m.startswith("Lake coverage:") for m in logs) and any(m == "2 of 3 members admitted" for m in logs)
    assert any(m == "2 of 2 members' option inputs admitted" for m in logs) and any(m.startswith("Option inputs: ") for m in logs)
    units = {e["unit"] for e in events if e["type"] == "job.progress"}
    assert units and units <= {"members admitted", "members saved", "tickers"}, "each stage counts what it does"


# ---------------------------------------------------------------------------
# #3527: a result with a failure in it is never served from cache
# ---------------------------------------------------------------------------


def test_a_ticker_that_errors_on_a_provider_failure_is_not_cached_and_the_next_run_recomputes(harness):
    """Before: the whole result, error row included, was cached for 24 hours and the UI could not bypass it."""
    attempts = {"n": 0}

    def flaky(rates: RateTally) -> pd.DataFrame | None:
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
    assert result["data_source"]["stock_bars"]["cache"]["hit"] is False
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
    assert second["data_source"]["stock_bars"]["cache"]["hit"] is True and second["data_source"]["stock_bars"]["cache"]["cached_at_ms"] > 0
    # The repeat reports what admission did this time (every member session reused); the risk-free rate stays the one the
    # stored run used, because that is what its numbers were computed from (FRED is live and is not in the key).
    assert second["data_source"]["stock_bars"]["coverage"]["window"]["reused"] == 2 * len(WINDOW)
    assert second["data_source"]["stock_bars"]["coverage"]["window"]["added"] == 0
    assert second["data_source"]["stock_bars"]["snapshot_digest"] == first["data_source"]["stock_bars"]["snapshot_digest"]
    assert second["data_source"]["option_inputs"]["digest"] == first["data_source"]["option_inputs"]["digest"]
    assert second["data_source"]["risk_free_rate"] == first["data_source"]["risk_free_rate"]


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
    assert after["snapshot_digest"] != before["snapshot_digest"]
    assert after["identity_digest"] == before["identity_digest"], "same request, new bytes"
    changed = {m["ticker"] for m, b in zip(after["members"], before["members"], strict=True) if m["snapshot_digest"] != b["snapshot_digest"]}
    assert changed == {"SPY"}, "only the member whose bytes were repaired has a new snapshot"


def test_changed_option_inputs_under_the_same_request_are_a_new_identity_and_the_same_ones_again_are_a_hit(harness):
    harness.run(params(), job_id="before")
    computed_before = harness.computed
    harness.option_listing_sha["QQQ"] = "b" * 64

    changed = harness.run(params(), job_id="changed")
    again = harness.run(params(), job_id="again")

    before, after = (harness.result(job)["data_source"]["option_inputs"] for job in ("before", "changed"))
    assert changed.get("cached") != "1" and harness.computed == computed_before + len(SYMBOLS), "the stored files moved, so it is recomputed"
    assert after["digest"] != before["digest"]
    digests = {m["ticker"]: m["digest"] for m in before["members"]}
    assert {m["ticker"] for m in after["members"] if m["digest"] != digests[m["ticker"]]} == {"QQQ"}, "only the member whose files moved"
    assert again["cached"] == "1" and harness.computed == computed_before + len(SYMBOLS), "and the same files again hit"


def test_a_lake_era_cache_entry_from_before_the_option_inputs_moved_to_the_lake_is_neither_returned_nor_relabelled(harness):
    p = params()
    poisoned = {"success": True, "feature_name": "iv_30d", "aggregate_ic": 99.0, "from": "the vendor's live option inputs"}
    old_hash = result_cache.store("cross_sectional:lake:v1", {"feature_name": "iv_30d"}, poisoned)
    old_key = f"result:cross_sectional:lake:v1:{old_hash}"
    seeded = harness.fake.strings[old_key]

    state = harness.run(p, job_id="fresh")

    result = harness.result("fresh")
    assert state["status"] == "completed" and state.get("cached") != "1" and result["aggregate_ic"] != 99.0 and "from" not in result
    assert harness.fake.strings[old_key] == seeded
    assert CACHE_NAMESPACE == "cross_sectional:lake:v2" and service.CROSS_SECTIONAL_CALCULATION_VERSION == "cross-sectional-study/2"


# ---------------------------------------------------------------------------
# A window the lake may not take the option data of yet is refused before the lake is touched
# ---------------------------------------------------------------------------


def test_a_window_whose_last_session_is_not_over_in_new_york_is_refused_before_anything_is_read_or_saved(harness):
    last_close = session_close_ms_utc(WINDOW[-1])

    state = harness.run(params(), job_id="too-soon", now_ms=last_close + 3_600_000)

    refusal = harness.result("too-soon")
    assert state["status"] == "failed" and state["error_code"] == "window_not_complete"
    assert WINDOW[-1].isoformat() in state["error_message"] and "midnight" in state["error_message"]
    assert refusal["status"] == "refused" and refusal["data_source"]["stock_bars"] is None and refusal["data_source"]["option_inputs"] is None
    assert harness.catalog.rows == {} and harness.computed == 0 and harness.cache_keys() == [], "the lake was not touched"
    assert harness.spot_received == {}


# ---------------------------------------------------------------------------
# T07: a saved result reads the same after the lake is repaired
# ---------------------------------------------------------------------------


def test_a_saved_result_is_byte_for_byte_unchanged_after_the_lake_is_repaired(harness):
    harness.run(params(), job_id="saved")
    saved = harness.result_text("saved")
    saved_digest = harness.result("saved")["data_source"]["stock_bars"]["snapshot_digest"]
    cached_before = {key: harness.fake.strings[key] for key in harness.cache_keys()}

    repair_day(harness)
    harness.run(params(), job_id="rerun")

    assert harness.result_text("saved") == saved, "a stored result is never rewritten from mutable lake data"
    assert harness.result("rerun")["data_source"]["stock_bars"]["snapshot_digest"] != saved_digest
    assert {key: harness.fake.strings[key] for key in cached_before} == cached_before, "the old identity keeps its entry"
    assert len(harness.cache_keys()) == len(cached_before) + 1


# ---------------------------------------------------------------------------
# The route: wiring and the defaults that must not change
# ---------------------------------------------------------------------------

_BROWSER_BODY = {
    "job_id": "j", "symbols": ["SPY", "QQQ"], "from_date": "2024-01-02", "to_date": "2024-02-23",
    "timespan": "day", "multiplier": 1, "session": "rth", "feature_name": "iv_30d", "target_type": "directional",
}


def test_the_request_defaults_to_the_study_it_has_always_been():
    request = CrossSectionalJobRequest.model_validate(_BROWSER_BODY)

    assert request.session == "rth" and request.force is False
    bare = {k: v for k, v in _BROWSER_BODY.items() if k not in ("timespan", "multiplier", "session")}
    assert CrossSectionalJobRequest.model_validate(bare).timespan == "day", "a caller that names no resolution gets daily bars"
    assert CrossSectionalJobRequest.model_validate(bare).session == "rth", "and the scheduled session, as the browser has always sent"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(("session", "policy"), [("rth", "regular"), ("extended", "extended")])
async def test_the_one_session_field_chooses_the_session_the_daily_bars_fold(monkeypatch: pytest.MonkeyPatch, session, policy):
    """#3524: ``extended`` used to be accepted by the route and silently computed on the regular session."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr("app.routers.jobs.run_in_thread", lambda job_id, work, **kwargs: captured.update(work=work))
    monkeypatch.setattr("app.routers.jobs.run_cross_sectional_job", lambda study_params, **kwargs: captured.update(params=study_params))

    await start_cross_sectional_job(CrossSectionalJobRequest.model_validate({**_BROWSER_BODY, "session": session}))
    captured["work"]("emit", "cancel")

    assert captured["params"].session_policy == policy
