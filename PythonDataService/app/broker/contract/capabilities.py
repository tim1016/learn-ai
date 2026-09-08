"""Broker capability descriptor (Broker System v2, Layer 3).

Callers gate on **capabilities, never on broker identity** (design decision
D2). Honest differences between brokers are declared here as data — e.g.
Alpaca's IEX feed gaps on illiquid symbols (``bars_may_gap=True``) and caps
free streams at 30 symbols — so no code needs an ``if broker == "alpaca"``
branch. Each vendor layer publishes one instance describing itself.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

_MINUTES_PER_DAY = 24 * 60


class ExtendedHoursWindow(BaseModel):
    """The broker's extended session as minutes past midnight, America/New_York.

    Capability data (ADR 0059 D5.2), not calendar authority: the regular
    session's bounds still come only from the canonical calendar module.
    Resolving an instant against this window happens in
    ``app.services.session_authority``; nothing else may read the minutes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    open_minute_et: int = Field(ge=0, lt=_MINUTES_PER_DAY)
    close_minute_et: int = Field(gt=0, le=_MINUTES_PER_DAY)

    @model_validator(mode="after")
    def _opens_before_it_closes(self) -> ExtendedHoursWindow:
        if self.open_minute_et >= self.close_minute_et:
            raise ValueError("extended_hours_window must open before it closes")
        return self


class BrokerCapabilities(BaseModel):
    """What a broker can and cannot do, as data.

    Phase-1 callers read the descriptor for display and honest-empty logic;
    later phases gate streaming and order construction on the same fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    broker: str
    # Trading surface (phase-1 informational; enforced from phase 2).
    paper_only: bool
    supports_fractional: bool
    supports_extended_hours: bool
    # The declared extended session (ADR 0059 D5.2); present iff supported.
    extended_hours_window: ExtendedHoursWindow | None = None
    supported_order_types: tuple[str, ...]
    # Market-data / streaming shape (designed now, enforced from phase 3).
    data_feed: str
    bars_may_gap: bool
    max_stream_symbols: int
    max_concurrent_streams: int
    # REST budget the caller must stay within.
    rest_rate_limit_per_min: int

    @model_validator(mode="after")
    def _window_agrees_with_support(self) -> BrokerCapabilities:
        if self.supports_extended_hours and self.extended_hours_window is None:
            raise ValueError("supports_extended_hours requires an extended_hours_window")
        if not self.supports_extended_hours and self.extended_hours_window is not None:
            raise ValueError("extended_hours_window is only meaningful when extended hours are supported")
        return self

    def revised(self, **changes: object) -> BrokerCapabilities:
        """A copy of this descriptor with ``changes`` applied, re-validated.

        Pydantic's ``model_copy(update=...)`` skips validators, so a copy could
        assert ``supports_extended_hours=True`` with no window — the exact pair
        ``_window_agrees_with_support`` exists to keep consistent. Every
        derived descriptor (the live-mode copy, the drill double) goes through
        here so a future edit cannot mint an incoherent one.
        """
        return BrokerCapabilities.model_validate({**self.model_dump(), **changes})
