"""Signal study (Research Lab → Signals → Engine): computes from admitted lake inputs (#3522).

The study's input contract. What it read before #3522 (the data-source census, #3491),
and what it reads now, field by field:

* **Bars.** Before: one Polygon ``fetch_aggregates`` call, trade aggregates for
  one stock symbol, in the size the request named. Now: the lake's one-minute
  trade bars, admitted through :mod:`app.research.study_inputs` (the existing
  materializer and committed-byte admission; the UI is never a writer), then
  folded to the requested size by ``fold_minute_bars`` — never the chart's
  resampler, which fills missing minutes forward.
* **Resolution.** Minute bars whose length divides the hour *and* can reach the
  engine's validity gate for the session the request reads. Anything else is
  refused up front with a reason, before the lake is touched (#3527).

  *The hour.* The fold floors the New York wall clock in whole bar lengths
  counted from 1970, so a length that does not divide the hour starts its
  buckets at a different minute on different sessions: 7-minute bars opened at
  04:00, 03:59 and 03:54 on three consecutive sessions, 29-minute bars at 03:56,
  03:57 and 03:38. Such bars answer to no clock a reader can check, and the
  provider path (which got the vendor's own aggregates) is no reference for
  them. The 19 such lengths up to 29 minutes are refused rather than measured.

  *The gate.* The target looks ``SignalConfig.horizon`` = 15 *bars* ahead, valid
  only when the forward bar falls on the same New York date, and the engine
  refuses a series with fewer than 70% valid targets. A session holds at most
  ``n`` bars and its last 15 have no forward bar in it, so at most ``1 - 15 / n``
  of them can have a target. ``n`` is the longest session the window holds under
  the policy: the calendar's own open and close for ``regular`` (an early close
  holds fewer), the vendor's whole tape for ``extended`` (``PRE_MARKET_HEAD_MS``
  before the open to ``AFTER_HOURS_TAIL_MS`` after the close). A length that
  cannot reach 70% there failed the run every time; hour bars, daily bars and
  minute bars past 29 can never reach it in any session. For an ordinary window
  that leaves 1, 2, 3, 4, 5 and 6 minutes under ``regular`` and those plus 10,
  12 and 15 under ``extended``; the page offers exactly these, read from
  ``contracts/signal-study-bar-minutes.json``, which
  ``tests/research/test_signal_study.py`` pins to the refusals below. A window
  of early closes alone is shorter, and the refusal names what it can use.
* **Adjustment.** Before: Polygon ``adjusted=True`` (split-only), never set
  explicitly. Now: the lake's ``polygon_split_adjusted`` mode, named in the
  identity. Prices sit on the lake's 1/10,000 grid (rounded half-up). The
  vendor's are already on it where measured: none of the 8,904 prices of a
  split-adjusted WMT tape (2024-02-20..23, the week before its 3:1 split) is
  finer, and every output is identical on the provider and lake paths. A tape
  finer than the grid would move the outputs by an amount that grows as the
  price falls (a change to the input data, not float rounding); that is
  measured on a synthetic tape at three price levels. Both are in
  ``tests/research/test_signal_study_lake_vs_provider.py``, and the owner
  accepted the grid on that evidence (#3522).
* **Session.** Before: **extended hours** — every bar the provider served, 04:00
  to 20:00 ET, whatever the request said (the old route never read the field the
  browser sent). Now the request's ``session`` is the one control and the person's
  choice (#3522), as in the Feature study: ``extended`` is the default, so no
  caller's sample moves, and ``rth`` is the scheduled session, named ``regular`` in
  the identity (``session_policy``). The two are different samples with different
  cache identities, and the page offers both. They also differ in which bars can
  clear the engine's 70% gate: on regular hours a session holds 26 fifteen-minute
  bars, a 15-bar target is valid for at most 11 of them (42%), so a 15-minute run is
  refused up front; 6-minute bars (77%) and shorter clear it. The comparison is
  measured in ``tests/research/test_signal_study.py``. A session counts as complete
  only once the vendor has delivered its whole tape (the close, plus four hours of
  after-hours for ``extended``, plus the vendor's 15-minute delay), and a held day
  captured before that is refused (``captured_before_final``) rather than read as
  complete or captured again (``app.data_lake.capture_finality``). The lake keeps one
  copy of a day for every reader, so a ``rth`` run that would have to capture a day
  waits for the extended tape too (``window_not_complete``).
* **Warmup.** None. Features run inside the window (the same one continuous
  series, straight across overnight gaps; their first values are NaN) and the
  engine needs at least ``SignalConfig.min_bars_for_signal`` bars.
* **Look-forward.** 15 bars, as above. Nothing is read past the window's end, so
  the last 15 bars of the window have no target (``window_runs_off_end``).
* **Window.** The scheduled sessions in ``[from_date, to_date]`` (inclusive,
  canonical calendar), completed sessions only: a window ending on a session that
  has not finished is refused, where the provider used to hand back the forming
  session's partial bars. The 70/30 train/test split is by bar index. Regime
  labels and walk-forward months bucket by **UTC** date; both are unchanged.
* **Cache and result.** Before: Redis ``signal_engine``, keyed on ticker, feature,
  dates, size and the two switches, looked up before the fetch, 24 h, not shared
  across sources. Now: a new namespace keyed on the input identity, the admitted
  snapshot digest, the signal configuration and the calculation version. Looked
  up after admission (the digest needs the bytes), so a repaired or refreshed
  lake yields a new key, and an entry written when the provider was the source
  can never be found. Nothing is relabelled.

The run itself (admit, fold, cache, compute, store, say where the data came
from, refuse with the counts) is the shared one,
:func:`app.research.lake_study.run_lake_study`. This module supplies the
identity, the math and the serializer.

Differences a run can show against the provider path are documented and
measured in ``tests/research/test_signal_study_lake_vs_provider.py``. That file also holds
two measurements on the vendor's real answers, recorded on 2026-10-24: Polygon's own
15-minute aggregates for SPY (2025-01-13..17, ``adjusted=false``) equal ``fold_minute_bars``
of the repo's SPY minute capture bar for bar, all 320 of them, extended hours and regular
hours alike; and the WMT tape above. The first does not cover other symbols, half-days,
split-adjusted aggregates or other bar sizes: the fold's alignment for the other lengths
that divide the hour is proven against an independent stand-in, not against the vendor.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from app.data_lake.capture_finality import AFTER_HOURS_TAIL_MS, PRE_MARKET_HEAD_MS
from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.research.lake_study import FoldedStudyIdentity, LakeStudy, StudyRun, run_lake_study
from app.research.signal.config import SignalConfig
from app.research.signal.engine import run_signal_engine
from app.research.study_inputs import AdjustmentMode, MinuteWindowIdentity, SessionPolicy, StudyInputRefusal
from app.research.target import DEFAULT_NON_NAN_RATIO_THRESHOLD
from app.utils.timestamps import ny_datetime

#: Bump when the signal, backtest, walk-forward, diagnostics or graduation math changes: a cached result is the
#: product of the code that made it, and the cache key is the only thing that tells a new run apart from an old one.
SIGNAL_STUDY_CALCULATION_VERSION = "signal-study/1"

#: The lake-sourced cache namespace. Entries written when the provider was the source live under
#: ``signal_engine`` and are never read by a lake request.
CACHE_NAMESPACE = "signal_engine:lake:v1"

_REQUESTER = "signal-study"

_MINUTES_PER_DAY = 24 * 60
_TIMESPAN_MINUTES = {"minute": 1, "hour": 60, "day": _MINUTES_PER_DAY}

#: Bar lengths, in minutes, whose buckets start at the same minute of every hour on every session: the divisors of 60.
HOUR_ALIGNED_BAR_MINUTES: tuple[int, ...] = tuple(minutes for minutes in range(1, 61) if 60 % minutes == 0)

#: The first and last minute of the New York day (minutes past midnight) in which a bar of one session can start.
MinuteSpan = tuple[int, int]

_POLICY_WORDS = {"regular": "regular-hours", "extended": "extended-hours"}


def bars_in_span(bar_minutes: int, first_minute: int, last_minute: int) -> int:
    """How many ``bar_minutes``-minute bars start in ``[first_minute, last_minute]`` when the grid starts at midnight.

    Exact for a bar length that divides the hour (every session's grid is the same). For the whole day it is also an
    upper bound for any length, because a date holds at most ``ceil(1440 / m)`` multiples of ``m``.
    """
    return last_minute // bar_minutes - first_minute // bar_minutes + 1


def _share(per_session: int, horizon_bars: int) -> float:
    return max(per_session - horizon_bars, 0) / per_session


def max_target_share(
    bar_minutes: int, horizon_bars: int, first_minute: int = 0, last_minute: int = _MINUTES_PER_DAY - 1
) -> float:
    """The most bars of one session that can have a ``horizon_bars``-ahead target, as a share of that session's bars.

    A target is valid only when the forward bar sits on the same New York date. A session whose bars start in
    ``[first_minute, last_minute]`` holds ``n = bars_in_span(...)`` of them, and the last ``horizon_bars`` have no
    forward bar in it, so the share is ``(n - h) / n`` at best. It is a ceiling for any data: gaps and a shorter
    session only lower it. With the default span (the whole day) it is a ceiling for any bar length.

    Formula: share_max(m, h) = max(n - h, 0) / n, with n = floor(last / m) - floor(first / m) + 1 bars of m minutes
      starting in the span; the whole day gives n = ceil(1440 / m)
    Reference: Internal — the same-New-York-date rule of ``app/research/target.py::compute_forward_log_return``; no
      external source.
    Canonical implementation: this file.
    Validated against: tests/research/test_signal_study.py::test_the_ceiling_on_valid_targets_is_what_the_target_function_gives_on_gap_free_bars
    """
    return _share(bars_in_span(bar_minutes, first_minute, last_minute), horizon_bars)


def _minute_of_day(instant_ms: int) -> int:
    local = ny_datetime(instant_ms)
    return local.hour * 60 + local.minute


def session_minute_spans(session_policy: SessionPolicy, window_start: date, window_end: date) -> frozenset[MinuteSpan]:
    """The distinct spans of New York minutes in which a bar can start, over the scheduled sessions of a window.

    ``regular`` is the calendar's own open and close, so an early close is a shorter span. ``extended`` is the
    vendor's whole tape around them (``PRE_MARKET_HEAD_MS`` before the open to ``AFTER_HOURS_TAIL_MS`` after the
    close). Empty when the window holds no session; admission refuses that with its own reason.
    """
    head, tail = (PRE_MARKET_HEAD_MS, AFTER_HOURS_TAIL_MS) if session_policy == "extended" else (0, 0)
    return frozenset(
        (_minute_of_day(window.open_ms_utc - head), _minute_of_day(window.close_ms_utc + tail) - 1)
        for window in session_windows_ms_utc(window_start, window_end)
    )


def longest_session_bars(bar_minutes: int, spans: frozenset[MinuteSpan]) -> int:
    """The most bars of this length any one session of the window can hold."""
    return max(bars_in_span(bar_minutes, first, last) for first, last in spans)


def bar_minutes_that_reach_the_gate(spans: frozenset[MinuteSpan], horizon_bars: int) -> tuple[int, ...]:
    """The hour-aligned bar lengths whose longest session can hold the engine's share of valid targets."""
    if not spans:
        return ()
    return tuple(
        minutes
        for minutes in HOUR_ALIGNED_BAR_MINUTES
        if _share(longest_session_bars(minutes, spans), horizon_bars) >= DEFAULT_NON_NAN_RATIO_THRESHOLD
    )


