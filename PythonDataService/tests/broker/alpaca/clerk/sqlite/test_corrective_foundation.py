"""Regression tests for the corrective foundation slice.

Covers the required-test matrix in
``docs/superpowers/plans/2026-08-05-alpaca-clerk-corrective-foundation-slice.md``
that isn't already exercised by ``test_schema_parity.py``,
``test_repository.py``, or ``test_commands.py`` (which were updated in
place for the behavior changes this slice makes). These tests fail on the
pre-correction commits and pass only after the source-model change.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite import schema
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.mirror import MirrorChainBroken
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    ExecutionLeaseLost,
    OperationClaimError,
    RepositoryPoisoned,
    SchemaVersionMismatch,
)

ACCOUNT_ID = "PA-TEST"
SID_A = "spy-bot"
SID_B = "qqq-bot"


def _clock_seq(start: int = 1_700_000_000_000):
    counter = {"t": start}

    def clock() -> int:
        counter["t"] += 1
        return counter["t"]

    clock.advance = lambda delta: counter.__setitem__("t", counter["t"] + delta)
    return clock


@pytest.fixture
def repo(tmp_path: Path):
    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID_A, symbol="SPY", config_hash="h1")
    r.register_strategy_instance(strategy_instance_id=SID_B, symbol="QQQ", config_hash="h2")
    yield r
    r.close()


# ---------------------------------------------------------------------------
# Scope A — contract and schema
# ---------------------------------------------------------------------------


def test_schema_version_bumped_for_the_corrective_ddl_changes() -> None:
    assert schema.SCHEMA_VERSION == 2


def test_stale_schema_version_fails_closed_on_open(tmp_path: Path) -> None:
    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r._conn.execute("UPDATE control_meta SET schema_version = 1 WHERE id = 1")
    r._conn.commit()
    r.close()

    with pytest.raises(SchemaVersionMismatch):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)


def test_genesis_prev_hash_column_is_not_null(repo: ClerkSqliteRepository) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute(
            "INSERT INTO custody_transitions (sequence, prev_hash, row_hash, authority_generation, "
            "transition_kind, custody_owner, execution_authority, operation_state, "
            "clerk_observed_at_ms, recorded_at_ms, summary_code, facts_schema_version, facts_json) "
            "VALUES (999, NULL, 'h', 1, 'K', 'ACCOUNT_CLERK', 'ACCOUNT_CLERK', 'succeeded', 1, 1, 'C', 1, '{}')"
        )


@pytest.mark.parametrize(
    ("scope", "strategy_instance_id", "should_fail"),
    [
        ("BOT", None, True),
        ("BOT", "spy-bot", False),
        ("ACCOUNT_CLERK", "spy-bot", True),
        ("ACCOUNT_CLERK", None, False),
    ],
)
def test_holds_scope_strategy_instance_coupling(
    repo: ClerkSqliteRepository, scope: str, strategy_instance_id: str | None, should_fail: bool
) -> None:
    stmt = (
        "INSERT INTO holds (hold_id, scope, strategy_instance_id, reason_code, state, "
        "opened_at_ms, resolved_at_ms, evidence_refs_json) "
        "VALUES ('h1', ?, ?, 'r', 'ACTIVE', 1, NULL, NULL)"
    )
    if should_fail:
        with pytest.raises(sqlite3.IntegrityError):
            repo._conn.execute(stmt, (scope, strategy_instance_id))
    else:
        repo._conn.execute(stmt, (scope, strategy_instance_id))
        repo._conn.commit()


@pytest.mark.parametrize(
    ("scope", "strategy_instance_id", "should_fail"),
    [
        ("BOT", None, True),
        ("BOT", "spy-bot", False),
        ("ACCOUNT_CLERK", "spy-bot", True),
        ("ACCOUNT_CLERK", None, False),
    ],
)
def test_uncertainties_scope_strategy_instance_coupling(
    repo: ClerkSqliteRepository, scope: str, strategy_instance_id: str | None, should_fail: bool
) -> None:
    stmt = (
        "INSERT INTO uncertainties (uncertainty_id, scope, severity, blocks_new_exposure, "
        "allows_reduction, strategy_instance_id, reason_code, headline, explanation, "
        "operator_impact, next_step, observed_at_ms, resolved_at_ms, evidence_refs_json, "
        "facts_schema_version, facts_json) "
        "VALUES ('u1', ?, 'HIGH', 0, 1, ?, 'r', 'h', 'e', 'oi', 'ns', 1, NULL, NULL, 1, '{}')"
    )
    if should_fail:
        with pytest.raises(sqlite3.IntegrityError):
            repo._conn.execute(stmt, (scope, strategy_instance_id))
    else:
        repo._conn.execute(stmt, (scope, strategy_instance_id))
        repo._conn.commit()


def test_cross_bot_command_run_link_rejected(repo: ClerkSqliteRepository) -> None:
    """A ``commands`` row cannot claim a ``run_id`` belonging to a different
    ``strategy_instance_id`` (open-pr-review-2026-08-05.md P2 "Commands/effects
    may point at another bot's run")."""
    submission = submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-1"
    )
    other_run_id = repo.active_run(SID_A).run_id
    assert submission.command.run_id == other_run_id

    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute(
            "INSERT INTO commands (command_id, authority_generation, idempotency_key, "
            "payload_hash, kind, strategy_instance_id, run_id, action, intended_end_state, "
            "state, effect_operation_id, receipt_id, created_at_ms, updated_at_ms) "
            "VALUES ('cmd:cross-bot', 1, 'cross-bot-key', 'h', 'operator_lifecycle', ?, ?, "
            "'START', 'ACTIVE', 'succeeded', NULL, NULL, 1, 1)",
            (SID_B, other_run_id),
        )


