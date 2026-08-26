"""Reducing a channel-health snapshot into a debounce sample (#1777 WP4).

Sampling takes the snapshot rather than the gate so the caller decides and
journals from the same observation, and judges it on connectivity so an
account-scoped hold is driven only by account-scoped facts.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.models import ChannelHealth
from app.broker.alpaca.clerk.stream_health import channel_evidence_refs, sample_channels

NOW_MS = 1_000_000


def _health(
    stream: str,
    *,
    healthy: bool,
    connected: bool | None = None,
    observed_at_ms: int = NOW_MS,
) -> ChannelHealth:
    return ChannelHealth(
        stream=stream,
        healthy=healthy,
        connected=healthy if connected is None else connected,
        reason="" if healthy else f"{stream} is unhealthy.",
        observed_at_ms=observed_at_ms,
    )


def _sample(*channels: ChannelHealth):
    return sample_channels(channels, now_ms=NOW_MS)


def test_no_snapshot_samples_unknown() -> None:
    """No gate installed is absence of evidence, not evidence of health."""
    assert sample_channels(None, now_ms=NOW_MS).verdict == "unknown"


def test_two_connected_channels_sample_healthy() -> None:
    sample = _sample(
        _health("market_data", healthy=True),
        _health("execution", healthy=True),
    )

    assert sample.verdict == "healthy"


def test_either_disconnected_channel_samples_unhealthy() -> None:
    sample = _sample(
        _health("market_data", healthy=True),
        _health("execution", healthy=False),
    )

    assert sample.verdict == "unhealthy"


def test_a_connected_channel_that_is_merely_unusable_samples_healthy() -> None:
    """The account hold is account-scoped, so it may only turn on
    account-scoped facts. The unscoped market-data fact reports unhealthy
    while any one subscribed symbol is warming; keying the hold on that
    would freeze entries for every bot on one symbol -- finding S6, at
    account scope. ENTER still refuses on it, symbol-scoped."""
    sample = _sample(
        _health("market_data", healthy=False, connected=True),
        _health("execution", healthy=True),
    )

    assert sample.verdict == "healthy"


def test_the_sample_is_identified_by_the_observing_clock() -> None:
    """Not by the channels' own timestamps: a broken channel reports when
    it *broke*, frozen thereafter, so keying on it made a long outage
    indistinguishable from a replayed reading."""
    sample = _sample(
        _health("market_data", healthy=True, observed_at_ms=NOW_MS - 9_000),
        _health("execution", healthy=False, observed_at_ms=NOW_MS - 600_000),
    )

    assert sample.observed_at_ms == NOW_MS


def test_evidence_names_only_the_disconnected_channels() -> None:
    refs = channel_evidence_refs(
        (
            _health("market_data", healthy=False, connected=True),
            _health("execution", healthy=False),
        )
    )

    assert refs == ["execution: execution is unhealthy."]


def test_evidence_identity_excludes_the_observation_timestamp() -> None:
    """The churn fix (#1777 WP4 decision 4). The ledger appends only when
    this identity changes, so the same outage observed twice must produce
    byte-identical evidence."""
    first = channel_evidence_refs((_health("execution", healthy=False),))
    later = channel_evidence_refs(
        (_health("execution", healthy=False, observed_at_ms=NOW_MS + 60_000),)
    )

    assert first == later
