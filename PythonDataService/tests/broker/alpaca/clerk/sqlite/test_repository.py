"""Repository-spine adversarial tests — the Slice-2 subset of the clerk's kill, tamper and rebuild cases.

Slice 2 ("Pass focused atomicity, idempotency, identity, corruption, and
rebuild-from-mirror tests") does not yet have commands/effects/broker
contact, so the broker-facing adversarial cases (idempotent commands,
UNKNOWN reconciliation, EXIT flows) are out of scope here and land with
their owning slices. This file covers every case a bare repository spine can
meaningfully exercise, plus the mirror-fence and hash-chain mechanics from
§4/§7/§9 of ADR 0035's binding annex and ``sqlite/mirror.py``.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite import repository_lifecycle, schema
from app.broker.alpaca.clerk.sqlite.facts import (
    UncertaintyRaisedFacts,
    UncertaintyResolvedFacts,
)
from app.broker.alpaca.clerk.sqlite.mirror import MirrorChainBroken, MirrorFile
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ProjectedCommand,
    ProjectedOperation,
    ProjectedOrder,
)
from app.broker.alpaca.clerk.sqlite.registry import (
    EstablishedAccountsRegistry,
    EstablishedGeneration,
    EstablishedRegistryInvalid,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    AlreadyInitialized,
    ClerkSqliteRepository,
    DatabaseBehindMirror,
    DatabaseIdentityMismatch,
    DatabaseMissingAfterEstablishment,
    ExecutionLeaseHeld,
    HashChainBroken,
    IntegrityCheckFailed,
    UnsupportedWalFilesystem,
)
from app.services.sqlite_clerk_compat import _operation_requires_reconciliation
from tests.broker.alpaca.clerk.sqlite.conftest import _hold_transition

ACCOUNT_ID = "PA-TEST"


def _clock_seq():
    """A deterministic, strictly-increasing millisecond clock for tests."""
    counter = {"t": 1_700_000_000_000}

    def clock() -> int:
        counter["t"] += 1
        return counter["t"]

    return clock


# ---------------------------------------------------------------------------
# Initialize / open lifecycle
# ---------------------------------------------------------------------------


def test_initialize_creates_db_mirror_and_registry_entry(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    assert repo.db_path.is_file()
    snapshot = repo.control_meta_snapshot()
    assert snapshot.account_id == ACCOUNT_ID
    assert snapshot.authority_generation == 1
    assert snapshot.control_revision == 0

    registry = EstablishedAccountsRegistry(tmp_path / "accounts" / "alpaca")
    established = registry.latest(ACCOUNT_ID)
    assert established is not None
    assert established.db_identity_token == snapshot.db_identity_token
    repo.close()


def test_initialize_refuses_virtiofs_for_wal_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        repository_lifecycle,
        "_linux_filesystem_type",
        lambda _path: "virtiofs",
    )

    with pytest.raises(UnsupportedWalFilesystem, match="virtiofs"):
        ClerkSqliteRepository.initialize(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            clock=_clock_seq(),
        )

    assert not (tmp_path / "accounts" / "alpaca" / ACCOUNT_ID / "clerk.db").exists()


def test_initialize_refuses_fuseblk_for_wal_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        repository_lifecycle,
        "_linux_filesystem_type",
        lambda _path: "fuseblk",
    )

    with pytest.raises(UnsupportedWalFilesystem, match="fuseblk"):
        ClerkSqliteRepository.initialize(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            clock=_clock_seq(),
        )


def test_configure_connection_refuses_a_file_database_that_retains_a_rollback_journal(
    tmp_path: Path,
) -> None:
    """SQLite answers ``PRAGMA journal_mode = WAL`` with the mode it kept.

    A change of journal mode inside a transaction is the one fallback a test
    can provoke on a real file: SQLite leaves the mode as it was (``delete``)
    and raises nothing, exactly as a filesystem without WAL support does.
    """
    conn = sqlite3.connect(tmp_path / "clerk.db", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(UnsupportedWalFilesystem, match="'delete'"):
            schema.configure_connection(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("target", [":memory:", ""])
def test_configure_connection_accepts_a_database_with_no_file(target: str) -> None:
    conn = sqlite3.connect(target)
    try:
        schema.configure_connection(conn)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def _recorded_connections(monkeypatch: pytest.MonkeyPatch) -> list[sqlite3.Connection]:
    """Every connection ``sqlite3.connect`` opens from here on, in open order."""
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def connect(*args, **kwargs) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    return opened


def _assert_closed(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_open_closes_its_connection_when_a_pending_migration_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo._conn.execute("UPDATE control_meta SET schema_version = ? WHERE id = 1", (schema.SCHEMA_VERSION - 1,))
    repo._conn.commit()
    repo.close()

    def locked(*_args, **_kwargs) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(schema, "migrate_schema", locked)
    opened = _recorded_connections(monkeypatch)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)

    assert len(opened) == 1
    _assert_closed(opened[0])


def test_open_closes_its_connection_when_the_registry_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock_seq()
    ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock).close()
    registry_file = tmp_path / "accounts" / "alpaca" / "_established_generations.jsonl"
    with registry_file.open("a", encoding="utf-8") as handle:
        handle.write('{"account_id": "PA-OTH')  # a torn trailing line
    opened = _recorded_connections(monkeypatch)

    with pytest.raises(ValueError, match="_established_generations"):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)

    assert len(opened) == 1
    _assert_closed(opened[0])


def test_initialize_that_cannot_configure_its_connection_strands_no_database_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock_seq()
    real_configure = schema.configure_connection

    def refuse(_conn: sqlite3.Connection) -> None:
        raise UnsupportedWalFilesystem("retained 'delete'")

    monkeypatch.setattr(schema, "configure_connection", refuse)
    opened = _recorded_connections(monkeypatch)
    with pytest.raises(UnsupportedWalFilesystem):
        ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)

    assert len(opened) == 1
    _assert_closed(opened[0])
    assert not (tmp_path / "accounts" / "alpaca" / ACCOUNT_ID / "clerk.db").exists()

    # The refusal was the filesystem's, so the next initialize is a first one, not AlreadyInitialized.
    monkeypatch.setattr(schema, "configure_connection", real_configure)
    ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock).close()


def test_open_refuses_existing_wal_authority_on_virtiofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_seq(),
    )
    repo.close()
    monkeypatch.setattr(
        repository_lifecycle,
        "_linux_filesystem_type",
        lambda _path: "virtiofs",
    )

    with pytest.raises(UnsupportedWalFilesystem, match="virtiofs"):
        ClerkSqliteRepository.open(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            clock=_clock_seq(),
        )


def test_reconcilable_effect_projection_matches_authority_for_every_state_and_order_status(
    tmp_path: Path,
) -> None:
    """#1412: the panel predicate stays in parity with the authority query."""
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_seq(),
    )
    repo.register_strategy_instance(
        strategy_instance_id="spy",
        symbol="SPY",
        config_hash="h1",
    )
    states = (
        "reserved",
        "accepted",
        "in_progress",
        "unknown",
        "succeeded",
        "failed",
        "rejected",
    )
    projected: list[ProjectedOperation] = []
    now_ms = 1_700_000_000_000
    for state in states:
        for kind in ("ENTER", "EXIT"):
            for order_status in (None, "new", "filled"):
                suffix = order_status or "absent"
                operation_id = f"op-{state}-{kind.lower()}-{suffix}"
                command_id = f"cmd-{state}-{kind.lower()}-{suffix}"
                repo._conn.execute(
                    "INSERT INTO commands "
                    "(command_id, authority_generation, subject_id, idempotency_key, payload_hash, "
                    "kind, strategy_instance_id, run_id, action, intended_end_state, state, "
                    "effect_operation_id, receipt_id, created_at_ms, updated_at_ms) "
                    "VALUES (?, 1, 'bot:spy', ?, 'hash', 'strategy_decision', 'spy', NULL, ?, NULL, "
                    "?, NULL, NULL, ?, ?)",
                    (command_id, command_id, kind, state, now_ms, now_ms),
                )
                repo._conn.execute(
                    "INSERT INTO effect_operations "
                    "(effect_operation_id, authority_generation, subject_id, idempotency_key, command_id, "
                    "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, "
                    "updated_at_ms) VALUES (?, 1, 'bot:spy', ?, ?, 'spy', NULL, ?, ?, "
                    "'ACCOUNT_CLERK', ?, ?)",
                    (operation_id, operation_id, command_id, kind, state, now_ms, now_ms),
                )
                orders: tuple[ProjectedOrder, ...] = ()
                if order_status is not None:
                    order_ref = f"order-{state}-{kind.lower()}-{order_status}"
                    role = "ENTRY" if kind == "ENTER" else "REDUCING"
                    repo._conn.execute(
                        "INSERT INTO orders "
                        "(order_ref, effect_operation_id, client_order_id, broker_order_id, "
                        "role, broker_state, submitted_at_ms, updated_at_ms) "
                        "VALUES (?, ?, ?, NULL, ?, ?, ?, ?)",
                        (order_ref, operation_id, order_ref, role, order_status, now_ms, now_ms),
                    )
                    repo._conn.execute(
                        "INSERT INTO operation_order_links "
                        "(effect_operation_id, order_ref, role, linked_at_ms) "
                        "VALUES (?, ?, ?, ?)",
                        (operation_id, order_ref, role, now_ms),
                    )
                    orders = (
                        ProjectedOrder(
                            order_ref=order_ref,
                            client_order_id=order_ref,
                            broker_order_id=None,
                            role=role,
                            broker_state=order_status,
                            submitted_at_ms=now_ms,
                            updated_at_ms=now_ms,
                            filled_quantity=(0.0 if order_status == "filled" else None),
                        ),
                    )
                projected.append(
                    ProjectedOperation(
                        effect_operation_id=operation_id,
                        kind=kind,
                        state=state,
                        custody_owner="ACCOUNT_CLERK",
                        strategy_instance_id="spy",
                        run_id=None,
                        created_at_ms=now_ms,
                        updated_at_ms=now_ms,
                        latest_transition_sequence=0,
                        transition_count=0,
                        terminal_receipt_id=None,
                        command=ProjectedCommand(
                            command_id=command_id,
                            kind="strategy_decision",
                            action=kind,
                            state=state,
                            run_id=None,
                            receipt_id=None,
                            created_at_ms=now_ms,
                            updated_at_ms=now_ms,
                        ),
                        orders=orders,
                    )
                )
    repo._conn.commit()

    authority_ids = {
        operation.effect_operation_id
        for operation in repo.reconcilable_effect_operations()
    }
    projection_ids = {
        operation.effect_operation_id
        for operation in projected
        if _operation_requires_reconciliation(operation)
    }

    assert projection_ids == authority_ids
    repo.close()


