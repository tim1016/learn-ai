"""Behavioral coverage for the paper-only developer clean-slate reset."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite import dev_reset as dev_reset_module
from app.broker.alpaca.clerk.sqlite.dev_reset import (
    DeveloperCleanSlateResetRefused,
    DevResetReceipt,
    developer_clean_slate_reset,
)
from app.broker.alpaca.clerk.sqlite.developer_reset_registry import (
    DeveloperCleanSlateResetRegistry,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    DatabaseMissingAfterEstablishment,
)
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
)
from tests._helpers.legacy_ibkr_artifacts import (
    write_historical_account_binding,
)

ACCOUNT_ID = "PADEVRESET"
NOW_MS = 1_800_000_000_000


def _clock() -> int:
    return NOW_MS


def _register_runner_catalog(runner_root: Path, strategy_instance_id: str) -> Path:
    write_historical_account_binding(
        runner_root,
        AccountInstanceBinding(
            account_id=ACCOUNT_ID,
            strategy_instance_id=strategy_instance_id,
            run_id=f"run-{strategy_instance_id}",
            bot_order_namespace=bot_order_namespace_for_instance(strategy_instance_id),
            lifecycle_state="ACTIVE",
            recorded_at_ms=NOW_MS,
            source="dev-reset.test",
        ),
    )
    catalog = runner_root / "live_state" / strategy_instance_id
    catalog.mkdir(parents=True)
    (catalog / "live_state.json").write_text("{}", encoding="utf-8")
    return catalog


def _reset(
    *,
    clerk_root: Path,
    runner_root: Path,
    account_mode: str = "paper",
) -> DevResetReceipt:
    return developer_clean_slate_reset(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        runner_artifacts_root=runner_root,
        account_mode=account_mode,
        clock=_clock,
    )


def test_reset_moves_sqlite_authority_and_runner_catalog_then_allows_reinitialize(
    tmp_path: Path,
) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=_clock,
    )
    account_dir = repo.db_path.parent
    repo.close()
    (account_dir / "clerk.db-wal").touch()
    (account_dir / "clerk.db-shm").touch()
    catalog = _register_runner_catalog(runner_root, "spy-dev")

    receipt = _reset(clerk_root=clerk_root, runner_root=runner_root)

    assert receipt.quarantine_reference is not None
    assert receipt.runner_quarantine_reference is not None
    quarantine = clerk_root / receipt.quarantine_reference
    runner_quarantine = runner_root / receipt.runner_quarantine_reference
    assert not (account_dir / "clerk.db").exists()
    assert not (account_dir / "custody_transitions.mirror").exists()
    assert not catalog.exists()
    assert (quarantine / "sqlite-authority" / "clerk.db").is_file()
    assert (quarantine / "sqlite-authority" / "clerk.db-wal").is_file()
    assert (quarantine / "sqlite-authority" / "clerk.db-shm").is_file()
    assert (quarantine / "sqlite-authority" / "custody_transitions.mirror").is_file()
    assert (runner_quarantine / "runner-catalogs" / "spy-dev").is_dir()
    manifest = json.loads((quarantine / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["moved_artifacts"] == [
        {
            "kind": artifact.kind,
            "quarantine_reference": artifact.quarantine_reference,
            "sha256": artifact.sha256,
            "source_reference": artifact.source_reference,
            "source_scope": artifact.source_scope,
        }
        for artifact in receipt.moved_artifacts
    ]
    assert manifest["runner_quarantine_reference"] == receipt.runner_quarantine_reference
    assert (quarantine / "receipt.json").is_file()

    regenerated = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=lambda: NOW_MS + 1,
    )
    assert regenerated.control_meta_snapshot().authority_generation == 2
    regenerated.close()


def test_reset_refuses_unactivated_legacy_authority_without_moving_it(
    tmp_path: Path,
) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    account_dir = clerk_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    for name in (
        "order_inbox.jsonl",
        "order_journal.jsonl",
        "custody_resolution_receipts.json",
    ):
        (account_dir / name).write_text("{}\n", encoding="utf-8")
    (account_dir / "bots").mkdir()
    _register_runner_catalog(runner_root, "legacy-dev")

    with pytest.raises(
        DeveloperCleanSlateResetRefused,
        match="requires an established SQLite authority",
    ):
        _reset(clerk_root=clerk_root, runner_root=runner_root)

    assert (account_dir / "order_inbox.jsonl").is_file()
    assert (account_dir / "order_journal.jsonl").is_file()
    assert (account_dir / "custody_resolution_receipts.json").is_file()
    assert (account_dir / "bots").is_dir()
    assert (runner_root / "live_state" / "legacy-dev").is_dir()


def test_reinitialize_refuses_a_tampered_developer_reset_manifest(tmp_path: Path) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=_clock,
    )
    repo.close()
    receipt = _reset(clerk_root=clerk_root, runner_root=runner_root)
    assert receipt.manifest_reference is not None
    (clerk_root / receipt.manifest_reference).write_text("{}", encoding="utf-8")

    with pytest.raises(DatabaseMissingAfterEstablishment):
        ClerkSqliteRepository.initialize(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            clock=lambda: NOW_MS + 1,
        )


def test_reinitialize_fails_closed_when_reset_ledger_is_malformed(tmp_path: Path) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=_clock,
    )
    repo.close()
    _reset(clerk_root=clerk_root, runner_root=runner_root)
    accounts_root = clerk_root / "accounts" / "alpaca"
    DeveloperCleanSlateResetRegistry(accounts_root).path.write_text(
        "[not an authorization record]\n",
        encoding="utf-8",
    )

    with pytest.raises(DatabaseMissingAfterEstablishment):
        ClerkSqliteRepository.initialize(
            account_id=ACCOUNT_ID,
            artifacts_root=clerk_root,
            clock=lambda: NOW_MS + 1,
        )


def test_reset_resumes_authorization_after_every_artifact_was_moved(tmp_path: Path) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=_clock,
    )
    repo.close()
    receipt = _reset(clerk_root=clerk_root, runner_root=runner_root)
    accounts_root = clerk_root / "accounts" / "alpaca"
    DeveloperCleanSlateResetRegistry(accounts_root).path.write_text("", encoding="utf-8")

    resumed = _reset(clerk_root=clerk_root, runner_root=runner_root)

    assert resumed == receipt
    regenerated = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=lambda: NOW_MS + 1,
    )
    assert regenerated.control_meta_snapshot().authority_generation == 2
    regenerated.close()


def test_reset_collects_sidecars_created_by_the_lease_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=_clock,
    )
    account_dir = repo.db_path.parent
    repo.close()
    original = dev_reset_module._assert_stopped_sqlite_authority

    def probe_then_create_sidecars(path: Path, *, now_ms: int) -> None:
        original(path, now_ms=now_ms)
        (path / "clerk.db-wal").touch()
        (path / "clerk.db-shm").touch()

    monkeypatch.setattr(
        dev_reset_module,
        "_assert_stopped_sqlite_authority",
        probe_then_create_sidecars,
    )

    receipt = _reset(clerk_root=clerk_root, runner_root=runner_root)

    assert receipt.quarantine_reference is not None
    quarantine = clerk_root / receipt.quarantine_reference
    assert (quarantine / "sqlite-authority" / "clerk.db-wal").is_file()
    assert (quarantine / "sqlite-authority" / "clerk.db-shm").is_file()
    assert not (account_dir / "clerk.db-wal").exists()
    assert not (account_dir / "clerk.db-shm").exists()


def test_reset_refuses_unsafe_runner_catalog_ids_before_constructing_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=_clock,
    )
    repo.close()
    monkeypatch.setattr(
        dev_reset_module,
        "_runner_strategy_instance_ids",
        lambda _root, _account_id: ("../../../victim",),
    )

    with pytest.raises(DeveloperCleanSlateResetRefused, match="unsafe strategy instance ID"):
        _reset(clerk_root=clerk_root, runner_root=runner_root)

    assert repo.db_path.is_file()


def test_cleanup_keeps_nonempty_quarantine_for_inspection(tmp_path: Path) -> None:
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    residue = quarantine / "unrestored-artifact"
    residue.write_text("inspect me", encoding="utf-8")

    dev_reset_module._remove_empty_quarantine(quarantine)

    assert residue.read_text(encoding="utf-8") == "inspect me"


def test_reset_refuses_non_paper_account_without_moving_authority(tmp_path: Path) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    account_dir = clerk_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    journal = account_dir / "order_journal.jsonl"
    journal.write_text("{}\n", encoding="utf-8")

    with pytest.raises(DeveloperCleanSlateResetRefused, match="only for paper"):
        _reset(clerk_root=clerk_root, runner_root=runner_root, account_mode="live")

    assert journal.is_file()


def test_reset_refuses_live_execution_lease_without_moving_authority(tmp_path: Path) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=_clock,
    )
    repo.close()
    conn = sqlite3.connect(repo.db_path)
    conn.execute(
        "UPDATE control_meta SET execution_lease_owner = 'live-process', "
        "execution_lease_expires_at_ms = ? WHERE id = 1",
        (NOW_MS + 1,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(DeveloperCleanSlateResetRefused, match="process to be stopped"):
        _reset(clerk_root=clerk_root, runner_root=runner_root)

    assert repo.db_path.is_file()


def test_reset_refuses_unclean_wal_or_symbolic_linked_sqlite_artifacts(
    tmp_path: Path,
) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_root,
        clock=_clock,
    )
    account_dir = repo.db_path.parent
    repo.close()
    wal = account_dir / "clerk.db-wal"
    wal.write_text("uncheckpointed", encoding="utf-8")

    with pytest.raises(DeveloperCleanSlateResetRefused, match=r"non-empty clerk\.db-wal"):
        _reset(clerk_root=clerk_root, runner_root=runner_root)
    assert repo.db_path.is_file()

    wal.unlink()
    mirror = account_dir / "custody_transitions.mirror"
    mirror.unlink()
    outside = tmp_path / "outside-mirror"
    outside.write_text("outside", encoding="utf-8")
    mirror.symlink_to(outside)
    with pytest.raises(DeveloperCleanSlateResetRefused, match="symbolic-link"):
        _reset(clerk_root=clerk_root, runner_root=runner_root)
    assert repo.db_path.is_file()
