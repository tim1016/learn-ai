"""Broker-neutral market-data port: MarketDataFeed, MarketDataBar, FeedHealth.

Also the canonical home of ``BarSessionPhase`` — the session-phase label
stamped on every bar, whatever produced it. It lives here rather than in
a broker silo because it is not vendor-specific: ``app.broker.ibkr.bar_models``
imports it from this module (#1813 PR-C, 2026-08-27).

Design constraints (from ADR 0022 + phase-3 design §4 + #1258 L2):

* No IBKR types escape this module.  The IBKR implementation (ibkr_feed.py)
  translates ``IbkrMinuteBar`` and ``IBKRBarStreamError`` at the boundary.
* All temporal fields are ``int64 ms UTC`` — no ISO strings, no naive datetimes.
* ``MarketDataFeed`` is a structural Protocol: any class with the right
  signature satisfies it without inheriting from it.
* Fan-out is reference-counted: N consumers of the same symbol share one
  underlying broker subscription; the last unsubscribe releases it.
* Ordinary bar gaps are non-fatal. A bounded stalled broker request is
  replaced internally; connection death is fatal and typed.
* A reconnect is survivable, not fatal (#1921). Every bar carries a
  ``BarProvenanceTag`` saying how it was produced, and a caller that must
  not miss a decision bar hands ``stream_bars`` a ``ContinuityPolicy``
  describing its decision clock, its recovery authorization and where to
  record ``FeedContinuityEvent``s.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class MarketDataFeedError(Exception):
    """Fatal feed error: connection death, a refused continuity recovery, or an invariant violation.

    Raised by ``MarketDataFeed.stream_bars`` when the underlying connection
    dies or violates an unrecoverable data invariant. Ordinary bar gaps
    (expected during pre-market or after-hours silence) are not errors.

    ``reason`` is an optional machine-readable code (e.g.
    ``"DECISION_BAR_MISSED"``) that a receipt or a ledger can key on; it is
    prefixed onto the message so a log line carries it without the reader
    having to reach for the attribute.
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if reason else message)
        self.reason = reason


BarSessionPhase = Literal["PRE", "RTH", "POST", "OVERNIGHT", "CLOSED", "UNKNOWN"]
"""Canonical session-phase label. Single definition repo-wide: every other site
imports this object instead of restating the six members —
``app.broker.ibkr.bar_models``, ``app.broker.ibkr.bars``,
``app.schemas.run_admission``, ``app.services.live_chart_window``, and
``app.services.session_authority``, which aliases it as ``TradingSessionPhase``
so session code reads in session vocabulary (``TradingSessionPhase is
BarSessionPhase``)."""


BarProvenanceTag = Literal["realtime", "realtime_across_reconnect", "historical_substitute", "history"]
"""How a delivered bar was produced, in broker-neutral vocabulary (#1921).

``realtime``                  — assembled wholly inside one live connection.
``realtime_across_reconnect`` — assembled from live contributions that span a
                                broker-socket interruption; every contribution
                                is still a real print, so the bar is a decision
                                input like any other.
``historical_substitute``     — backfilled from the broker's history endpoint
                                to replace a window the live stream missed.
                                Only ever delivered under an explicit
                                ``SubstitutionGrant``.
``history``                   — served by ``recent_closed_bars`` for warmup;
                                never itself a decision.
"""