def test_initialize_twice_raises_already_initialized(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    repo.close()
    with pytest.raises(AlreadyInitialized):
        ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())


def test_open_a_never_initialized_account_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ClerkSqliteRepository.open(account_id="NEVER-SEEN", artifacts_root=tmp_path, clock=_clock_seq())


def test_remove_db_after_established_is_not_silently_recreated(tmp_path: Path) -> None:
    """Remove ``clerk.db`` after authority was established and prove it is not
    recreated. Closes the gap the Slice-1 review found."""
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    db_path = repo.db_path
    repo.close()
    db_path.unlink()

    with pytest.raises(DatabaseMissingAfterEstablishment):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    # And initialize() must refuse too — it is not a "new account" anymore.
    with pytest.raises(DatabaseMissingAfterEstablishment):
        ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())


def test_reopen_after_close_preserves_state(tmp_path: Path) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    repo.close()

    reopened = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    assert len(reopened.strategy_instances()) == 1
    assert reopened.control_meta_snapshot().control_revision == 1
    reopened.close()


# ---------------------------------------------------------------------------
# Atomicity / hash chain
# ---------------------------------------------------------------------------


def test_append_transition_advances_sequence_hash_chain_and_revision(tmp_path: Path) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    first = repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    second = repo.register_strategy_instance(strategy_instance_id="qqq-bot", symbol="QQQ", config_hash="h2")

    assert first.sequence == 1
    assert first.prev_hash == "GENESIS"
    assert second.sequence == 2
    assert second.prev_hash == first.row_hash
    assert second.control_revision == 2
    repo.close()


