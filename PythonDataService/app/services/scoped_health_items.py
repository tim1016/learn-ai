"""What each health source's reading says, as scoped-health items (PRD #3490 R30).

Pure: a reading goes in, items come out. Nothing here reads a source or holds
state, and no source's own judgement is redone: the archive's severity, the
IBKR condition and the lake's per-symbol counts are mapped, not recomputed.
What this module adds is the part no source knows: which operations the
finding gets in the way of, where the evidence is shown, and how to say it in
plain words.

A reading that could not be taken becomes ``unknown`` items, never an absent
or healthy one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, get_args

from app.broker.ibkr.models import IbkrConnectionHealth
from app.data_lake.types import DataType, PriceAdjustmentMode, RunReadinessResponse, SymbolRunReadiness
from app.schemas.option_archive import ArchiveJobHealth, ArchiveSeverity, OptionArchiveHealth, UnderlyingArchiveHealth
from app.schemas.scoped_health import (
    AFFECTS_OF_KIND,
    ATTENTION_SEVERITIES,
    SOURCE_OF_KIND,
    AffectedOperation,
    Freshness,
    HealthDataKind,
    HealthItem,
    HealthLink,
    HealthScope,
    HealthSeverity,
    HealthSource,
)

#: Longest source-authored reason carried into an explanation; a vendor's refusal text can run long.
REASON_LIMIT: Final = 200

#: The view of the lake the projection speaks for, and the Lake tab's own default: the split-adjusted
#: trade bars every sweep and Golden Search reads.
LAKE_MODE: Final[PriceAdjustmentMode] = "polygon_split_adjusted"
LAKE_DATA_TYPE: Final[DataType] = "trade"

_SEVERITY_RANK: Final = {severity: rank for rank, severity in enumerate(get_args(HealthSeverity))}
_SOURCE_RANK: Final = {source: rank for rank, source in enumerate(get_args(HealthSource))}

_ARCHIVE_SEVERITY: Final[dict[ArchiveSeverity, HealthSeverity]] = {
    "healthy": "ok",
    "catching_up": "info",
    "waiting": "info",
    "at_risk": "warning",
    "failed": "critical",
    "unavailable": "unknown",
}

_STOCK_IMPACT: Final = (
    "Studies and exports that read this symbol's stock history are affected; option history, quotes and other symbols are not."
)
_ARCHIVE_IMPACT: Final = (
    "Options runs that need archived sessions may be refused or incomplete; stock charts, quotes and the live chain are not affected."
)
_RECOVERY_IMPACT: Final = "This concerns recoverability only: stored prices, quotes and charts are not marked invalid by it."
_IBKR_IMPACT: Final = "Live option chain and bot feed readiness are affected; archived history is not."

#: Per kind: the summary, the cause when the source gives none of its own, and what the unknown gets in the way of.
_UNREADABLE: Final[dict[HealthDataKind, tuple[str, str, str]]] = {
    "stock_bars": (
        "Lake status unknown",
        "The lake catalog could not be read, so which stock sessions a run can use is unknown.",
        _STOCK_IMPACT,
    ),
    "option_archive": (
        "Option archive status unknown",
        "The option archive status could not be read, so whether the sessions an options run needs are preserved is unknown.",
        _ARCHIVE_IMPACT,
    ),
    "lake_backup": (
        "Backup status unknown",
        "The backup status could not be read, so whether the lake can be recovered is unknown.",
        _RECOVERY_IMPACT,
    ),
    "restore_check": (
        "Restore check status unknown",
        "The restore check could not be read, so whether a backup restores is unknown.",
        _RECOVERY_IMPACT,
    ),
    "ibkr_market_data": (
        "IBKR feed status unknown",
        "The IBKR feed status could not be read, so whether live option chains and bot feeds can stream is unknown.",
        _IBKR_IMPACT,
    ),
}


@dataclass(frozen=True)
class Reading[T]:
    """One source, as read: its answer, or the plain reason it gave none."""

    #: When the read was taken (or tried).
    read_ms: int
    freshness: Freshness
    value: T | None = None
    #: Why there is no value, as a sentence, when the source knows. ``None``: it failed unexpectedly.
    reason: str | None = None


def kinds_for(operations: frozenset[AffectedOperation]) -> frozenset[HealthDataKind]:
    """The data kinds whose failure can get in the way of any of ``operations``; every kind for none."""
    return frozenset(
        kind for kind, affects in AFFECTS_OF_KIND.items() if not operations or operations.intersection(affects)
    )


def sources_for(kinds: frozenset[HealthDataKind]) -> frozenset[HealthSource]:
    return frozenset(SOURCE_OF_KIND[kind] for kind in kinds)


def concerns(item: HealthItem, operations: frozenset[AffectedOperation], instrument: str | None) -> bool:
    """Whether ``item`` bears on a page that does ``operations`` for ``instrument``.

    A broad item bears on every instrument: a source that cannot name which are
    affected is never narrowed to one by guess.
    """
    if operations and not operations.intersection(item.affects):
        return False
    return instrument is None or item.scope.instrument in (None, instrument)


def worst_first(items: list[HealthItem]) -> list[HealthItem]:
    return sorted(
        items,
        key=lambda item: (_SEVERITY_RANK[item.severity], _SOURCE_RANK[item.source], item.scope.instrument or ""),
    )


def _brief(text: str) -> str:
    return text if len(text) <= REASON_LIMIT else text[: REASON_LIMIT - 3].rstrip() + "..."


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _link(kind: HealthDataKind, *, instrument: str | None = None, mode: str | None = None) -> HealthLink | None:
    match kind:
        case "stock_bars":
            query = (
                {}
                if instrument is None or mode is None
                else {"symbol": instrument, "price_adjustment_mode": mode, "data_type": LAKE_DATA_TYPE}
            )
            return HealthLink(tab="lake", query=query)
        case "option_archive":
            fragment = "option-archive" if instrument is None else f"option-archive-{instrument}"
            return HealthLink(tab="option-archive", fragment=fragment)
        case "lake_backup":
            return HealthLink(tab="backups", fragment="option-archive-backup")
        case "restore_check":
            return HealthLink(tab="backups", fragment="option-archive-restore")
        case "ibkr_market_data":
            # No Operations tab shows the IBKR session; the pages that depend on it carry their own reason.
            return None


def unreadable_items(kinds: tuple[HealthDataKind, ...], reading: Reading[object]) -> list[HealthItem]:
    """``unknown`` for each of ``kinds``: the source gave no answer, so nothing is claimed healthy."""
    items: list[HealthItem] = []
    for kind in kinds:
        summary, cause, impact = _UNREADABLE[kind]
        items.append(HealthItem(
            scope=HealthScope(data_kind=kind),
            observed_ms=reading.read_ms,
            freshness="unknown",
            severity="unknown",
            summary=summary,
            explanation=f"{_brief(reading.reason) if reading.reason else cause} {impact}",
            link=_link(kind),
        ))
    return items


def lake_items(reading: Reading[RunReadinessResponse], *, instrument: str | None, limit: int) -> list[HealthItem]:
    """One item per symbol with sessions a run refuses or that the calendar says are missing.

    Asked for one ``instrument``, only that symbol is answered: its problem, or
    that it is clean (nothing at all when the lake does not hold it). Asked for
    the whole lake, a clean lake is one broad item and a long list of problems
    is cut at ``limit``, the rest folded into one broad item, since the Lake tab
    holds the full list.
    """
    response = reading.value
    if response is None:
        return unreadable_items(("stock_bars",), reading)
    rows = [row for row in response.symbols if instrument in (None, row.symbol)]
    problems = [row for row in rows if _lake_problem(row)]

    def item(row: SymbolRunReadiness | None, severity: HealthSeverity, summary: str, explanation: str) -> HealthItem:
        return _lake_item(reading, response.price_adjustment_mode, row, severity, summary, explanation)

    if not problems:
        if instrument is not None:
            return [item(row, "ok", f"{instrument}: lake readable",
                         f"All of {instrument}'s held sessions are complete and readable by a run.") for row in rows]
        return [item(None, "ok", "Lake readable", "Every held symbol's sessions are complete and readable by a run.")]

    shown = problems if instrument is not None else problems[:limit]
    items = [item(row, "warning", f"{row.symbol}: {_lake_problem(row)}", _lake_explanation(row)) for row in shown]
    if folded := problems[len(shown):]:
        count = _count(len(folded), "more symbol")
        items.append(item(None, "warning", f"{count} need attention",
                          f"{count} have sessions a run refuses or that are missing; the Lake tab lists them. {_STOCK_IMPACT}"))
    return items


def _lake_item(
    reading: Reading[RunReadinessResponse],
    mode: str,
    row: SymbolRunReadiness | None,
    severity: HealthSeverity,
    summary: str,
    explanation: str,
) -> HealthItem:
    symbol = None if row is None else row.symbol
    starts = () if row is None else (row.rebuild_first_trading_date_ms, row.fill_first_trading_date_ms)
    return HealthItem(
        scope=HealthScope(
            data_kind="stock_bars",
            instrument=symbol,
            session_ms=min((ms for ms in starts if ms is not None), default=None),
            mode=mode,
        ),
        observed_ms=reading.read_ms,
        freshness=reading.freshness,
        severity=severity,
        summary=summary,
        explanation=explanation,
        link=_link("stock_bars", instrument=symbol, mode=mode),
    )


def _lake_problem(row: SymbolRunReadiness) -> str:
    """A symbol's problem in a few words; empty when a run can read all it holds and nothing is missing."""
    parts: list[str] = []
    if (unreadable := row.session_count - row.run_ready_count) > 0:
        parts.append(f"{_count(unreadable, 'session')} a run refuses")
    if row.gap_session_count > 0:
        parts.append(f"{_count(row.gap_session_count, 'session')} missing")
    return ", ".join(parts)


