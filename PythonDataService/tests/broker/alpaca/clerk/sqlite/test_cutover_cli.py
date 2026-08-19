"""Operator CLI boundary tests for the SQLite cutover ceremony."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.manage_alpaca_sqlite_clerk as recovery_cli_module
from app.broker.alpaca.clerk.sqlite.activation import ActivationStore
from app.broker.alpaca.clerk.sqlite.catalog_quarantine import CatalogQuarantineRefused
from app.broker.alpaca.clerk.sqlite.cutover import CutoverRefused
from app.broker.alpaca.clerk.sqlite.dev_reset import DeveloperCleanSlateResetRefused
from scripts.manage_alpaca_sqlite_clerk import (
    _read_catalog_quarantine_plan,
    _read_cutover_evidence,
    _read_process_stop_evidence,
    _read_reset_evidence,
)
from scripts.manage_alpaca_sqlite_clerk import main as recovery_cli
from tests.broker.alpaca.clerk.sqlite.cutover_test_support import (
    PLAN_MS,
    write_stopped_runner_bot,
)

ACCOUNT_ID = "PACUTOVER"


def _catalog_quarantine_plan_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "plan_id": "a" * 64,
        "confirmation_token": "a" * 64,
        "account_id": "PA-TEST",
        "created_at_ms": 1_700_000_000_000,
        "expires_at_ms": 1_700_000_120_000,
        "max_candidates": 2,
        "max_total_bytes": 100_000,
        "database": {
            "account_id": "PA-TEST",
            "authority_generation": 1,
            "db_identity_token": "b" * 32,
            "schema_version": 1,
            "control_revision": 7,
            "transition_count": 4,
            "last_sequence": 4,
            "last_row_hash": "c" * 64,
        },
        "registered_strategy_instance_ids": ["current-sqlite"],
        "candidates": [
            {
                "strategy_instance_id": "legacy-a",
                "relative_path": "live_state/legacy-a",
                "sha256": "d" * 64,
                "size_bytes": 123,
            }
        ],
    }


def test_main_plan_and_apply_require_the_exact_confirmation_token(tmp_path: Path) -> None:
    account_id = "PACLICUTOVER"
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    write_stopped_runner_bot(runner_root, account_id=account_id)
    account_dir = clerk_root / "accounts" / "alpaca" / account_id
    account_dir.mkdir(parents=True)
    (account_dir / "order_journal.jsonl").write_text("{}\n", encoding="utf-8")
    evidence_path = tmp_path / "broker-evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "account_id": account_id,
                "account_mode": "paper",
                "observed_at_ms": time.time_ns() // 1_000_000,
                "proof_reference": "fake-cli-proof",
                "positions": {},
                "open_order_ids": [],
            }
        ),
        encoding="utf-8",
    )
    plan_path = tmp_path / "cutover-plan.json"
    common = [
        "--artifacts-root",
        str(clerk_root),
        "--account-id",
        account_id,
    ]
    evidence_args = [
        "--broker-evidence",
        str(evidence_path),
        "--max-evidence-age-ms",
        "60000",
        "--runner-artifacts-root",
        str(runner_root),
    ]

    assert recovery_cli([*common, "cutover-initialize", *evidence_args]) == 0
    assert recovery_cli(
        [*common, "cutover-plan", *evidence_args, "--output", str(plan_path)]
    ) == 0
    token = json.loads(plan_path.read_text(encoding="utf-8"))["confirmation_token"]
    with pytest.raises(CutoverRefused, match="confirmation token"):
        recovery_cli(
            [
                *common,
                "cutover-apply",
                *evidence_args,
                "--plan",
                str(plan_path),
                "--confirmation-token",
                "wrong-token",
            ]
        )
    assert ActivationStore(clerk_root / "accounts" / "alpaca").latest(account_id) is None
    assert (account_dir / "order_journal.jsonl").is_file()

    assert recovery_cli(
        [
            *common,
            "cutover-apply",
            *evidence_args,
            "--plan",
            str(plan_path),
            "--confirmation-token",
            token,
        ]
    ) == 0
    assert ActivationStore(clerk_root / "accounts" / "alpaca").latest(account_id) is not None
    assert not (account_dir / "order_journal.jsonl").exists()

    disposable = write_stopped_runner_bot(
        runner_root,
        account_id=account_id,
        strategy_instance_id="disposable-after-activation",
    )
    catalog_plan_path = tmp_path / "catalog-quarantine-plan.json"
    assert recovery_cli(
        [
            *common,
            "catalog-quarantine-plan",
            "--runner-artifacts-root",
            str(runner_root),
            "--output",
            str(catalog_plan_path),
            "--max-candidates",
            "2",
            "--max-total-bytes",
            "1000000",
        ]
    ) == 0
    catalog_token = json.loads(catalog_plan_path.read_text(encoding="utf-8"))[
        "confirmation_token"
    ]
    with pytest.raises(CatalogQuarantineRefused, match="confirmation token"):
        recovery_cli(
            [
                *common,
                "catalog-quarantine-apply",
                "--runner-artifacts-root",
                str(runner_root),
                "--plan",
                str(catalog_plan_path),
                "--confirmation-token",
                "wrong-token",
            ]
        )
    assert disposable.is_dir()

    assert recovery_cli(
        [
            *common,
            "catalog-quarantine-apply",
            "--runner-artifacts-root",
            str(runner_root),
            "--plan",
            str(catalog_plan_path),
            "--confirmation-token",
            catalog_token,
        ]
    ) == 0
    assert not disposable.exists()


def test_read_catalog_quarantine_plan_rejects_malformed_nested_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog-plan.json"
    malformed_payloads: list[dict[str, Any]] = []

    database_not_object = _catalog_quarantine_plan_payload()
    database_not_object["database"] = []
    malformed_payloads.append(database_not_object)

    candidate_not_object = _catalog_quarantine_plan_payload()
    candidate_not_object["candidates"] = [None]
    malformed_payloads.append(candidate_not_object)

    candidate_wrong_scalar = _catalog_quarantine_plan_payload()
    candidate_wrong_scalar["candidates"][0]["size_bytes"] = "123"
    malformed_payloads.append(candidate_wrong_scalar)

    registered_id_wrong_scalar = _catalog_quarantine_plan_payload()
    registered_id_wrong_scalar["registered_strategy_instance_ids"] = [17]
    malformed_payloads.append(registered_id_wrong_scalar)

    for payload in malformed_payloads:
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="catalog quarantine"):
            _read_catalog_quarantine_plan(path)


def test_read_reset_and_cutover_evidence_use_distinct_models(
    tmp_path: Path,
) -> None:
    payload = {
        "account_id": ACCOUNT_ID,
        "observed_at_ms": PLAN_MS,
        "proof_reference": "fake-cli-proof",
        "positions": {},
        "open_order_ids": [],
    }
    evidence_path = tmp_path / "broker-evidence.json"
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")

    reset = _read_reset_evidence(evidence_path, ACCOUNT_ID)

    assert reset.account_id == ACCOUNT_ID
    assert not hasattr(reset, "account_mode")
    with pytest.raises(ValueError, match="cutover broker evidence"):
        _read_cutover_evidence(evidence_path, ACCOUNT_ID)


def test_v9_upgrade_and_rollback_cli_require_account_bound_process_stop_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_path = tmp_path / "process-stop.json"
    evidence_path.write_text(
        json.dumps(
            {
                "account_id": ACCOUNT_ID,
                "observed_at_ms": PLAN_MS,
                "proof_reference": "tests/process-stop-proof.json",
            }
        ),
        encoding="utf-8",
    )
    captured: list[tuple[str, Any]] = []

    def capture_upgrade(**kwargs: Any) -> Any:
        captured.append(("upgrade", kwargs["process_stop_proof"]))
        return kwargs["process_stop_proof"]

    def capture_rollback(**kwargs: Any) -> Any:
        captured.append(("rollback", kwargs["process_stop_proof"]))
        return kwargs["process_stop_proof"]

    monkeypatch.setattr(recovery_cli_module, "upgrade_v8_authority_offline", capture_upgrade)
    monkeypatch.setattr(recovery_cli_module, "rollback_v9_upgrade_offline", capture_rollback)
    common = ["--artifacts-root", str(tmp_path / "clerk"), "--account-id", ACCOUNT_ID]

    assert recovery_cli([*common, "upgrade-v9", "--process-stop-evidence", str(evidence_path)]) == 0
    assert recovery_cli([*common, "rollback-v9", "--process-stop-evidence", str(evidence_path)]) == 0
    assert [(operation, proof.account_id) for operation, proof in captured] == [
        ("upgrade", ACCOUNT_ID),
        ("rollback", ACCOUNT_ID),
    ]

    with pytest.raises(ValueError, match="requires --process-stop-evidence"):
        recovery_cli([*common, "upgrade-v9"])


def test_dev_reset_cli_refuses_unactivated_legacy_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    account_dir = clerk_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    journal = account_dir / "order_journal.jsonl"
    journal.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        recovery_cli_module,
        "get_alpaca_settings",
        lambda: SimpleNamespace(mode="paper"),
    )

    with pytest.raises(
        DeveloperCleanSlateResetRefused,
        match="requires an established SQLite authority",
    ):
        recovery_cli(
            [
                "--artifacts-root",
                str(clerk_root),
                "--account-id",
                ACCOUNT_ID,
                "dev-reset",
                "--runner-artifacts-root",
                str(runner_root),
            ]
        )

    assert journal.is_file()
    assert not (account_dir / "dev-reset-quarantine").exists()


def test_dev_reset_cli_refuses_live_mode_without_moving_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    account_dir = clerk_root / "accounts" / "alpaca" / ACCOUNT_ID
    account_dir.mkdir(parents=True)
    journal = account_dir / "order_journal.jsonl"
    journal.write_text("{}\n", encoding="utf-8")
    runner_registry = runner_root / "accounts" / ACCOUNT_ID / "instance_registry.jsonl"
    runner_registry.parent.mkdir(parents=True)
    runner_registry.write_text("{}\n", encoding="utf-8")
    journal_bytes = journal.read_bytes()
    runner_bytes = runner_registry.read_bytes()
    monkeypatch.setattr(
        recovery_cli_module,
        "get_alpaca_settings",
        lambda: SimpleNamespace(mode="live"),
    )

    with pytest.raises(DeveloperCleanSlateResetRefused, match="only for paper"):
        recovery_cli(
            [
                "--artifacts-root",
                str(clerk_root),
                "--account-id",
                ACCOUNT_ID,
                "dev-reset",
                "--runner-artifacts-root",
                str(runner_root),
            ]
        )

    assert journal.read_bytes() == journal_bytes
    assert runner_registry.read_bytes() == runner_bytes
    assert not (account_dir / "dev-reset-quarantine").exists()
    assert not (runner_root / "dev-reset-quarantine").exists()


def test_read_process_stop_evidence_requires_exact_account_bound_fields(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "process-stop-evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "account_id": ACCOUNT_ID,
                "observed_at_ms": PLAN_MS,
                "proof_reference": "container-supervisor:stopped",
            }
        ),
        encoding="utf-8",
    )

    proof = _read_process_stop_evidence(evidence_path, ACCOUNT_ID)

    assert proof is not None
    assert proof.proof_reference == "container-supervisor:stopped"
    with pytest.raises(ValueError, match="account_id"):
        _read_process_stop_evidence(evidence_path, "OTHER")
    assert _read_process_stop_evidence(None, ACCOUNT_ID) is None