def test_disk_full_before_mirror_prepare_fails_closed_without_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill before the command commit: no command, no broker call.
    Generalized to the spine: if the mirror PREPARE fsync fails, no SQLite
    transaction opens and no row is appended."""
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)

    def boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device during mirror prepare fsync")

    monkeypatch.setattr(repo._mirror, "prepare", boom)
    with pytest.raises(OSError, match="No space left on device"):
        repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")

    assert repo.custody_transitions() == []
    assert repo.strategy_instances() == []
    assert repo.control_meta_snapshot().control_revision == 0
    repo.close()


def test_kill_after_sqlite_commit_before_mirror_finalize_reopen_finalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill after SQLite commit but before mirror finalization: an intact DB
    finalizes on restart."""
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)

    def boom(*_args, **_kwargs):
        raise OSError("simulated crash between SQLite commit and mirror finalize")

    monkeypatch.setattr(repo._mirror, "finalize", boom)
    with pytest.raises(OSError):
        repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")

    # The SQLite side is durable even though finalize never ran.
    assert len(repo.custody_transitions()) == 1
    assert len(repo.strategy_instances()) == 1
    committed_sequence = repo.custody_transitions()[0]["sequence"]
    assert repo._mirror.has_finalize(committed_sequence) is False
    repo.close()

    # Startup check 9 finalizes the dangling PREPARE from an intact DB.
    reopened = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    assert reopened._mirror.has_finalize(committed_sequence) is True
    reopened.close()


# ---------------------------------------------------------------------------
# Corruption / identity / integrity
# ---------------------------------------------------------------------------


def test_corrupt_database_page_fails_closed_on_open(tmp_path: Path) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    db_path = repo.db_path
    repo.close()

    with db_path.open("r+b") as handle:
        handle.seek(100)
        handle.write(b"\xff" * 200)

    with pytest.raises(IntegrityCheckFailed):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    # The corrupt file is preserved for diagnosis, never overwritten.
    assert db_path.is_file()


def test_tampered_custody_transitions_row_fails_hash_chain_check(tmp_path: Path) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    db_path = repo.db_path
    repo.close()

    # A real tamper (raw file edit, or DB surgery from an older buggy build)
    # would not go through the append-only trigger the way a same-process
    # caller would — drop it first so the UPDATE below reaches the row, the
    # way an out-of-band tamper effectively would.
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER trg_custody_transitions_immutable_update")
    conn.execute("UPDATE custody_transitions SET operation_state = 'in_progress' WHERE sequence = 1")
    conn.commit()
    conn.close()

    with pytest.raises(HashChainBroken):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)


def test_substituted_database_rejected_on_identity_mismatch(tmp_path: Path) -> None:
    clock = _clock_seq()
    account_a = ClerkSqliteRepository.initialize(account_id="PA-AAAA", artifacts_root=tmp_path, clock=clock)
    account_a.close()
    account_b = ClerkSqliteRepository.initialize(account_id="PA-BBBB", artifacts_root=tmp_path, clock=clock)
    account_b.close()

    # Substitute account B's file in for account A's.
    a_dir = tmp_path / "accounts" / "alpaca" / "PA-AAAA"
    b_dir = tmp_path / "accounts" / "alpaca" / "PA-BBBB"
    shutil.copyfile(b_dir / "clerk.db", a_dir / "clerk.db")

    with pytest.raises(DatabaseIdentityMismatch):
        ClerkSqliteRepository.open(account_id="PA-AAAA", artifacts_root=tmp_path, clock=clock)


