"""Realized vs IV study (Edge → Realized vs IV): computes from admitted lake inputs (#3525).

The study's input contract. What it read before #3525 (the data-source census, #3491),
and what it reads now, field by field:

* **Bars.** Before: the browser called ``POST /api/aggregates/fetch`` (one Polygon
  ``fetch_aggregates`` call, then ``DataSanitizer``) and posted the bars inline to
  ``POST /api/edge/realized-vs-iv/series``. Now: the worker reads the lake's one-minute
  trade bars, admitted through :mod:`app.research.study_inputs` (the existing
  materializer and committed-byte admission; the UI is never a writer), folded by
  ``fold_minute_bars`` to the bar size asked for. The inline-bars route is gone, so no
  caller can hand the study bars the lake did not admit.
* **Sanitizer.** Before: ``DataSanitizer.sanitize_aggregates`` filled NaN with each
  column's median and clipped open, high, low, close, volume, vwap and transactions each
  to its own 1st to 99th percentile across the window, so every estimator's extremes were
  winsorized. Now: lake bars are not clipped. The effect is measured in
  ``tests/research/test_realized_vs_iv_lake_vs_provider.py`` and classified there
  (``data-quality``). The owner ruled (#3525) that bars stay unclipped: the clip dropped real moves and shaped past bars
  with the whole window's future.
* **Resolution.** ``15m`` and ``1d``; the annualization and the HF
  estimator are defined for those two. Before: ``5m`` and ``1h`` were offered by the page
  and sent to the series route as ``"1d"``, so they were annualized as daily bars. Now
  they are refused with ``resolution_unsupported`` and the page no longer offers them.
  A 15-minute bar is a fold of fifteen 1-minute bars; a daily bar is the fold of a day's minute
  bars (``bar_minutes=1440``: the bar is stamped at ET midnight of its date).
* **Adjustment.** Before: Polygon ``adjusted=True`` (split-only). Now: the lake's
  ``polygon_split_adjusted`` mode, named in the identity. Prices sit on the lake's 1/10,000
  grid (``data-quality``: an input change, not float rounding).
* **Session.** Before, 15m: every bar Polygon served (04:00 to 20:00 ET), with the HF
  estimator's own ``hf_session`` (ETH, the default, or RTH) filtering inside it. Daily:
  Polygon's day bar, which the census could not prove is regular-session (unverified).
  Now: the identity names the session the *bars* are folded from. The defaults keep each
  size on what it read: ``extended`` for 15m and ``regular`` for 1d (a day folded from
  extended minutes would open at the first pre-market print). ``session`` overrides it. The
  owner ruled (#3525) that this is the choice: a 15m run is extended or regular hours, extended
  by default, and daily bars are regular hours only. The server holds those rules
  (:func:`resolve_session_choice`): ``hf_session``, the HF estimator's own filter, follows the session (regular hours
  RTH, extended hours ETH) and is derived when a request omits it; a request that names a pair that disagrees, or
  extended-hours daily bars, is refused with ``resolution_unsupported`` and the reason. Only 15m bars have an HF
  estimator, so a daily identity records none. A session counts as complete only once the vendor has delivered
  its whole tape (``app.data_lake.capture_finality``).
* **Annualization.** Before: a 15m bar was counted 26 to a day whatever session it was folded from, so an
  extended-hours run (64 bars a day) was understated by sqrt(26 / 64), about 0.64 times. Now: a year is 252 sessions of the
  bars a full session of the session read holds, counted from the calendar (:mod:`app.engine.edge.session_bars`): 26 for
  regular hours, 64 for extended. Regular-hours numbers do not move; extended-hours 15m trailing lines grow by sqrt(64 / 26).
* **Window.** Before: the browser took today as the end and one year (daily) or one month
  (intraday) back as the start, as UTC dates, so the in-progress session from the
  delayed feed was included. A year holds about 251 daily bars, fewer than the 273 a VRP z-score needs. Now: unless a
  request names its own, the end is the last session the study can read or capture
  (:func:`last_final_session`: its extended tape final and the capture lease passed, whatever session the run reads,
  because the lake keeps one copy of a day) and the start is as many sessions earlier as the z-score needs
  (:func:`resolve_window`, sized from ``VRP_LOOKBACK_BARS`` and the forward tail: 273 sessions of daily bars; for 15m
  bars the sessions that hold that many bars of the session read). The sessions come from the canonical calendar.
  Today's partial session is no longer in the sample (``off-by-one``: the window ends one
  session earlier until the tape is final), and a requested window that ends on a forming
  session is refused (``window_not_complete``). The window is in the identity, so each
  day's default window is a new identity. ``GET /api/edge/realized-vs-iv/window`` tells a page the window a run
  would use, so the page shows it and counts what the lake lacks over exactly that span.
* **Warmup.** None fetched, as before. Trailing RV needs its own window of bars inside the
  window; the HF 21-day windows need 21 sessions, so an intraday window carries no HF trailing value
  in its first 21 sessions. Reported as ``forward_nan_bars``.
* **Look-forward.** 21 trading days (the HF forward RV; for daily bars YZ-21). Nothing is
  read past the window's end: the last 21 sessions' forward RV is unrealized, stays NaN, and
  so does every VRP value built on it.
* **IV.** Recorded or caller-supplied, never fetched here, and never produced from the
  live marker. The recorder's snapshots in the window (or the caller's ``iv_series``) are
  part of the identity by digest. They reach bars by :mod:`app.engine.edge.iv_alignment`:
  the snapshot inside the bar's interval, latest wins, nothing carried or interpolated; a
  bar with none has no IV and the counts say how many snapshots fell outside every bar.
  Before: equality of timestamps, which almost never held. The current-IV marker
  (``POST /api/edge/iv30/*``) stays a separate browser call and a separate field.
  The recorder is scheduled (:mod:`app.services.iv_recorder_schedule`, #3525): one snapshot per trading day at 15:55 ET
  (12:55 on an early close), filed at the quote time of the chain it read (before the close), so the regular-hours
  15:45 bar, the extended 15:45 bar and the day's bar each hold it, and the history builds from the day the schedule
  first ran. A 15m bar holds an IV only in the bar its snapshot falls in, so a once-a-day recorder never gives a 15m
  window the 252 consecutive bars of IV a z-score needs.
* **Cache and result.** Before: none, and nothing was stored. Now: a namespace keyed on the
  input identity (data policy, the IV digest, the estimators), the admitted snapshot
  digest and the calculation version, looked up after admission. Nothing is relabelled.

Differences a run can show against the provider path are documented and measured in
``tests/research/test_realized_vs_iv_lake_vs_provider.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd

from app.data_lake.capture_finality import CLAIM_LEASE_MS, capture_final_ms
from app.engine.edge.confidence import DEFAULT_CONFIDENCE_FLOOR
from app.engine.edge.features_realtime.hf_realized_vol import Session
from app.engine.edge.iv_alignment import IV_ALIGNMENT_RULE, align_iv_snapshots_to_bars
from app.engine.edge.realized_vs_iv_series import (
    FORWARD_TRADING_DAYS,
    VRP_LOOKBACK_BARS,
    IvSource,
    compute_realized_vs_iv_series,
)
from app.engine.edge.session_bars import DAY_BAR_MINUTES, bars_in_session, full_session_bars
from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.lake_study import FoldedStudyIdentity, LakeStudy, StudyRun, run_lake_study
from app.research.runs.hashing import hash_payload
from app.research.study_inputs import AdjustmentMode, MinuteWindowIdentity, SessionPolicy, StudyInputRefusal
from app.services.iv_recorder import get_iv_store
from app.utils.session_anchors import et_date_at_ms, et_day_end_ms, et_midnight_ms
from app.utils.timestamps import now_ms_utc

#: Bump when the series composition, the annualization, the IV alignment rule, the window rule or the bar fold changes: a
#: cached result is the product of the code that made it, and the cache key is the only thing that tells a new run from
#: an old one.
REALIZED_VS_IV_CALCULATION_VERSION = "realized-vs-iv-study/2"

#: The lake-sourced cache namespace. This study had no result cache before #3525; nothing under the bare job type
#: ``realized_vs_iv`` is ever read by a lake request.
CACHE_NAMESPACE = "realized_vs_iv:lake:v1"

_REQUESTER = "realized-vs-iv-study"

#: The bar sizes the study is defined for.
BarSizeLabel = Literal["15m", "1d"]

#: Bar sizes the study is defined for, and the minutes each folds.
BAR_MINUTES: Mapping[str, int] = {"15m": 15, "1d": DAY_BAR_MINUTES}

#: The session each bar size reads unless a request names one; see the module docstring (#3525).
DEFAULT_SESSION_POLICY: Mapping[str, SessionPolicy] = {"15m": "extended", "1d": "regular"}

#: The HF estimator's session filter that goes with each session the bars are folded from: regular hours are RTH, extended ETH.
HF_SESSION_FOR_POLICY: Mapping[SessionPolicy, Session] = {"regular": "RTH", "extended": "ETH"}

#: The tail of the calendar searched for the last final session: longer than any run of closed days plus a session.
_FINAL_SESSION_SEARCH_DAYS = 14

#: Calendar days of sessions read at a time while walking a window back from its end.
_WINDOW_WALK_DAYS = 400


@dataclass(frozen=True)
class IvInput:
    """The IV a run is given: recorded, caller-supplied or none, and a digest of exactly the points in it."""

    source: IvSource
    #: ``{ts, iv30, health_score?, variance_contribution_synthetic?}``, stamped at the moment its quotes describe (a recorded row's quote time).
    points: tuple[Mapping[str, Any], ...]
    digest: str


@dataclass(frozen=True)
class RealizedVsIvParams:
    """The scientific request: what a run is asked to study."""

    symbol: str
    bar_size: str = "1d"
    tenor_days: int = 30
    #: The HF estimator's own session filter (ETH 04:00-20:00 ET, or RTH). It follows the session the bars are folded from;
    #: ``None`` is the one that goes with it, and a different one is refused (:func:`resolve_session_choice`). It applies to
    #: ``15m`` bars only: a daily run has no HF estimator, so the identity records none rather than a choice nothing read.
    hf_session: Session | None = None
    #: Which bars the study folds from; ``None`` is the session that bar size has always read.
    session_policy: SessionPolicy | None = None
    estimators: tuple[str, ...] = ("yz",)
    windows: tuple[int, ...] = (5, 10, 30)
    #: Caller-supplied IV; ``None`` or empty reads the recorder.
    iv_series: Sequence[Mapping[str, Any]] | None = None
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR
    price_adjustment_mode: AdjustmentMode = "polygon_split_adjusted"
    #: The first and last session to study; ``None`` is the default window (:func:`resolve_window`). A window that
    #: ends on a session whose tape is still forming is refused by admission, never read short.
    window: tuple[date, date] | None = None


@dataclass(frozen=True)
class RealizedVsIvIdentity(FoldedStudyIdentity):
    """Every input that decides this study's numbers: the lake input, the bar fold, the estimators and the IV."""

    bar_size: str
    tenor_days: int
    hf_session: Session | None
    estimators: tuple[str, ...]
    windows: tuple[int, ...]
    confidence_floor: float
    iv: IvInput = field(compare=False)

    def study_fields(self) -> dict[str, Any]:
        return {
            "bar_size": self.bar_size,
            "tenor_days": self.tenor_days,
            "hf_session": self.hf_session,
            "estimators": list(self.estimators),
            "windows": list(self.windows),
            "confidence_floor": self.confidence_floor,
            "look_forward_trading_days": FORWARD_TRADING_DAYS,
            "look_forward_extends_window": False,
            "iv_source": self.iv.source,
            "iv_snapshots": len(self.iv.points),
            "iv_digest": self.iv.digest,
            "iv_alignment_rule": IV_ALIGNMENT_RULE,
        }


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def last_final_session(now_ms: int) -> date:
    """The latest scheduled session the study can read or capture at ``now_ms`` (canonical calendar).

    A session is the window's end once its extended tape is final and the capture lease has passed. That is the later of
    the two sessions' finals, so it holds for a regular-hours run too: the lake keeps one copy of a day and a study that
    has to capture a day waits for the extended tape (``app.research.study_inputs``), so a default window never ends on a
    day it could only be refused for.
    """
    today = et_date_at_ms(now_ms)
    for day in reversed(expected_sessions(today - timedelta(days=_FINAL_SESSION_SEARCH_DAYS), today)):
        if now_ms >= capture_final_ms(day, "extended") + CLAIM_LEASE_MS:
            return day
    raise ValueError(f"no session in the {_FINAL_SESSION_SEARCH_DAYS} days to {today} has a final tape")