def test_commands_terminal_state_cannot_regress(repo: ClerkSqliteRepository) -> None:
    submission = submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-1"
    )
    assert submission.command.state == "succeeded"
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute(
            "UPDATE commands SET state = 'in_progress' WHERE command_id = ?",
            (submission.command.command_id,),
        )


def test_effect_operations_terminal_state_cannot_regress(repo: ClerkSqliteRepository) -> None:
    submission = submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-1"
    )
    repo._conn.execute(
        "INSERT INTO effect_operations (effect_operation_id, authority_generation, "
        "idempotency_key, command_id, strategy_instance_id, run_id, kind, state, "
        "custody_owner, created_at_ms, updated_at_ms, terminal_receipt_id) "
        "VALUES ('eff:1', 1, 'eff-key', ?, ?, ?, 'ENTER', 'succeeded', 'ACCOUNT_CLERK', 1, 1, NULL)",
        (submission.command.command_id, SID_A, submission.command.run_id),
    )
    repo._conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute("UPDATE effect_operations SET state = 'failed' WHERE effect_operation_id = 'eff:1'")


def test_rejected_effect_operation_cannot_regress_either(repo: ClerkSqliteRepository) -> None:
    """open-pr-review-2026-08-05.md P2 "Treat rejected effects as terminal":
    'rejected' is part of effect_operations.state's own CHECK vocabulary, so
    the terminal-state backstop must cover it too, not only succeeded/failed —
    otherwise a rejected effect could be moved back to accepted/in_progress
    and become eligible for broker work after rejection."""
    submission = submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-1"
    )
    repo._conn.execute(
        "INSERT INTO effect_operations (effect_operation_id, authority_generation, "
        "idempotency_key, command_id, strategy_instance_id, run_id, kind, state, "
        "custody_owner, created_at_ms, updated_at_ms, terminal_receipt_id) "
        "VALUES ('eff:2', 1, 'eff-key-2', ?, ?, ?, 'ENTER', 'rejected', 'ACCOUNT_CLERK', 1, 1, NULL)",
        (submission.command.command_id, SID_A, submission.command.run_id),
    )
    repo._conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute("UPDATE effect_operations SET state = 'accepted' WHERE effect_operation_id = 'eff:2'")


