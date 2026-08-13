"""Repository-spine adversarial tests — PRD §15.1/§15.4 subset in Slice-2 scope.

Slice 2 ("Pass focused atomicity, idempotency, identity, corruption, and
rebuild-from-mirror tests") does not yet have commands/effects/broker
contact, so the PRD's broker-facing adversarial cases (idempotent commands,
UNKNOWN reconciliation, EXIT flows) are out of scope here and land with
their owning slices. This file covers every §15.4 case a bare repository
spine can meaningfully exercise, plus the mirror-fence and hash-chain
mechanics from §4/§7/§8/§9 of the pinned contracts doc.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from conftest import _hold_transition

from app.broker.alpaca.clerk.sqlite import process_repositories, repository_lifecycle
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
from app.broker.alpaca.clerk.sqlite.registry import EstablishedAccountsRegistry
from app.broker.alpaca.clerk.sqlite.repository import (
    AlreadyInitialized,
    ClerkSqliteRepository,
    DatabaseIdentityMismatch,
    DatabaseMissingAfterEstablishment,
    ExecutionLeaseHeld,
    HashChainBroken,
    IntegrityCheckFailed,
    UnsupportedWalFilesystem,
)
from app.services.sqlite_clerk_compat import _operation_requires_reconciliation

ACCOUNT_ID = "PA-TEST"


def _clock_seq():
    """A deterministic, strictly-increasing millisecond clock for tests."""
    counter = {"t": 1_700_000_000_000}

    def clock() -> int:
        counter["t"] += 1
        return counter["t"]

    return clock


def test_process_repository_shutdown_closes_every_cached_handle_after_one_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    closed: list[str] = []

    class FakeRepository:
        def __init__(self, account_id: str, *, fail: bool = False) -> None:
            self.account_id = account_id
            self.fail = fail

        def close(self) -> None:
            closed.append(self.account_id)
            if self.fail:
                raise OSError("close failed")

    with process_repositories._lock:
        process_repositories._repositories.update(
            {
                "PA-FAIL": FakeRepository("PA-FAIL", fail=True),
                "PA-OK": FakeRepository("PA-OK"),
            }
        )

    process_repositories.close_all_repositories()

    assert closed == ["PA-FAIL", "PA-OK"]
    assert process_repositories._repositories == {}
    assert "failed to close cached SQLite Alpaca Clerk repository" in caplog.text


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
    """PRD §15.4: 'remove clerk.db after authority was established and prove
    it is not recreated.' Closes the gap the Slice-1 review found."""
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
    """PRD §15.1 'kill before the command commit: no command, no broker call',
    generalized to the spine: if the mirror PREPARE fsync fails, no SQLite
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
    """PRD §15.4 'kill after SQLite commit but before mirror finalization:
    ... an intact DB finalizes on restart.'"""
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
    """PRD §15.4: 'tamper with a mirror line and prove hash-chain-break
    detection fails closed (no tampered import).'"""
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

    hold = repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER")
    assert hold is not None
    assert hold["state"] == "ACTIVE"
    assert hold["strategy_instance_id"] is None
    assert json.loads(hold["evidence_refs_json"]) == ["bo-1", "bo-2"]
    repo.close()


def test_active_hold_returns_none_when_no_hold_of_that_reason_exists(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    assert repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER") is None
    repo.close()


def test_two_hold_transitions_each_mint_a_distinct_hold_id(tmp_path: Path) -> None:
    """Two independent raises (e.g. two different reason codes, or a test
    directly exercising the fold twice) must never collide on hold_id — the
    id is minted from the transition's own globally-unique sequence, not
    from content or a timestamp that a fast test clock could repeat."""
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    repo.append_transition(_hold_transition(reason_code="UNEXPLAINED_ORDER"))
    repo.append_transition(_hold_transition(reason_code="OTHER_REASON"))

    first = repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER")
    second = repo.active_hold(scope="ACCOUNT_CLERK", reason_code="OTHER_REASON")
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

    # Golden fractional aggregation; the tolerance is the source-backed
    # contract in docs/references/clerk-position-drift-tolerance.md.
    assert repo.attributed_positions_by_symbol() == {"SPY": pytest.approx(0.3, abs=1e-9)}
    repo.close()


def test_uncertain_orders_is_empty_on_a_fresh_repository(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    assert repo.uncertain_orders() == []
    repo.close()


# ---------------------------------------------------------------------------
# #1380 primitives: UNCERTAINTY_RAISED/RESOLVED folds + raise_uncertainty_if_
# none_active/active_uncertainty*/active_holds_for_admission read helpers.
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


def test_active_uncertainty_returns_none_when_none_exists(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    assert repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code="TEST_REASON", strategy_instance_id=None) is None
    repo.close()


def test_raise_uncertainty_if_none_active_is_idempotent(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    first = repo.raise_uncertainty_if_none_active(
        scope="ACCOUNT_CLERK",
        reason_code="TEST_REASON",
        strategy_instance_id=None,
        build_transition=lambda: _uncertainty_transition(strategy_instance_id=None),
    )
    before = len(repo.custody_transitions())
    second = repo.raise_uncertainty_if_none_active(
        scope="ACCOUNT_CLERK",
        reason_code="TEST_REASON",
        strategy_instance_id=None,
        build_transition=lambda: _uncertainty_transition(strategy_instance_id=None),
    )
    assert first is True
    assert second is False
    assert len(repo.custody_transitions()) == before  # no second raise
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
