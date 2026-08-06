"""Operator CLI boundary tests for the SQLite cutover ceremony."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.activation import ActivationStore
from scripts.manage_alpaca_sqlite_clerk import (
    _read_cutover_evidence,
    _read_reset_evidence,
)
from scripts.manage_alpaca_sqlite_clerk import main as recovery_cli
from tests.broker.alpaca.clerk.sqlite.cutover_test_support import (
    PLAN_MS,
    write_stopped_runner_bot,
)

ACCOUNT_ID = "PA-CUTOVER"


def test_cli_plan_and_apply_require_the_exact_confirmation_token(tmp_path: Path) -> None:
    account_id = "PA-CLI-CUTOVER"
    clerk_root = tmp_path / "clerk"
    runner_root = tmp_path / "runner"
    write_stopped_runner_bot(runner_root)
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


def test_cli_uses_distinct_reset_and_paper_cutover_evidence_models(
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
