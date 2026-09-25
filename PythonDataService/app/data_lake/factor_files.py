"""LEAN factor-file CSV builder.

Spec: docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md § 5.1

LEAN factor-file format (factor_files/<sym>.csv under the equity/usa subtree):
  date,price_factor,split_factor,reference_price
  - date: YYYYMMDD of the last completed trading session *before* the
    corporate action's ex-date. LEAN applies the event on the next
    trading day, so a row dated D produces an event on D's successor.
  - price_factor: cumulative dividend back-adjustment multiplier.
  - split_factor: cumulative split-adjustment multiplier.
  - reference_price: the raw close on the row's date.

The reference price is NOT optional and must be positive. LEAN's
``DividendEventProvider`` divides the cash dividend by it; a zero or
missing reference price raises ``InvalidOperationException: Zero
reference price``, which kills the subscription worker and silently
truncates the backtest at the first in-window dividend. An earlier
revision of this module emitted ``reference_price=0`` on every row
under the (false) belief that LEAN ignores the column — see the
cross-engine parity-matrix incident where a 6-month SPY backtest ran
only ~35 days.

Math mirrors LEAN's own ``FactorFileGenerator``: walking corporate
actions newest-to-oldest,

  reference_price = close of the trading session before the ex-date
  price_factor    = next_factor * (1 - cash_amount * split_factor / reference_price)

and each split contributes ``split_from / split_to`` to the cumulative
split factor.

The builder emits exactly the actions its :class:`FactorFilePlan` keeps
(those inside a covered span, below), between two anchor rows dated at
the first and last captured sessions. There is no second selection rule:
what the file holds is decided once, by :func:`plan_factor_file`, from
every captured session. A planned action without a positive reference
close fails the build loudly rather than emitting a poison row.

Coverage (#2452)
----------------
A factor file is built over the symbol's **captured sessions**, not over
whichever request happened to rebuild it last, and it records what it
covers in a *coverage record* beside the CSV (``<symbol>.coverage.json``,
:class:`FactorCoverageRecord`; LEAN never reads it). The record lists
**covered spans** — maximal runs of consecutive scheduled NYSE sessions
the lake held when the file was built — and the SHA-256 of the exact CSV
bytes it vouches for.

What a span promises, under the owner's latest-known policy (#2432:
backward adjustment with every corporate action known today): every
corporate action whose ex-date ``X`` satisfies ``first < X <= last`` is in
the file, priced against the close of the session immediately before
``X`` (which the span contains, so it was captured). For two bars ``p <= q``
inside one span, ``multiplier(p) / multiplier(q)`` is then exactly the
product of the actions between them — every return, gap, or price ratio
*within* a span is on the latest-known basis. An action outside every span
(after the last captured session, in a capture gap, or on a span's first
session) scales every bar of a span by the same factor, so it cancels from
every in-span ratio; omitting it is exact for ratios, and it could not be
priced anyway (its reference session was never captured).

One exception is inherited from the dividend formula above, not from
coverage: a dividend's factor multiplies its cash by the cumulative split
factor of the splits after it *in the file*, so a split in a later span
rescales an earlier span's dividend-day ratio. That is LEAN's ToolBox
generator verbatim, but QuantConnect's own factor files encode the raw/raw
ratio (``1 - cash / reference_close``; see
``tests/data_lake/test_factor_files_coverage.py::test_a_later_split_rescales_an_earlier_dividend_under_the_ported_formula``
for the AAPL evidence) — an open numerical question for the owner, pinned
there rather than silently changed here.

The one thing a span does **not** promise is the absolute adjusted level:
bars are on the basis of the last covered session, not of today, and a
split in a capture gap separates the levels of the spans on either side of
it. So no reader presents adjusted levels from this file: the return study
reads only ratios, and its day-candle pane shows raw prices. Moving levels
to a "today" basis needs captured closes through today and a versioned
action list — #2454's territory, not this module's.

A reference session whose close cannot be read (no regular-session bar,
or an unreadable minute zip) ends its span there
(``plan_factor_file(..., unpriced_sessions=...)``): the action it would
price then falls on the next span's first session, outside every span, so
only reads crossing that session are refused — one bad day does not
leave the whole symbol unadjustable.

A consumer **covers** a set of sessions when every run of
scheduled-adjacent sessions in it lies inside one span
(:func:`read_covering_factor_rows`, the single check every adjusted reader
runs). A file with no record, an unreadable record, or a record bound to
other bytes covers nothing — it is never read as covering everything.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data_lake.path_policy import LeanFactorFilePath
from app.data_lake.polygon_corp_actions import DividendEvent, SplitEvent
from app.data_lake.types import trading_date_at_ms
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.utils.session_anchors import MAX_TIMESTAMP_MS

_FACTOR_QUANTUM = Decimal("0.0000000001")


class FactorFileBuildError(ValueError):
    """The captured sessions cannot produce a factor file (a named refusal, not a crash)."""


class FactorFileReferenceError(FactorFileBuildError):
    """A planned corporate action has no positive reference close."""


def build_factor_file_bytes(
    symbol: str,
    plan: FactorFilePlan,
    daily_closes: Mapping[date, Decimal],
) -> bytes:
    """Build the deterministic factor-file CSV body for ``plan``.

    ``daily_closes`` maps session dates to their regular-trading-hours
    close (the caller derives it from the captured minute bars, see
    ``derived_daily.factor_file_reference_closes``). It must hold every
    ``plan.reference_sessions`` close; the anchor sessions' closes are used
    when present. The returned bytes are ASCII CSV without a header row,
    which is what LEAN expects.

    Raises ``FactorFileReferenceError`` when a reference session has no
    close, or a non-positive one. A missing close is refused rather than
    looked up on an older session: that would bind the action to the wrong
    reference price and drift silently.
    """
    missing = [d for d in plan.reference_sessions if d not in daily_closes]
    if missing:
        raise FactorFileReferenceError(
            f"{symbol}: no regular-session close captured for "
            f"{', '.join(d.isoformat() for d in missing)}, the session(s) before a corporate "
            "action's ex-date; its reference price cannot be established"
        )
    # Only the reference and anchor sessions' closes: every reference
    # session is present and is the scheduled session right before its
    # ex-date, so the largest close before an ex-date is exactly it.
    wanted = {*plan.reference_sessions, *plan.anchor_sessions}
    closes: dict[date, Decimal] = {d: Decimal(v) for d, v in daily_closes.items() if d in wanted}
    session_dates: list[date] = sorted(closes)

    # Walk corporate actions newest-to-oldest, accumulating the cumulative
    # price/split factors. Each event row carries the factors that apply
    # to raw data up to and including that row's date.
    price_factor = Decimal(1)
    split_factor = Decimal(1)
    event_rows: list[tuple[date, Decimal, Decimal, Decimal]] = []
    for ev in reversed(_merge_events(plan.splits, plan.dividends)):
        row_date = session_dates[bisect_left(session_dates, _event_date(ev)) - 1]
        reference_price = closes[row_date]
        if reference_price <= 0:
            raise FactorFileReferenceError(
                f"{symbol}: reference close on {row_date.isoformat()} is "
                f"{reference_price}; a non-positive reference price makes LEAN's "
                f"DividendEventProvider throw and truncates the backtest"
            )
        if isinstance(ev, DividendEvent):
            cash = Decimal(str(ev.cash_amount))
            price_factor = price_factor * (Decimal(1) - cash * split_factor / reference_price)
        else:
            split_factor = split_factor * (Decimal(str(ev.split_from)) / Decimal(str(ev.split_to)))
        event_rows.append((row_date, price_factor, split_factor, reference_price))
    event_rows.reverse()

    # The first anchor carries the fully-cumulated (oldest) factors; the row
    # is not dividend-processed by LEAN but its reference price still
    # must be positive, so anchor it to the nearest available close.
    first_anchor, last_anchor = plan.anchor_sessions
    rows: list[tuple[date, Decimal, Decimal, Decimal]] = [
        (
            first_anchor,
            price_factor,
            split_factor,
            _anchor_reference(first_anchor, closes, session_dates),
        ),
        *event_rows,
        (
            last_anchor,
            Decimal(1),
            Decimal(1),
            _anchor_reference(last_anchor, closes, session_dates),
        ),
    ]

    body = "\n".join(f"{_yyyymmdd(d)},{_fmt_factor(pf)},{_fmt_factor(sf)},{_fmt_price(rp)}" for d, pf, sf, rp in rows)
    return (body + "\n").encode("ascii")


def _event_date(ev: SplitEvent | DividendEvent) -> date:
    raw = ev.execution_date if isinstance(ev, SplitEvent) else ev.ex_dividend_date
    return date.fromisoformat(raw)


def _merge_events(
    splits: Sequence[SplitEvent], dividends: Sequence[DividendEvent]
) -> list[SplitEvent | DividendEvent]:
    """Merge splits + dividends into one chronologically-sorted list."""
    return sorted([*splits, *dividends], key=_event_date)


def _anchor_reference(d: date, closes: Mapping[date, Decimal], session_dates: list[date]) -> Decimal:
    """Reference close for an anchor row: the close on ``d``, else the
    nearest earlier session, else the earliest session available."""
    if d in closes:
        return closes[d]
    if not session_dates:
        raise FactorFileReferenceError(
            "no regular-session close was read for any anchor or reference session; "
            "cannot anchor the factor file with a reference price"
        )
    idx = bisect_left(session_dates, d)
    return closes[session_dates[idx - 1]] if idx > 0 else closes[session_dates[0]]


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _fmt_factor(x: Decimal) -> str:
    """Factor formatted at 10 dp, trailing zeros stripped, fixed notation."""
    return format(x.quantize(_FACTOR_QUANTUM).normalize(), "f")


def _fmt_price(x: Decimal) -> str:
    """Reference price in fixed (non-scientific) notation."""
    return format(x.normalize(), "f")


# ---------------------------------------------------------------------------
# Read side: parsing the CSV and resolving the LEAN as-of lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactorRow:
    """One LEAN factor-file row: factors in effect for data up to ``row_date``.

    A row dated D covers data ≤ D back to the previous row's date (the
    builder dates each row at the last completed session *before* the
    corporate action's ex-date, so the event lands on D's successor).
    """

    row_date: date
    price_factor: Decimal
    split_factor: Decimal


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
        except (ValueError, ArithmeticError) as exc:
            # ArithmeticError covers decimal.InvalidOperation (Decimal("abc")),
            # which is not a ValueError — without it a garbage factor column
            # escapes the documented malformed-row contract.
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
    in effect for data dated ``d``, per LEAN's own application.

    LEAN's ``CorporateFactorProvider.GetScalingFactors`` (QuantConnect/Lean,
    ``Common/Data/Auxiliary/CorporateFactorProvider.cs``) walks the factor
    file newest-to-oldest and keeps the last row whose date is ≥ the search
    date — i.e. the **earliest row dated on or after** ``d`` wins, and a
    search date newer than every row resolves to the identity. Because a
    row dated D covers data ≤ D, this lookup returns that row for every bar
    in ``(previous_row_date, D]``; data before the first row uses the first
    row's factors (the file's factors are cumulative from file start).

    An empty file is the identity multiplier — the honest reading of "no
    corporate actions in the capture window".
    """
    if not rows:
        return Decimal(1)
    idx = bisect_left([r.row_date for r in rows], d)
    if idx >= len(rows):
        return Decimal(1)
    row = rows[idx]
    return row.price_factor * row.split_factor