@dataclass(frozen=True)
class SignalStudyParams:
    """The scientific request: what a run is asked to study."""

    symbol: str
    feature_name: str
    from_date: date
    to_date: date
    timespan: str
    multiplier: int
    flip_sign: bool = True
    regime_gate_enabled: bool = True
    session_policy: SessionPolicy = "extended"
    price_adjustment_mode: AdjustmentMode = "polygon_split_adjusted"

    def config(self) -> SignalConfig:
        return SignalConfig(
            feature_name=self.feature_name, flip_sign=self.flip_sign, regime_gate_enabled=self.regime_gate_enabled
        )


@dataclass(frozen=True)
class SignalStudyIdentity(FoldedStudyIdentity):
    """Every input that decides this study's data: the lake input, the bar length and the forward target."""

    look_forward_bars: int

    def study_fields(self) -> dict[str, Any]:
        return {"look_forward_bars": self.look_forward_bars, "look_forward_extends_window": False}


def _use_instead(lengths: tuple[int, ...]) -> str:
    return f" Use minute bars of {', '.join(str(minutes) for minutes in lengths)} minutes." if lengths else ""


def signal_study_identity(params: SignalStudyParams) -> SignalStudyIdentity:
    """The study's input identity, or a typed refusal for a resolution it can never run on."""
    horizon = params.config().horizon
    threshold = DEFAULT_NON_NAN_RATIO_THRESHOLD
    unit = _TIMESPAN_MINUTES.get(params.timespan)
    if unit is None:
        raise StudyInputRefusal("resolution_unsupported", f"{params.timespan!r} is not a bar size this study reads.")
    bar_minutes = params.multiplier * unit
    label = f"{params.multiplier}-{params.timespan}"
    inputs = MinuteWindowIdentity(
        symbol=params.symbol,
        price_adjustment_mode=params.price_adjustment_mode,
        session_policy=params.session_policy,
        window_start=params.from_date,
        window_end=params.to_date,
        warmup_sessions=0,
    )
    spans = session_minute_spans(inputs.session_policy, inputs.window_start, inputs.window_end)
    use_instead = _use_instead(bar_minutes_that_reach_the_gate(spans, horizon))

    # The refusals are one cause each and checked widest first, so the reason named is the one that holds for the
    # most requests: too long for any session, then off the hour, then too long for this window's sessions.
    per_day = bars_in_span(bar_minutes, 0, _MINUTES_PER_DAY - 1)
    if _share(per_day, horizon) < threshold:
        raise StudyInputRefusal(
            "resolution_unsupported",
            f"{label} bars are too long for the {horizon}-bar forward target: a day holds at most {per_day} of them, so "
            f"at most {_share(per_day, horizon) * 100:.1f}% of bars can have a target inside the same session, and the "
            f"study needs {threshold * 100:.0f}%.{use_instead}",
        )
    if bar_minutes not in HOUR_ALIGNED_BAR_MINUTES:
        raise StudyInputRefusal(
            "resolution_unsupported",
            f"{label} bars do not divide the hour, so the exchange-clock fold would start them at a different minute on "
            f"different sessions and they would line up with no clock.{use_instead}",
        )
    if spans:
        per_session = longest_session_bars(bar_minutes, spans)
        if _share(per_session, horizon) < threshold:
            raise StudyInputRefusal(
                "resolution_unsupported",
                f"{label} bars are too long for the {horizon}-bar forward target under "
                f"{_POLICY_WORDS[inputs.session_policy]} sessions: the longest session in this window holds at most "
                f"{per_session} of them, so at most {_share(per_session, horizon) * 100:.1f}% of bars can have a target "
                f"inside the same session, and the study needs {threshold * 100:.0f}%.{use_instead}",
            )
    return SignalStudyIdentity(inputs=inputs, bar_minutes=bar_minutes, look_forward_bars=horizon)


