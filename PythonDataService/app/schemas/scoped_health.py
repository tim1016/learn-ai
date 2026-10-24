"""The scoped-health projection: what each health source says, and what it affects (PRD #3490 R30).

Four sources already know their own state: the lake catalog, the option-archive
capture, the backups with their restore check, and the IBKR feed. This is the
one wire shape they are folded into, so a page asks "does anything I depend on
need attention?" instead of keeping its own idea of healthy or stale.

An item names the evidence it rests on, never more:

* ``scope`` says which data kind it is about and, when the source can say,
  which instrument, session and mode. A source that cannot name the
  instruments says so: ``breadth`` reads ``broad``, it is never narrowed by
  guess.
* ``affects`` lists the operations that depend on that evidence. A backup that
  is behind concerns ``recovery``; it does not touch ``stock_history``, so no
  chart or quote is flagged by it. IBKR down touches ``live_chain`` and
  ``bot_feed``, not archived history.
* A source that could not be read yields ``unknown`` items, never an absent or
  healthy one.

Every ``*_ms`` is ``int64 ms UTC``. A session is the canonical session open.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

TimestampMs = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]

#: The four sources, which are also the four chips on the Operations header.
HealthSource = Literal["lake_catalog", "option_archive", "backups", "ibkr_feed"]
#: What an item is about. Finer than its source: the backups source holds two.
HealthDataKind = Literal["stock_bars", "option_archive", "lake_backup", "restore_check", "ibkr_market_data"]
#: The work a failing source can get in the way of.
AffectedOperation = Literal["stock_history", "options_history", "recovery", "live_chain", "bot_feed"]
#: Worst first: a list sorted by this puts the item to show first.
HealthSeverity = Literal["critical", "unknown", "warning", "info", "ok"]
#: ``fresh``: read for this request. ``cached``: an earlier read, still inside its window. ``unknown``: no read.
Freshness = Literal["fresh", "cached", "unknown"]
#: The Operations tabs a link can open; the ids are their URL segments.
OperationsTabId = Literal["lake", "option-archive", "backups"]

SOURCE_OF_KIND: dict[HealthDataKind, HealthSource] = {
    "stock_bars": "lake_catalog",
    "option_archive": "option_archive",
    "lake_backup": "backups",
    "restore_check": "backups",
    "ibkr_market_data": "ibkr_feed",
}

AFFECTS_OF_KIND: dict[HealthDataKind, tuple[AffectedOperation, ...]] = {
    "stock_bars": ("stock_history",),
    "option_archive": ("options_history",),
    "lake_backup": ("recovery",),
    "restore_check": ("recovery",),
    "ibkr_market_data": ("live_chain", "bot_feed"),
}

#: The severities that deserve a note on a page that depends on the item.
ATTENTION_SEVERITIES: frozenset[HealthSeverity] = frozenset({"critical", "unknown", "warning"})


class HealthScope(BaseModel):
    """Which evidence an item is about."""

    model_config = ConfigDict(frozen=True)

    data_kind: HealthDataKind
    #: The affected instrument, when the source can name it.
    instrument: str | None = None
    #: The earliest affected session, as its canonical session open.
    session_ms: TimestampMs | None = None
    #: The price-adjustment mode of stock bars, or the IBKR session's paper/live mode.
    mode: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def breadth(self) -> Literal["broad", "scoped"]:
        """``scoped`` only when an instrument is named; otherwise the item concerns every instrument."""
        return "broad" if self.instrument is None else "scoped"


class HealthLink(BaseModel):
    """Where the evidence is: an Operations tab, with the filters and card that show it."""

    model_config = ConfigDict(frozen=True)

    tab: OperationsTabId
    query: dict[str, str] = Field(default_factory=dict)
    fragment: str | None = None


class HealthItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: HealthScope
    #: When the source was read. ``unknown`` freshness: when the read was tried.
    observed_ms: TimestampMs
    freshness: Freshness
    severity: HealthSeverity
    #: One line naming the finding.
    summary: str
    #: Plain words on what is wrong, and what it does and does not get in the way of.
    explanation: str
    #: ``None`` where no Operations tab shows the evidence.
    link: HealthLink | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source(self) -> HealthSource:
        return SOURCE_OF_KIND[self.scope.data_kind]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def affects(self) -> list[AffectedOperation]:
        return list(AFFECTS_OF_KIND[self.scope.data_kind])

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_attention(self) -> bool:
        """The server's call on whether a page that depends on this should say so."""
        return self.severity in ATTENTION_SEVERITIES


class ScopedHealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_ms: TimestampMs
    #: Worst severity first, then by source, then by instrument.
    items: list[HealthItem]
