"""Durable source-bar and synthetic-price boundaries for Dry Run."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.synthetic_broker import SyntheticBarBindingError, SyntheticBroker
from app.broker.contract.models import BrokerOrderLeg
from app.marketdata.feed import ContinuityPolicy, FeedContinuityEvent, MarketDataBar
from app.services import source_bar_ledger
from app.services.bot_trade_strategy import _RetainedSourceBarFeed
from app.services.source_bar_ledger import (
    SourceBarCheckpointBusyError,
    SourceBarConflictError,
    SourceBarLedger,
    SourceBarLedgerCorruptError,
    SourceBarRetentionLimitError,
    verify_ledger_file,
)


def _bar(
    *,
    close: str = "501.25",
    start_ms: int = 1_700_000_000_000,
    fetched_at_ms: int = 1_700_000_061_000,
) -> MarketDataBar:
    return MarketDataBar(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal("500"),
        high=Decimal("502"),
        low=Decimal("499"),
        close=Decimal(close),
        volume=42,
        fetched_at_ms=fetched_at_ms,
        feed_id="polygon-minute",
        session_phase="RTH",
    )


def test_source_bar_ledger_is_idempotent_but_refuses_revised_identity(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")

    first = ledger.append(_bar())
    replay = ledger.append(_bar())

    assert replay == first
    assert len(ledger.bars(provider="polygon-minute", symbol="SPY")) == 1
    with pytest.raises(SourceBarConflictError, match="SOURCE_BAR_IDENTITY_CONFLICT"):
        ledger.append(_bar(close="502.25"))


def test_source_bar_ledger_treats_later_fetch_time_as_exact_redelivery(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")

    retained = ledger.append(_bar())
    replay = ledger.append(_bar(fetched_at_ms=1_700_000_099_000))

    assert replay == retained
    assert ledger.find_by_market_bar(_bar()) == retained


def test_source_bar_ledger_uses_sqlite_identity_index_after_restart(tmp_path: Path) -> None:
    original = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    original.append(_bar())
    restarted = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")

    retained = restarted.append(_bar(start_ms=1_700_000_060_000))

    assert retained.seq == 2
    assert restarted.path.name == "source_bars.sqlite3"


def test_source_bar_ledger_rejects_non_monotonic_live_delivery(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    ledger.append(_bar(start_ms=1_700_000_060_000))

    with pytest.raises(SourceBarConflictError, match="SOURCE_BAR_NON_MONOTONIC_LIVE"):
        ledger.append(_bar())


def test_source_bar_ledger_rejects_history_after_live_delivery_begins(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    ledger.append_history(_bar())
    ledger.append(_bar(start_ms=1_700_000_060_000))

    with pytest.raises(SourceBarConflictError, match="SOURCE_BAR_HISTORY_AFTER_LIVE"):
        ledger.append_history(_bar(start_ms=1_700_000_120_000))


def test_source_bar_ledger_fails_closed_at_its_explicit_retention_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_bar_ledger, "SOURCE_BAR_STREAM_CAPACITY", 1)
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    ledger.append(_bar())

    with pytest.raises(SourceBarRetentionLimitError, match="SOURCE_BAR_RETENTION_LIMIT"):
        ledger.append(_bar(start_ms=1_700_000_060_000))


@pytest.mark.asyncio
async def test_retained_feed_persists_unfiltered_stream_before_applying_rth_policy(tmp_path: Path) -> None:
    class _Source:
        feed_id = "polygon-minute"

        def __init__(self) -> None:
            self.use_rth_calls: list[bool] = []

        async def stream_bars(
            self,
            _symbol: str,
            *,
            use_rth: bool = True,
            continuity: ContinuityPolicy | None = None,
        ):
            del continuity
            self.use_rth_calls.append(use_rth)
            yield _bar().model_copy(update={"session_phase": "PRE"})
            yield _bar(start_ms=1_700_000_060_000).model_copy(update={"session_phase": "RTH"})

    source = _Source()
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    retained = _RetainedSourceBarFeed(source, ledger)

    yielded = [bar async for bar in retained.stream_bars("SPY", use_rth=True)]

    assert source.use_rth_calls == [False]
    assert [bar.session_phase for bar in yielded] == ["RTH"]
    assert [bar.session_phase for bar in ledger.bars(provider="polygon-minute", symbol="SPY")] == [
        "PRE",
        "RTH",
    ]


@pytest.mark.asyncio
async def test_synthetic_port_fills_only_from_the_retained_decision_bar_and_recovers(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    retained = ledger.append(_bar())
    broker = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)

    order = await broker.submit(
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
        client_order_id="bot:ema:enter",
    )

    assert order.status == "filled"
    assert order.filled_avg_price == float(retained.close)
    assert order.filled_at_ms == retained.end_ms
    restarted = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)
    recovered = await restarted.get_order_by_client_order_id("bot:ema:enter")
    assert recovered == order
    assert (await restarted.list_positions())[0].quantity == 2


@pytest.mark.asyncio
async def test_synthetic_port_consumes_order_scoped_exact_bar_binding(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    evaluated = ledger.append(_bar(close="501.25"))
    broker = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)
    broker.bind_evaluated_bar("bot:ema:enter", evaluated)
    ledger.append(_bar(close="502.25", start_ms=1_700_000_060_000))

    order = await broker.submit(
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
        client_order_id="bot:ema:enter",
    )

    assert order.filled_avg_price == float(evaluated.close)
    assert order.filled_at_ms == evaluated.end_ms


@pytest.mark.asyncio
async def test_synthetic_position_projection_preserves_average_cost_through_reduce_and_flip(
    tmp_path: Path,
) -> None:
    """Average-cost fold parity: buy, reduce, add, then cross through flat."""
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    broker = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)

    async def submit(order_id: str, side: str, quantity: int, close: str, offset: int) -> None:
        retained = ledger.append(_bar(close=close, start_ms=1_700_000_000_000 + offset))
        broker.bind_evaluated_bar(order_id, retained)
        await broker.submit(
            BrokerOrderLeg(symbol="SPY", side=side, quantity=quantity),
            client_order_id=order_id,
        )

    await submit("buy-1", "buy", 10, "100", 0)
    await submit("sell-reduce", "sell", 4, "120", 60_000)
    await submit("buy-add", "buy", 2, "110", 120_000)

    [long_position] = await broker.list_positions()
    assert long_position.quantity == 8
    assert long_position.average_entry_price == 102.5
    assert long_position.cost_basis == 820

    await submit("sell-flip", "sell", 10, "130", 180_000)

    [short_position] = await broker.list_positions()
    assert short_position.quantity == -2
    assert short_position.side == "short"
    assert short_position.average_entry_price == 130
    assert short_position.cost_basis == 260


def test_synthetic_port_rejects_binding_from_another_account_authority(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    retained = ledger.append(_bar())
    broker = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)

    with pytest.raises(SyntheticBarBindingError, match="different account authority"):
        broker.bind_evaluated_bar(
            "bot:ema:enter",
            retained.model_copy(update={"account_id": "sim:other"}),
        )


def _submit_in_thread(
    broker: SyntheticBroker,
    barrier: threading.Barrier,
    client_order_id: str,
) -> None:
    barrier.wait(timeout=10)
    asyncio.run(
        broker.submit(
            BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
            client_order_id=client_order_id,
        )
    )


def test_synthetic_port_serializes_concurrent_order_id_and_sequence_writes(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    ledger.append(_bar())
    first = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)
    second = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)
    barrier = threading.Barrier(2)
    duplicate = [
        threading.Thread(target=_submit_in_thread, args=(broker, barrier, "bot:ema:enter"))
        for broker in (first, second)
    ]
    for worker in duplicate:
        worker.start()
    for worker in duplicate:
        worker.join(timeout=10)
        assert not worker.is_alive()

    order_path = ledger.path.with_name("simulated_orders.jsonl")
    duplicate_rows = [json.loads(line) for line in order_path.read_text(encoding="utf-8").splitlines()]
    assert [row["seq"] for row in duplicate_rows] == [1]

    sequence_barrier = threading.Barrier(2)
    distinct = [
        threading.Thread(target=_submit_in_thread, args=(broker, sequence_barrier, client_order_id))
        for broker, client_order_id in ((first, "bot:ema:exit"), (second, "bot:ema:stop"))
    ]
    for worker in distinct:
        worker.start()
    for worker in distinct:
        worker.join(timeout=10)
        assert not worker.is_alive()

    rows = [json.loads(line) for line in order_path.read_text(encoding="utf-8").splitlines()]
    assert [row["seq"] for row in rows] == [1, 2, 3]


def test_close_checkpoints_and_truncates_the_wal(tmp_path: Path) -> None:
    """Controlled shutdown leaves no un-checkpointed WAL behind (#1740).

    A second connection is held open across ``close`` on purpose: SQLite only
    checkpoints-and-removes the WAL by itself when the LAST connection
    closes, so this proves the ledger's own explicit TRUNCATE checkpoint in
    ``close`` -- the one its ``checkpoint_wal`` docstring promises for
    shutdown -- rather than SQLite's last-connection behaviour.
    """
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="PA-WAL")
    for index in range(3):
        ledger.append_history(_bar(start_ms=1_800_000_000_000 + index * 60_000))
    wal_path = ledger.path.with_name(f"{ledger.path.name}-wal")
    assert wal_path.exists() and wal_path.stat().st_size > 0, "setup: writes must land in the WAL first"
    bystander = sqlite3.connect(ledger.path)
    try:
        # A statement is what actually opens the file; an unused connection
        # would leave the ledger's handle as the last one after all.
        assert bystander.execute("SELECT COUNT(*) FROM source_bars").fetchone()[0] == 3
        ledger.close()
        assert wal_path.exists() and wal_path.stat().st_size == 0, (
            "close() must checkpoint and truncate the WAL itself while another connection is open"
        )
    finally:
        bystander.close()


def test_close_fails_loudly_when_a_reader_still_holds_the_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``wal_checkpoint(TRUNCATE)`` reports a blocked checkpoint as a ``busy``
    column, not an exception. A close that ignored it would return normally
    with the WAL it promised to fold still on disk (#1740 review)."""
    monkeypatch.setattr(source_bar_ledger, "_BUSY_TIMEOUT_MS", 50)
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="PA-WAL")
    for index in range(3):
        ledger.append_history(_bar(start_ms=1_800_000_000_000 + index * 60_000))
    wal_path = ledger.path.with_name(f"{ledger.path.name}-wal")
    reader = sqlite3.connect(ledger.path, isolation_level=None)
    try:
        # An open read transaction pins the WAL snapshot it started on.
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM source_bars").fetchone()[0] == 3

        with pytest.raises(SourceBarCheckpointBusyError, match="SOURCE_BAR_CHECKPOINT_BUSY"):
            ledger.close()

        assert wal_path.stat().st_size > 0, "the WAL must not be reported folded while a reader pins it"
        reader.execute("COMMIT")
        ledger.close()
        assert wal_path.stat().st_size == 0
    finally:
        reader.close()