def _lake_explanation(row: SymbolRunReadiness) -> str:
    sentences: list[str] = []
    if (unreadable := row.session_count - row.run_ready_count) > 0:
        sentences.append(
            f"{row.symbol} has {_count(unreadable, 'session')} on disk that a run refuses: each needs a current "
            "adjustment record. A backfill rebuilds them."
        )
        if row.unrebuildable_count > 0:
            sentences.append(f"{row.unrebuildable_count} are older than the provider's history and cannot be rebuilt.")
    if row.gap_session_count > 0:
        verb = "is" if row.gap_session_count == 1 else "are"
        sentences.append(
            f"{_count(row.gap_session_count, 'scheduled session')} between {row.symbol}'s first and last held "
            f"session {verb} not in the lake."
        )
        if row.unfillable_gap_session_count > 0:
            sentences.append(f"{row.unfillable_gap_session_count} are older than the provider's history and cannot be fetched.")
    return " ".join([*sentences, _STOCK_IMPACT])


def archive_items(reading: Reading[OptionArchiveHealth]) -> list[HealthItem]:
    """One item per underlying: whether the sessions an options run needs are preserved."""
    health = reading.value
    if health is None:
        return unreadable_items(("option_archive",), reading)
    return [_archive_item(reading, health, underlying) for underlying in health.underlyings]


