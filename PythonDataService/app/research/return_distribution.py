"""Daily return distribution study: minute bars → session-segmented daily
returns → fixed-width histogram with open edge bins → summary statistics.

Formula:
  Segment log-return  r_seg = ln(P_end / P_start) between session anchors;
  daily simple return in percent  R = (exp(r_close_to_close) − 1) · 100;
  histogram counts of R into fixed-width bins [lo, hi) spanning ±span plus
  open edge bins (R < −span, R ≥ +span); sample moments (ddof=1), adjusted
  Fisher-Pearson skew G1, bias-corrected Fisher excess kurtosis G2,
  historical VaR-95 = linear-interpolated 5th percentile, historical
  CVaR-95 = mean of {R ≤ VaR-95}; normal-overlay expected count per bin =
  N · (Φ((hi−μ)/σ) − Φ((lo−μ)/σ)).
Reference:
  Study shape (empirical distribution of daily returns): Fama, "The Behavior
  of Stock-Market Prices" (1965) §I.B; Mandelbrot (1963). Moment estimators:
  scipy.stats documentation for skew(bias=False) / kurtosis(fisher=True,
  bias=False) — the adjusted Fisher-Pearson standardized coefficients G1/G2,
  https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.skew.html.
  Historical VaR/CVaR: McNeil, Frey, Embrechts, "Quantitative Risk
  Management" (2e) §2.2–2.3 (empirical quantile and tail-expectation
  estimators). Normal CDF via erf, identical to
  app/engine/results/lean_statistics.py::_normal_cdf.
Canonical implementation: this file (registered in docs/math-sources-of-truth.md
  as "Daily return distribution from minute bars").
Validated against: tests/research/test_return_distribution.py and the golden
  fixture tests/fixtures/golden/return-distribution/RD-001 (scipy.stats oracle).

Conventions (all deliberate, all tested):
  * All returns are computed in log space and displayed as simple percent via
    expm1. Segment identities hold exactly in log space (morning + afternoon
    = session *by construction* — ``afternoon_log = session_log -
    morning_log``, because two independent ``math.log`` calls can drift from
    their sum by an ulp and the log-space identity is the point of the
    decomposition). In displayed percent the same sums hold to second order
    (geometric compounding: morning 1.2% + afternoon 0.8% ≈ day 2.0%), which
    is the standard convention for decomposing a day's move.
  * Prices arrive as Decimal (LEAN deci-cent grid, 1e-4) and are converted to
    float64 once, at the anchor boundary — the same precision seam the chart
    documents (app/services/chart_bar_source.py, "Known precision seam").
  * Corporate-action adjustment multiplies each day's anchors by the LEAN
    factor file's ``price_factor · split_factor`` as of that date (row dated D
    covers data up to and including D; LEAN applies the event on D's
    successor). Data dated before the first row uses the first row's factors,
    matching LEAN's own factor-file application.
  * Bin membership: left edge bin is R < −span, right edge bin is R ≥ +span,
    inner bins are [lo, hi) with edges at multiples of the bin width so 0 is
    always an edge. The union covers ℝ with no overlap.
  * Timestamps on output are int64 ms UTC anchored at each session's
    scheduled open (the lake's trading-date wire convention,
    app/data_lake/types.py). Session boundaries and half-day closes come
    only from app/lean_sidecar/trading_calendar.py.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar

TRADING_DAYS_PER_YEAR = 252
DEFAULT_BIN_WIDTH_PCT = 0.5
DEFAULT_SPAN_PCT = 5.0

_ET = ZoneInfo("America/New_York")

ReturnKind = Literal["close_to_close", "session", "overnight"]
RETURN_KINDS: tuple[ReturnKind, ...] = ("close_to_close", "session", "overnight")

#: The full-24h session segmentation shown in the per-day drill-down, in
#: chronological order. ``overnight``/``morning``/``afternoon`` are the
#: RTH-anchored decomposition (overnight + session = close-to-close,
#: morning + afternoon = session — exact in log space, second-order in the
#: displayed percent); ``pre_market`` is the portion of the overnight gap
#: covered by actual pre-market trading (first extended bar open → RTH open)
#: and ``after_hours`` is the post-close tape (RTH close → last extended
#: bar close). Both are supplementary: they overlap the overnight and
#: next-day boundaries by construction, which is documented rather than
#: hidden.
SEGMENT_NAMES: tuple[str, ...] = (
    "overnight",
    "pre_market",
    "morning",
    "afternoon",
    "after_hours",
)


class NonMonotonicBarError(ValueError):
    """A day's bars did not arrive in chronological order."""