@pytest.mark.parametrize(
    ("bad_line", "reason", "refused_for"),
    [
        ('{"account_id": "PA-OTH', "JSONDecodeError", (ACCOUNT_ID, "PA-OTHER", "PA-NEVER-SEEN")),
        ('["PA-OTHER", 1]', "TypeError", (ACCOUNT_ID, "PA-OTHER", "PA-NEVER-SEEN")),
        ('{"account_id": "PA-OTHER", "authority_generation": 1}', "TypeError", ("PA-OTHER",)),
    ],
)
def test_one_unreadable_registry_line_refuses_the_read_naming_its_line(
    tmp_path: Path, bad_line: str, reason: str, refused_for: tuple[str, ...]
) -> None:
    """The registry file is shared, so a torn line must not read as "never established".

    Skipping it would let ``latest`` answer ``None`` for an account whose
    only entry that line was, and ``initialize`` would then mint generation 1
    over an established authority.
    """
    registry = EstablishedAccountsRegistry(tmp_path)
    registry.record(
        EstablishedGeneration(
            account_id=ACCOUNT_ID, authority_generation=1, db_identity_token="tok", established_at_ms=1
        )
    )
    with (tmp_path / "_established_generations.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(bad_line + "\n")

    for account_id in refused_for:
        with pytest.raises(EstablishedRegistryInvalid, match=r"_established_generations\.jsonl line 2") as refused:
            registry.latest(account_id)
        assert reason in str(refused.value)


def test_database_behind_its_mirror_fails_closed_on_open_without_repair(tmp_path: Path) -> None:
    """A clerk.db that lost its committed tail must not open (#3161).

    Startup check 9 walked only the rows the database still has, so an older
    file copied over the live one passed: every surviving row matched its
    FINALIZE. The mirror finalized sequences the database does not hold, which
    is the rewound-database case the mirror exists to make visible.
    """
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    db_path = repo.db_path
    mirror_path = repo.mirror_path
    repo.close()
    behind_copy = tmp_path / "behind.db"
    shutil.copyfile(db_path, behind_copy)

    repo = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="qqq-bot", symbol="QQQ", config_hash="h2")
    repo.register_strategy_instance(strategy_instance_id="iwm-bot", symbol="IWM", config_hash="h3")
    assert len(repo.custody_transitions()) == 3
    repo.close()

    for sidecar in (Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        sidecar.unlink(missing_ok=True)
    shutil.copyfile(behind_copy, db_path)
    mirror_before = mirror_path.read_bytes()
    database_before = db_path.read_bytes()

    with pytest.raises(DatabaseBehindMirror, match="behind its mirror"):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)

    # Refused without taking the lease or repairing either file.
    assert mirror_path.read_bytes() == mirror_before
    assert db_path.read_bytes() == database_before
    inspected = sqlite3.connect(db_path)
    try:
        (lease_owner,) = inspected.execute(
            "SELECT execution_lease_owner FROM control_meta WHERE id = 1"
        ).fetchone()
    finally:
        inspected.close()
    assert lease_owner is None


