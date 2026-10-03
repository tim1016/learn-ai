"""Retention floor for sealed Signal Programs (#1740, closing #1727's last criterion).

#1727 required retention "covering warmup plus the open cycle". Two stores
retain what a program replays after a crash or restore, and only one of them
has a floor to pin:

* ``SOURCE_BAR_STREAM_CAPACITY`` -- retained one-minute source bars per
  provider/symbol stream (``app.services.source_bar_ledger``). Nothing is
  pruned; the stream fails closed at capacity and needs a reviewed rollover.
  Asserted here: a fresh deploy's sealed warmup plus one open session fits.
* ``MAX_DECISION_RECEIPTS_PER_STRATEGY`` -- decision receipts per strategy
  instance (``app.broker.alpaca.clerk.sqlite.decision_receipts``). Not
  asserted. This file used to size that window against the warmup lookback,
  but the lookback stopped bounding what a Resume replays: a Resume replays
  every retained bar back to the instance's warm floor (#2314), so a
  long-lived instance replays more decisions than any fixed window holds.
  Replay leaves a bucket whose receipt has rotated out unflagged instead
  (#2850, ``tests/services/test_candidate_uncaptured_at_crash.py``).

The floor is checked against what a seal of each program records, derived
from the registry so a future promotion is covered on registration: the
warmup lookback its parameters resolve (#2841), at its validated settings and
at one long-period deploy. "The open cycle" is bounded here by one full
session -- the widest any sealed exit path (countdown or session barrier) can
stay open without a new decision.

Two stated limits of this floor: the session span is the regular RTH session
from the canonical calendar (a program deciding in extended hours retains
more bars over the same days than this model assumes); and the assertion only
guards absurd inputs -- 200,000 bars is ~511 sessions, so it exists to state
the invariant, not because any sealed program is near it.
"""

from __future__ import annotations

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.services.source_bar_ledger import SOURCE_BAR_STREAM_CAPACITY
from tests._helpers.signal_program import LONG_PERIODS, SEALED_KEYS, anchor_session_date

_MINUTE_MS = 60_000

_SEALED_POINTS = [
    *[pytest.param(key, {}, id=f"{key}-validated") for key in SEALED_KEYS],
    *[pytest.param(key, periods, id=f"{key}-long") for key, periods in sorted(LONG_PERIODS.items())],
]


def _ordinary_session_minutes() -> int:
    """Minutes in a regular NYSE session, from the canonical calendar, not a literal."""
    session_date = anchor_session_date()
    return (session_close_ms_utc(session_date) - session_open_ms_utc(session_date)) // _MINUTE_MS


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_declared_warmup_is_at_least_one_day(key: str) -> None:
    """``warmup_lookback_days`` is not only for indicators: a fresh deploy
    warms on it, and a Resume reaches no further back to fill the hole since
    its last run (#2314). At 0 the runtime built an invalid broker duration,
    the failure was swallowed, and a Resume after a crash replayed nothing --
    a shipped recovery path switched off by a field whose comment only
    discussed indicators (#1734). Never again."""
    contract = _STRATEGY_REGISTRY[key].signal_program_contract
    assert contract is not None
    assert contract.warmup_lookback_days >= 1, (
        f"'{key}' declares warmup_lookback_days={contract.warmup_lookback_days}; "
        "0 silently disables FR-016 crash replay"
    )


@pytest.mark.parametrize(("key", "overrides"), _SEALED_POINTS)
def test_source_bar_stream_covers_warmup_plus_one_open_cycle(key: str, overrides: dict[str, int]) -> None:
    registration = _STRATEGY_REGISTRY[key]
    contract = registration.signal_program_contract
    assert contract is not None
    params = registration.param_schema(**{**contract.validated_settings, **overrides})
    # What a seal of these parameters records, which is what the runner warms on (#2841).
    lookback_days = contract.resolved_warmup_lookback_days(params)
    session_minutes = _ordinary_session_minutes()
    sessions_needed = lookback_days + 1  # +1: the open cycle, bounded by one session

    bars_needed = sessions_needed * session_minutes
    assert bars_needed <= SOURCE_BAR_STREAM_CAPACITY, (
        f"'{key}' needs {bars_needed} retained minute bars for warmup ({lookback_days}d) "
        f"plus one open session, but the source-bar stream caps at {SOURCE_BAR_STREAM_CAPACITY}"
    )