def test_rejected_command_has_a_durable_rejection_receipt(repo: ClerkSqliteRepository) -> None:
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-1")
    rejected = submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-2"
    )
    assert rejected.command.state == "rejected"
    assert rejected.command.receipt_id is not None

    row = repo._conn.execute(
        "SELECT terminal_state FROM receipts WHERE receipt_id = ?",
        (rejected.command.receipt_id,),
    ).fetchone()
    assert row["terminal_state"] == "rejected"


def test_reserve_command_and_serialized_no_longer_exist() -> None:
    """The corrective foundation slice deletes both (Scope B1) — a command
    first becomes durable only via commit_first_transition()."""
    assert not hasattr(ClerkSqliteRepository, "reserve_command")
    assert not hasattr(ClerkSqliteRepository, "serialized")


# ---------------------------------------------------------------------------
# Scope C — fail-closed R9 mirror fence state machine
# ---------------------------------------------------------------------------


def test_mirror_reconciles_every_committed_sequence_not_only_the_tail(tmp_path: Path) -> None:
    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID_A, symbol="SPY", config_hash="h1")
    r.register_strategy_instance(strategy_instance_id=SID_B, symbol="QQQ", config_hash="h2")
    mirror_path = r.mirror_path
    r.close()

    lines = mirror_path.read_text().splitlines()
    # Drop sequence 1's FINALIZE only (keep its PREPARE and sequence 2 intact) —
    # an earlier gap that a tail-only check would never see. "sequence" sorts
    # last among json.dumps(sort_keys=True)'s keys, so it is always followed
    # by the closing brace, never a comma.
    kept = [
        line
        for line in lines
        if not ('"phase":"FINALIZE"' in line and '"sequence":1}' in line)
    ]
    assert len(kept) == len(lines) - 1
    mirror_path.write_text("\n".join(kept) + "\n")

    reopened = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    assert reopened._mirror.has_finalize(1) is True
    assert reopened._mirror.has_finalize(2) is True
    reopened.close()


def test_reconcile_fails_closed_when_missing_finalize_has_no_matching_prepare(
    tmp_path: Path,
) -> None:
    """open-pr-review-2026-08-05.md P2 "require PREPARE before catching up
    FINALIZE": completing a FINALIZE from the committed DB row alone, with
    no matching PREPARE left in the mirror, would pass startup while leaving
    the mirror unable to ever reconstruct that row's payload again — a
    silent loss of the disaster-recovery property R9 exists to guarantee."""
    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID_A, symbol="SPY", config_hash="h1")
    r.register_strategy_instance(strategy_instance_id=SID_B, symbol="QQQ", config_hash="h2")
    mirror_path = r.mirror_path
    r.close()

    lines = mirror_path.read_text().splitlines()
    # Drop BOTH sequence 1's PREPARE and FINALIZE, keeping sequence 2 intact —
    # clerk.db still has the committed row, the mirror has lost all evidence of it.
    kept = [line for line in lines if '"sequence":1}' not in line]
    assert len(kept) == len(lines) - 2
    mirror_path.write_text("\n".join(kept) + "\n")

    with pytest.raises(MirrorChainBroken):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)


def test_reconcile_fails_closed_on_conflicting_duplicate_finalize_lines(tmp_path: Path) -> None:
    """Two FINALIZE lines for the same sequence with different hashes is
    genuine corruption regardless of line order (open-pr-review-2026-08-05.md
    P2 "Reject conflicting duplicate finalizes"). The bogus record is placed
    *before* the real one so a naive last-wins dict would pick the real
    (matching) one and see nothing wrong — proving detection does not
    depend on which record happens to survive a dict overwrite."""
    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID_A, symbol="SPY", config_hash="h1")
    mirror_path = r.mirror_path
    r.close()

    lines = mirror_path.read_text().splitlines()
    finalize_index = next(i for i, line in enumerate(lines) if '"phase":"FINALIZE"' in line)
    finalize_line = lines[finalize_index]
    import re

    bogus = re.sub(
        r'"row_hash":"([0-9a-f])',
        lambda m: f'"row_hash":"{"0" if m.group(1) != "0" else "1"}',
        finalize_line,
        count=1,
    )
    assert bogus != finalize_line
    lines.insert(finalize_index, bogus)  # bogus now comes before the real, matching FINALIZE
    mirror_path.write_text("\n".join(lines) + "\n")

    with pytest.raises(MirrorChainBroken):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)