# ---------------------------------------------------------------------------
# Coverage (#2452): what a factor file covers, and the one check readers run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRun:
    """``first_session..last_session``: consecutive scheduled NYSE sessions.

    A factor file's *covered spans* are runs whose corporate actions it
    holds in full (see the module docstring for the exact promise); a
    consumer's reads are grouped into runs to be checked against them.
    """

    first_session: date
    last_session: date

    def contains(self, first: date, last: date) -> bool:
        return self.first_session <= first and last <= self.last_session


def _scheduled_runs(sessions: Iterable[date]) -> list[SessionRun]:
    """Group dates into maximal runs of scheduled-adjacent NYSE sessions.

    Adjacency is the canonical calendar's, so a weekend or holiday never
    breaks a run and a missing session always does. A date the calendar
    does not schedule stands alone (it cannot be adjacent to anything).
    """
    ordered = sorted(set(sessions))
    if not ordered:
        return []
    index = {d: i for i, d in enumerate(expected_sessions(ordered[0], ordered[-1]))}
    runs: list[SessionRun] = []
    first = previous = ordered[0]
    for current in ordered[1:]:
        adjacent = current in index and previous in index and index[current] == index[previous] + 1
        if not adjacent:
            runs.append(SessionRun(first, previous))
            first = current
        previous = current
    runs.append(SessionRun(first, previous))
    return runs


