"""Clerk submission-channel health evaluation (§7.3).

Extracted from ``panel_projection_service`` when #1777 added the
connectivity question: the two verdicts, their shared presence/freshness
core, and the threshold defining "fresh" are one concept, and that module
was past a healthy size to keep absorbing it.

Two questions, deliberately distinct:

* :func:`evaluate_channel_health` -- is this channel *usable*? Submission
  gates and symbol-scoped views ask this.
* :func:`evaluate_channel_connectivity` -- is this channel good enough at
  *account* scope? Account-level surfaces ask this, so one symbol's warm-up
  cannot refuse every deploy on the account (finding S6). The per-channel
  answer is the clerk's canonical ``account_scope_satisfied``, shared with
  the durable stream-health hold.

Presence and freshness bind both: an observation older than the threshold
is absence of evidence, not evidence of health.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.broker.alpaca.clerk.models import ChannelHealth
from app.broker.alpaca.clerk.stream_health import account_scope_satisfied
from app.broker.v2panel.vocabulary import ChannelState

# A channel-health observation older than this is not "fresh" for the
# clear-hold gate (§7.3). Derived from the hold sync's own cadence (#1777
# WP4 decision 8) — this used to reuse the station staleness threshold, one
# trading day, which at a 15 s observation cadence never fired.
# Three ticks of the clerk's stream-health cadence. Deliberately its own
# constant rather than an import: this gates a deploy on how recently a
# channel fact was observed, which is a separate question from how often
# the hold sync runs, and coupling them made an unrelated cadence change
# move a safety threshold. The value it replaced was one trading day,
# which made this gate decorative.
CHANNEL_FRESH_THRESHOLD_MS = 45_000
REQUIRED_CLERK_CHANNELS = ("market_data", "execution")


@dataclass(frozen=True)
class ChannelHealthEvaluation:
    """Canonical health/freshness verdict for Clerk submission channels."""

    ready: bool
    missing: tuple[str, ...]
    stale: tuple[str, ...]
    unhealthy: tuple[str, ...]

    @property
    def failing(self) -> frozenset[str]:
        """Every stream that failed this evaluation, whatever the reason."""
        return frozenset(self.missing) | frozenset(self.stale) | frozenset(self.unhealthy)


def evaluate_channel_health(
    channel_healths: Sequence[ChannelHealth] | None,
    now_ms: int,
    *,
    required_streams: tuple[str, ...] = REQUIRED_CLERK_CHANNELS,
) -> ChannelHealthEvaluation:
    """Evaluate the exact channel set required by every submission gate.

    Takes the raw channel-health facts (not a full ``ClerkStatus``) so
    callers that are still assembling a ``ClerkStatus`` — e.g. one that must
    fold this verdict into the same status object (#1664) — can evaluate
    channel readiness first.

    ``required_streams`` defaults to both Clerk-submission channels. Dry Run
    admission (#1702) needs only ``market_data`` — it never opens the
    ``execution`` channel — so it passes a narrower tuple.
    """
    return _evaluate_channels(
        channel_healths,
        now_ms,
        required_streams=required_streams,
        satisfied=lambda health: health.healthy,
    )


def _evaluate_channels(
    channel_healths: Sequence[ChannelHealth] | None,
    now_ms: int,
    *,
    required_streams: tuple[str, ...],
    satisfied: Callable[[ChannelHealth], bool],
) -> ChannelHealthEvaluation:
    """Shared core: presence, freshness, then the caller's own predicate.

    Presence and freshness are never negotiable — an observation older than
    the threshold is absence of evidence, not evidence of health, whatever
    it claims. Only the per-channel predicate differs between the usability
    and connectivity questions.
    """
    by_stream = {health.stream: health for health in channel_healths or []}
    missing = tuple(stream for stream in required_streams if stream not in by_stream)
    stale = tuple(
        stream
        for stream in required_streams
        if stream in by_stream
        and now_ms - by_stream[stream].observed_at_ms > CHANNEL_FRESH_THRESHOLD_MS
    )
    unsatisfied = tuple(
        stream
        for stream in required_streams
        if stream in by_stream and not satisfied(by_stream[stream])
    )
    return ChannelHealthEvaluation(
        ready=not missing and not stale and not unsatisfied,
        missing=missing,
        stale=stale,
        unhealthy=unsatisfied,
    )


def evaluate_channel_connectivity(
    channel_healths: Sequence[ChannelHealth] | None,
    now_ms: int,
    *,
    required_streams: tuple[str, ...] = REQUIRED_CLERK_CHANNELS,
) -> ChannelHealthEvaluation:
    """Evaluate the channels at *account* scope.

    The account-level deploy view uses this instead of
    :func:`evaluate_channel_health` so that one symbol still warming up its
    first closed bar cannot refuse every deploy on the account (#1777,
    finding S6). Per-symbol usability is the symbol-scoped view's question,
    and remains the submission gate's question in every case.

    What "account scope" relaxes is decided once, by the clerk's
    :func:`~app.broker.alpaca.clerk.stream_health.account_scope_satisfied`:
    market data drops to connectivity because warm-up is per-symbol;
    execution still requires health because an unusable evidence frame is
    account-wide. Presence and freshness still apply on top -- an
    observation older than the threshold proves nothing about the channel's
    current state, whatever it claims. ``unhealthy`` here names the streams
    that failed that predicate.
    """
    return _evaluate_channels(
        channel_healths,
        now_ms,
        required_streams=required_streams,
        satisfied=account_scope_satisfied,
    )


def channel_state(*, healthy: bool, observed_at_ms: int, now_ms: int) -> ChannelState:
    """Derive the closed channel state from health + freshness (§7.3)."""
    if now_ms - observed_at_ms > CHANNEL_FRESH_THRESHOLD_MS:
        return "unknown"
    return "healthy" if healthy else "unhealthy"


__all__ = [
    "CHANNEL_FRESH_THRESHOLD_MS",
    "REQUIRED_CLERK_CHANNELS",
    "ChannelHealthEvaluation",
    "channel_state",
    "evaluate_channel_connectivity",
    "evaluate_channel_health",
]