class MarketDataBar(BaseModel):
    """Broker-neutral closed 1-minute bar.

    All temporal values are ``int64 ms UTC`` per ``.claude/rules/temporal-rigor.md``.
    ``start_ms`` is the bar-open boundary (inclusive); ``end_ms`` is bar-close
    (exclusive), i.e. ``end_ms = start_ms + 60_000``.

    OHLC prices use ``Decimal`` for exactness — no silent float rounding at the
    port boundary.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    start_ms: int = Field(..., description="Bar-open boundary, int64 ms UTC, inclusive.")
    end_ms: int = Field(..., description="Bar-close boundary, int64 ms UTC, exclusive.")
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    fetched_at_ms: int = Field(..., description="Wall-clock at which the bar was assembled, int64 ms UTC.")
    feed_id: str = Field(
        ...,
        description=(
            "Source feed identity, e.g. 'ibkr'. NOT an execution identity and NOT the bar's "
            "provenance (see `provenance`)."
        ),
    )
    session_phase: BarSessionPhase = "UNKNOWN"
    provenance: BarProvenanceTag = Field(
        default="realtime", description="How this bar was produced; see BarProvenanceTag."
    )
    authorization_id: str | None = Field(
        default=None,
        description="Substitution grant that authorized a 'historical_substitute' bar; None otherwise.",
    )
    continuity_event_ref: str | None = Field(
        default=None,
        description="'<run_id>:<evidence_seq>' of the continuity event explaining this bar; None otherwise.",
    )


class FeedHealth(BaseModel):
    """Point-in-time health snapshot for a MarketDataFeed.

    ``connected`` — the underlying broker connection is alive.
    ``stale``     — connected but required source liveness has not been proven
                    within the implementation's stale threshold.
    ``last_bar_ms`` — ``start_ms`` of the most recently emitted bar, or ``None``
                      if no bar has been emitted yet.
    ``reason``    — human-readable detail when unhealthy (empty string when healthy).
    ``active_subscription_count`` — number of symbols currently subscribed.
    ``observed_at_ms`` — wall-clock of this snapshot, int64 ms UTC.
    """

    model_config = ConfigDict(frozen=True)

    connected: bool
    stale: bool
    last_bar_ms: int | None
    reason: str
    active_subscription_count: int
    observed_at_ms: int = Field(..., description="Snapshot wall-clock, int64 ms UTC.")


# ---------------------------------------------------------------------------
# Reconnect continuity (#1921)
# ---------------------------------------------------------------------------

ContinuityEventKind = Literal["interruption", "recovered", "gap", "substituted", "refused"]
"""What a ``FeedContinuityEvent`` records.

``interruption`` — the source connection was lost or fenced.
``recovered``    — live delivery resumed on a new connection generation.
``gap``          — a window of minutes the live stream never delivered.
``substituted``  — a gap window was backfilled under a ``SubstitutionGrant``.
``refused``      — a substitution was asked for and denied; the bar stays missing.
"""

InterruptionCause = Literal["socket_down", "soft_loss_1100", "stall", "generation_changed"]
"""Why delivery stopped, in the vocabulary the broker boundary can prove."""

DecisionSession = Literal["rth", "all"]
"""Which minutes the consumer's decision clock treats as decidable."""


class FeedContinuityEvent(BaseModel):
    """One recordable fact about the feed's continuity for a symbol.

    Every temporal field is ``int64 ms UTC``. Fields not meaningful for a
    given ``kind`` stay ``None`` rather than being filled with a placeholder:
    an absent generation is a different fact from generation zero.
    """

    model_config = ConfigDict(frozen=True)

    kind: ContinuityEventKind
    feed_id: str
    symbol: str
    observed_at_ms: int = Field(..., description="Wall-clock at which the fact was observed, int64 ms UTC.")
    cause: InterruptionCause | None = None
    generation_from: int | None = None
    generation_to: int | None = None
    window_start_ms: int | None = None
    window_end_ms: int | None = None
    bar_identity: str | None = None
    authorization_id: str | None = None
    reason: str | None = None
    last_delivered_end_ms: int | None = None
    deadline_ms: int | None = None
    contribution_count: int | None = Field(
        default=None,
        description=(
            "How many source contributions the minute this fact is about actually held, "
            "when it is one emitted-but-incomplete minute. None for a window nothing was "
            "ever assembled for — absent is a different fact from zero."
        ),
    )


