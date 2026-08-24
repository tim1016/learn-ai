"""Operator CLI coverage for audited Signal Program canary activation."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

import scripts.manage_canary_admission as canary_cli
from app.services import canary_admission
from app.services.canary_admission import active_canary_pairings
from scripts.manage_canary_admission import main


def test_cli_plans_applies_reports_and_revokes_exact_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "artifacts/canary_admission/events.json"
    plan_path = tmp_path / "review/ema-canary-plan.json"
    monkeypatch.setattr(
        canary_admission,
        "DEFAULT_CANARY_ADMISSION_LEDGER_PATH",
        ledger_path,
    )

    assert main(
        [
            "plan",
            "--program",
            "ema_crossover_signal",
            "--account-id",
            "paper-account",
            "--reason",
            "Reviewed EMA paper canary.",
            "--output",
            str(plan_path),
        ]
    ) == 0
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan_payload["program_key"] == "ema_crossover_signal"
    assert plan_payload["account_id"] == "paper-account"
    assert plan_payload["actor"].startswith("local:")
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
    assert ledger_path.exists() is False
    plan_summary = json.loads(capsys.readouterr().out)
    assert plan_summary == {
        "account_id": "paper-account",
        "expires_at_ms": plan_payload["expires_at_ms"],
        "plan_path": str(plan_path),
        "program_key": "ema_crossover_signal",
    }
    assert plan_payload["confirmation_token"] not in json.dumps(plan_summary)

    monkeypatch.setattr(
        canary_cli,
        "_read_confirmation_token",
        lambda: plan_payload["confirmation_token"],
    )
    assert main(
        [
            "apply",
            "--plan",
            str(plan_path),
        ]
    ) == 0
    assert active_canary_pairings() == frozenset(
        {("ema_crossover_signal", "paper-account")}
    )
    capsys.readouterr()

    assert main(["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {
        "active_pairings": [
            {
                "account_id": "paper-account",
                "program_key": "ema_crossover_signal",
            }
        ]
    }

    assert main(
        [
            "revoke",
            "--program",
            "ema_crossover_signal",
            "--account-id",
            "paper-account",
            "--reason",
            "Canary review complete.",
        ]
    ) == 0
    assert active_canary_pairings() == frozenset()


def test_cli_refuses_a_tampered_plan_without_creating_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "artifacts/canary_admission/events.json"
    plan_path = tmp_path / "ema-canary-plan.json"
    monkeypatch.setattr(
        canary_admission,
        "DEFAULT_CANARY_ADMISSION_LEDGER_PATH",
        ledger_path,
    )
    assert main(
        [
            "plan",
            "--program",
            "ema_crossover_signal",
            "--account-id",
            "paper-account",
            "--reason",
            "Reviewed EMA paper canary.",
            "--output",
            str(plan_path),
        ]
    ) == 0
    capsys.readouterr()
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    token = payload["confirmation_token"]
    payload["account_id"] = "different-account"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(canary_cli, "_read_confirmation_token", lambda: token)

    assert main(
        [
            "apply",
            "--plan",
            str(plan_path),
        ]
    ) == 2
    assert "refused" in capsys.readouterr().err.lower()
    assert ledger_path.exists() is False
