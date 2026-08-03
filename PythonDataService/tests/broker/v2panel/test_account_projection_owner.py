"""Tests for the account projection owner's incremental sync (S1, spec §15).

The owner must stay *fresh*: fills appended to the journal after the first poll
have to show up on the next read. A bootstrap-once cache that never folds new
fills would freeze exposure/P&L for the process lifetime — the regression these
tests pin.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.journal import JOURNAL_FILENAME, OrderJournal
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


def test_refresh_journal_does_not_reread_history_on_warm_refresh(tmp_path: Path) -> None:
    owner = _owner()
    journal = OrderJournal(account_id=ACCT, root=tmp_path)
    first = fill_entry(sid=SID, intent="i1", ts_ms=1_000, qty=100.0)
    second = fill_entry(sid=SID, intent="i2", ts_ms=2_000, qty=50.0)
    journal.append(first)

    cold_entries = owner.refresh_journal(journal)
    assert cold_entries == [first]
    _, bytes_after_cold_replay = owner.journal_read_stats
    assert owner.refresh_journal(journal) is cold_entries
    _, bytes_after_unchanged_refresh = owner.journal_read_stats
    journal.append(second)
    appended_entries = owner.refresh_journal(journal)
    assert appended_entries == [first, second]
    assert appended_entries is not cold_entries
    assert cold_entries == [first]
    _, bytes_after_append = owner.journal_read_stats

    assert bytes_after_unchanged_refresh == bytes_after_cold_replay
    assert bytes_after_append - bytes_after_unchanged_refresh == len(
        (second.model_dump_json() + "\n").encode("utf-8")
    )


def test_refresh_journal_rebuilds_index_after_file_replacement(tmp_path: Path) -> None:
    owner = _owner()
    journal = OrderJournal(account_id=ACCT, root=tmp_path)
    original = fill_entry(sid=SID, intent="old-1", ts_ms=1_000, qty=100.0)
    second_original = fill_entry(sid=SID, intent="old-2", ts_ms=1_500, qty=25.0)
    replacement = fill_entry(sid=OTHER_SID, intent="new", ts_ms=2_000, qty=50.0)
    journal.append(original)
    journal.append(second_original)
    owner.refresh_journal(journal)

    (journal.account_dir / JOURNAL_FILENAME).unlink()
    journal.append(replacement)
    refreshed = owner.refresh_journal(journal)

    assert refreshed == [replacement]
    assert owner.entries_for_sid(SID) == []
    assert owner.entries_for_sid(OTHER_SID) == [replacement]


def test_refresh_journal_warns_without_exposing_invalid_order_reference(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = _owner()
    journal = OrderJournal(account_id=ACCT, root=tmp_path)
    malformed = fill_entry(sid=SID, intent="bad", ts_ms=1_000).model_copy(
        update={"order_ref": "not-a-valid-order-reference"}
    )
    journal.append(malformed)

    with caplog.at_level(logging.WARNING):
        owner.refresh_journal(journal)

    assert "invalid Clerk order reference" in caplog.text
    assert "not-a-valid-order-reference" not in caplog.text
    assert owner.entries_for_sid(SID) == []
