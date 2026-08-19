"""Fail-closed migration-gate tests for ADR 0037 consequence 5."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.qualify_alpaca_activation_inventory as cli_module
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecord, ActivationStore
from app.broker.alpaca.clerk.sqlite.activation_inventory import (
    ActivationInventoryQualificationFailed,
    qualify_activation_inventory,
    read_activation_inventory,
)
from app.broker.alpaca.clerk.sqlite.database_verification import verify_database
from app.broker.alpaca.clerk.sqlite.operational_files import atomic_write_json
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

NOW_MS = 1_800_000_000_000


def _activate(tmp_path: Path, account_id: str) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id=account_id,
        artifacts_root=tmp_path,
        clock=lambda: NOW_MS,
    )
    db_path = repo.db_path
    repo.close()
    database = verify_database(db_path, expected_account_id=account_id)
    proof_reference = f"accounts/alpaca/{account_id}/cutover/broker-proof.json"
    manifest_reference = f"accounts/alpaca/{account_id}/cutover/quarantine.json"
    payload = {
        "account_id": account_id,
        "authority_generation": database.authority_generation,
        "db_identity_token": database.db_identity_token,
    }
    proof_sha256 = atomic_write_json(tmp_path / proof_reference, payload)
    manifest_sha256 = atomic_write_json(tmp_path / manifest_reference, payload)
    ActivationStore(tmp_path / "accounts" / "alpaca").append(
        ActivationRecord.create(
            account_id=account_id,
            authority_generation=database.authority_generation,
            db_identity_token=database.db_identity_token,
            broker_proof_reference=proof_reference,
            broker_proof_sha256=proof_sha256,
            legacy_quarantine_manifest=manifest_reference,
            legacy_quarantine_manifest_sha256=manifest_sha256,
            activated_at_ms=NOW_MS - 10_000,
        )
    )


def _write_inventory(tmp_path: Path, account_ids: list[str]) -> Path:
    path = tmp_path / "in-use-alpaca-accounts.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "operator account inventory export",
                "captured_at_ms": NOW_MS - 1_000,
                "account_ids": account_ids,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_qualification_receipt_covers_every_explicit_in_use_account(tmp_path: Path) -> None:
    _activate(tmp_path, "PA-ONE")
    _activate(tmp_path, "PA-TWO")
    inventory = read_activation_inventory(
        _write_inventory(tmp_path, ["PA-TWO", "PA-ONE"])
    )

    receipt = qualify_activation_inventory(
        inventory,
        artifacts_root=tmp_path,
        qualified_at_ms=NOW_MS,
        max_inventory_age_ms=60_000,
    )

    assert receipt.status == "qualified"
    assert receipt.source == "operator account inventory export"
    assert receipt.captured_at_ms == NOW_MS - 1_000
    assert [account.account_id for account in receipt.accounts] == ["PA-ONE", "PA-TWO"]
    assert all(account.activation_id for account in receipt.accounts)
    assert all(account.last_row_hash for account in receipt.accounts)
    assert len(receipt.inventory_sha256) == 64
    assert len(receipt.qualification_id) == 64


def test_qualification_refuses_an_account_without_activation(tmp_path: Path) -> None:
    _activate(tmp_path, "PA-ACTIVE")
    inventory = read_activation_inventory(
        _write_inventory(tmp_path, ["PA-ACTIVE", "PA-NOT-ACTIVATED"])
    )

    with pytest.raises(
        ActivationInventoryQualificationFailed,
        match=r"PA-NOT-ACTIVATED.*activation",
    ):
        qualify_activation_inventory(
            inventory,
            artifacts_root=tmp_path,
            qualified_at_ms=NOW_MS,
            max_inventory_age_ms=60_000,
        )


def test_qualification_refuses_invalid_activation_artifacts(tmp_path: Path) -> None:
    _activate(tmp_path, "PA-BAD-PROOF")
    proof = tmp_path / "accounts/alpaca/PA-BAD-PROOF/cutover/broker-proof.json"
    proof.write_text("{}", encoding="utf-8")
    inventory = read_activation_inventory(_write_inventory(tmp_path, ["PA-BAD-PROOF"]))

    with pytest.raises(
        ActivationInventoryQualificationFailed,
        match=r"PA-BAD-PROOF.*activation",
    ):
        qualify_activation_inventory(
            inventory,
            artifacts_root=tmp_path,
            qualified_at_ms=NOW_MS,
            max_inventory_age_ms=60_000,
        )


@pytest.mark.parametrize(
    ("account_ids", "match"),
    [
        ([], "at least one"),
        (["PA-ONE", "PA-ONE"], "duplicate"),
        (["../escape"], "unsafe"),
    ],
)
def test_inventory_rejects_incomplete_or_ambiguous_scope(
    tmp_path: Path,
    account_ids: list[str],
    match: str,
) -> None:
    with pytest.raises(ActivationInventoryQualificationFailed, match=match):
        read_activation_inventory(_write_inventory(tmp_path, account_ids))


def test_inventory_reader_rejects_symbolic_links_and_unknown_fields(tmp_path: Path) -> None:
    actual = _write_inventory(tmp_path, ["PA-ONE"])
    symlink = tmp_path / "inventory-link.json"
    symlink.symlink_to(actual)

    with pytest.raises(ActivationInventoryQualificationFailed, match="symbolic link"):
        read_activation_inventory(symlink)

    payload = json.loads(actual.read_text(encoding="utf-8"))
    payload["unreviewed_account_filter"] = "paper-only"
    actual.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ActivationInventoryQualificationFailed, match="fields"):
        read_activation_inventory(actual)


def test_qualification_refuses_stale_or_future_inventory(tmp_path: Path) -> None:
    _activate(tmp_path, "PA-ONE")
    path = _write_inventory(tmp_path, ["PA-ONE"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["captured_at_ms"] = NOW_MS - 60_001
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ActivationInventoryQualificationFailed, match="stale"):
        qualify_activation_inventory(
            read_activation_inventory(path),
            artifacts_root=tmp_path,
            qualified_at_ms=NOW_MS,
            max_inventory_age_ms=60_000,
        )

    payload["captured_at_ms"] = NOW_MS + 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ActivationInventoryQualificationFailed, match="future"):
        qualify_activation_inventory(
            read_activation_inventory(path),
            artifacts_root=tmp_path,
            qualified_at_ms=NOW_MS,
            max_inventory_age_ms=60_000,
        )


def test_cli_publishes_the_content_addressed_qualification_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _activate(tmp_path, "PA-ONE")
    inventory_path = _write_inventory(tmp_path, ["PA-ONE"])
    output_path = tmp_path / "receipts" / "activation-inventory.json"
    monkeypatch.setattr(cli_module, "now_ms_utc", lambda: NOW_MS)

    assert (
        cli_module.main(
            [
                "--artifacts-root",
                str(tmp_path),
                "--inventory",
                str(inventory_path),
                "--max-inventory-age-ms",
                "60000",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["status"] == "qualified"
    assert [account["account_id"] for account in payload["accounts"]] == ["PA-ONE"]
    assert len(payload["qualification_id"]) == 64