def test_conflicting_finalize_record_fails_closed_on_open(tmp_path: Path) -> None:
    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID_A, symbol="SPY", config_hash="h1")
    mirror_path = r.mirror_path
    r.close()

    lines = mirror_path.read_text().splitlines()
    import re

    tampered = [
        re.sub(r'"row_hash":"([0-9a-f])', lambda m: f'"row_hash":"{"0" if m.group(1) != "0" else "1"}', line, count=1)
        if '"phase":"FINALIZE"' in line
        else line
        for line in lines
    ]
    assert tampered != lines
    mirror_path.write_text("\n".join(tampered) + "\n")

    with pytest.raises(MirrorChainBroken):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)


def test_rebuild_fails_closed_on_prepare_finalize_generation_mismatch(tmp_path: Path) -> None:
    """open-pr-review-2026-08-05.md P2 "require authority-generation
    equality in the pair": a FINALIZE whose own authority_generation field
    disagrees with its matching-hash PREPARE's is rejected even though the
    row_hash itself matches."""
    from app.broker.alpaca.clerk.sqlite.hashchain import GENESIS, canonical_payload, compute_row_hash
    from app.broker.alpaca.clerk.sqlite.mirror import MirrorFile, PendingTransition

    mirror = MirrorFile(tmp_path / "genmismatch.mirror")
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
        "operation_state": "succeeded",
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
    # A FINALIZE with the same row_hash but a tampered top-level generation.
    mirror.finalize(sequence=1, authority_generation=2, row_hash=row_hash, recorded_at_ms=2)

    with pytest.raises(MirrorChainBroken):
        mirror.rebuild()


def test_finalize_failure_poisons_the_handle_and_blocks_further_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)

    def boom(*_args, **_kwargs):
        raise OSError("simulated crash between SQLite commit and mirror finalize")

    monkeypatch.setattr(r._mirror, "finalize", boom)
    with pytest.raises(OSError):
        r.register_strategy_instance(strategy_instance_id=SID_A, symbol="SPY", config_hash="h1")

    monkeypatch.undo()
    with pytest.raises(RepositoryPoisoned):
        r.register_strategy_instance(strategy_instance_id=SID_B, symbol="QQQ", config_hash="h2")

    # Reconciliation (matching what a restart's startup check 9 would find)
    # clears the poison: the dangling PREPARE finalizes and writes resume.
    r.reconcile_poison()
    r.register_strategy_instance(strategy_instance_id=SID_B, symbol="QQQ", config_hash="h2")
    assert len(r.strategy_instances()) == 2
    r.close()


def test_db_path_symlink_escape_rejected(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    escape_target = tmp_path / "outside"  # a sibling of artifacts_root, genuinely outside it
    escape_target.mkdir()
    account_dir = artifacts_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    (account_dir / "clerk.db").symlink_to(escape_target / "clerk.db")

    with pytest.raises(ValueError, match="escapes root"):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=artifacts_root)


def test_mirror_path_symlink_escape_rejected(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=artifacts_root, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID_A, symbol="SPY", config_hash="h1")
    r.close()

    escape_target = tmp_path / "outside"  # a sibling of artifacts_root, genuinely outside it
    escape_target.mkdir()
    mirror_path = r.mirror_path
    mirror_path.unlink()
    mirror_path.symlink_to(escape_target / "custody_transitions.mirror")

    with pytest.raises(ValueError, match="escapes root"):
        ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=artifacts_root, clock=clock)


