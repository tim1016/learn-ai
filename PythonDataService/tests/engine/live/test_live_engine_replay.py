"""LiveEngine replay parity tests."""

from __future__ import annotations

from app.engine.live.config import LiveConfig


def test_live_runtime_scaffold_imports() -> None:
    """Phase 1 smoke test for the live package scaffold."""
    assert LiveConfig().symbol == "SPY"