def test_open_racing_a_live_writer_is_refused_as_lease_held_not_as_behind_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holder that appends between the opener's database read and its mirror
    parse leaves the mirror ahead of the opener's snapshot (#3161).

    That is a held lease, not a lost tail: the restart path retries only
    ``ExecutionLeaseHeld``, and the account-held-elsewhere mapping keys on it.
    """
    clock = _clock_seq()
    live = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_owner="process-A"
    )
    live.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    parse = MirrorFile._parsed_records
    appended: list[object] = []

    def parse_after_a_live_append(self: MirrorFile):
        if not appended:
            appended.append(
                live.register_strategy_instance(
                    strategy_instance_id="qqq-bot", symbol="QQQ", config_hash="h2"
                )
            )
        return parse(self)

    monkeypatch.setattr(MirrorFile, "_parsed_records", parse_after_a_live_append)

    with pytest.raises(ExecutionLeaseHeld):
        ClerkSqliteRepository.open(
            account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_owner="process-B"
        )
    assert appended
    live.close()


def test_execution_lease_blocks_a_second_concurrent_open(tmp_path: Path) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_owner="process-A"
    )
    with pytest.raises(ExecutionLeaseHeld):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_owner="process-B")
    repo.close()
    # After close (which releases the lease), a second process may open it.
    other = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_owner="process-B"
    )
    other.close()


def test_a_stale_handle_s_close_does_not_release_the_new_owner_s_lease(tmp_path: Path) -> None:
    """The lease is the live writer's fence, so only its holder releases it (#2620).

    A handle whose lease expired and was taken over used to clear whatever
    lease row it found on close, silently unfencing the successor process.
    """
    clock = _clock_seq()
    stale = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_owner="process-A"
    )
    # process-A's lease expires; process-B takes over the account.
    stale._conn.execute("UPDATE control_meta SET execution_lease_expires_at_ms = 1 WHERE id = 1")
    stale._conn.commit()
    live = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_owner="process-B"
    )

    stale.close()  # the taken-over handle shuts down after the takeover

    owner, expires_at = live._conn.execute(
        "SELECT execution_lease_owner, execution_lease_expires_at_ms FROM control_meta WHERE id = 1"
    ).fetchone()
    assert owner == "process-B", "a stale handle released the successor's lease"
    assert expires_at is not None, "a stale handle expired the successor's lease"
    live.close()


def test_close_closes_the_connection_even_when_the_lease_release_fails(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    repo._conn.execute("PRAGMA busy_timeout = 20")
    rival = sqlite3.connect(repo.db_path, isolation_level=None)
    rival.execute("BEGIN IMMEDIATE")  # holds the write lock past the busy timeout
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            repo.close()
    finally:
        rival.execute("ROLLBACK")
        rival.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        repo._conn.execute("SELECT 1")


def test_expired_lease_allows_a_new_process_to_take_over(tmp_path: Path) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_owner="process-A"
    )
    # Simulate process-A crashing without releasing its lease, long enough
    # for the lease to have naturally expired (the repository has no other
    # way to distinguish "crashed" from "still alive" — expiry is the fence).
    repo._conn.execute("UPDATE control_meta SET execution_lease_expires_at_ms = 1 WHERE id = 1")
    repo._conn.commit()

    other = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_owner="process-B"
    )
    other.close()
    repo._conn.close()  # avoid leaking the first connection in the test process


# ---------------------------------------------------------------------------
# Mirror rebuild
# ---------------------------------------------------------------------------


def test_rebuild_from_mirror_reconstructs_identical_state(tmp_path: Path) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    repo.register_strategy_instance(strategy_instance_id="qqq-bot", symbol="QQQ", config_hash="h2")
    original_transitions = repo.custody_transitions()
    original_instances = repo.strategy_instances()
    db_path = repo.db_path
    repo.close()

    corrupt_path = db_path.with_suffix(".db.corrupt")
    db_path.rename(corrupt_path)

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    assert rebuilt.custody_transitions() == original_transitions
    assert rebuilt.strategy_instances() == original_instances
    rebuilt.close()


def test_rebuild_refuses_when_db_path_already_exists(tmp_path: Path) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.close()

    with pytest.raises(AlreadyInitialized):
        ClerkSqliteRepository.rebuild_from_mirror(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)


def test_tampered_mirror_line_fails_closed_on_rebuild(tmp_path: Path) -> None:
    """Tamper with a mirror line and prove hash-chain-break detection fails
    closed (no tampered import)."""
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    mirror_path = repo.mirror_path
    db_path = repo.db_path
    repo.close()
    db_path.unlink()

    import re

    lines = mirror_path.read_text().splitlines()
    # Flip one hex character in the top-level (unescaped) row_hash field —
    # payload_canonical's own contents are double-JSON-escaped, so a naive
    # substring match on plain text inside it would silently match nothing.
    prepare_index = next(
        index for index, line in enumerate(lines) if '"phase":"PREPARE"' in line
    )
    tampered = re.sub(
        r'"row_hash":"([0-9a-f])',
        lambda m: f'"row_hash":"{"0" if m.group(1) != "0" else "1"}',
        lines[prepare_index],
        count=1,
    )
    assert tampered != lines[prepare_index]
    lines[prepare_index] = tampered
    mirror_path.write_text("\n".join(lines) + "\n")

    with pytest.raises(MirrorChainBroken):
        ClerkSqliteRepository.rebuild_from_mirror(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    # No half-built database was left behind.
    assert not db_path.is_file()


def test_mirror_sequence_gap_fails_closed_on_rebuild(tmp_path: Path) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    repo.register_strategy_instance(strategy_instance_id="qqq-bot", symbol="QQQ", config_hash="h2")
    mirror_path = repo.mirror_path
    db_path = repo.db_path
    repo.close()
    db_path.unlink()

    lines = mirror_path.read_text().splitlines()
    # Drop sequence 1's PREPARE+FINALIZE, keeping sequence 2's — a gap.
    # "sequence" sorts last among json.dumps(sort_keys=True)'s keys, so it is
    # always followed by the closing brace, never a comma.
    remaining = [line for line in lines if '"sequence":1}' not in line]
    assert len(remaining) == len(lines) - 2
    mirror_path.write_text("\n".join(remaining) + "\n")

    with pytest.raises(MirrorChainBroken):
        ClerkSqliteRepository.rebuild_from_mirror(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)


@pytest.mark.parametrize(
    "record",
    [{"sequence": 1, "row_hash": "h"}, {"phase": "BOGUS", "sequence": 1, "row_hash": "h"}],
    ids=["no_phase", "unknown_phase"],
)
def test_a_mirror_record_with_no_known_phase_is_a_chain_break(tmp_path: Path, record: dict) -> None:
    mirror_path = tmp_path / "custody_transitions.mirror"
    mirror_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(MirrorChainBroken, match="unknown mirror record phase"):
        MirrorFile(mirror_path).rebuild()


def test_an_append_after_a_torn_mirror_tail_is_refused_not_glued_on(tmp_path: Path) -> None:
    """A write that died part-way leaves bytes with no newline.

    The next append used to land on those bytes and succeed, and the open
    after it failed on a line that was never one record. The mirror is
    custody evidence: it refuses, and it does not trim the tail itself.
    """
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    with repo.mirror_path.open("ab") as handle:
        handle.write(b'{"phase":"PREPARE","seq')
    torn = repo.mirror_path.read_bytes()

    with pytest.raises(MirrorChainBroken, match="partial"):
        repo.register_strategy_instance(strategy_instance_id="qqq-bot", symbol="QQQ", config_hash="h2")

    assert repo.mirror_path.read_bytes() == torn
    assert [row["sequence"] for row in repo.custody_transitions()] == [1]
    repo.close()


def test_an_append_after_a_complete_record_that_lost_only_its_newline_restores_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A write that died between the record and its newline lost nothing but the newline."""
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    intact = repo.mirror_path.read_bytes()
    assert intact.endswith(b"\n")
    repo.mirror_path.write_bytes(intact[:-1])

    with caplog.at_level(logging.WARNING):
        repo.register_strategy_instance(strategy_instance_id="qqq-bot", symbol="QQQ", config_hash="h2")

    restored = repo.mirror_path.read_bytes()
    assert restored.startswith(intact)
    assert [json.loads(line)["phase"] for line in restored.splitlines()] == [
        "IDENTITY",
        "PREPARE",
        "FINALIZE",
        "PREPARE",
        "FINALIZE",
    ]
    assert [r.action for r in caplog.records if getattr(r, "action", None) == "mirror_tail_newline_restored"]
    repo.close()
    reopened = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    assert len(reopened.custody_transitions()) == 2
    reopened.close()


def test_an_unterminated_record_longer_than_one_read_block_is_still_found_whole(tmp_path: Path) -> None:
    mirror_path = tmp_path / "custody_transitions.mirror"
    long_record = json.dumps({"phase": "PREPARE", "sequence": 1, "payload_canonical": "x" * 200_000})
    mirror_path.write_text('{"phase":"IDENTITY"}\n' + long_record, encoding="utf-8")

    MirrorFile(mirror_path).finalize(sequence=1, authority_generation=1, row_hash="h", recorded_at_ms=1)

    lines = mirror_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["phase"] for line in lines] == ["IDENTITY", "PREPARE", "FINALIZE"]