def _archive_item(
    reading: Reading[OptionArchiveHealth], health: OptionArchiveHealth, underlying: UnderlyingArchiveHealth
) -> HealthItem:
    severity = _ARCHIVE_SEVERITY[underlying.severity]
    said = f"{underlying.underlying}: {_brief(underlying.reason)}"
    return HealthItem(
        scope=HealthScope(
            data_kind="option_archive",
            instrument=underlying.underlying,
            session_ms=underlying.gaps[0].session_ms if underlying.gaps else None,
        ),
        observed_ms=health.generated_ms,
        freshness=reading.freshness,
        severity=severity,
        summary=said,
        explanation=f"{said} {_ARCHIVE_IMPACT}" if severity in ATTENTION_SEVERITIES else said,
        link=_link("option_archive", instrument=underlying.underlying),
    )


def backup_items(reading: Reading[OptionArchiveHealth]) -> list[HealthItem]:
    """The lake backup and the weekly restore check: whether the lake can be recovered."""
    health = reading.value
    if health is None:
        return unreadable_items(("lake_backup", "restore_check"), reading)
    return [
        _job_item(reading, health, "lake_backup", health.backup),
        _job_item(reading, health, "restore_check", health.test_restore),
    ]


def _job_item(
    reading: Reading[OptionArchiveHealth],
    health: OptionArchiveHealth,
    kind: Literal["lake_backup", "restore_check"],
    job: ArchiveJobHealth,
) -> HealthItem:
    severity = _ARCHIVE_SEVERITY[job.severity]
    said = _brief(job.reason)
    return HealthItem(
        scope=HealthScope(data_kind=kind),
        observed_ms=health.generated_ms,
        freshness=reading.freshness,
        severity=severity,
        summary=said,
        explanation=f"{said} {_RECOVERY_IMPACT}" if severity in ATTENTION_SEVERITIES else said,
        link=_link(kind),
    )


def ibkr_items(reading: Reading[IbkrConnectionHealth]) -> list[HealthItem]:
    """The IBKR session, in the words the broker health already authors for it."""
    health = reading.value
    if health is None:
        return unreadable_items(("ibkr_market_data",), reading)
    condition = health.condition
    if condition is None:
        return unreadable_items(("ibkr_market_data",), Reading(read_ms=health.fetched_at_ms, freshness="unknown"))
    said = condition.summary + (f" {condition.remediation}" if condition.remediation else "")
    return [HealthItem(
        scope=HealthScope(data_kind="ibkr_market_data", mode=health.mode),
        observed_ms=health.fetched_at_ms,
        freshness=reading.freshness,
        severity=condition.severity,
        summary=condition.title,
        explanation=f"{said} {_IBKR_IMPACT}" if condition.severity in ATTENTION_SEVERITIES else said,
        link=_link("ibkr_market_data"),
    )]