def _break_after(runs: list[SessionRun], breaks: set[date], calendar: list[date]) -> list[SessionRun]:
    """Split each run right after every session of ``breaks`` inside it.

    ``calendar`` is the scheduled sessions over the runs, so the session
    after a break is the next run's first.
    """
    position = {d: i for i, d in enumerate(calendar)}
    out: list[SessionRun] = []
    for run in runs:
        first = run.first_session
        for session in sorted(d for d in breaks if run.first_session <= d < run.last_session):
            out.append(SessionRun(first, session))
            first = calendar[position[session] + 1]
        out.append(SessionRun(first, run.last_session))
    return out


@dataclass(frozen=True)
class FactorFilePlan:
    """What a factor file built over a set of captured sessions contains.

    ``spans`` are the captured sessions' maximal scheduled runs, broken
    after any unpriced session; ``splits``/``dividends`` are exactly the
    actions inside a span (``first < ex-date <= last``);
    ``reference_sessions`` are the sessions before those ex-dates, whose
    regular-session closes price them — every one of them is captured by
    construction, and one whose close cannot be read is re-planned as
    unpriced rather than handed to the build. ``anchor_sessions`` are the
    first and last captured sessions, whose closes the two anchor rows
    carry when available (LEAN does not dividend-process an anchor row).
    """

    spans: tuple[SessionRun, ...]
    splits: tuple[SplitEvent, ...]
    dividends: tuple[DividendEvent, ...]
    reference_sessions: tuple[date, ...]
    anchor_sessions: tuple[date, date]