class ContinuityEventRef(BaseModel):
    """Where a recorded ``FeedContinuityEvent`` landed in a run's evidence."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    evidence_seq: int

    def ref(self) -> str:
        """Return the compact ``'<run_id>:<evidence_seq>'`` bar-stampable form."""
        return f"{self.run_id}:{self.evidence_seq}"


class SubstitutionGrant(BaseModel):
    """Authorization to deliver historical bars for one missed window."""

    model_config = ConfigDict(frozen=True)

    authorization_id: str
    window_start_ms: int
    window_end_ms: int


class SubstitutionRefusal(BaseModel):
    """Refusal to authorize substitution, with the reason the caller must receipt."""

    model_config = ConfigDict(frozen=True)

    reason: Literal[
        "SUBSTITUTION_NOT_AUTHORIZED",
        "SUBSTITUTION_SHAPE_UNPROVEN",
        "SUBSTITUTION_WARMUP_TAINTED",
    ]


@dataclass(frozen=True)
class ContinuityPolicy:
    """What one consumer needs the feed to do when delivery is interrupted.

    A ``dataclass`` rather than a Pydantic model because it carries the
    consumer's callables — its decision clock, its substitution authority and
    its evidence sink — which Pydantic would have to validate as opaque
    objects anyway.

    ``next_trigger_ms(last_end_ms)`` returns the ``end_ms`` of the next bar the
    consumer would decide on, strictly after ``last_end_ms``.
    ``substitution_grant(window_start_ms, window_end_ms)`` either authorizes
    backfill of that window or refuses it. ``record_event`` persists a
    continuity event and returns the reference to stamp on affected bars.
    """

    decision_session: DecisionSession
    next_trigger_ms: Callable[[int], int]
    substitution_grant: Callable[[int, int], SubstitutionGrant | SubstitutionRefusal]
    record_event: Callable[[FeedContinuityEvent], Awaitable[ContinuityEventRef]]
    delivery_allowance_ms: int = 20_000

    def __post_init__(self) -> None:
        # ``DecisionSession`` reserves "all" (spec §12) but no calendar-proven
        # trigger set exists for it yet (ruling R1). Refusing it here, where the
        # policy is authored, is the only place the consumer can be told; left
        # to the stream, ``inside_decision_session`` would quietly fail open
        # while ``next_trigger_ms`` raised ``NotImplementedError`` mid-run.
        if self.decision_session != "rth":
            raise ValueError(
                f"decision_session {self.decision_session!r} has no calendar-proven "
                "trigger set yet; only 'rth' can be scheduled against."
            )

    def deadline_ms(self, last_delivered_end_ms: int) -> int:
        """Wall-clock by which the next decision bar must have been delivered."""
        return self.next_trigger_ms(last_delivered_end_ms) + self.delivery_allowance_ms

    def is_trigger_ms(self, end_ms: int) -> bool:
        """Whether a bar closing at ``end_ms`` is one the consumer decides on.

        ``next_trigger_ms`` answers "the smallest trigger strictly after this
        instant", so asking it about ``end_ms - 1`` asks for the smallest
        trigger at or after ``end_ms`` — which is ``end_ms`` itself exactly when
        ``end_ms`` is a trigger.
        """
        return self.next_trigger_ms(end_ms - 1) == end_ms


async def record_continuity_event(
    policy: ContinuityPolicy, event: FeedContinuityEvent
) -> ContinuityEventRef:
    """Write one continuity fact through the consumer's sink, or fail closed.

    Spec §4.2 rule 9: continuing without the evidence that was promised is
    forbidden. Every writer goes through this one function so a sink failure
    surfaces as the same typed ``CONTINUITY_EVIDENCE_UNWRITABLE`` wherever it
    happens, rather than escaping as whatever the sink's own failure was --
    the bot layer's own refusals write to the same journal the feed does.
    """
    try:
        return await policy.record_event(event)
    except Exception as exc:
        raise MarketDataFeedError(
            f"continuity evidence for {event.symbol} could not be written: {exc}",
            reason="CONTINUITY_EVIDENCE_UNWRITABLE",
        ) from exc


class MarketDataFeed(Protocol):
    """Broker-neutral market-data port.

    Implementations back this with a specific vendor (IBKR today; a
    multi-symbol hub later).  Consumers depend only on this Protocol —
    they never import vendor-specific feed code.

    ``feed_id`` is a provenance tag stamped on every bar (e.g. ``"ibkr"``).
    It is NOT an execution-broker identity and must not be used for routing
    orders.

    ``stream_bars`` returns an async iterator of closed 1-minute bars.  It
    raises ``MarketDataFeedError`` when the underlying connection dies or an
    unrecoverable invariant fails. Implementations may replace a bounded
    stalled subscription transparently; ordinary bar gaps are silent.

    ``continuity`` is how a caller that cannot miss a decision bar states its
    decision clock, its substitution authority and its evidence sink.
    ``None`` — the default — is the pre-#1921 behavior: the feed neither
    recovers across a reconnect nor records continuity evidence.

    ``health`` is a synchronous point-in-time snapshot.  Callers may poll it
    on any cadence; it never blocks.
    """

    feed_id: str

    @property
    def capability_account_id(self) -> str | None:
        """Account whose broker capability snapshots authorize this feed."""
        ...

    def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        continuity: ContinuityPolicy | None = None,
    ) -> AsyncIterator[MarketDataBar]: ...

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        """Return closed 1-minute bars from the trailing ``lookback_days``
        calendar days, oldest first.

        Used to warm up a consumer's indicator state before it starts
        making decisions from ``stream_bars`` -- never itself a decision
        input. A source that cannot serve history returns an empty list;
        callers must treat that as "no warmup available", not an error.
        """
        ...

    def health(self, symbol: str | None = None) -> FeedHealth: ...