@dataclass(frozen=True)
class FactorRow:
    """One LEAN factor-file row: factors in effect for data up to ``row_date``."""

    row_date: date
    price_factor: Decimal
    split_factor: Decimal


@dataclass(frozen=True)
class DayAnchors:
    """The five price anchors of one trading day, unadjusted (raw lake grid).

    ``None`` anchors mean the day genuinely produced no bars in that segment
    (e.g. no pre-market trades); days with no RTH bars at all are excluded
    from every return series by ``compute_daily_returns``.
    """

    trading_date: date
    session_open_ms_utc: int
    first_open: Decimal
    rth_open: Decimal | None
    noon_boundary_close: Decimal | None
    rth_close: Decimal | None
    last_close: Decimal
    volume: int
    has_pre_market: bool
    has_after_hours: bool


@dataclass(frozen=True)
class DailyReturns:
    """One day's returns in simple percent, plus wire anchors.

    Every percentage field is ``None`` when the anchors it needs are absent;
    the log-space identities that do hold are listed in SEGMENT_NAMES above.
    """

    trading_date: date
    session_open_ms_utc: int
    close_to_close_pct: float | None
    session_pct: float | None
    overnight_pct: float | None
    pre_market_pct: float | None
    morning_pct: float | None
    afternoon_pct: float | None
    after_hours_pct: float | None
    volume: int


@dataclass(frozen=True)
class HistogramBin:
    """One histogram basket. ``lower_pct``/``upper_pct`` are ``None`` on the
    open side of an edge bin; both bounds are in simple-percent units."""

    lower_pct: float | None
    upper_pct: float | None
    count: int
    is_edge: bool


@dataclass(frozen=True)
class Histogram:
    bin_width_pct: float
    span_pct: float
    bins: tuple[HistogramBin, ...]

    @property
    def total_count(self) -> int:
        return sum(b.count for b in self.bins)


@dataclass(frozen=True)
class ExtremeDay:
    trading_date: date
    session_open_ms_utc: int
    value_pct: float


@dataclass(frozen=True)
class DistributionStats:
    """Sample statistics of one return series, in simple-percent units."""

    n_days: int
    mean_pct: float
    std_pct: float
    annualized_vol_pct: float
    skewness: float
    excess_kurtosis: float
    var_95_pct: float
    cvar_95_pct: float
    best_day: ExtremeDay
    worst_day: ExtremeDay


@dataclass(frozen=True)
class KindDistribution:
    """Histogram + stats + normal overlay for one return kind."""

    kind: ReturnKind
    histogram: Histogram
    stats: DistributionStats
    normal_expected_counts: tuple[float, ...]


@dataclass(frozen=True)
class ReturnDistributionResult:
    kinds: tuple[KindDistribution, ...]
    days: tuple[DailyReturns, ...]
    bin_width_pct: float
    span_pct: float
    adjustment: Literal["split_and_dividend", "raw"]


def noon_et_ms_utc(d: date) -> int:
    """12:00 ET of ``d`` as int64 ms UTC — the morning/afternoon split.

    Conversion through the NY zone (never a fixed offset) so DST days stay
    correct, mirroring ``trading_calendar.session_open_ms_utc``.
    """
    noon_et = datetime(d.year, d.month, d.day, 12, 0, tzinfo=_ET)
    return int(noon_et.timestamp() * 1000)