def test_ledger_refuses_another_accounts_evidence_on_open_and_in_verification(tmp_path: Path) -> None:
    """A ledger is only structurally a ledger; its rows carry the account they
    belong to. A file cut for another account must be refused both by
    ``verify_ledger_file`` (backup/restore) and by ``SourceBarLedger`` opening
    it in place (an operator copy), or foreign ``bar_ref`` evidence would mix
    with this account's new appends (#1740 review)."""
    foreign = SourceBarLedger(artifacts_root=tmp_path, account_id="PA-OTHER")
    foreign.append_history(_bar())
    foreign.close()

    assert verify_ledger_file(foreign.path, account_id="PA-OTHER") == 1
    with pytest.raises(SourceBarLedgerCorruptError, match="'PA-OTHER', not 'PA-MINE'"):
        verify_ledger_file(foreign.path, account_id="PA-MINE")

    mine_dir = tmp_path / "accounts" / "alpaca" / "PA-MINE"
    mine_dir.mkdir(parents=True)
    (mine_dir / foreign.path.name).write_bytes(foreign.path.read_bytes())
    with pytest.raises(SourceBarLedgerCorruptError, match="SOURCE_BAR_ACCOUNT_MISMATCH"):
        SourceBarLedger(artifacts_root=tmp_path, account_id="PA-MINE")

    empty = SourceBarLedger(artifacts_root=tmp_path, account_id="PA-EMPTY")
    empty.close()
    assert verify_ledger_file(empty.path, account_id="PA-MINE") == 0, (
        "an empty ledger carries no evidence and cannot be foreign"
    )


