"""The backtest's loaded execution identity refuses changed code, apart from the approval identity (#3423)."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_backtest_identity_refuses_decoder_changes_without_changing_approval_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.options_backtest import execution_identity as identity

    before = identity.backtest_execution_identity()
    assert "backtest_execution" in before
    (tmp_path / "frozen_inputs.py").write_text("changed decoder")
    monkeypatch.setattr(identity, "_ROOT", tmp_path)
    with pytest.raises(ValueError, match="code changed"):
        identity.backtest_execution_identity()
