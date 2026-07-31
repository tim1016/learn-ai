"""Tests for the account projection owner's incremental sync (S1, spec §15).

The owner must stay *fresh*: fills appended to the journal after the first poll
have to show up on the next read. A bootstrap-once cache that never folds new
fills would freeze exposure/P&L for the process lifetime — the regression these
tests pin.
"""

from __future__ import annotations

from app.services.broker_v2_panel.account_projection_owner import AccountProjectionOwner
from tests.broker.v2panel.fixtures import (
    ACCT,
    OTHER_SID,
    SID,
    fill_entry,
    inventory_baseline_entry,
)


def _owner() -> AccountProjectionOwner:
    return AccountProjectionOwner(account_id=ACCT, broker="alpaca")


def test_sync_reflects_fills_appended_after_first_read() -> None:
    """A fill appended after the first sync is reflected on the next sync.

    Fails against a bootstrap-once owner (the cache would freeze at the first
    poll's state).
    """
    owner = _owner()
    entries = [fill_entry(sid=SID, intent="i1", ts_ms=1_000, qty=100.0, price=500.0)]
    owner.sync(entries, [SID])
    assert owner.get_rollup(SID).exposure == {"SPY": 100.0}

    # A second fill lands in the journal after the first read.
    entries = [
        *entries,
        fill_entry(sid=SID, intent="i2", ts_ms=2_000, qty=50.0, price=510.0),
    ]
    owner.sync(entries, [SID])
    assert owner.get_rollup(SID).exposure == {"SPY": 150.0}


def test_sync_bootstraps_bot_first_seen_after_initial_sync() -> None:
    """A bot that appears in the roster later is bootstrapped from full history.

    Its fills may predate the consumed watermark, so folding only the tail
    would miss them.
    """
    owner = _owner()
    entries = [
        fill_entry(sid=SID, intent="i1", ts_ms=1_000, qty=100.0),
        fill_entry(sid=OTHER_SID, intent="j1", ts_ms=1_500, qty=40.0),
    ]

    # First sync only knows SID; OTHER_SID's fill is now behind the watermark.
    owner.sync(entries, [SID])
    assert owner.get_rollup(SID).exposure == {"SPY": 100.0}
    assert owner.get_rollup(OTHER_SID).exposure == {}

    # OTHER_SID joins the roster — its historical fill must be picked up.
    owner.sync(entries, [SID, OTHER_SID])
    assert owner.get_rollup(OTHER_SID).exposure == {"SPY": 40.0}


def test_sync_rebuilds_when_journal_shrinks() -> None:
    """A journal that shrank (compaction/rotation) triggers a clean rebuild."""
    owner = _owner()
    entries = [
        fill_entry(sid=SID, intent=f"i{i}", ts_ms=i * 1_000, qty=100.0)
        for i in (1, 2, 3)
    ]
    owner.sync(entries, [SID])
    assert owner.get_rollup(SID).exposure == {"SPY": 300.0}

    # The journal now holds a single entry — the watermark no longer indexes it.
    shrunk = [fill_entry(sid=SID, intent="i1", ts_ms=1_000, qty=100.0)]
    owner.sync(shrunk, [SID])
    assert owner.get_rollup(SID).exposure == {"SPY": 100.0}


def test_sync_is_idempotent_on_repeated_identical_reads() -> None:
    """Re-syncing the same journal does not double-count (cache dedups)."""
    owner = _owner()
    entries = [fill_entry(sid=SID, intent="i1", ts_ms=1_000, qty=100.0)]
    owner.sync(entries, [SID])
    owner.sync(entries, [SID])
    owner.sync(entries, [SID])
    assert owner.get_rollup(SID).exposure == {"SPY": 100.0}


def test_sync_inventory_baseline_retires_prior_exposure_and_accepts_later_fills() -> None:
    owner = _owner()
    entries = [
        fill_entry(sid=SID, intent="old", ts_ms=1_000, qty=100.0),
    ]
    owner.sync(entries, [SID])
    assert owner.get_rollup(SID).exposure == {"SPY": 100.0}

    entries = [*entries, inventory_baseline_entry(ts_ms=2_000)]
    owner.sync(entries, [SID])
    assert owner.get_rollup(SID).exposure == {}

    entries = [
        *entries,
        fill_entry(sid=SID, intent="new", ts_ms=3_000, qty=25.0),
    ]
    owner.sync(entries, [SID])
    assert owner.get_rollup(SID).exposure == {"SPY": 25.0}