def test_mirror_prepare_without_finalize_is_excluded_not_imported(tmp_path: Path) -> None:
    """A PREPARE with no matching FINALIZE is an aborted preparation — dropped
    silently from rebuild, not an error by itself, because the fence
    guarantees it had no broker effect."""
    mirror_path = tmp_path / "orphan.mirror"
    mirror = MirrorFile(mirror_path)
    from app.broker.alpaca.clerk.sqlite.hashchain import (
        GENESIS,
        canonical_payload,
        compute_row_hash,
    )
    from app.broker.alpaca.clerk.sqlite.mirror import PendingTransition

    row = {
        "authority_generation": 1,
        "strategy_instance_id": None,
        "run_id": None,
        "command_id": None,
        "effect_operation_id": None,
        "order_ref": None,
        "broker_order_id": None,
        "transition_kind": "K",
        "custody_owner": "ACCOUNT_CLERK",
        "execution_authority": "ACCOUNT_CLERK",
        "operation_state": "reserved",
        "broker_state": None,
        "proof_reference": None,
        "source_event_at_ms": None,
        "clerk_observed_at_ms": 1,
        "recorded_at_ms": 1,
        "summary_code": "C",
        "facts_schema_version": 1,
        "facts_json": "{}",
    }
    payload = canonical_payload(row)
    row_hash = compute_row_hash(GENESIS, payload)
    mirror.prepare(
        PendingTransition(
            sequence=1,
            authority_generation=1,
            row_hash=row_hash,
            prev_hash=GENESIS,
            payload_canonical=payload,
            recorded_at_ms=1,
        )
    )
    # No finalize call — simulates a crash right after step 1.
    assert mirror.rebuild() == []


def _mirror_row(summary_code: str) -> dict:
    return {
        "authority_generation": 1,
        "strategy_instance_id": None,
        "run_id": None,
        "command_id": None,
        "effect_operation_id": None,
        "order_ref": None,
        "broker_order_id": None,
        "transition_kind": "K",
        "custody_owner": "ACCOUNT_CLERK",
        "execution_authority": "ACCOUNT_CLERK",
        "operation_state": "reserved",
        "broker_state": None,
        "proof_reference": None,
        "source_event_at_ms": None,
        "clerk_observed_at_ms": 1,
        "recorded_at_ms": 1,
        "summary_code": summary_code,
        "facts_schema_version": 1,
        "facts_json": "{}",
    }


def test_mirror_rebuild_tolerates_an_abandoned_prepare_reusing_a_sequence(tmp_path: Path) -> None:
    """A failed fold (e.g. the concurrent-decision race a unique constraint
    catches, #1376's review) can leave an unfinalized PREPARE at a sequence
    that the next successful append then reuses with a different payload —
    next_sequence is computed from the *committed* row count, which the
    failed attempt never joined. That abandoned attempt must not be
    mistaken for tampering: only the PREPARE matching the sequence's
    FINALIZE is authoritative."""
    from app.broker.alpaca.clerk.sqlite.hashchain import (
        GENESIS,
        canonical_payload,
        compute_row_hash,
    )
    from app.broker.alpaca.clerk.sqlite.mirror import PendingTransition

    mirror = MirrorFile(tmp_path / "reused.mirror")

    abandoned_payload = canonical_payload(_mirror_row("ABANDONED"))
    abandoned_hash = compute_row_hash(GENESIS, abandoned_payload)
    mirror.prepare(
        PendingTransition(
            sequence=1,
            authority_generation=1,
            row_hash=abandoned_hash,
            prev_hash=GENESIS,
            payload_canonical=abandoned_payload,
            recorded_at_ms=1,
        )
    )
    # No finalize for the abandoned attempt — its owning SQLite commit failed.

    real_payload = canonical_payload(_mirror_row("REAL"))
    real_hash = compute_row_hash(GENESIS, real_payload)
    mirror.prepare(
        PendingTransition(
            sequence=1,
            authority_generation=1,
            row_hash=real_hash,
            prev_hash=GENESIS,
            payload_canonical=real_payload,
            recorded_at_ms=2,
        )
    )
    mirror.finalize(sequence=1, authority_generation=1, row_hash=real_hash, recorded_at_ms=2)

    rebuilt = mirror.rebuild()
    assert len(rebuilt) == 1
    assert rebuilt[0].row_hash == real_hash
    assert rebuilt[0].payload["summary_code"] == "REAL"


def test_mirror_rebuild_still_fails_closed_on_conflicting_finalize(tmp_path: Path) -> None:
    """Two FINALIZE records at the same sequence with different hashes is
    genuine corruption (SQLite's own PRIMARY KEY on ``sequence`` would
    prevent this from ever arising on a real commit path) and must still
    fail closed — unlike the benign abandoned-PREPARE case above."""
    from app.broker.alpaca.clerk.sqlite.hashchain import (
        GENESIS,
        canonical_payload,
        compute_row_hash,
    )
    from app.broker.alpaca.clerk.sqlite.mirror import PendingTransition

    mirror = MirrorFile(tmp_path / "conflict.mirror")

    for summary_code, recorded_at in (("A", 1), ("B", 2)):
        payload = canonical_payload(_mirror_row(summary_code))
        row_hash = compute_row_hash(GENESIS, payload)
        mirror.prepare(
            PendingTransition(
                sequence=1,
                authority_generation=1,
                row_hash=row_hash,
                prev_hash=GENESIS,
                payload_canonical=payload,
                recorded_at_ms=recorded_at,
            )
        )
        mirror.finalize(sequence=1, authority_generation=1, row_hash=row_hash, recorded_at_ms=recorded_at)

    with pytest.raises(MirrorChainBroken):
        mirror.rebuild()


# ---------------------------------------------------------------------------
# #1378 primitives: ACCOUNT_HOLD_RAISED fold + active_hold/uncertain_orders/
# attributed_positions_by_symbol read helpers. Exercised directly through
# append_transition() here (no ENTER/reconciliation domain flow needed) —
# the full sweep-driven flow is covered in test_reconcile.py.
# ---------------------------------------------------------------------------


