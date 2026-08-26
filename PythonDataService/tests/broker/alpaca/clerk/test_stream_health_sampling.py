"""Reducing a channel-health snapshot into a debounce sample (#1777 WP4).

Sampling takes the snapshot rather than the gate so the caller decides and
journals from the same observation.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.models import ChannelHealth
from app.broker.alpaca.clerk.stream_health import channel_evidence_refs, sample_channels

WINDOW_MS = 45_000
NOW_MS = 1_000_000


def _health(stream: str, *, healthy: bool, observed_at_ms: int) -> ChannelHealth:
    return ChannelHealth(
        stream=stream,
        healthy=healthy,
        connected=healthy,
        reason="" if healthy else f"{stream} is unhealthy.",
        observed_at_ms=observed_at_ms,
    )


def _sample(*channels: ChannelHealth):
    return sample_channels(channels, now_ms=NOW_MS, freshness_window_ms=WINDOW_MS)


def test_no_snapshot_samples_unknown() -> None:
    """No gate installed is absence of evidence, not evidence of health."""
    assert sample_channels(None, now_ms=NOW_MS, freshness_window_ms=WINDOW_MS).verdict == (
        "unknown"
    )


def test_two_healthy_fresh_channels_sample_healthy() -> None:
    sample = _sample(
        _health("market_data", healthy=True, observed_at_ms=NOW_MS - 1_000),
        _health("execution", healthy=True, observed_at_ms=NOW_MS - 2_000),
    )

    assert sample.verdict == "healthy"


def test_either_unhealthy_channel_samples_unhealthy() -> None:
    sample = _sample(
        _health("market_data", healthy=True, observed_at_ms=NOW_MS - 1_000),
        _health("execution", healthy=False, observed_at_ms=NOW_MS - 1_000),
    )

    assert sample.verdict == "unhealthy"


def test_an_observation_older_than_the_window_samples_unknown() -> None:
    """A provider that has stopped observing cannot vote either way.

    Without this, a dead provider's last healthy reading would release a
    hold forever -- the "stale hold never releases" failure mode inverted.
    """
    sample = _sample(
        _health("market_data", healthy=True, observed_at_ms=NOW_MS - 1_000),
        _health("execution", healthy=True, observed_at_ms=NOW_MS - WINDOW_MS - 1),
    )

    assert sample.verdict == "unknown"


def test_the_sample_is_identified_by_its_newest_channel_observation() -> None:
    sample = _sample(
        _health("market_data", healthy=True, observed_at_ms=NOW_MS - 9_000),
        _health("execution", healthy=True, observed_at_ms=NOW_MS - 3_000),
    )

    assert sample.observed_at_ms == NOW_MS - 3_000


def test_evidence_names_only_the_broken_channels() -> None:
    refs = channel_evidence_refs(
        (
            _health("market_data", healthy=True, observed_at_ms=NOW_MS),
            _health("execution", healthy=False, observed_at_ms=NOW_MS),
        )
    )

    assert refs == ["execution: execution is unhealthy."]


def test_evidence_identity_excludes_the_observation_timestamp() -> None:
    """The churn fix (#1777 WP4 decision 4). The ledger appends only when
    this identity changes, so the same outage observed twice must produce
    byte-identical evidence."""
    first = channel_evidence_refs(
        (_health("execution", healthy=False, observed_at_ms=NOW_MS),)
    )
    later = channel_evidence_refs(
        (_health("execution", healthy=False, observed_at_ms=NOW_MS + 60_000),)
    )

    assert first == later