def _event(kind: str = "interruption", observed_at_ms: int = 1_700_000_000_500, **extra) -> FeedContinuityEvent:
    return FeedContinuityEvent(kind=kind, feed_id="ibkr", symbol="SPY", observed_at_ms=observed_at_ms, **extra)


def test_bars_and_events_share_one_causal_order_per_run(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        first = ledger.append(_bar(start_ms=1_700_000_000_000), run_id="run-a")
        ref = ledger.append_event(_event(cause="socket_down", generation_from=1), run_id="run-a")
        second = ledger.append(_bar(start_ms=1_700_000_060_000), run_id="run-a")
        assert first.run_id == "run-a" and first.evidence_seq is not None
        assert ref.run_id == "run-a" and ref.ref() == f"run-a:{ref.evidence_seq}"
        assert first.evidence_seq < ref.evidence_seq < second.evidence_seq
        events = ledger.events(run_id="run-a")
        assert [e.kind for e in events] == ["interruption"]
        assert events[0].cause == "socket_down" and events[0].generation_from == 1
        assert ledger.evidence_end_seq() == second.evidence_seq
        assert ledger.events(run_id="run-a", evidence_end_seq=first.evidence_seq) == []
    finally:
        ledger.close()


def test_provenance_and_continuity_columns_persist(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        substitute = _bar(start_ms=1_700_000_000_000).model_copy(
            update={
                "provenance": "historical_substitute",
                "authorization_id": "auth-1",
                "continuity_event_ref": "run-a:7",
            }
        )
        retained = ledger.append(substitute, run_id="run-a")
        assert retained.provenance == "historical_substitute"
        assert retained.authorization_id == "auth-1"
        assert retained.continuity_event_ref == "run-a:7"
        assert ledger.bars(provider="polygon-minute", symbol="SPY")[0].provenance == "historical_substitute"
    finally:
        ledger.close()


def test_pre_channel_ledger_migrates_with_a_journal_row_per_bar(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    ledger.append(_bar(start_ms=1_700_000_000_000))
    ledger.append(_bar(start_ms=1_700_000_060_000))
    path = ledger.path
    ledger.close()
    conn = sqlite3.connect(path)
    conn.executescript("DROP TABLE source_evidence_journal; DROP TABLE source_stream_events;")
    conn.execute("ALTER TABLE source_bars DROP COLUMN provenance")
    conn.execute("ALTER TABLE source_bars DROP COLUMN authorization_id")
    conn.execute("ALTER TABLE source_bars DROP COLUMN continuity_event_ref")
    conn.commit()
    conn.close()

    reopened = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        bars = reopened.bars(provider="polygon-minute", symbol="SPY")
        assert [b.provenance for b in bars] == ["realtime", "realtime"]
        assert [b.run_id for b in bars] == [None, None]
        assert bars[0].evidence_seq is not None and bars[0].evidence_seq < bars[1].evidence_seq
        assert reopened.evidence_end_seq() == bars[1].evidence_seq
    finally:
        reopened.close()


def _row_count(path: Path, table: str) -> int:
    """Count a table directly: an orphaned row is invisible to the joined readers."""
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _refuse_journal(*_args: object, **_kwargs: object) -> int:
    raise RuntimeError("journal unavailable")


def test_append_event_refuses_an_unknown_continuity_kind(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        bogus = _event(kind="interruption").model_copy(update={"kind": "bogus"})
        with pytest.raises(sqlite3.IntegrityError):
            ledger.append_event(bogus, run_id="run-a")
        assert ledger.events(run_id="run-a") == []
        assert ledger.evidence_end_seq() is None
    finally:
        ledger.close()


def test_append_event_rolls_back_the_event_when_its_journal_row_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A continuity fact with no journal position is evidence outside the run's
    causal order -- and ``events`` inner-joins the journal, so it could not even
    be read back. The event row must not survive its journal row failing."""
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        monkeypatch.setattr(SourceBarLedger, "_journal", _refuse_journal)

        with pytest.raises(RuntimeError, match="journal unavailable"):
            ledger.append_event(_event(cause="socket_down"), run_id="run-a")

        assert _row_count(ledger.path, "source_stream_events") == 0
        assert _row_count(ledger.path, "source_evidence_journal") == 0
    finally:
        ledger.close()


def test_append_rolls_back_the_bar_when_its_journal_row_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same pairing for the other half: a retained bar with no journal
    position could never be ordered against the run's continuity events."""
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        monkeypatch.setattr(SourceBarLedger, "_journal", _refuse_journal)

        with pytest.raises(RuntimeError, match="journal unavailable"):
            ledger.append(_bar(start_ms=1_700_000_000_000), run_id="run-a")

        assert _row_count(ledger.path, "source_bars") == 0
        assert _row_count(ledger.path, "source_evidence_journal") == 0
        assert ledger.bars(provider="polygon-minute", symbol="SPY") == []
    finally:
        ledger.close()


def test_journal_refuses_a_second_position_for_one_bar(tmp_path: Path) -> None:
    """Every bar read LEFT JOINs the journal, so a duplicated journal row would
    duplicate the bar itself -- and turn ``find_by_closed_end`` into a spurious
    ``SOURCE_BAR_DECISION_AMBIGUITY``. The index makes that unrepresentable."""
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        retained = ledger.append(_bar(start_ms=1_700_000_000_000), run_id="run-a")
        conn = sqlite3.connect(ledger.path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO source_evidence_journal (run_id, kind, bar_seq, observed_at_ms) "
                    "VALUES ('run-b', 'bar', ?, 1)",
                    (retained.seq,),
                )
        finally:
            conn.close()
        assert len(ledger.bars(provider="polygon-minute", symbol="SPY")) == 1
    finally:
        ledger.close()