def test_account_hold_raised_fold_creates_an_active_account_clerk_hold(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    repo.append_transition(_hold_transition(evidence_refs=["bo-1", "bo-2"]))

    hold = repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER_HOLD")
    assert hold is not None
    assert hold["state"] == "ACTIVE"
    assert hold["strategy_instance_id"] is None
    assert json.loads(hold["evidence_refs_json"]) == ["bo-1", "bo-2"]
    repo.close()


def test_two_hold_transitions_each_mint_a_distinct_hold_id(tmp_path: Path) -> None:
    """Two independent raises (e.g. two different reason codes, or a test
    directly exercising the fold twice) must never collide on hold_id — the
    id is minted from the transition's own globally-unique sequence, not
    from content or a timestamp that a fast test clock could repeat."""
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    repo.append_transition(_hold_transition(reason_code="UNEXPLAINED_ORDER_HOLD"))
    repo.append_transition(_hold_transition(reason_code="STREAM_HEALTH_HOLD"))

    first = repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER_HOLD")
    second = repo.active_hold(scope="ACCOUNT_CLERK", reason_code="STREAM_HEALTH_HOLD")
    assert first is not None and second is not None
    assert first["hold_id"] != second["hold_id"]
    repo.close()


def test_attributed_positions_by_symbol_sums_across_bots(tmp_path: Path) -> None:
    """Account-wide comparison needs the sum across every bot's namespace-
    attributed position, not any single bot's row (schema.py's ``positions``
    comment: never derived by netting the raw broker position, but the
    account-wide sweep still needs one number per symbol to compare)."""
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    with repo._write_lock:
        repo._conn.execute(
            "INSERT INTO strategy_instances (strategy_instance_id, symbol, config_hash, "
            "created_at_ms, retired_at_ms) VALUES ('bot-a', 'SPY', 'h', 1, NULL)"
        )
        repo._conn.execute(
            "INSERT INTO strategy_instances (strategy_instance_id, symbol, config_hash, "
            "created_at_ms, retired_at_ms) VALUES ('bot-b', 'SPY', 'h', 1, NULL)"
        )
        repo._conn.executemany(
            "INSERT INTO custody_subjects "
            "(subject_id, kind, strategy_instance_id, operator_id, created_at_ms) "
            "VALUES (?, 'BOT', ?, NULL, 1)",
            (("bot:bot-a", "bot-a"), ("bot:bot-b", "bot-b")),
        )
        repo._conn.execute(
            "INSERT INTO positions (subject_id, strategy_instance_id, symbol, attributed_qty, updated_at_ms) "
            "VALUES ('bot:bot-a', 'bot-a', 'SPY', 0.1, 1)"
        )
        repo._conn.execute(
            "INSERT INTO positions (subject_id, strategy_instance_id, symbol, attributed_qty, updated_at_ms) "
            "VALUES ('bot:bot-b', 'bot-b', 'SPY', 0.2, 1)"
        )
        repo._conn.commit()

    # Golden fractional aggregation; the tolerance matches
    # POSITION_QTY_EPSILON (1e-9).
    assert repo.attributed_positions_by_symbol() == {"SPY": pytest.approx(0.3, abs=1e-9)}
    repo.close()


# ---------------------------------------------------------------------------
# #1380 primitives: UNCERTAINTY_RAISED/RESOLVED folds +
# active_uncertainty*/active_holds_for_admission read helpers.
# ---------------------------------------------------------------------------


def _uncertainty_transition(
    *,
    strategy_instance_id: str | None,
    reason_code: str = "TEST_REASON",
    blocks_new_exposure: bool = True,
    allows_reduction: bool = True,
) -> TransitionInput:
    facts = UncertaintyRaisedFacts(
        severity="warning",
        blocks_new_exposure=blocks_new_exposure,
        allows_reduction=allows_reduction,
        reason_code=reason_code,
        headline="Test headline",
        explanation="Test explanation",
        operator_impact="Test operator impact",
        next_step="Test next step",
        evidence_refs=["ev-1"],
    )
    return TransitionInput(
        strategy_instance_id=strategy_instance_id,
        transition_kind="UNCERTAINTY_RAISED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="succeeded",
        clerk_observed_at_ms=1,
        summary_code="UNCERTAINTY_RAISED",
        facts_json=facts.to_facts_json(),
    )


def test_uncertainty_raised_fold_creates_an_active_account_clerk_uncertainty(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    repo.append_transition(_uncertainty_transition(strategy_instance_id=None))

    uncertainty = repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code="TEST_REASON", strategy_instance_id=None)
    assert uncertainty is not None
    assert uncertainty["scope"] == "ACCOUNT_CLERK"
    assert uncertainty["strategy_instance_id"] is None
    assert uncertainty["blocks_new_exposure"] == 1
    assert uncertainty["resolved_at_ms"] is None
    assert json.loads(uncertainty["evidence_refs_json"]) == ["ev-1"]
    repo.close()


def test_uncertainty_raised_fold_creates_an_active_bot_scoped_uncertainty(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    with repo._write_lock:
        repo._conn.execute(
            "INSERT INTO strategy_instances (strategy_instance_id, symbol, config_hash, "
            "created_at_ms, retired_at_ms) VALUES ('spy-bot', 'SPY', 'h', 1, NULL)"
        )
        repo._conn.execute(
            "INSERT INTO custody_subjects "
            "(subject_id, kind, strategy_instance_id, operator_id, created_at_ms) "
            "VALUES ('bot:spy-bot', 'BOT', 'spy-bot', NULL, 1)"
        )
        repo._conn.commit()

    repo.append_transition(_uncertainty_transition(strategy_instance_id="spy-bot"))

    uncertainty = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code="TEST_REASON",
        strategy_instance_id="spy-bot",
    )
    assert uncertainty is not None
    assert uncertainty["scope"] == "CUSTODY_SUBJECT"
    assert uncertainty["strategy_instance_id"] == "spy-bot"

    # A different bot's identical reason_code must not collide.
    assert repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code="TEST_REASON",
        strategy_instance_id="qqq-bot",
    ) is None
    repo.close()