def parse_factor_file(text: str) -> list[FactorRow]:
    """Parse a LEAN factor-file CSV body (``date,price_factor,split_factor,
    reference_price``, no header) into rows sorted ascending by date.

    Malformed rows raise ``ValueError`` — a factor file this repo's own
    writers produced is trusted input, but a hand-edited or truncated one
    must fail loudly rather than silently mis-adjust a two-year series.
    """
    rows: list[FactorRow] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(",")
        if len(parts) != 4:
            raise ValueError(f"factor file row does not have 4 columns: {line!r}")
        raw_date, raw_price_factor, raw_split_factor, _reference = parts
        try:
            row_date = datetime.strptime(raw_date, "%Y%m%d").date()
            price_factor = Decimal(raw_price_factor)
            split_factor = Decimal(raw_split_factor)
        except ValueError as exc:
            raise ValueError(f"malformed factor file row {line!r}: {exc}") from exc
        rows.append(
            FactorRow(
                row_date=row_date,
                price_factor=price_factor,
                split_factor=split_factor,
            )
        )
    rows.sort(key=lambda r: r.row_date)
    return rows


def factor_multiplier_as_of(rows: Sequence[FactorRow], d: date) -> Decimal:
    """The cumulative adjustment multiplier ``price_factor · split_factor``
    in effect for data dated ``d``.

    The latest row dated ≤ ``d`` wins; data before the first row uses the
    first row's factors (LEAN's own application — the file's factors are
    cumulative from file start). An empty file is the identity multiplier,
    which is the honest reading of "no corporate actions in the capture
    window" (see the factor-file builder, app/data_lake/factor_files.py).
    """
    if not rows:
        return Decimal(1)
    idx = bisect_right([r.row_date for r in rows], d) - 1
    row = rows[0] if idx < 0 else rows[idx]
    return row.price_factor * row.split_factor


def extract_day_anchors(
    bars_by_day: Mapping[date, Sequence[TradeBar]],
    windows: Mapping[date, tuple[int, int]],
) -> list[DayAnchors]:
    """Reduce each day's minute bars to the five anchor prices.

    ``windows`` maps trading date → ``(session_open_ms_utc,
    session_close_ms_utc)`` from the canonical calendar (callers build it
    via ``trading_calendar.session_windows_ms_utc``); half-day closes are
    whatever the calendar says, never a 16:00 literal.

    Bars within a day must arrive chronologically (the LEAN zip contract);
    a non-monotonic day raises rather than being silently reordered.
    """
    anchors: list[DayAnchors] = []
    for trading_date in sorted(bars_by_day):
        bars = list(bars_by_day[trading_date])
        if not bars:
            continue
        window = windows.get(trading_date)
        if window is None:
            # A zip without a scheduled session is lake corruption, not a
            # trading day; refuse it rather than guessing boundaries.
            raise ValueError(f"no scheduled NYSE session for bar date {trading_date.isoformat()}")
        open_ms, close_ms = window
        noon_ms = noon_et_ms_utc(trading_date)

        prev_start = -1
        rth_open: Decimal | None = None
        rth_close: Decimal | None = None
        noon_boundary_close: Decimal | None = None
        for bar in bars:
            if bar.start_ms <= prev_start:
                raise NonMonotonicBarError(
                    f"bars for {trading_date.isoformat()} are not strictly chronological"
                )
            prev_start = bar.start_ms
            if open_ms <= bar.start_ms < close_ms:
                if rth_open is None:
                    rth_open = bar.open
                rth_close = bar.close
                if bar.start_ms < noon_ms:
                    noon_boundary_close = bar.close

        anchors.append(
            DayAnchors(
                trading_date=trading_date,
                session_open_ms_utc=open_ms,
                first_open=bars[0].open,
                rth_open=rth_open,
                noon_boundary_close=noon_boundary_close,
                rth_close=rth_close,
                last_close=bars[-1].close,
                volume=sum(bar.volume for bar in bars),
                has_pre_market=bars[0].start_ms < open_ms,
                has_after_hours=bars[-1].start_ms >= close_ms,
            )
        )
    return anchors


