"""One page of the union of every registered store, exactly.

The first ``page * page_size`` records of the union are among the first ``page * page_size`` of each store, so every
store is asked for that many and the lists are merged in the shared order. That is the whole trick: a union built from
several independently truncated first pages is not a history, and this one is never built from them. The total is the
sum of what each store counted, so the pages add up to it.

A store that fails is named on the page, left out of the total and flagged ``partial``. Failing silently into an empty
list is the one thing this module exists not to do: when every queried store fails there is no page to give, and the
caller is told so.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import islice, pairwise

from app.research.history.contract import HistoryQuery, HistorySource, SourceRead, SourceUnavailableError
from app.schemas.research_history import (
    HistoryType,
    ResearchHistoryEntry,
    ResearchHistoryPage,
    ResearchHistorySource,
)

logger = logging.getLogger(__name__)

SOURCE_TIMEOUT_SECONDS = 20.0


class HistoryUnavailableError(Exception):
    """Every store that was asked failed, so there is no history to show, empty or otherwise."""

    def __init__(self, sources: list[ResearchHistorySource]) -> None:
        super().__init__("every queried research store failed")
        self.sources = sources


class _InconsistentSourceError(Exception):
    """A store's answer broke the contract the merge relies on."""


@dataclass(frozen=True, slots=True)
class _Outcome:
    source: HistorySource
    read: SourceRead | None
    error: str | None


def _check(source: HistorySource, answer: SourceRead, take: int) -> None:
    """Refuse an answer the merge cannot trust: the page would be wrong without any sign of it."""
    if any(entry.type != source.type for entry in answer.entries):
        raise _InconsistentSourceError(f"{source.type}: an entry of another type")
    if len(answer.entries) > min(take, answer.total):
        raise _InconsistentSourceError(f"{source.type}: more entries than were asked for or counted")
    if len(answer.entries) < min(take, answer.total):
        raise _InconsistentSourceError(f"{source.type}: fewer entries than the count and the page require")
    keys = [(-entry.created_at_ms, source.order_key(entry.native_id)) for entry in answer.entries]
    if any(later <= earlier for earlier, later in pairwise(keys)):
        raise _InconsistentSourceError(f"{source.type}: entries out of order or repeated")


async def _read_one(source: HistorySource, query: HistoryQuery, take: int) -> _Outcome:
    try:
        async with asyncio.timeout(SOURCE_TIMEOUT_SECONDS):
            answer = await source.read(query, take)
        _check(source, answer, take)
    except SourceUnavailableError as exc:
        logger.warning(
            "research history store unavailable",
            extra={"action": "research_history_source_unavailable", "source": source.type, "reason": str(exc)},
        )
        return _Outcome(source, None, f"The {source.label} could not be read: {exc}.")
    except TimeoutError:
        logger.warning(
            "research history store timed out",
            extra={"action": "research_history_source_timeout", "source": source.type},
        )
        return _Outcome(source, None, f"The {source.label} could not be read: it did not answer in time.")
    except _InconsistentSourceError as exc:
        logger.error(
            "research history store answered inconsistently",
            extra={"action": "research_history_source_inconsistent", "source": source.type, "reason": str(exc)},
        )
        return _Outcome(source, None, f"The {source.label} could not be read: its answer was inconsistent.")
    except Exception as exc:
        logger.error(
            "research history store failed",
            extra={"action": "research_history_source_failed", "source": source.type, "error_type": type(exc).__name__},
            exc_info=True,
        )
        return _Outcome(source, None, f"The {source.label} could not be read: it failed unexpectedly.")
    return _Outcome(source, answer, None)


def _report(source: HistorySource, outcome: _Outcome | None) -> ResearchHistorySource:
    if outcome is None:
        return ResearchHistorySource(
            type=source.type, label=source.label, state="not_queried", total=None, unreadable=0, error=None
        )
    if outcome.read is None:
        return ResearchHistorySource(
            type=source.type, label=source.label, state="unavailable", total=None, unreadable=0, error=outcome.error
        )
    return ResearchHistorySource(
        type=source.type,
        label=source.label,
        state="read",
        total=outcome.read.total,
        unreadable=outcome.read.unreadable,
        error=None,
    )


async def read_history(
    sources: Sequence[HistorySource],
    query: HistoryQuery,
    *,
    type_filter: HistoryType | None,
    page: int,
    page_size: int,
) -> ResearchHistoryPage:
    """Page ``page`` (1-based) of the stores' union under ``query``. Raises ``HistoryUnavailableError`` if all failed."""
    take = page * page_size
    queried = [source for source in sources if type_filter in (None, source.type)]
    outcomes = {
        outcome.source.type: outcome
        for outcome in await asyncio.gather(*(_read_one(source, query, take) for source in queried))
    }
    reports = [_report(source, outcomes.get(source.type)) for source in sources]
    answered = [outcome for outcome in outcomes.values() if outcome.read is not None]
    if not answered:
        raise HistoryUnavailableError(reports)

    order_keys = {outcome.source.type: outcome.source.order_key for outcome in answered}

    def shared_order(entry: ResearchHistoryEntry) -> tuple[int, str, int | str]:
        return (-entry.created_at_ms, entry.type, order_keys[entry.type](entry.native_id))

    union = heapq.merge(*(outcome.read.entries for outcome in answered if outcome.read), key=shared_order)
    return ResearchHistoryPage(
        entries=list(islice(union, (page - 1) * page_size, take)),
        total=sum(outcome.read.total for outcome in answered if outcome.read),
        page=page,
        page_size=page_size,
        partial=any(report.state == "unavailable" or report.unreadable > 0 for report in reports),
        sources=reports,
        symbols=sorted(set().union(*(outcome.read.symbols for outcome in answered if outcome.read))),
    )
