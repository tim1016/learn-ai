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

import shutil
import sqlite3
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.mirror import MirrorChainBroken, MirrorFile
from app.broker.alpaca.clerk.sqlite.registry import EstablishedAccountsRegistry
from app.broker.alpaca.clerk.sqlite.repository import (
    AlreadyInitialized,
    ClerkSqliteRepository,
    DatabaseIdentityMismatch,
    DatabaseMissingAfterEstablishment,
    ExecutionLeaseHeld,
    HashChainBroken,
    IntegrityCheckFailed,
)

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
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq()
    )
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


def test_initialize_twice_raises_already_initialized(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq()
    )
    repo.close()
    with pytest.raises(AlreadyInitialized):
        ClerkSqliteRepository.initialize(
            account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq()
        )


def test_open_a_never_initialized_account_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ClerkSqliteRepository.open(
            account_id="NEVER-SEEN", artifacts_root=tmp_path, clock=_clock_seq()
        )


def test_remove_db_after_established_is_not_silently_recreated(tmp_path: Path) -> None:
    """PRD §15.4: 'remove clerk.db after authority was established and prove
    it is not recreated.' Closes the gap the Slice-1 review found."""
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq()
    )
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


def test_kill_before_mirror_prepare_records_no_transition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD §15.1 'kill before the command commit: no command, no broker call',
    generalized to the spine: if the mirror PREPARE fsync fails, no SQLite
    transaction opens and no row is appended."""
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)

    def boom(*_args, **_kwargs):
        raise OSError("simulated disk failure before prepare fsync")

    monkeypatch.setattr(repo._mirror, "prepare", boom)
    with pytest.raises(OSError):
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
    conn.execute(
        "UPDATE custody_transitions SET operation_state = 'in_progress' WHERE sequence = 1"
    )
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
        ClerkSqliteRepository.open(
            account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_owner="process-B"
        )
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

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    assert rebuilt.custody_transitions() == original_transitions
    assert rebuilt.strategy_instances() == original_instances
    rebuilt.close()


def test_rebuild_refuses_when_db_path_already_exists(tmp_path: Path) -> None:
    clock = _clock_seq()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.close()

    with pytest.raises(AlreadyInitialized):
        ClerkSqliteRepository.rebuild_from_mirror(
            account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
        )


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
    tampered = re.sub(
        r'"row_hash":"([0-9a-f])',
        lambda m: f'"row_hash":"{"0" if m.group(1) != "0" else "1"}',
        lines[0],
        count=1,
    )
    assert tampered != lines[0]
    mirror_path.write_text("\n".join([tampered, *lines[1:]]) + "\n")

    with pytest.raises(MirrorChainBroken):
        ClerkSqliteRepository.rebuild_from_mirror(
            account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
        )
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
        ClerkSqliteRepository.rebuild_from_mirror(
            account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
        )


def test_mirror_prepare_without_finalize_is_excluded_not_imported(tmp_path: Path) -> None:
    """A PREPARE with no matching FINALIZE is an aborted preparation — dropped
    silently from rebuild, not an error by itself, because the fence
    guarantees it had no broker effect."""
    mirror_path = tmp_path / "orphan.mirror"
    mirror = MirrorFile(mirror_path)
    from app.broker.alpaca.clerk.sqlite.hashchain import GENESIS, canonical_payload, compute_row_hash
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