def test_first_mirror_write_fsyncs_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    import app.broker.alpaca.clerk.sqlite.mirror as mirror_module

    real_fsync_directory = mirror_module.fsync_directory

    def spy(path: Path) -> None:
        calls.append(path)
        real_fsync_directory(path)

    monkeypatch.setattr(mirror_module, "fsync_directory", spy)

    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    assert len(calls) == 0  # initialize() writes control_meta only, no transition yet
    r.register_strategy_instance(strategy_instance_id=SID_A, symbol="SPY", config_hash="h1")
    assert len(calls) == 1  # the mirror's first PREPARE line creates the file
    r.register_strategy_instance(strategy_instance_id=SID_B, symbol="QQQ", config_hash="h2")
    assert len(calls) == 1  # subsequent writes to the existing file do not re-fsync the dir
    r.close()


def test_full_command_lifecycle_rebuild_parity(tmp_path: Path) -> None:
    """Database deletion followed by rebuild reproduces command, run,
    receipt, idempotency lookup, and transition timeline exactly — now that
    a fold (not a standalone reservation) is what creates the command row."""
    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID_A, symbol="SPY", config_hash="h1")
    start = submit_start_run(
        r, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-1"
    )
    submit_stop_run(
        r, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-1"
    )
    original_transitions = r.custody_transitions()
    original_command = r.get_command(start.command.command_id)
    db_path = r.db_path
    r.close()

    db_path.rename(db_path.with_suffix(".db.corrupt"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    assert rebuilt.custody_transitions() == original_transitions
    assert rebuilt.get_command(start.command.command_id) == original_command
    assert rebuilt.active_run(SID_A) is None  # stopped, survives rebuild
    rebuilt.close()


# ---------------------------------------------------------------------------
# Scope D — leases and operation claims
# ---------------------------------------------------------------------------


def test_lease_renews_on_every_write_and_lost_lease_blocks_further_writes(tmp_path: Path) -> None:
    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=1000
    )
    # A write well inside the TTL renews successfully.
    r.register_strategy_instance(strategy_instance_id=SID_A, symbol="SPY", config_hash="h1")

    # Simulate losing the lease to another owner (as if it expired and a new
    # process took over) without going through close()/open().
    r._conn.execute(
        "UPDATE control_meta SET execution_lease_owner = 'someone-else', "
        "execution_lease_expires_at_ms = ? WHERE id = 1",
        (clock() + 1000,),
    )
    r._conn.commit()

    with pytest.raises(ExecutionLeaseLost):
        r.register_strategy_instance(strategy_instance_id=SID_B, symbol="QQQ", config_hash="h2")


def test_operation_claim_cas_fences_a_second_owner_until_expiry(repo: ClerkSqliteRepository) -> None:
    submission = submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-1"
    )
    repo._conn.execute(
        "INSERT INTO effect_operations (effect_operation_id, authority_generation, "
        "idempotency_key, command_id, strategy_instance_id, run_id, kind, state, "
        "custody_owner, created_at_ms, updated_at_ms, terminal_receipt_id) "
        "VALUES ('eff:1', 1, 'eff-key', ?, ?, ?, 'ENTER', 'accepted', 'ACCOUNT_CLERK', 1, 1, NULL)",
        (submission.command.command_id, SID_A, submission.command.run_id),
    )
    repo._conn.commit()

    claim = repo.claim_effect_operation(effect_operation_id="eff:1", owner="worker-1", ttl_ms=10_000)
    assert repo.verify_operation_claim(effect_operation_id="eff:1", token=claim.token)
    assert not repo.verify_operation_claim(effect_operation_id="eff:1", token="wrong-token")

    with pytest.raises(OperationClaimError):
        repo.claim_effect_operation(effect_operation_id="eff:1", owner="worker-2", ttl_ms=10_000)

    # Force the claim to expire, then a different worker may take it over.
    repo._conn.execute(
        "UPDATE effect_operations SET claim_expires_at_ms = 0 WHERE effect_operation_id = 'eff:1'"
    )
    repo._conn.commit()
    takeover = repo.claim_effect_operation(effect_operation_id="eff:1", owner="worker-2", ttl_ms=10_000)
    assert takeover.owner == "worker-2"
    assert not repo.verify_operation_claim(effect_operation_id="eff:1", token=claim.token)


