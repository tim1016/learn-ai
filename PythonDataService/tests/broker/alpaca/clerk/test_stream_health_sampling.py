"""Reducing a channel-health snapshot into a debounce sample (#1777 WP4).

Sampling takes the snapshot rather than the gate so the caller decides and
journals from the same observation, and judges it with the canonical
``account_scope_satisfied`` so an account-scoped hold is driven only by
account-scoped facts -- which is not the same as "connectivity only".
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


def _sample(*channels: ChannelHealth) -> str:
    return sample_channels(channels)


def test_no_snapshot_samples_unknown() -> None:
    """No gate installed is absence of evidence, not evidence of health."""
    assert sample_channels(None) == "unknown"


def test_two_connected_channels_sample_healthy() -> None:
    verdict = _sample(
        _health("market_data", healthy=True),
        _health("execution", healthy=True),
    )

    assert verdict == "healthy"


def test_either_disconnected_channel_samples_unhealthy() -> None:
    verdict = _sample(
        _health("market_data", healthy=True),
        _health("execution", healthy=False),
    )

    assert verdict == "unhealthy"


def test_connected_market_data_that_is_merely_warming_samples_healthy() -> None:
    """The account hold is account-scoped, so it may only turn on
    account-scoped facts. The unscoped market-data fact reports unhealthy
    while any one subscribed symbol is warming; keying the hold on that
    would freeze entries for every bot on one symbol -- finding S6, at
    account scope. ENTER still refuses on it, symbol-scoped."""
    verdict = _sample(
        _health("market_data", healthy=False, connected=True),
        _health("execution", healthy=True),
    )

    assert verdict == "healthy"


def test_connected_execution_that_is_unusable_samples_unhealthy() -> None:
    """Execution gets no warm-up forgiveness, because it has no warm-up.

    A `trade_updates` socket that delivered an unusable evidence frame
    reports `connected=True, healthy=False` and is broken for every symbol
    on the account. Judging the whole snapshot on connectivity relaxed this
    too, leaving the durable account-wide hold -- the strictest mechanism --
    the only one that called a broken execution channel fine while every
    submission gate rejected it.
    """
    verdict = _sample(
        _health("market_data", healthy=True),
        _health("execution", healthy=False, connected=True),
    )

    assert verdict == "unhealthy"


def test_a_channels_own_timestamp_never_changes_the_verdict() -> None:
    """The sample is a fresh pull, so nothing about it ages.

    A broken channel reports when it *broke*, frozen thereafter. An
    outage ten minutes old is exactly as actionable as one ten seconds
    old, and the verdict says so.
    """
    stale_break = _sample(
        _health("market_data", healthy=True, observed_at_ms=NOW_MS - 9_000),
        _health("execution", healthy=False, observed_at_ms=NOW_MS - 600_000),
    )
    fresh_break = _sample(
        _health("market_data", healthy=True, observed_at_ms=NOW_MS),
        _health("execution", healthy=False, observed_at_ms=NOW_MS),
    )

    assert stale_break == fresh_break == "unhealthy"


def test_evidence_names_only_the_account_scope_broken_channels() -> None:
    """A warming symbol never appears in the evidence for a hold it did not
    raise; an unusable execution frame always does."""
    refs = channel_evidence_refs(
        (
            _health("market_data", healthy=False, connected=True),
            _health("execution", healthy=False, connected=True),
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