def serialize_signal_report(report: Any) -> dict:
    """Serialize a SignalEngineReport for storage and delivery."""
    return {
        "success": report.error is None,
        "ticker": report.ticker,
        "feature_name": report.feature_name,
        "start_date": report.start_date,
        "end_date": report.end_date,
        "bars_used": report.bars_used,
        "flip_sign": report.flip_sign,
        "thresholds_tested": report.thresholds_tested,
        "cost_bps_options": report.cost_bps_options,
        "best_threshold": report.best_threshold,
        "best_cost_bps": report.best_cost_bps,
        "backtest_grid": [asdict(bt) for bt in report.backtest_grid],
        "walk_forward": asdict(report.walk_forward) if report.walk_forward is not None else None,
        "graduation": asdict(report.graduation) if report.graduation is not None else None,
        "signal_diagnostics": (asdict(report.signal_diagnostics) if report.signal_diagnostics is not None else None),
        "data_sufficiency": (asdict(report.data_sufficiency) if report.data_sufficiency is not None else None),
        "effective_sample": (asdict(report.effective_sample) if report.effective_sample is not None else None),
        "regime_coverage": report.regime_coverage,
        "joint_regime_coverage": [asdict(b) for b in report.joint_regime_coverage],
        "signal_behavior": (asdict(report.signal_behavior) if report.signal_behavior is not None else None),
        "oos_sharpe_ci": (asdict(report.oos_sharpe_ci) if report.oos_sharpe_ci is not None else None),
        "deflated_sharpe": (asdict(report.deflated_sharpe) if report.deflated_sharpe is not None else None),
        "methodology": report.methodology,
        "research_log": report.research_log,
        "error": report.error,
    }