def plan_factor_file(
    captured_sessions: Iterable[date],
    splits: Sequence[SplitEvent],
    dividends: Sequence[DividendEvent],
    *,
    unpriced_sessions: Iterable[date] = (),
) -> FactorFilePlan:
    """Decide what a factor file over ``captured_sessions`` holds.

    Pure: the caller supplies the sessions the lake holds for the symbol
    (all of them — never a request window, which is how #2452's narrow
    rebuild dropped an older split) and every corporate action the provider
    knows, and reads the returned ``reference_sessions`` closes.

    ``unpriced_sessions`` are captured sessions whose close could not be
    read. Each ends its span, so the action it would have priced falls
    outside every span (see the module docstring); the caller re-plans with
    the reference sessions it found no close for. Raises
    :class:`FactorFileBuildError` when no captured date is a scheduled
    session.
    """
    captured = sorted(set(captured_sessions))
    if not captured:
        raise FactorFileBuildError("a factor file needs at least one captured session")
    calendar = expected_sessions(captured[0], captured[-1])
    scheduled = set(calendar)
    spans = tuple(
        _break_after(_scheduled_runs(d for d in captured if d in scheduled), set(unpriced_sessions), calendar)
    )
    if not spans:
        raise FactorFileBuildError("none of the captured dates is a scheduled NYSE session")

    def inside_a_span(event: SplitEvent | DividendEvent) -> bool:
        ex_date = _event_date(event)
        return any(span.first_session < ex_date <= span.last_session for span in spans)

    kept_splits = tuple(s for s in splits if inside_a_span(s))
    kept_dividends = tuple(d for d in dividends if inside_a_span(d))
    reference_sessions = tuple(
        sorted({calendar[bisect_left(calendar, _event_date(ev)) - 1] for ev in (*kept_splits, *kept_dividends)})
    )
    return FactorFilePlan(
        spans=spans,
        splits=kept_splits,
        dividends=kept_dividends,
        reference_sessions=reference_sessions,
        anchor_sessions=(spans[0].first_session, spans[-1].last_session),
    )