def first_session_for_vrp_z(end: date, bar_minutes: int, session_policy: SessionPolicy) -> date:
    """The first session of the shortest window ending at ``end`` that holds a VRP z-score's bars.

    The z-score rolls over ``VRP_LOOKBACK_BARS`` bars that each have a forward value, and the last ``FORWARD_TRADING_DAYS``
    sessions have none: walking back from ``end``, those sessions come first and then the sessions whose bars (counted
    from the calendar for the session read) reach the lookback. For daily bars that is 21 + 252 = 273 sessions.
    """
    in_tail, bars = 0, 0
    chunk_end = end
    while True:
        chunk_start = chunk_end - timedelta(days=_WINDOW_WALK_DAYS)
        for day in reversed(expected_sessions(chunk_start, chunk_end)):
            if in_tail < FORWARD_TRADING_DAYS:
                in_tail += 1
                continue
            bars += bars_in_session(day, bar_minutes, session_policy)
            if bars >= VRP_LOOKBACK_BARS:
                return day
        if chunk_start.year < 1990:
            raise ValueError(f"the calendar holds too few sessions before {end.isoformat()} to hold a VRP z-score")
        chunk_end = chunk_start - timedelta(days=1)


def resolve_window(bar_size: str, session_policy: SessionPolicy, now_ms: int) -> tuple[date, date]:
    """The run's default window: the last session the study can read or capture, and enough sessions before it for a z-score."""
    end = last_final_session(now_ms)
    return first_session_for_vrp_z(end, BAR_MINUTES[bar_size], session_policy), end