def test_uncertainty_resolved_fold_closes_the_active_row(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    repo.append_transition(_uncertainty_transition(strategy_instance_id=None))
    uncertainty = repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code="TEST_REASON", strategy_instance_id=None)
    assert uncertainty is not None

    resolved_facts = UncertaintyResolvedFacts(
        uncertainty_id=uncertainty["uncertainty_id"],
        resolution_kind="CLEAN_BROKER_RECONCILIATION",
        evidence_refs=["fresh_snapshot"],
    )
    repo.append_transition(
        TransitionInput(
            transition_kind="UNCERTAINTY_RESOLVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=2,
            summary_code="UNCERTAINTY_RESOLVED",
            facts_json=resolved_facts.to_facts_json(),
        )
    )

    assert repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code="TEST_REASON", strategy_instance_id=None) is None
    repo.close()


def test_active_uncertainties_for_admission_includes_account_and_bot_scope(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    with repo._write_lock:
        repo._conn.execute(
            "INSERT INTO strategy_instances (strategy_instance_id, symbol, config_hash, "
            "created_at_ms, retired_at_ms) VALUES ('spy-bot', 'SPY', 'h', 1, NULL)"
        )
        repo._conn.execute(
            "INSERT INTO strategy_instances (strategy_instance_id, symbol, config_hash, "
            "created_at_ms, retired_at_ms) VALUES ('qqq-bot', 'QQQ', 'h', 1, NULL)"
        )
        repo._conn.executemany(
            "INSERT INTO custody_subjects "
            "(subject_id, kind, strategy_instance_id, operator_id, created_at_ms) "
            "VALUES (?, 'BOT', ?, NULL, 1)",
            (("bot:spy-bot", "spy-bot"), ("bot:qqq-bot", "qqq-bot")),
        )
        repo._conn.commit()

    repo.append_transition(_uncertainty_transition(strategy_instance_id=None, reason_code="ACCOUNT_WIDE"))
    repo.append_transition(_uncertainty_transition(strategy_instance_id="spy-bot", reason_code="SPY_ONLY"))
    repo.append_transition(_uncertainty_transition(strategy_instance_id="qqq-bot", reason_code="QQQ_ONLY"))

    for_spy = {u["reason_code"] for u in repo.active_uncertainties_for_admission(strategy_instance_id="spy-bot")}
    assert for_spy == {"ACCOUNT_WIDE", "SPY_ONLY"}  # not QQQ_ONLY

    for_qqq = {u["reason_code"] for u in repo.active_uncertainties_for_admission(strategy_instance_id="qqq-bot")}
    assert for_qqq == {"ACCOUNT_WIDE", "QQQ_ONLY"}
    repo.close()


def test_active_holds_for_admission_includes_account_and_bot_scope(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    with repo._write_lock:
        repo._conn.execute(
            "INSERT INTO strategy_instances (strategy_instance_id, symbol, config_hash, "
            "created_at_ms, retired_at_ms) VALUES ('spy-bot', 'SPY', 'h', 1, NULL)"
        )
        repo._conn.commit()
    repo.append_transition(_hold_transition(evidence_refs=["bo-1"]))  # ACCOUNT_CLERK-scoped

    holds = repo.active_holds_for_admission(strategy_instance_id="spy-bot")
    assert len(holds) == 1
    assert holds[0]["scope"] == "ACCOUNT_CLERK"
    repo.close()


def _seed_reconcilable_effect(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    operation_id: str,
    now_ms: int,
) -> None:
    """Insert one nonterminal ENTER for a bot subject, with no linked order."""
    command_id = f"cmd-{operation_id}"
    subject_id = f"bot:{strategy_instance_id}"
    repo._conn.execute(
        "INSERT INTO commands "
        "(command_id, authority_generation, subject_id, idempotency_key, payload_hash, "
        "kind, strategy_instance_id, run_id, action, intended_end_state, state, "
        "effect_operation_id, receipt_id, created_at_ms, updated_at_ms) "
        "VALUES (?, 1, ?, ?, 'hash', 'strategy_decision', ?, NULL, 'ENTER', NULL, "
        "'accepted', NULL, NULL, ?, ?)",
        (command_id, subject_id, command_id, strategy_instance_id, now_ms, now_ms),
    )
    repo._conn.execute(
        "INSERT INTO effect_operations "
        "(effect_operation_id, authority_generation, subject_id, idempotency_key, command_id, "
        "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, "
        "updated_at_ms) VALUES (?, 1, ?, ?, ?, ?, NULL, 'ENTER', 'accepted', "
        "'ACCOUNT_CLERK', ?, ?)",
        (operation_id, subject_id, operation_id, command_id, strategy_instance_id, now_ms, now_ms),
    )
    repo._conn.commit()


def test_reconcilable_effect_operations_scope_to_one_custody_subject(
    tmp_path: Path,
) -> None:
    """#1793: one bot's unresolved intent must be readable without the account's.

    ``effect_operations.subject_id`` is NOT NULL with a foreign key into
    ``custody_subjects``, so every effect belongs to exactly one subject and
    the filter is total -- there is no unattributable remainder to fall back
    to an account-wide read for.
    """
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_seq(),
    )
    now_ms = 1_700_000_000_000
    for sid in ("frozen-bot", "healthy-bot"):
        repo.register_strategy_instance(strategy_instance_id=sid, symbol="SPY", config_hash="h1")
    _seed_reconcilable_effect(
        repo, strategy_instance_id="frozen-bot", operation_id="op-frozen", now_ms=now_ms
    )

    account_wide = repo.reconcilable_effect_operations()
    frozen = repo.reconcilable_effect_operations(subject_id="bot:frozen-bot")
    healthy = repo.reconcilable_effect_operations(subject_id="bot:healthy-bot")

    assert [op.effect_operation_id for op in account_wide] == ["op-frozen"]
    assert [op.effect_operation_id for op in frozen] == ["op-frozen"]
    assert healthy == []

    repo.close()