# ---------------------------------------------------------------------------
# Reviewer follow-up fixes (Codex + CodeRabbit on PR #1388)
# ---------------------------------------------------------------------------


def test_operation_claim_lets_the_live_owner_renew_before_expiry(
    repo: ClerkSqliteRepository,
) -> None:
    """The docstring promises renewal ("Claim (or renew this owner's own
    claim on)"), but the original CAS predicate only admitted unclaimed or
    expired rows — a live owner renewing its own long-running claim got
    OperationClaimError instead of an extended lease."""
    submission = submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-1"
    )
    repo._conn.execute(
        "INSERT INTO effect_operations (effect_operation_id, authority_generation, "
        "idempotency_key, command_id, strategy_instance_id, run_id, kind, state, "
        "custody_owner, created_at_ms, updated_at_ms, terminal_receipt_id) "
        "VALUES ('eff:3', 1, 'eff-key-3', ?, ?, ?, 'ENTER', 'accepted', 'ACCOUNT_CLERK', 1, 1, NULL)",
        (submission.command.command_id, SID_A, submission.command.run_id),
    )
    repo._conn.commit()

    first = repo.claim_effect_operation(effect_operation_id="eff:3", owner="worker-1", ttl_ms=10_000)
    renewed = repo.claim_effect_operation(effect_operation_id="eff:3", owner="worker-1", ttl_ms=10_000)
    assert renewed.owner == "worker-1"
    assert renewed.expires_at_ms >= first.expires_at_ms
    # A different owner is still fenced out while the renewal is live.
    with pytest.raises(OperationClaimError):
        repo.claim_effect_operation(effect_operation_id="eff:3", owner="worker-2", ttl_ms=10_000)


def test_claiming_an_unknown_effect_operation_reports_not_found_not_taken(
    repo: ClerkSqliteRepository,
) -> None:
    """rowcount == 0 previously conflated two distinct causes: no such row,
    and a row claimed by a live other owner. Distinguish them."""
    with pytest.raises(OperationClaimError, match="does not exist"):
        repo.claim_effect_operation(effect_operation_id="eff:does-not-exist", owner="worker-1")


def test_get_command_is_synchronized_against_an_in_flight_write(
    repo: ClerkSqliteRepository,
) -> None:
    """open-pr-review-2026-08-05.md P1 "Use a committed-read path for
    command GETs": get_command() must not run concurrently with an
    in-flight write on the shared connection — proven here by holding the
    repository's write coordinator on the main thread and confirming a
    background get_command() call cannot complete until it is released."""
    submission = submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-1"
    )
    entered = threading.Event()
    result: dict = {}

    def reader() -> None:
        entered.set()
        result["command"] = repo.get_command(submission.command.command_id)

    with repo._write_lock:
        thread = threading.Thread(target=reader)
        thread.start()
        assert entered.wait(timeout=1)
        thread.join(timeout=0.1)  # reader must still be blocked on the lock
        assert "command" not in result
        assert thread.is_alive()

    thread.join(timeout=1)
    assert result["command"] is not None
    assert result["command"].command_id == submission.command.command_id


def test_commit_first_transition_blocks_an_existing_command_retry_when_poisoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """open-pr-review-2026-08-05.md P2 "Block retries on poisoned handles":
    the existing-command short-circuit in commit_first_transition() must
    also refuse to serve a retry while the handle is poisoned — otherwise a
    caller gets back an accepted command whose mirror fence is unconfirmed."""
    clock = _clock_seq()
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID_A, symbol="SPY", config_hash="h1")
    submit_start_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-1")

    def boom(*_args, **_kwargs):
        raise OSError("simulated crash between SQLite commit and mirror finalize")

    monkeypatch.setattr(r._mirror, "finalize", boom)
    with pytest.raises(OSError):
        submit_start_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-2")
    monkeypatch.undo()

    # run-2's Start command already exists (rejected, since run-1 is still
    # active) — retrying it is the existing-command path, which must still
    # see the poison.
    with pytest.raises(RepositoryPoisoned):
        submit_start_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID_A, lifecycle_run_id="run-2")
    r._conn.close()