@dataclass(frozen=True)
class DefaultWindow:
    """The window a run reads when its request names none, and the session it reads it in."""

    bar_size: BarSizeLabel
    session_policy: SessionPolicy
    start: date
    end: date
    #: Scheduled sessions from ``start`` to ``end``, both included.
    sessions: int


def default_window(bar_size: BarSizeLabel, session_policy: SessionPolicy | None, now_ms: int) -> DefaultWindow:
    """What a run of ``bar_size`` would read now: ``session_policy`` (``None`` is the bar size's own) and its default window.

    A page asks for this before a run so it can show the window and count what the lake lacks over exactly that span.
    A pair the study does not offer (extended-hours daily bars) is refused as a run would refuse it.
    """
    policy, _ = resolve_session_choice(bar_size, session_policy, None)
    start, end = resolve_window(bar_size, policy, now_ms)
    return DefaultWindow(bar_size, policy, start, end, len(expected_sessions(start, end)))


# ---------------------------------------------------------------------------
# IV in
# ---------------------------------------------------------------------------


def recorded_iv_points(symbol: str, *, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """Recorded IV snapshots in ``[start_ms, end_ms]`` as ``iv_series`` points.

    Prefers ``iv30_vix_style`` and falls back to ``iv30_parametric``; rows
    where both are None or that carry an ``error`` are skipped (no synthesis).
    Pulls ``variance_contribution_synthetic`` from ``iv_provenance`` and
    ``health_score`` directly off the row when present so downstream
    confidence gating and regime-feature weighting use real evidence. Rows
    from before the recorder persisted ``health_score`` (or rows where the
    health computation itself failed) lack the field; ``_parse_iv_series``
    takes the drop-health-factor branch (confidence = 1 − vcs) and
    surfaces the imputed-ness via ``health_imputed_now`` so the UI can
    flag the bar.

    Returns ``[]`` when the recorder has nothing in the window; the run then
    reports ``iv_source="absent"``.
    """
    out: list[dict[str, Any]] = []
    for r in get_iv_store().read_series(symbol, start_ms=start_ms, end_ms=end_ms):
        if r.error is not None:
            continue
        iv = r.iv30_vix_style if r.iv30_vix_style is not None else r.iv30_parametric
        if iv is None:
            continue
        item: dict[str, Any] = {"ts": r.snapshot_ts_ms, "iv30": float(iv)}
        prov = r.iv_provenance or {}
        if "variance_contribution_synthetic" in prov:
            item["variance_contribution_synthetic"] = float(prov["variance_contribution_synthetic"])
        if r.health_score is not None:
            item["health_score"] = float(r.health_score)
        out.append(item)
    return out


def load_iv_input(
    symbol: str, window: tuple[date, date], supplied: Sequence[Mapping[str, Any]] | None
) -> IvInput:
    """The caller's IV when it sent any, else the recorder's snapshots across the window, else none.

    Read once, here, so the points hashed into the identity are the points the run aligns.
    """
    if supplied:
        source: IvSource = "caller_supplied"
        points = [dict(p) for p in supplied]
    else:
        points = recorded_iv_points(symbol, start_ms=et_midnight_ms(window[0]), end_ms=et_day_end_ms(window[1]))
        source = "recorder" if points else "absent"
    ordered = sorted(points, key=lambda p: int(p["ts"]))
    return IvInput(source=source, points=tuple(ordered), digest=hash_payload({"source": source, "points": ordered}))


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def resolve_session_choice(
    bar_size: str, session_policy: SessionPolicy | None, hf_session: Session | None
) -> tuple[SessionPolicy, Session | None]:
    """The session the bars are folded from and the HF filter that goes with it; a typed refusal for a choice not offered.

    ``session_policy`` ``None`` is the session the bar size reads by default. Daily bars are regular hours only and have no
    HF estimator (``None``, whatever ``hf_session`` was sent). A 15-minute run's HF filter follows its session; ``None``
    is derived, and a different one is refused. Every refusal is ``resolution_unsupported``, raised before the lake is touched.
    """
    if bar_size not in BAR_MINUTES:
        raise StudyInputRefusal(
            "resolution_unsupported",
            f"{bar_size} bars are not supported: the annualization and the HF estimator are defined for "
            f"{' and '.join(BAR_MINUTES)} bars only.",
        )
    policy = session_policy if session_policy is not None else DEFAULT_SESSION_POLICY[bar_size]
    if bar_size == "1d":
        if policy != "regular":
            raise StudyInputRefusal(
                "resolution_unsupported",
                "Daily bars are folded from regular hours only: a day folded from extended minutes would open at the "
                "first pre-market print. Ask for regular hours, or for 15-minute bars.",
            )
        return policy, None
    expected = HF_SESSION_FOR_POLICY[policy]
    if hf_session is not None and hf_session != expected:
        raise StudyInputRefusal(
            "resolution_unsupported",
            f"Bars folded from {policy} hours are measured with the {expected} high-frequency session, not "
            f"{hf_session}: the two follow each other.",
        )
    return policy, expected


def realized_vs_iv_identity(params: RealizedVsIvParams, *, now_ms: int) -> RealizedVsIvIdentity:
    """The study's input identity, or a typed refusal for a bar size or session it is not defined for."""
    policy, hf_session = resolve_session_choice(params.bar_size, params.session_policy, params.hf_session)
    window = params.window if params.window is not None else resolve_window(params.bar_size, policy, now_ms)
    return RealizedVsIvIdentity(
        inputs=MinuteWindowIdentity(
            symbol=params.symbol,
            price_adjustment_mode=params.price_adjustment_mode,
            session_policy=policy,
            window_start=window[0],
            window_end=window[1],
            warmup_sessions=0,
        ),
        bar_minutes=BAR_MINUTES[params.bar_size],
        bar_size=params.bar_size,
        tenor_days=params.tenor_days,
        hf_session=hf_session,
        estimators=params.estimators,
        windows=params.windows,
        confidence_floor=params.confidence_floor,
        iv=load_iv_input(params.symbol, window, params.iv_series),
    )


# ---------------------------------------------------------------------------
# The math, around the shared run
# ---------------------------------------------------------------------------


def bar_ends_ms(starts_ms: Sequence[int], bar_minutes: int) -> list[int]:
    """Where each bar ends: its start plus the fold, or the next ET midnight for a daily bar (a DST day is 23 or 25 hours)."""
    if bar_minutes == DAY_BAR_MINUTES:
        return [et_day_end_ms(et_date_at_ms(start)) for start in starts_ms]
    return [start + bar_minutes * 60_000 for start in starts_ms]


def _bars_frame(rows: Sequence[Mapping[str, float | int]]) -> pd.DataFrame:
    """The study's bars as the estimators take them: an ``int64 ms UTC`` index, strictly increasing."""
    index = pd.Index([int(r["timestamp"]) for r in rows], name="ts", dtype=np.int64)
    if not index.is_monotonic_increasing or not index.is_unique:
        raise ValueError("the admitted bars are not strictly increasing in time")
    return pd.DataFrame({column: [float(r[column]) for r in rows] for column in ("open", "high", "low", "close", "volume")}, index=index)


def compute_study(run: StudyRun) -> dict[str, Any]:
    """The series over the admitted, folded bars, with the IV aligned to them and the bars themselves for the chart."""
    identity = run.identity
    if not isinstance(identity, RealizedVsIvIdentity):
        raise TypeError(f"the Realized vs IV study was handed a {type(identity).__name__} identity")
    run.hooks.on_phase("computing")
    bars = _bars_frame(run.rows)
    starts = [int(t) for t in bars.index.tolist()]
    alignment = align_iv_snapshots_to_bars(
        identity.iv.points, bar_starts_ms=starts, bar_ends_ms=bar_ends_ms(starts, identity.bar_minutes)
    )
    if identity.iv.source == "absent":
        run.hooks.on_log("IV: none recorded for this window and none supplied")
    else:
        run.hooks.on_log(
            f"IV: {alignment.snapshots_read} {identity.iv.source.replace('_', ' ')} snapshots, "
            f"{alignment.bars_with_iv} bars hold one, {alignment.outside_bars} fall inside no bar"
        )
    sessions = expected_sessions(identity.inputs.window_start, identity.inputs.window_end)
    series = compute_realized_vs_iv_series(
        bars,
        symbol=identity.inputs.symbol,
        bar_size=identity.bar_size,
        tenor_days=identity.tenor_days,
        hf_session=identity.hf_session,
        estimators=identity.estimators,
        windows=identity.windows,
        iv_points=alignment.points or None,
        iv_source=identity.iv.source,
        confidence_floor=identity.confidence_floor,
        bars_per_session=full_session_bars(sessions, identity.bar_minutes, identity.inputs.session_policy),
    )
    series["coverage"]["iv_alignment"] = alignment.as_dict()
    return {
        "success": True,
        **series,
        "bar_size": identity.bar_size,
        "tenor_days": identity.tenor_days,
        "hf_session": identity.hf_session,
        "estimators": list(identity.estimators),
        "windows": list(identity.windows),
        "bars": {column: bars[column].tolist() for column in ("open", "high", "low", "close", "volume")},
    }


def realized_vs_iv_study(params: RealizedVsIvParams) -> LakeStudy:
    """The study as the shared lake run takes it: its names and its math. Everything that decides a number is in the identity."""
    size_label = "daily" if params.bar_size == "1d" else params.bar_size
    return LakeStudy(
        job_type="realized_vs_iv",
        cache_namespace=CACHE_NAMESPACE,
        calculation_version=REALIZED_VS_IV_CALCULATION_VERSION,
        requester=_REQUESTER,
        subject={"symbol": params.symbol, "bar_size": params.bar_size},
        admit_message=f"Checking the lake for {params.symbol} {size_label} bars",
        load_message=f"Loading {params.symbol} {size_label} bars from the lake",
        compute=compute_study,
    )


def run_realized_vs_iv_study(
    params: RealizedVsIvParams,
    *,
    job_id: str,
    force: bool,
    emit: ProgressEmitter,
    cancel: CancellationCheck,
    now_ms: int | None = None,
) -> dict | None:
    """Admit the study's lake inputs, compute, and return the result — or answer a refusal or cache hit itself.

    The run is the shared one (:func:`app.research.lake_study.run_lake_study`); this supplies the identity and the math.
    ``now_ms`` fixes the clock for the window, the completed-session check and the provider floor; production passes none.
    """
    now = now_ms_utc() if now_ms is None else now_ms
    return run_lake_study(
        realized_vs_iv_study(params),
        lambda: realized_vs_iv_identity(params, now_ms=now),
        job_id=job_id,
        force=force,
        emit=emit,
        cancel=cancel,
        now_ms=now,
    )
