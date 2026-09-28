"""The activation recovery fallback survives; session/receipt commands cannot execute."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.shadow_activation import ShadowActivationStore
from scripts import manage_alpaca_shadow as cli


def test_activation_recovery_still_writes_only_the_isolated_activation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--live-account-id", "9LIVE0001", "--artifacts-root", str(tmp_path), "activate"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["account_id"] == "shadow:9LIVE0001"
    assert ShadowActivationStore(tmp_path).latest("shadow:9LIVE0001") is not None
    assert not (tmp_path / "accounts" / "arming").exists()


@pytest.mark.parametrize("operation", ["sessions", "receipt"])
def test_retired_shadow_commands_refuse_without_writing(tmp_path: Path, capsys: pytest.CaptureFixture[str], operation: str) -> None:
    assert cli.main(["--live-account-id", "9LIVE0001", "--artifacts-root", str(tmp_path), operation]) == 1
    assert "invalid choice" in json.loads(capsys.readouterr().out)["error"]
    assert not (tmp_path / "accounts").exists()


def test_reserved_account_refuses_before_creating_custody(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--live-account-id", "shadow:9LIVE0001", "--artifacts-root", str(tmp_path), "activate"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)
    assert not (tmp_path / "accounts").exists()
