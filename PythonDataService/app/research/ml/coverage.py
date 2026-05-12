"""Bar-clock coverage check.

The engine only evaluates strategy logic when ``TradeBarConsolidator``
emits a bar — never on a wall-clock grid. So predictions are required
for every emitted bar, not for every minute between start and end.

This helper sits at the run-pipeline boundary (where the data source
and consolidator are known) and asserts the loaded prediction set
covers every bar the engine will see. Predictions for bars the engine
won't see (e.g. predictions written for a 24x7 calendar grid against a
market-hours stream) are allowed — they're a superset, not a mismatch.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections.abc import Iterable, Iterator
from datetime import date as Date
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from app.engine.consolidators.trade_bar_consolidator import TradeBarConsolidator
from app.engine.data.trade_bar import TradeBar
from app.research.ml.loader import PredictionCoverageError, PredictionSet
from app.utils.timestamps import to_ms_utc

if TYPE_CHECKING:
    from app.engine.strategy.spec.schema import PredictionRef

logger = logging.getLogger(__name__)


class _BarLike(Protocol):
    """Anything with an ``end_time: datetime`` (TradeBar duck-types fine)."""

    @property
    def end_time(self): ...


def assert_bar_clock_coverage(
    prediction_set: PredictionSet,
    bar_stream: Iterable[_BarLike],
    *,
    refs: Iterable[PredictionRef],
) -> None:
    """Raise ``PredictionCoverageError`` if any fired bar lacks a matching
    prediction under any declared ``PredictionRef``'s ``lookup`` mode.

    For each ``ref`` and each fired bar's ``end_time_ms``:

    - ``ref.lookup == "exact_bar_close"``: ``prediction_set.index`` must
      contain the bar's ``end_time_ms``, AND that row must contain
      ``ref.field``.
    - ``ref.lookup == "next_after_bar_close"``: there must be a row whose
      timestamp is strictly greater than the bar's ``end_time_ms``, AND
      that row must contain ``ref.field``.

    Raises on the first violation. The error message names ``ref.id``,
    ``ref.lookup``, the fired ``ts_ms``, and ``ref.field``; for the
    next_after-row-missing-field case, also names the matched next-row's
    ``ts_ms`` so the offending prediction row can be located in the
    artifact.

    ``bar_stream`` must be the bars the run will actually evaluate —
    typically obtained by running the data source through the same
    ``TradeBarConsolidator`` configuration the engine will use.
    Iterating consumes the stream once.

    ``refs`` is the spec's ``predictions`` list. A spec may mix lookup
    modes across refs; each ref is validated independently.
    """
    bars: list[_BarLike] = list(bar_stream)
    fired_ms: list[int] = [to_ms_utc(bar.end_time) for bar in bars]
    have_ms: set[int] = set(prediction_set.index.keys())
    sorted_have: list[int] = prediction_set._sorted_ts

    refs_list = list(refs)
    if not refs_list:
        return

    extra = have_ms - set(fired_ms)
    if extra:
        logger.info(
            "[ML] prediction_set %s has %d predictions for bars the engine will not evaluate; "
            "this is allowed but suggests the artifact and run window may be misaligned",
            prediction_set.manifest.prediction_set_id,
            len(extra),
        )

    for ref in refs_list:
        if ref.lookup == "exact_bar_close":
            for fired in fired_ms:
                if fired not in have_ms:
                    raise PredictionCoverageError(
                        f"ref {ref.id!r} (exact_bar_close): no prediction row "
                        f"at fired bar ts_ms={fired}; field={ref.field!r}"
                    )
                row = prediction_set.index[fired]
                if ref.field not in row:
                    raise PredictionCoverageError(
                        f"ref {ref.id!r} (exact_bar_close): row at fired bar "
                        f"ts_ms={fired} is missing field {ref.field!r} "
                        f"(available: {sorted(row)})"
                    )
        elif ref.lookup == "next_after_bar_close":
            for fired in fired_ms:
                i = bisect_right(sorted_have, fired)
                if i == len(sorted_have):
                    raise PredictionCoverageError(
                        f"ref {ref.id!r} (next_after_bar_close): fired bar "
                        f"ts_ms={fired} has no prediction row strictly after; "
                        f"field={ref.field!r}"
                    )
                matched_ts = sorted_have[i]
                row = prediction_set.index[matched_ts]
                if ref.field not in row:
                    raise PredictionCoverageError(
                        f"ref {ref.id!r} (next_after_bar_close): fired bar "
                        f"ts_ms={fired} matched next row at ts_ms={matched_ts} "
                        f"but it is missing field {ref.field!r} "
                        f"(available: {sorted(row)})"
                    )
        else:
            raise PredictionCoverageError(
                f"ref {ref.id!r}: unknown lookup mode {ref.lookup!r}; coverage.py needs an updated branch"
            )


def iter_consolidated_bars(
    data_source,
    *,
    symbol: str,
    start_date: Date,
    end_date: Date,
    resolution_minutes: int,
) -> Iterator[TradeBar]:
    """Yield consolidated bars the engine will see for a run, in order.

    Drives a ``TradeBarConsolidator`` configured for ``resolution_minutes``
    over the data source's minute bars — same configuration the engine
    uses internally. The result is a forward-only iterator of the bars
    the engine will fire ``on_bar`` for, ready to feed into
    ``assert_bar_clock_coverage``.

    The engine itself iterates the same ``data_source.iter_bars(...)``
    independently; both ``LeanMinuteDataReader.iter_bars`` and the
    in-memory ``FakeDataReader`` return a fresh iterator on each call,
    so harvesting the consolidated stream here does not consume the
    minute stream the engine will later iterate.

    Note: ``TradeBarConsolidator`` does not emit a partial trailing bar
    by default (see its ``scan`` docstring). The engine matches that
    convention, so we don't call ``scan`` here either — the prediction
    set must cover the same set the engine will actually evaluate.

    Contract on ``data_source``:
        ``data_source.iter_bars(symbol, start_date, end_date)`` MUST return
        a fresh iterator on every call. The bar-clock coverage check
        consumes the stream once here; the runner / engine then iterates it
        again for the actual backtest. If a data source caches a single
        iterator and reuses it, the second iteration sees an exhausted
        stream and the engine silently produces no bars. Every concrete
        reader in the repo (``LeanMinuteDataReader``, the fake test reader)
        is a generator-returning method and satisfies this contract.
    """
    consolidator = TradeBarConsolidator(timedelta(minutes=resolution_minutes))
    fired: list[TradeBar] = []
    consolidator.on_data_consolidated = fired.append
    for minute_bar in data_source.iter_bars(symbol, start_date, end_date):
        consolidator.update(minute_bar)
        # Drain anything fired so we yield in causal order; the
        # consolidator only fires at most one bar per ``update`` call.
        while fired:
            yield fired.pop(0)