def adjust_anchors(
    anchors: Sequence[DayAnchors],
    factor_rows: Sequence[FactorRow],
) -> list[DayAnchors]:
    """Apply the LEAN cumulative factor (splits + dividends) to every anchor.

    Each day's anchors are scaled by that day's multiplier; ratios *within*
    a day are unchanged and ratios *across* an ex-date pick up exactly the
    corporate action, which is what makes the return series total-return
    consistent. Empty ``factor_rows`` returns the anchors unchanged.
    """
    if not factor_rows:
        return list(anchors)
    out: list[DayAnchors] = []
    for a in anchors:
        m = factor_multiplier_as_of(factor_rows, a.trading_date)
        if m == Decimal(1):
            out.append(a)
            continue
        out.append(
            DayAnchors(
                trading_date=a.trading_date,
                session_open_ms_utc=a.session_open_ms_utc,
                first_open=a.first_open * m,
                rth_open=None if a.rth_open is None else a.rth_open * m,
                noon_boundary_close=None
                if a.noon_boundary_close is None
                else a.noon_boundary_close * m,
                rth_close=None if a.rth_close is None else a.rth_close * m,
                last_close=a.last_close * m,
                volume=a.volume,
                has_pre_market=a.has_pre_market,
                has_after_hours=a.has_after_hours,
            )
        )
    return out


def _pct(log_return: float) -> float:
    """Log return → simple percent. The single display-conversion point."""
    return math.expm1(log_return) * 100.0


def _ln_later_over_earlier(later: Decimal, earlier: Decimal) -> float:
    return math.log(float(later) / float(earlier))


def compute_daily_returns(anchors: Sequence[DayAnchors]) -> list[DailyReturns]:
    """Turn adjusted anchors into per-day returns (simple percent).

    Days whose own RTH anchors are missing are skipped entirely (counted by
    the caller as excluded sessions); the first day in the sequence has no
    previous close, so its ``close_to_close_pct``/``overnight_pct`` are
    ``None`` while its session/segment fields are still populated.
    """
    out: list[DailyReturns] = []
    prev: DayAnchors | None = None
    for a in anchors:
        if a.rth_open is None or a.rth_close is None:
            # A skipped day still hands its close to the chain when it has
            # one (an openless day is pathological but its last RTH price is
            # a usable previous close); with neither anchor the chain keeps
            # the last usable close rather than resetting — an RTH-less day
            # must not erase the return across it.
            if a.rth_close is not None:
                prev = a
            continue

        session_log = _ln_later_over_earlier(a.rth_close, a.rth_open)
        if a.noon_boundary_close is not None:
            morning_log = _ln_later_over_earlier(a.noon_boundary_close, a.rth_open)
            # Constructed by subtraction so morning + afternoon == session
            # exactly in float space (see module docstring).
            afternoon_log = session_log - morning_log
        else:
            morning_log = None
            afternoon_log = session_log

        if a.has_pre_market:
            pre_market_log = _ln_later_over_earlier(a.rth_open, a.first_open)
        else:
            pre_market_log = None
        if a.has_after_hours:
            after_hours_log = _ln_later_over_earlier(a.last_close, a.rth_close)
        else:
            after_hours_log = None

        if prev is not None and prev.rth_close is not None:
            close_to_close_log = _ln_later_over_earlier(a.rth_close, prev.rth_close)
            overnight_log = _ln_later_over_earlier(a.rth_open, prev.rth_close)
        else:
            close_to_close_log = None
            overnight_log = None

        out.append(
            DailyReturns(
                trading_date=a.trading_date,
                session_open_ms_utc=a.session_open_ms_utc,
                close_to_close_pct=None if close_to_close_log is None else _pct(close_to_close_log),
                session_pct=_pct(session_log),
                overnight_pct=None if overnight_log is None else _pct(overnight_log),
                pre_market_pct=None if pre_market_log is None else _pct(pre_market_log),
                morning_pct=None if morning_log is None else _pct(morning_log),
                afternoon_pct=None if afternoon_log is None else _pct(afternoon_log),
                after_hours_pct=None if after_hours_log is None else _pct(after_hours_log),
                volume=a.volume,
            )
        )
        prev = a
    return out