class _CoveredSpanRecord(BaseModel):
    """One span on disk: its first and last sessions as int64 ms UTC,
    anchored at each session's 09:30 ET open (the lake's trading-date
    convention, ``app/data_lake/types.py::trading_date_at_ms``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    first_session_open_ms_utc: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    last_session_open_ms_utc: int = Field(ge=0, le=MAX_TIMESTAMP_MS)

    def to_span(self) -> SessionRun:
        return SessionRun(
            trading_date_at_ms(self.first_session_open_ms_utc),
            trading_date_at_ms(self.last_session_open_ms_utc),
        )


class FactorCoverageRecord(BaseModel):
    """The exact, closed shape of ``<symbol>.coverage.json``.

    ``factor_file_sha256`` binds the record to one set of CSV bytes: a CSV
    replaced without its record (a torn publish, a hand edit, a writer
    that predates #2452) no longer matches, and so covers nothing.
    ``schema_version`` is the seam #2454 extends with the corporate-action
    version the file was built against.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    symbol: str = Field(min_length=1)
    factor_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    covered_spans: list[_CoveredSpanRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def _spans_are_ordered_and_disjoint(self) -> FactorCoverageRecord:
        bounds = [(s.first_session_open_ms_utc, s.last_session_open_ms_utc) for s in self.covered_spans]
        if any(first > last for first, last in bounds):
            raise ValueError("a covered span ends before it starts")
        if any(bounds[i][1] >= bounds[i + 1][0] for i in range(len(bounds) - 1)):
            raise ValueError("covered spans must be ascending and disjoint")
        return self


def factor_coverage_record_bytes(symbol: str, factor_file: bytes, spans: Sequence[SessionRun]) -> bytes:
    """Serialize the coverage record for ``factor_file``'s exact bytes."""
    record = FactorCoverageRecord(
        symbol=symbol,
        factor_file_sha256=hashlib.sha256(factor_file).hexdigest(),
        covered_spans=[
            _CoveredSpanRecord(
                first_session_open_ms_utc=session_open_ms_utc(span.first_session),
                last_session_open_ms_utc=session_open_ms_utc(span.last_session),
            )
            for span in spans
        ],
    )
    return (record.model_dump_json() + "\n").encode("ascii")


class FactorFileNotCoveringError(Exception):
    """The lake's factor file cannot vouch for a split/dividend-adjusted read.

    ``reason`` is operator-facing: it names what is missing (no file, no
    record, a record bound to other bytes, or the sessions outside every
    covered span).
    """

    def __init__(self, symbol: str, reason: str) -> None:
        super().__init__(f"{symbol}: {reason}")
        self.symbol = symbol
        self.reason = reason


@dataclass(frozen=True)
class RecordedFactorFile:
    """A factor file whose coverage record matches its bytes on disk."""

    rows: list[FactorRow]
    spans: tuple[SessionRun, ...]
    file_sha256: str


def read_recorded_factor_file(lake_root: Path, *, market: str, symbol: str) -> RecordedFactorFile:
    """The symbol's factor file, only if its coverage record vouches for its exact bytes.

    Raises :class:`FactorFileNotCoveringError` for a missing file, a missing
    or unreadable record, or a record bound to different bytes — each of
    those covers nothing. A CSV the record vouches for that does not parse
    is lake corruption and raises ``ValueError`` from :func:`parse_factor_file`.
    """
    paths = LeanFactorFilePath(market=market, symbol=symbol)
    csv_path = lake_root.joinpath(*paths.relative_path().parts)
    record_path = lake_root.joinpath(*paths.coverage_record_path().parts)
    if not csv_path.is_file():
        raise FactorFileNotCoveringError(symbol, "no factor file has been built for this symbol")
    if not record_path.is_file():
        raise FactorFileNotCoveringError(
            symbol,
            "the factor file carries no coverage record (it predates coverage recording), "
            "so it cannot vouch for any window",
        )
    try:
        record = FactorCoverageRecord.model_validate(json.loads(record_path.read_bytes()))
    except ValueError as exc:  # malformed JSON or UTF-8, or a schema violation
        raise FactorFileNotCoveringError(symbol, f"the factor file's coverage record is unreadable: {exc}") from exc
    csv_bytes = csv_path.read_bytes()
    file_sha256 = hashlib.sha256(csv_bytes).hexdigest()
    if record.symbol != symbol or record.factor_file_sha256 != file_sha256:
        raise FactorFileNotCoveringError(
            symbol, "the factor file's coverage record describes different bytes than the file on disk"
        )
    return RecordedFactorFile(
        rows=parse_factor_file(csv_bytes.decode("ascii")),
        spans=tuple(span.to_span() for span in record.covered_spans),
        file_sha256=file_sha256,
    )


def read_covering_factor_rows(
    lake_root: Path,
    *,
    market: str,
    symbol: str,
    sessions: Iterable[date],
) -> list[FactorRow]:
    """The factor rows for an adjusted read of ``sessions`` — the one coverage check.

    Every consumer that labels output split-and-dividend adjusted calls
    this with the sessions it adjusts. ``sessions`` are grouped into runs of
    scheduled-adjacent sessions (the only pairs any consumer compares
    across days); each run must lie inside one covered span, or this raises
    :class:`FactorFileNotCoveringError` naming the first run that does not.
    """
    recorded = read_recorded_factor_file(lake_root, market=market, symbol=symbol)
    uncovered = [
        run
        for run in _scheduled_runs(sessions)
        if not any(span.contains(run.first_session, run.last_session) for span in recorded.spans)
    ]
    if uncovered:
        first = uncovered[0]
        raise FactorFileNotCoveringError(
            symbol,
            f"its corporate actions cover {_render_spans(recorded.spans)}, which does not include "
            f"the sessions {first.first_session.isoformat()}..{first.last_session.isoformat()}"
            + (f" (and {len(uncovered) - 1} more uncovered run(s))" if len(uncovered) > 1 else ""),
        )
    return recorded.rows


_MAX_RENDERED_SPANS = 3


def _render_spans(spans: Sequence[SessionRun]) -> str:
    shown = ", ".join(f"{s.first_session.isoformat()}..{s.last_session.isoformat()}" for s in spans[:_MAX_RENDERED_SPANS])
    if len(spans) > _MAX_RENDERED_SPANS:
        return f"{shown}, and {len(spans) - _MAX_RENDERED_SPANS} more span(s)"
    return shown