def signal_study(params: SignalStudyParams) -> LakeStudy:
    """The Signal study as the shared lake run takes it: its names, its cache inputs, and its math."""
    config = params.config()

    def compute(run: StudyRun) -> dict[str, Any]:
        report = run_signal_engine(
            ticker=params.symbol,
            feature_name=params.feature_name,
            bars=run.rows,
            start_date=params.from_date.isoformat(),
            end_date=params.to_date.isoformat(),
            config=config,
            on_phase=run.hooks.on_phase,
            on_log=run.hooks.on_log,
            on_progress=run.hooks.on_progress,
            cancel_check=run.hooks.cancel_check,
        )
        if report.error:
            # Surface the failure as an exception so run_in_thread emits job.failed (the engine caught the exception
            # internally to populate report.error; re-raising gives the SSE consumer a clean terminal event instead of
            # "completed but error_field=…").
            raise ValueError(report.error)
        return serialize_signal_report(report)

    return LakeStudy(
        job_type="signal_engine",
        cache_namespace=CACHE_NAMESPACE,
        calculation_version=SIGNAL_STUDY_CALCULATION_VERSION,
        requester=_REQUESTER,
        subject={"ticker": params.symbol, "feature_name": params.feature_name},
        admit_message=(
            f"Checking the lake for {params.symbol} {params.multiplier}-minute bars from "
            f"{params.from_date.isoformat()} to {params.to_date.isoformat()}"
        ),
        load_message=f"Loading {params.symbol} {params.multiplier}-minute bars from the lake",
        compute=compute,
        cache_extra={"signal_config": asdict(config)},
    )


def run_signal_study(
    params: SignalStudyParams,
    *,
    job_id: str,
    force: bool,
    emit: ProgressEmitter,
    cancel: CancellationCheck,
    now_ms: int | None = None,
) -> dict | None:
    """Admit the study's lake inputs, compute, and return the result — or answer a refusal or cache hit itself.

    The run is the shared one (:func:`app.research.lake_study.run_lake_study`); this supplies the identity and the math.
    """
    return run_lake_study(
        signal_study(params),
        lambda: signal_study_identity(params),
        job_id=job_id,
        force=force,
        emit=emit,
        cancel=cancel,
        now_ms=now_ms,
    )