def compute_histogram(
    values_pct: Sequence[float],
    *,
    bin_width_pct: float = DEFAULT_BIN_WIDTH_PCT,
    span_pct: float = DEFAULT_SPAN_PCT,
) -> Histogram:
    """Count ``values_pct`` into fixed-width bins plus open edge bins.

    Inner edges are multiples of ``bin_width_pct`` covering ``[−span,
    +span]``; membership is ``[lo, hi)`` with the left edge bin taking
    ``v < −span`` and the right edge bin ``v ≥ +span``.
    """
    if bin_width_pct <= 0:
        raise ValueError(f"bin_width_pct must be positive, got {bin_width_pct}")
    if span_pct <= 0:
        raise ValueError(f"span_pct must be positive, got {span_pct}")
    span_over_width = span_pct / bin_width_pct
    if not math.isclose(span_over_width, round(span_over_width), rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(
            f"span_pct ({span_pct}) must be an integer multiple of bin_width_pct ({bin_width_pct})"
        )

    step = round(span_over_width)
    edges = [-span_pct + i * bin_width_pct for i in range(2 * step + 1)]
    counts = [0] * (len(edges) - 1)
    left_edge = 0
    right_edge = 0

    for v in values_pct:
        if v < -span_pct:
            left_edge += 1
        elif v >= span_pct:
            right_edge += 1
        else:
            # Values in (-span, span): the bin whose [lo, hi) contains v.
            # Scale by 1/bin_width in float can straddle a boundary by one
            # ulp, so locate by comparison against the edge list instead.
            idx = 0
            for edge in edges[1:]:
                if v < edge:
                    break
                idx += 1
            counts[idx] += 1

    bins = [
        HistogramBin(lower_pct=edges[i], upper_pct=edges[i + 1], count=counts[i], is_edge=False)
        for i in range(len(counts))
    ]
    bins.insert(0, HistogramBin(lower_pct=None, upper_pct=-span_pct, count=left_edge, is_edge=True))
    bins.append(HistogramBin(lower_pct=span_pct, upper_pct=None, count=right_edge, is_edge=True))
    return Histogram(
        bin_width_pct=bin_width_pct,
        span_pct=span_pct,
        bins=tuple(bins),
    )


def _sample_std(values: Sequence[float]) -> float:
    n = len(values)
    m = math.fsum(values) / n
    return math.sqrt(math.fsum((v - m) ** 2 for v in values) / (n - 1))


def compute_distribution_stats(
    values_pct: Sequence[float],
    dates: Sequence[date],
    session_open_ms: Sequence[int],
) -> DistributionStats:
    """Sample statistics of a daily-return series (simple-percent units).

    Estimators: mean; std with ddof=1; adjusted Fisher-Pearson skew G1 and
    bias-corrected Fisher excess kurtosis G2 (the scipy ``bias=False``
    definitions — see module provenance); VaR-95 as the 5th percentile with
    linear interpolation (numpy's default method); CVaR-95 as the mean of
    the values at or below VaR-95.
    """
    if len(values_pct) != len(dates) or len(values_pct) != len(session_open_ms):
        raise ValueError("values, dates, and session anchors must have equal length")
    n = len(values_pct)
    if n < 3:
        raise ValueError(f"need at least 3 returns for distribution statistics, got {n}")

    mean = math.fsum(values_pct) / n
    std = _sample_std(values_pct)

    # G1 and G2 in their sample-standardized forms: z_i = (x_i − mean)/std
    # with the ddof=1 std, exactly as scipy evaluates bias=False on the
    # same inputs (scipy.stats.skew/kurtosis reduce to these closed forms).
    z3 = math.fsum(((v - mean) / std) ** 3 for v in values_pct)
    z4 = math.fsum(((v - mean) / std) ** 4 for v in values_pct)
    skewness = (n / ((n - 1) * (n - 2))) * z3
    excess_kurtosis = (
        (n * (n + 1)) / ((n - 1) * (n - 2) * (n - 3)) * z4
        - (3 * (n - 1) ** 2) / ((n - 2) * (n - 3))
    )

    ordered = sorted(values_pct)
    var_95 = _percentile_linear(ordered, 5.0)
    tail = [v for v in ordered if v <= var_95]
    cvar_95 = math.fsum(tail) / len(tail) if tail else var_95

    best_i = max(range(n), key=lambda i: values_pct[i])
    worst_i = min(range(n), key=lambda i: values_pct[i])
    return DistributionStats(
        n_days=n,
        mean_pct=mean,
        std_pct=std,
        annualized_vol_pct=std * math.sqrt(TRADING_DAYS_PER_YEAR),
        skewness=skewness,
        excess_kurtosis=excess_kurtosis,
        var_95_pct=var_95,
        cvar_95_pct=cvar_95,
        best_day=ExtremeDay(
            trading_date=dates[best_i],
            session_open_ms_utc=session_open_ms[best_i],
            value_pct=values_pct[best_i],
        ),
        worst_day=ExtremeDay(
            trading_date=dates[worst_i],
            session_open_ms_utc=session_open_ms[worst_i],
            value_pct=values_pct[worst_i],
        ),
    )


def _percentile_linear(ordered: Sequence[float], q: float) -> float:
    """q-th percentile of an ascending sequence, numpy's ``method="linear"``:
    rank h = (n − 1) · q/100 between the two surrounding order statistics."""
    if not ordered:
        raise ValueError("percentile of an empty series")
    if len(ordered) == 1:
        return ordered[0]
    h = (len(ordered) - 1) * q / 100.0
    lo = math.floor(h)
    hi = math.ceil(h)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (h - lo) * (ordered[hi] - ordered[lo])


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def normal_expected_counts(
    histogram: Histogram,
    mean_pct: float,
    std_pct: float,
) -> tuple[float, ...]:
    """Expected bin counts under Normal(mean_pct, std_pct) fitted to the
    same series — the bell-curve overlay. Each inner bin gets
    ``N · (Φ(hi) − Φ(lo))``; edge bins take the open tails."""
    n = histogram.total_count
    if std_pct <= 0:
        return (0.0,) * len(histogram.bins)
    out: list[float] = []
    for b in histogram.bins:
        p_lo = 0.0 if b.lower_pct is None else _normal_cdf((b.lower_pct - mean_pct) / std_pct)
        p_hi = 1.0 if b.upper_pct is None else _normal_cdf((b.upper_pct - mean_pct) / std_pct)
        out.append(n * (p_hi - p_lo))
    return tuple(out)


def _values_for_kind(days: Sequence[DailyReturns], kind: ReturnKind) -> tuple[list[float], list[date], list[int], int]:
    """The non-None series for one kind, plus how many days were excluded."""
    values: list[float] = []
    dates: list[date] = []
    anchors: list[int] = []
    excluded = 0
    for d in days:
        v: float | None
        if kind == "close_to_close":
            v = d.close_to_close_pct
        elif kind == "session":
            v = d.session_pct
        else:
            v = d.overnight_pct
        if v is None:
            excluded += 1
        else:
            values.append(v)
            dates.append(d.trading_date)
            anchors.append(d.session_open_ms_utc)
    return values, dates, anchors, excluded


def build_return_distribution(
    days: Sequence[DailyReturns],
    *,
    bin_width_pct: float = DEFAULT_BIN_WIDTH_PCT,
    span_pct: float = DEFAULT_SPAN_PCT,
    adjustment: Literal["split_and_dividend", "raw"] = "split_and_dividend",
) -> ReturnDistributionResult:
    """Histogram + stats + overlay for all three return kinds over ``days``."""
    kinds: list[KindDistribution] = []
    for kind in RETURN_KINDS:
        values, dates, anchors, _excluded = _values_for_kind(days, kind)
        histogram = compute_histogram(values, bin_width_pct=bin_width_pct, span_pct=span_pct)
        stats = compute_distribution_stats(values, dates, anchors)
        kinds.append(
            KindDistribution(
                kind=kind,
                histogram=histogram,
                stats=stats,
                normal_expected_counts=normal_expected_counts(
                    histogram, stats.mean_pct, stats.std_pct
                ),
            )
        )
    return ReturnDistributionResult(
        kinds=tuple(kinds),
        days=tuple(days),
        bin_width_pct=bin_width_pct,
        span_pct=span_pct,
        adjustment=adjustment,
    )
