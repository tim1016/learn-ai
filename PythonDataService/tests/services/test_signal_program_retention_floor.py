"""Retention floors for sealed Signal Programs (#1740, closing #1727's last criterion).

#1727 required retention "covering warmup plus the open cycle". Two fixed
budgets bound what a program can replay after a crash or restore:

* ``SOURCE_BAR_STREAM_CAPACITY`` -- retained one-minute source bars per
  provider/symbol stream (``app.services.source_bar_ledger``). Nothing is
  pruned; the stream fails closed at capacity and needs a reviewed rollover.
* ``MAX_DECISION_RECEIPTS_PER_STRATEGY`` -- decision receipts per strategy
  instance (``app.broker.alpaca.clerk.sqlite.decision_receipts``). Every
  decision clock writes one (``no_action`` included); only ``protected_*``
  classes survive pruning, so this is the window of ordinary dispositions
  FR-016 replay can compare against.

Both are asserted against each program's own sealed contract, derived from
the registry so a future promotion is covered on registration. "The open
cycle" is bounded here by one full session -- the widest any sealed exit
path (countdown or session barrier) can stay open without a new decision.
"""

from __future__ import annotations

import math

import pytest

from app.broker.alpaca.clerk.sqlite.decision_receipts import MAX_DECISION_RECEIPTS_PER_STRATEGY
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.services.source_bar_ledger import SOURCE_BAR_STREAM_CAPACITY
from tests._helpers.signal_program import SEALED_KEYS, anchor_session_date

_MINUTE_MS = 60_000


def _ordinary_session_minutes() -> int:
    """Minutes in a regular NYSE session, from the canonical calendar, not a literal."""
    session_date = anchor_session_date()
    return (session_close_ms_utc(session_date) - session_open_ms_utc(session_date)) // _MINUTE_MS


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_declared_warmup_is_at_least_one_day(key: str) -> None:
    """``warmup_lookback_days`` also sizes FR-016 crash-candidate recreation.
    At 0 the runtime built an invalid broker duration, the failure was
    swallowed, and a Resume after a crash replayed nothing -- a shipped
    recovery path switched off by a field whose comment only discussed
    indicators (#1734). Never again."""
    contract = _STRATEGY_REGISTRY[key].signal_program_contract
    assert contract is not None
    assert contract.warmup_lookback_days >= 1, (
        f"'{key}' declares warmup_lookback_days={contract.warmup_lookback_days}; "
        "0 silently disables FR-016 crash replay"
    )


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_retention_budgets_cover_warmup_plus_one_open_cycle(key: str) -> None:
    contract = _STRATEGY_REGISTRY[key].signal_program_contract
    assert contract is not None
    session_minutes = _ordinary_session_minutes()
    sessions_needed = contract.warmup_lookback_days + 1  # +1: the open cycle, bounded by one session
    clocks_per_session = math.ceil(session_minutes * _MINUTE_MS / contract.decision_timeframe_ms)

    bars_needed = sessions_needed * session_minutes
    assert bars_needed <= SOURCE_BAR_STREAM_CAPACITY, (
        f"'{key}' needs {bars_needed} retained minute bars for warmup ({contract.warmup_lookback_days}d) "
        f"plus one open session, but the source-bar stream caps at {SOURCE_BAR_STREAM_CAPACITY}"
    )
    receipts_needed = sessions_needed * clocks_per_session
    assert receipts_needed <= MAX_DECISION_RECEIPTS_PER_STRATEGY, (
        f"'{key}' writes {receipts_needed} decision receipts over warmup plus one open session "
        f"(at its qualified {contract.decision_timeframe_ms}ms clock), but only "
        f"{MAX_DECISION_RECEIPTS_PER_STRATEGY} ordinary receipts are retained per strategy"
    )
